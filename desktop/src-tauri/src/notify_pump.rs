//! The generation-aware long-poll notification pump, free of Tauri.
//!
//! One loop owns banner duty for the whole process: it long-polls the
//! runtime's `/notifications` feed (the request itself keeps the claim alive
//! and wakes the moment an event is queued), posts each new item natively,
//! and acknowledges only what was actually shown. A failed native post asks the
//! runtime to deliver that item through its osascript fallback instead.
//!
//! Everything the loop touches outside itself sits behind a small trait, so
//! the delivery contract is testable with fakes:
//!
//! - [`NotifyHost`]: permission, the current discovery, re-discovery, and the
//!   "runtime answered" signal (the shell's `AppState`).
//! - [`NotifyTransport`]: the three HTTP calls (poll, ack, fallback).
//! - [`BannerPoster`]: post one banner / withdraw an agent's banners.
//! - [`Clock`]: sleeping, and the two notions of "now" the loop reads.
//!
//! The shell's adapter (`notifications::start_notify_pump`) wires these to
//! `AppHandle`, `http::*` and the native poster.

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::banners;
use crate::sidecar::Discovery;
use crate::sync_util::LockExt;

/// The runtime holds a long poll for 25 s; leave transport headroom around it.
pub(crate) const NOTIFY_POLL_TIMEOUT: Duration = Duration::from_secs(30);
pub(crate) const NOTIFY_RETRY_DELAY: Duration = Duration::from_millis(500);
/// A source without a notification feed (demo/mock) answers `/notifications`
/// with 404. That will not change until the shell is pointed at a different
/// runtime, so the pump sleeps this long — or until discovery changes.
pub(crate) const NOTIFY_UNSUPPORTED_BACKOFF: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct NotifyCursor {
    pub(crate) generation: Option<String>,
    pub(crate) seq: u64,
}

#[derive(Debug, Clone)]
pub(crate) struct PendingNotification {
    pub(crate) id: String,
    pub(crate) generation: String,
    pub(crate) seq: u64,
    pub(crate) title: String,
    pub(crate) body: String,
    pub(crate) sound: serde_json::Value,
    /// PNG of the agent's project mark written by the runtime (notify_icons).
    pub(crate) icon: Option<String>,
    pub(crate) created_at_ms: Option<i64>,
    /// Which agent the banner is about, plus answer buttons / reply field.
    pub(crate) meta: banners::BannerMeta,
    /// Feed item kind: "alert" (a banner) or "withdraw" (remove the agent's
    /// delivered banners); None from a runtime that predates kinds.
    pub(crate) kind: Option<String>,
}

pub(crate) fn notification_batch(
    state_json: &serde_json::Value,
    cursor: &NotifyCursor,
) -> Option<(String, u64, Vec<PendingNotification>)> {
    let generation = state_json.get("generation")?.as_str()?.to_string();
    let acked_seq = state_json
        .get("acked_seq")
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let floor = if cursor.generation.as_deref() == Some(generation.as_str()) {
        cursor.seq.max(acked_seq)
    } else {
        acked_seq
    };
    let mut items = Vec::new();
    for item in state_json.get("items").and_then(|v| v.as_array()).into_iter().flatten() {
        let seq = item.get("seq").and_then(|v| v.as_u64()).unwrap_or(0);
        if seq <= floor {
            continue;
        }
        items.push(PendingNotification {
            id: item
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            generation: generation.clone(),
            seq,
            title: item
                .get("title")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            body: item
                .get("body")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            sound: item.get("sound").cloned().unwrap_or(serde_json::Value::Bool(false)),
            icon: item.get("icon").and_then(|v| v.as_str()).map(str::to_string),
            created_at_ms: item.get("created_at_ms").and_then(|v| v.as_i64()),
            meta: banners::BannerMeta::from_item(item),
            kind: item.get("kind").and_then(|v| v.as_str()).map(str::to_string),
        });
    }
    items.sort_by_key(|item| item.seq);
    Some((generation, acked_seq, items))
}

/// What the pump does with the result of one `/notifications` round trip.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum NotifyPoll {
    /// A 2xx feed snapshot to deliver from.
    Deliver(String),
    /// 404: this runtime's source has no feed (demo/mock). Not an outage —
    /// re-discovery would find the very same runtime — so back off instead of
    /// hammering it twice a second.
    Unsupported,
    /// Transport failure or any other status: retry, possibly re-discovering.
    Failed,
}

pub(crate) fn classify_notify_poll(result: Result<(u16, String), String>) -> NotifyPoll {
    match result {
        Ok((code, body)) if (200..300).contains(&code) => NotifyPoll::Deliver(body),
        Ok((404, _)) => NotifyPoll::Unsupported,
        _ => NotifyPoll::Failed,
    }
}

/// The runtime the pump should poll right now, or how long to idle first.
/// Both "no permission yet" and "sidecar not discovered yet" must SLEEP: a bare
/// `continue` there spun a core at 100% through every sidecar boot, and forever
/// while a crashing sidecar never reported in.
pub(crate) fn notify_target(
    permission: bool,
    discovery: Option<Discovery>,
) -> Result<Discovery, Duration> {
    match (permission, discovery) {
        (true, Some(d)) => Ok(d),
        _ => Err(NOTIFY_RETRY_DELAY),
    }
}

/// Sleep up to `max`, in `NOTIFY_RETRY_DELAY` steps, returning early as soon as
/// `changed()` reports true. Returns whether it woke early.
pub(crate) fn backoff_until(clock: &impl Clock, max: Duration, changed: impl Fn() -> bool) -> bool {
    let deadline = clock.now() + max;
    loop {
        if changed() {
            return true;
        }
        let now = clock.now();
        if now >= deadline {
            return false;
        }
        clock.sleep(NOTIFY_RETRY_DELAY.min(deadline - now));
    }
}

/// The shell state the pump reads (the `AppState` side of the adapter).
pub(crate) trait NotifyHost {
    /// Whether native notifications are permitted (banner duty may be claimed).
    fn permission(&self) -> bool;
    /// The runtime the shell currently points at, if any.
    fn discovery(&self) -> Option<Discovery>;
    /// Re-read runtime.json after a failed poll; the adopted runtime, if any.
    fn rediscover(&self) -> Option<Discovery>;
    /// A poll of the current runtime got an answer (ends a failure streak).
    fn note_runtime_ok(&self);
}

/// The pump's three runtime calls. The adapter adds token, timeouts and the
/// shell identity/feature headers.
pub(crate) trait NotifyTransport {
    /// `GET /notifications` long poll from `cursor` → `(status, body)`.
    fn poll(&self, d: &Discovery, cursor: &NotifyCursor) -> Result<(u16, String), String>;
    /// `POST /notifications/ack` → status (204 = acknowledged).
    fn ack(&self, d: &Discovery, generation: &str, seq: u64) -> Result<u16, String>;
    /// `POST /notifications/fallback` → status (204 = the runtime delivered it).
    fn fallback(&self, d: &Discovery, generation: &str, seq: u64, error: &str)
        -> Result<u16, String>;
}

/// Native banner side effects.
pub(crate) trait BannerPoster {
    /// Show one banner; `Err` means nothing was shown.
    fn post(&self, item: &PendingNotification) -> Result<(), String>;
    /// Remove the banners this shell delivered for `agent`.
    fn withdraw(&self, agent: &banners::AgentRef);
}

/// Time as the pump sees it.
pub(crate) trait Clock {
    fn now(&self) -> Instant;
    fn sleep(&self, d: Duration);
    /// Wall-clock milliseconds since the Unix epoch (delivery latency only).
    fn unix_ms(&self) -> Option<i64>;
}

/// The real clock.
pub(crate) struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> Instant {
        Instant::now()
    }

    fn sleep(&self, d: Duration) {
        std::thread::sleep(d);
    }

    fn unix_ms(&self) -> Option<i64> {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .ok()
            .map(|d| d.as_millis() as i64)
    }
}

/// How one pump iteration ended (what the loop would `continue` from).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum PumpStep {
    /// No permission or no discovery yet: idled one retry delay.
    Idle,
    /// The runtime has no feed: backed off (until discovery changed).
    Unsupported,
    /// The poll failed (even after re-discovery): idled one retry delay.
    PollFailed,
    /// The body was not a feed snapshot: idled one retry delay.
    BadFeed,
    /// Every pending item was delivered and acknowledged.
    Delivered { items: usize },
    /// A native post failed; the fallback was requested and the batch stopped.
    FellBack { fallback_ok: bool },
    /// An acknowledgement failed; the batch stopped.
    AckFailed,
}

/// Deliver one feed item: post its banner, or withdraw the agent's delivered
/// banners (answered / back to work — the runtime says when), or skip a kind
/// this shell does not know. Every outcome but a failed post is acknowledged.
pub(crate) fn deliver_feed_item(
    poster: &impl BannerPoster,
    item: &PendingNotification,
) -> Result<(), String> {
    match banners::feed_item_action(item.kind.as_deref(), &item.meta) {
        banners::FeedItemAction::Post => poster.post(item),
        banners::FeedItemAction::Withdraw => {
            if let Some(agent) = item.meta.agent.as_ref() {
                poster.withdraw(agent);
            }
            Ok(())
        }
        banners::FeedItemAction::Skip => Ok(()),
    }
}

/// Identity of a discovered runtime, for "has the shell been repointed?".
pub(crate) fn discovery_key(d: &Discovery) -> (String, u16, String) {
    (d.host.clone(), d.port, d.token.clone())
}

pub(crate) struct NotifyPump<H, T, P, C> {
    pub(crate) host: H,
    pub(crate) transport: T,
    pub(crate) poster: P,
    pub(crate) clock: C,
    /// Generation-scoped cursor, advanced only after native delivery.
    pub(crate) cursor: Arc<Mutex<NotifyCursor>>,
}

impl<H, T, P, C> NotifyPump<H, T, P, C>
where
    H: NotifyHost,
    T: NotifyTransport,
    P: BannerPoster,
    C: Clock,
{
    /// Run forever (the pump thread's body).
    pub(crate) fn run(&self) -> ! {
        loop {
            self.step();
        }
    }

    fn poll(&self, d: &Discovery) -> NotifyPoll {
        let cursor = self.cursor.lock_or_recover().clone();
        classify_notify_poll(self.transport.poll(d, &cursor))
    }

    fn unsupported_backoff(&self, d: &Discovery) {
        let key = discovery_key(d);
        backoff_until(&self.clock, NOTIFY_UNSUPPORTED_BACKOFF, || {
            self.host.discovery().as_ref().map(discovery_key) != Some(key.clone())
        });
    }

    fn advance_cursor(&self, generation: &str, seq: u64) {
        let mut cursor = self.cursor.lock_or_recover();
        cursor.generation = Some(generation.to_string());
        cursor.seq = cursor.seq.max(seq);
    }

    /// One iteration of the pump loop.
    pub(crate) fn step(&self) -> PumpStep {
        let d = match notify_target(self.host.permission(), self.host.discovery()) {
            Ok(d) => d,
            Err(delay) => {
                self.clock.sleep(delay);
                return PumpStep::Idle;
            }
        };
        let (body, active_discovery) = match self.poll(&d) {
            NotifyPoll::Deliver(body) => {
                self.host.note_runtime_ok();
                (body, d)
            }
            NotifyPoll::Unsupported => {
                self.host.note_runtime_ok();
                self.unsupported_backoff(&d);
                return PumpStep::Unsupported;
            }
            NotifyPoll::Failed => {
                let Some(d) = self.host.rediscover() else {
                    self.clock.sleep(NOTIFY_RETRY_DELAY);
                    return PumpStep::PollFailed;
                };
                match self.poll(&d) {
                    NotifyPoll::Deliver(body) => (body, d),
                    NotifyPoll::Unsupported => {
                        self.unsupported_backoff(&d);
                        return PumpStep::Unsupported;
                    }
                    NotifyPoll::Failed => {
                        self.clock.sleep(NOTIFY_RETRY_DELAY);
                        return PumpStep::PollFailed;
                    }
                }
            }
        };
        let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&body) else {
            self.clock.sleep(NOTIFY_RETRY_DELAY);
            return PumpStep::BadFeed;
        };
        let snapshot = self.cursor.lock_or_recover().clone();
        let Some((generation, acked_seq, items)) = notification_batch(&parsed, &snapshot) else {
            self.clock.sleep(NOTIFY_RETRY_DELAY);
            return PumpStep::BadFeed;
        };
        {
            let mut cursor = self.cursor.lock_or_recover();
            if cursor.generation.as_deref() != Some(generation.as_str()) {
                cursor.generation = Some(generation.clone());
                cursor.seq = acked_seq;
            } else {
                cursor.seq = cursor.seq.max(acked_seq);
            }
        }
        let mut delivered = 0;
        for item in items {
            if let Err(err) = deliver_feed_item(&self.poster, &item) {
                eprintln!("herdeck: native notification failed id={}: {err}", item.id);
                let code =
                    self.transport
                        .fallback(&active_discovery, &item.generation, item.seq, &err);
                let fallback_ok = code == Ok(204);
                if fallback_ok {
                    self.advance_cursor(&item.generation, item.seq);
                    eprintln!("herdeck: notification fallback delivered id={}", item.id);
                } else {
                    eprintln!(
                        "herdeck: notification fallback failed id={} result={code:?}",
                        item.id
                    );
                }
                self.clock.sleep(NOTIFY_RETRY_DELAY);
                return PumpStep::FellBack { fallback_ok };
            }
            // Advance the process-local cursor immediately after visible
            // delivery. A transient ACK failure must never show the banner a
            // second time in this shell; the next long poll carries this cursor
            // and repairs the server ACK before waiting.
            self.advance_cursor(&item.generation, item.seq);
            let code = self.transport.ack(&active_discovery, &item.generation, item.seq);
            if code != Ok(204) {
                eprintln!("herdeck: notification ack failed id={} result={code:?}", item.id);
                self.clock.sleep(NOTIFY_RETRY_DELAY);
                return PumpStep::AckFailed;
            }
            delivered += 1;
            let latency = item.created_at_ms.map(|created| {
                let now = self.clock.unix_ms().unwrap_or(created);
                (now - created).max(0)
            });
            eprintln!(
                "herdeck: notification delivered id={} latency_ms={}",
                item.id,
                latency.map(|v| v.to_string()).unwrap_or_else(|| "unknown".into())
            );
        }
        PumpStep::Delivered { items: delivered }
    }
}

#[cfg(test)]
mod tests {
    //! The delivery contract, driven through fakes: which items are posted,
    //! what is acknowledged or handed to the fallback, where the cursor ends
    //! up, and which runtime each call goes to.

    use super::*;
    use std::cell::{Cell, RefCell};
    use std::collections::{HashSet, VecDeque};
    use std::rc::Rc;

    fn runtime(port: u16) -> Discovery {
        Discovery {
            url: format!("http://127.0.0.1:{port}"),
            host: "127.0.0.1".into(),
            port,
            token: format!("t{port}"),
            source: "live".into(),
        }
    }

    /// State the fakes share, so one fake can change what another sees
    /// (a discovery swapped mid-loop).
    #[derive(Default)]
    struct World {
        discovery: RefCell<Option<Discovery>>,
        runtime_ok: Cell<usize>,
        rediscovered: RefCell<VecDeque<Option<Discovery>>>,
        rediscover_calls: Cell<usize>,
        /// Scripted poll results, in order; an empty script is a failure.
        polls: RefCell<VecDeque<Result<(u16, String), String>>>,
        /// `(port, cursor)` of every poll.
        polled: RefCell<Vec<(u16, NotifyCursor)>>,
        /// Scripted ack results; an empty script acknowledges (204).
        acks: RefCell<VecDeque<Result<u16, String>>>,
        acked: RefCell<Vec<(u16, String, u64)>>,
        /// Scripted fallback results; an empty script delivers (204).
        fallbacks: RefCell<VecDeque<Result<u16, String>>>,
        fell_back: RefCell<Vec<(u16, String, u64, String)>>,
        /// Item ids whose native post fails.
        failing: RefCell<HashSet<String>>,
        posted: RefCell<Vec<String>>,
        withdrawn: RefCell<Vec<banners::AgentRef>>,
        /// Swap the discovery to this runtime when an item is posted.
        swap_on_post: RefCell<Option<Discovery>>,
        /// Swap the discovery to this runtime after this many sleeps.
        swap_after_sleeps: RefCell<Option<(usize, Discovery)>>,
        slept: RefCell<Vec<Duration>>,
        elapsed: Cell<Duration>,
    }

    struct Fake(Rc<World>);

    impl NotifyHost for Fake {
        fn permission(&self) -> bool {
            true
        }
        fn discovery(&self) -> Option<Discovery> {
            self.0.discovery.borrow().clone()
        }
        fn rediscover(&self) -> Option<Discovery> {
            self.0.rediscover_calls.set(self.0.rediscover_calls.get() + 1);
            let next = self.0.rediscovered.borrow_mut().pop_front().flatten();
            if let Some(d) = &next {
                *self.0.discovery.borrow_mut() = Some(d.clone());
            }
            next
        }
        fn note_runtime_ok(&self) {
            self.0.runtime_ok.set(self.0.runtime_ok.get() + 1);
        }
    }

    impl NotifyTransport for Fake {
        fn poll(&self, d: &Discovery, cursor: &NotifyCursor) -> Result<(u16, String), String> {
            self.0.polled.borrow_mut().push((d.port, cursor.clone()));
            self.0
                .polls
                .borrow_mut()
                .pop_front()
                .unwrap_or_else(|| Err("connection refused".into()))
        }
        fn ack(&self, d: &Discovery, generation: &str, seq: u64) -> Result<u16, String> {
            self.0.acked.borrow_mut().push((d.port, generation.into(), seq));
            self.0.acks.borrow_mut().pop_front().unwrap_or(Ok(204))
        }
        fn fallback(
            &self,
            d: &Discovery,
            generation: &str,
            seq: u64,
            error: &str,
        ) -> Result<u16, String> {
            self.0
                .fell_back
                .borrow_mut()
                .push((d.port, generation.into(), seq, error.into()));
            self.0.fallbacks.borrow_mut().pop_front().unwrap_or(Ok(204))
        }
    }

    impl BannerPoster for Fake {
        fn post(&self, item: &PendingNotification) -> Result<(), String> {
            if let Some(d) = self.0.swap_on_post.borrow_mut().take() {
                *self.0.discovery.borrow_mut() = Some(d);
            }
            if self.0.failing.borrow().contains(&item.id) {
                return Err("no notification center".into());
            }
            self.0.posted.borrow_mut().push(item.id.clone());
            Ok(())
        }
        fn withdraw(&self, agent: &banners::AgentRef) {
            self.0.withdrawn.borrow_mut().push(agent.clone());
        }
    }

    struct FakeClock {
        world: Rc<World>,
        base: Instant,
    }

    impl Clock for FakeClock {
        fn now(&self) -> Instant {
            self.base + self.world.elapsed.get()
        }
        fn sleep(&self, d: Duration) {
            self.world.elapsed.set(self.world.elapsed.get() + d);
            self.world.slept.borrow_mut().push(d);
            let count = self.world.slept.borrow().len();
            let mut swap = self.world.swap_after_sleeps.borrow_mut();
            if swap.as_ref().is_some_and(|(n, _)| *n == count) {
                let (_, d) = swap.take().unwrap();
                *self.world.discovery.borrow_mut() = Some(d);
            }
        }
        fn unix_ms(&self) -> Option<i64> {
            Some(1_000)
        }
    }

    type TestPump = NotifyPump<Fake, Fake, Fake, FakeClock>;

    fn pump_on(port: u16) -> (TestPump, Rc<World>) {
        let world = Rc::new(World::default());
        *world.discovery.borrow_mut() = Some(runtime(port));
        let pump = NotifyPump {
            host: Fake(world.clone()),
            transport: Fake(world.clone()),
            poster: Fake(world.clone()),
            clock: FakeClock {
                world: world.clone(),
                base: Instant::now(),
            },
            cursor: Arc::new(Mutex::new(NotifyCursor::default())),
        };
        (pump, world)
    }

    fn feed(generation: &str, acked_seq: u64, seqs: &[u64]) -> Result<(u16, String), String> {
        let items: Vec<_> = seqs
            .iter()
            .map(|seq| {
                serde_json::json!({
                    "id": format!("{generation}:{seq}"), "seq": seq,
                    "title": "t", "body": "b", "created_at_ms": 400,
                })
            })
            .collect();
        let body = serde_json::json!({
            "generation": generation, "acked_seq": acked_seq, "items": items,
        });
        Ok((200, body.to_string()))
    }

    fn script(world: &World, polls: Vec<Result<(u16, String), String>>) {
        world.polls.borrow_mut().extend(polls);
    }

    fn cursor(pump: &TestPump) -> NotifyCursor {
        pump.cursor.lock_or_recover().clone()
    }

    fn at(generation: &str, seq: u64) -> NotifyCursor {
        NotifyCursor {
            generation: Some(generation.into()),
            seq,
        }
    }

    #[test]
    fn items_are_posted_and_acknowledged_in_sequence_order() {
        let (pump, world) = pump_on(1);
        script(&world, vec![feed("g", 0, &[3, 1, 2])]);

        assert_eq!(pump.step(), PumpStep::Delivered { items: 3 });
        assert_eq!(*world.posted.borrow(), ["g:1", "g:2", "g:3"]);
        assert_eq!(
            *world.acked.borrow(),
            [(1, "g".into(), 1), (1, "g".into(), 2), (1, "g".into(), 3)]
        );
        assert_eq!(cursor(&pump), at("g", 3));
        assert_eq!(world.runtime_ok.get(), 1);
        assert!(world.slept.borrow().is_empty(), "a clean batch never sleeps");
        // The first poll starts from nothing; the cursor rides the next one.
        script(&world, vec![feed("g", 3, &[])]);
        pump.step();
        assert_eq!(world.polled.borrow()[0].1, NotifyCursor::default());
        assert_eq!(world.polled.borrow()[1].1, at("g", 3));
    }

    #[test]
    fn already_acknowledged_items_are_not_posted_again() {
        let (pump, world) = pump_on(1);
        script(&world, vec![feed("g", 2, &[1, 2, 3])]);
        assert_eq!(pump.step(), PumpStep::Delivered { items: 1 });
        assert_eq!(*world.posted.borrow(), ["g:3"]);
        assert_eq!(cursor(&pump), at("g", 3));
    }

    #[test]
    fn a_failed_native_post_hands_the_item_to_the_fallback() {
        let (pump, world) = pump_on(1);
        world.failing.borrow_mut().insert("g:2".into());
        script(&world, vec![feed("g", 0, &[1, 2, 3])]);

        assert_eq!(pump.step(), PumpStep::FellBack { fallback_ok: true });
        assert_eq!(*world.posted.borrow(), ["g:1"]);
        assert_eq!(
            *world.fell_back.borrow(),
            [(1, "g".into(), 2, "no notification center".into())]
        );
        // The failed item is never acknowledged; the batch stops there.
        assert_eq!(*world.acked.borrow(), [(1, "g".into(), 1)]);
        // A delivered fallback counts as delivery: the cursor passes it.
        assert_eq!(cursor(&pump), at("g", 2));
        assert_eq!(*world.slept.borrow(), [NOTIFY_RETRY_DELAY]);
    }

    #[test]
    fn a_refused_fallback_leaves_the_item_pending_for_the_next_poll() {
        let (pump, world) = pump_on(1);
        world.failing.borrow_mut().insert("g:2".into());
        world.fallbacks.borrow_mut().push_back(Ok(409));
        script(&world, vec![feed("g", 0, &[1, 2]), feed("g", 1, &[1, 2])]);

        assert_eq!(pump.step(), PumpStep::FellBack { fallback_ok: false });
        assert_eq!(cursor(&pump), at("g", 1), "cursor stops before the failed item");

        // Native posting works again: the next poll retries item 2.
        world.failing.borrow_mut().clear();
        assert_eq!(pump.step(), PumpStep::Delivered { items: 1 });
        assert_eq!(world.polled.borrow()[1].1, at("g", 1));
        assert_eq!(*world.posted.borrow(), ["g:1", "g:2"]);
        assert_eq!(cursor(&pump), at("g", 2));
    }

    #[test]
    fn a_fallback_transport_error_is_a_refusal() {
        let (pump, world) = pump_on(1);
        world.failing.borrow_mut().insert("g:1".into());
        world.fallbacks.borrow_mut().push_back(Err("timed out".into()));
        script(&world, vec![feed("g", 0, &[1])]);
        assert_eq!(pump.step(), PumpStep::FellBack { fallback_ok: false });
        assert_eq!(cursor(&pump), at("g", 0));
    }

    #[test]
    fn a_failed_ack_stops_the_batch_but_never_reposts_the_banner() {
        let (pump, world) = pump_on(1);
        world.acks.borrow_mut().push_back(Err("reset".into()));
        script(&world, vec![feed("g", 0, &[1, 2]), feed("g", 0, &[1, 2])]);

        assert_eq!(pump.step(), PumpStep::AckFailed);
        assert_eq!(*world.posted.borrow(), ["g:1"]);
        // Shown, so the local cursor moves even though the server never heard.
        assert_eq!(cursor(&pump), at("g", 1));
        assert_eq!(*world.slept.borrow(), [NOTIFY_RETRY_DELAY]);

        // The runtime still says acked_seq 0; the shell must not show g:1 twice.
        assert_eq!(pump.step(), PumpStep::Delivered { items: 1 });
        assert_eq!(world.polled.borrow()[1].1, at("g", 1));
        assert_eq!(*world.posted.borrow(), ["g:1", "g:2"]);
    }

    #[test]
    fn a_non_204_ack_is_a_failure_too() {
        let (pump, world) = pump_on(1);
        world.acks.borrow_mut().push_back(Ok(409));
        script(&world, vec![feed("g", 0, &[1, 2])]);
        assert_eq!(pump.step(), PumpStep::AckFailed);
        assert_eq!(*world.posted.borrow(), ["g:1"]);
    }

    #[test]
    fn a_runtime_restart_resets_the_cursor_and_delivers_low_sequence_items() {
        let (pump, world) = pump_on(1);
        *pump.cursor.lock_or_recover() = at("old", 10);
        script(&world, vec![feed("new", 0, &[1, 2])]);

        assert_eq!(pump.step(), PumpStep::Delivered { items: 2 });
        assert_eq!(*world.posted.borrow(), ["new:1", "new:2"]);
        assert_eq!(
            *world.acked.borrow(),
            [(1, "new".into(), 1), (1, "new".into(), 2)]
        );
        assert_eq!(cursor(&pump), at("new", 2));
    }

    #[test]
    fn a_new_generation_adopts_the_runtimes_acked_sequence() {
        let (pump, world) = pump_on(1);
        *pump.cursor.lock_or_recover() = at("old", 10);
        script(&world, vec![feed("new", 4, &[])]);
        assert_eq!(pump.step(), PumpStep::Delivered { items: 0 });
        assert_eq!(cursor(&pump), at("new", 4));
    }

    #[test]
    fn a_feedless_runtime_backs_off_without_rediscovering() {
        let (pump, world) = pump_on(1);
        script(&world, vec![Ok((404, String::new()))]);

        assert_eq!(pump.step(), PumpStep::Unsupported);
        assert_eq!(world.runtime_ok.get(), 1, "a 404 is an answer, not an outage");
        assert_eq!(world.rediscover_calls.get(), 0);
        let slept: Duration = world.slept.borrow().iter().sum();
        assert_eq!(slept, NOTIFY_UNSUPPORTED_BACKOFF);
        assert!(world.slept.borrow().iter().all(|d| *d <= NOTIFY_RETRY_DELAY));
    }

    #[test]
    fn the_unsupported_backoff_ends_when_the_shell_is_repointed() {
        let (pump, world) = pump_on(1);
        script(&world, vec![Ok((404, String::new())), feed("g", 0, &[1])]);
        *world.swap_after_sleeps.borrow_mut() = Some((3, runtime(2)));

        assert_eq!(pump.step(), PumpStep::Unsupported);
        assert_eq!(world.slept.borrow().len(), 3, "woke on the discovery swap");
        // The next iteration polls the new runtime and acks there.
        assert_eq!(pump.step(), PumpStep::Delivered { items: 1 });
        assert_eq!(world.polled.borrow()[1].0, 2);
        assert_eq!(*world.acked.borrow(), [(2, "g".into(), 1)]);
    }

    #[test]
    fn a_failed_poll_rediscovers_and_retries_against_the_new_runtime() {
        let (pump, world) = pump_on(1);
        world.rediscovered.borrow_mut().push_back(Some(runtime(2)));
        script(&world, vec![Err("connection refused".into()), feed("g", 0, &[1])]);

        assert_eq!(pump.step(), PumpStep::Delivered { items: 1 });
        assert_eq!(world.rediscover_calls.get(), 1);
        let ports: Vec<u16> = world.polled.borrow().iter().map(|(p, _)| *p).collect();
        assert_eq!(ports, [1, 2]);
        assert_eq!(*world.acked.borrow(), [(2, "g".into(), 1)]);
        assert!(world.slept.borrow().is_empty(), "the retry is immediate");
    }

    #[test]
    fn a_failed_poll_with_nothing_to_rediscover_idles() {
        let (pump, world) = pump_on(1);
        script(&world, vec![Ok((500, String::new()))]);
        assert_eq!(pump.step(), PumpStep::PollFailed);
        assert_eq!(world.rediscover_calls.get(), 1);
        assert_eq!(world.runtime_ok.get(), 0);
        assert_eq!(*world.slept.borrow(), [NOTIFY_RETRY_DELAY]);
    }

    #[test]
    fn a_rediscovered_runtime_that_also_fails_idles_once() {
        let (pump, world) = pump_on(1);
        world.rediscovered.borrow_mut().push_back(Some(runtime(2)));
        script(&world, vec![Err("down".into()), Err("down too".into())]);
        assert_eq!(pump.step(), PumpStep::PollFailed);
        assert_eq!(world.polled.borrow().len(), 2);
        assert_eq!(*world.slept.borrow(), [NOTIFY_RETRY_DELAY]);
    }

    #[test]
    fn a_rediscovered_feedless_runtime_backs_off() {
        let (pump, world) = pump_on(1);
        world.rediscovered.borrow_mut().push_back(Some(runtime(2)));
        script(&world, vec![Err("down".into()), Ok((404, String::new()))]);
        assert_eq!(pump.step(), PumpStep::Unsupported);
        let slept: Duration = world.slept.borrow().iter().sum();
        assert_eq!(slept, NOTIFY_UNSUPPORTED_BACKOFF);
    }

    #[test]
    fn withdraw_items_remove_banners_and_are_acknowledged() {
        let (pump, world) = pump_on(1);
        let body = serde_json::json!({"generation": "g", "acked_seq": 0, "items": [
            {"id": "g:1", "seq": 1, "kind": "withdraw",
             "agent": {"server_id": "prod", "pane_id": "p1"}},
            {"id": "g:2", "seq": 2, "kind": "someday-kind", "title": "t"},
            {"id": "g:3", "seq": 3, "kind": "alert", "title": "t"},
        ]});
        script(&world, vec![Ok((200, body.to_string()))]);

        assert_eq!(pump.step(), PumpStep::Delivered { items: 3 });
        assert_eq!(
            *world.withdrawn.borrow(),
            [banners::AgentRef {
                server_id: "prod".into(),
                pane_id: "p1".into()
            }]
        );
        assert_eq!(*world.posted.borrow(), ["g:3"], "withdraw and unknown kinds post nothing");
        let acked: Vec<u64> = world.acked.borrow().iter().map(|(_, _, s)| *s).collect();
        assert_eq!(acked, [1, 2, 3]);
        assert_eq!(cursor(&pump), at("g", 3));
    }

    #[test]
    fn a_discovery_swap_mid_batch_keeps_acking_the_runtime_that_served_it() {
        let (pump, world) = pump_on(1);
        *world.swap_on_post.borrow_mut() = Some(runtime(2));
        world.failing.borrow_mut().insert("g:2".into());
        script(&world, vec![feed("g", 0, &[1, 2]), feed("g", 2, &[])]);

        assert_eq!(pump.step(), PumpStep::FellBack { fallback_ok: true });
        // Both the ack and the fallback went to the runtime whose generation
        // and sequence numbers they name, not to the freshly swapped one.
        assert_eq!(*world.acked.borrow(), [(1, "g".into(), 1)]);
        assert_eq!(world.fell_back.borrow()[0].0, 1);
        // The next iteration follows the swap, carrying the cursor with it.
        pump.step();
        assert_eq!(world.polled.borrow()[1], (2, at("g", 2)));
    }

    #[test]
    fn without_a_runtime_the_pump_idles_instead_of_polling() {
        let (pump, world) = pump_on(1);
        *world.discovery.borrow_mut() = None;
        assert_eq!(pump.step(), PumpStep::Idle);
        assert!(world.polled.borrow().is_empty());
        assert_eq!(*world.slept.borrow(), [NOTIFY_RETRY_DELAY]);
    }

    #[test]
    fn an_unparseable_feed_idles_without_touching_the_cursor() {
        let (pump, world) = pump_on(1);
        *pump.cursor.lock_or_recover() = at("g", 5);
        script(&world, vec![Ok((200, "not json".into())), Ok((200, "{}".into()))]);
        assert_eq!(pump.step(), PumpStep::BadFeed);
        assert_eq!(pump.step(), PumpStep::BadFeed, "no generation is no feed");
        assert_eq!(cursor(&pump), at("g", 5));
        assert!(world.posted.borrow().is_empty());
    }

    #[test]
    fn a_poisoned_cursor_does_not_take_the_pump_down() {
        let (pump, world) = pump_on(1);
        let cursor_lock = pump.cursor.clone();
        let _ = std::thread::spawn(move || {
            let _guard = cursor_lock.lock();
            panic!("poison the cursor");
        })
        .join();
        assert!(pump.cursor.is_poisoned());
        script(&world, vec![feed("g", 0, &[1])]);
        assert_eq!(pump.step(), PumpStep::Delivered { items: 1 });
        assert_eq!(cursor(&pump), at("g", 1));
    }
}
