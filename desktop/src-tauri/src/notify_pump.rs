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
