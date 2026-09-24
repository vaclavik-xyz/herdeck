//! Actionable notification banners — the platform-independent half.
//!
//! The runtime's feed items name the agent a banner is about (and, for a
//! blocked agent, its block episode plus optional answer buttons / reply
//! field). This module parses those fields, remembers which delivered banner
//! belongs to which agent (bounded), and decides what a banner activation
//! means. Everything that touches Notification Center lives in `lib.rs`
//! (`post_native_notification`, `banner_clicks`); everything here is pure and
//! unit-tested.

use std::collections::VecDeque;

/// `(server_id, pane_id)` — the runtime's AgentKey.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct AgentRef {
    pub server_id: String,
    pub pane_id: String,
}

/// One answer button: `id` is what the runtime expects back ("approve" /
/// "deny"), `label` the localized button title.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BannerAction {
    pub id: String,
    pub label: String,
}

/// The agent fields of one feed item. Everything is optional: a usage alert
/// or the settings test banner carries none of it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct BannerMeta {
    pub agent: Option<AgentRef>,
    pub event: Option<String>,
    pub episode: Option<String>,
    /// Fingerprint of the prompt's options the actions were built for.
    pub sig: Option<String>,
    pub actions: Vec<BannerAction>,
    /// Placeholder of the inline reply field; `Some` = offer a reply.
    pub reply: Option<String>,
}

fn string_field(item: &serde_json::Value, key: &str) -> Option<String> {
    item.get(key)
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

impl BannerMeta {
    /// Parse the optional agent fields of a feed item; malformed parts are
    /// dropped (never a whole-item failure), and answer buttons / reply are
    /// kept only when the item also names its agent and episode — without
    /// them the runtime could not validate an answer anyway.
    pub fn from_item(item: &serde_json::Value) -> Self {
        let agent = item.get("agent").and_then(|a| {
            Some(AgentRef {
                server_id: string_field(a, "server_id")?,
                pane_id: string_field(a, "pane_id")?,
            })
        });
        let episode = string_field(item, "episode");
        let answerable = agent.is_some() && episode.is_some();
        let actions = if answerable {
            item.get("actions")
                .and_then(|v| v.as_array())
                .into_iter()
                .flatten()
                .filter_map(|a| {
                    let id = string_field(a, "id")?;
                    let label = string_field(a, "label")?;
                    matches!(id.as_str(), "approve" | "deny").then_some(BannerAction { id, label })
                })
                .take(2)
                .collect()
        } else {
            Vec::new()
        };
        BannerMeta {
            agent,
            event: string_field(item, "event"),
            episode,
            sig: string_field(item, "sig"),
            actions,
            reply: if answerable { string_field(item, "reply") } else { None },
        }
    }
}

/// What a delivered banner needs to be answered later, keyed by the banner's
/// Notification Center identifier.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BannerContext {
    pub agent: AgentRef,
    pub episode: Option<String>,
    pub sig: Option<String>,
    pub actions: Vec<BannerAction>,
}

/// Banners this shell delivered, newest last, bounded so a shell that runs for
/// weeks never grows without limit. The oldest entry falls off first — its
/// banner then only reveals the deck (as before this feature).
#[derive(Debug, Default)]
pub struct BannerBook {
    entries: VecDeque<(String, BannerContext)>,
}

pub const BANNER_BOOK_MAX: usize = 64;

impl BannerBook {
    pub const fn new() -> Self {
        BannerBook { entries: VecDeque::new() }
    }

    pub fn record(&mut self, identifier: String, context: BannerContext) {
        self.entries.retain(|(id, _)| id != &identifier);
        self.entries.push_back((identifier, context));
        while self.entries.len() > BANNER_BOOK_MAX {
            self.entries.pop_front();
        }
    }

    pub fn get(&self, identifier: &str) -> Option<&BannerContext> {
        self.entries.iter().find(|(id, _)| id == identifier).map(|(_, c)| c)
    }

    pub fn forget(&mut self, identifier: &str) {
        self.entries.retain(|(id, _)| id != identifier);
    }

    /// Remove and return the identifiers of every banner about `agent`.
    pub fn take_agent(&mut self, agent: &AgentRef) -> Vec<String> {
        let mut taken = Vec::new();
        self.entries.retain(|(id, ctx)| {
            if &ctx.agent == agent {
                taken.push(id.clone());
                false
            } else {
                true
            }
        });
        taken
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

/// The Notification Center identifier of a feed item's banner. Unique per
/// runtime feed generation + sequence; the "herdeck:" prefix keeps it apart
/// from the UUIDs mac-notification-sys gives its own banners.
pub fn banner_identifier(generation: &str, seq: u64) -> String {
    format!("herdeck:{generation}:{seq}")
}

/// What a banner activation asks for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BannerIntent {
    /// Nothing (activation type None, or an empty reply).
    Ignore,
    /// Bring the deck forward (a banner we know nothing about).
    Reveal,
    /// Bring the deck forward and open this agent's drill.
    Drill(AgentRef),
    /// Approve / deny through the runtime (which re-validates the episode).
    Answer { agent: AgentRef, episode: String, sig: String, choice: String },
    /// Type the reply text into the agent's pane.
    Reply { agent: AgentRef, episode: String, text: String },
}

/// Map a raw `NSUserNotificationActivationType` (1 contents clicked, 2 action
/// button, 3 replied, 4 additional action) plus the banner's context to an
/// intent. `additional_id` is the clicked additional action's identifier,
/// `reply` the typed reply text.
pub fn banner_intent(
    activation_type: isize,
    context: Option<&BannerContext>,
    additional_id: Option<&str>,
    reply: Option<&str>,
) -> BannerIntent {
    let drill_or_reveal = || match context {
        Some(ctx) => BannerIntent::Drill(ctx.agent.clone()),
        None => BannerIntent::Reveal,
    };
    let answer = |choice: &str| -> Option<BannerIntent> {
        let ctx = context?;
        let action = ctx.actions.iter().find(|a| a.id == choice)?;
        Some(BannerIntent::Answer {
            agent: ctx.agent.clone(),
            episode: ctx.episode.clone()?,
            sig: ctx.sig.clone()?,
            choice: action.id.clone(),
        })
    };
    match activation_type {
        1 => drill_or_reveal(),
        // The main action button is the first answer (approve) when the banner
        // has answers; otherwise it is macOS's plain "Show".
        2 => context
            .and_then(|ctx| ctx.actions.first())
            .and_then(|first| answer(&first.id))
            .unwrap_or_else(drill_or_reveal),
        3 => {
            let text = reply.map(str::trim).unwrap_or("");
            match context.and_then(|ctx| Some((ctx, ctx.episode.clone()?))) {
                Some(_) if text.is_empty() => BannerIntent::Ignore,
                Some((ctx, episode)) => BannerIntent::Reply {
                    agent: ctx.agent.clone(),
                    episode,
                    text: text.to_string(),
                },
                None => BannerIntent::Reveal,
            }
        }
        4 => additional_id.and_then(answer).unwrap_or_else(drill_or_reveal),
        _ => BannerIntent::Ignore,
    }
}

/// JSON body of `POST /agents/drill`.
pub fn drill_body(agent: &AgentRef) -> String {
    serde_json::json!({"server_id": agent.server_id, "pane_id": agent.pane_id}).to_string()
}

/// JSON body of `POST /agents/answer` for an answer / reply intent.
pub fn answer_body(intent: &BannerIntent) -> Option<String> {
    let body = match intent {
        BannerIntent::Answer { agent, episode, sig, choice } => serde_json::json!({
            "server_id": agent.server_id,
            "pane_id": agent.pane_id,
            "episode": episode,
            "choice": choice,
            "sig": sig,
        }),
        BannerIntent::Reply { agent, episode, text } => serde_json::json!({
            "server_id": agent.server_id,
            "pane_id": agent.pane_id,
            "episode": episode,
            "text": text,
        }),
        _ => return None,
    };
    Some(body.to_string())
}

/// After an answer request: `true` when the deck should still open the
/// agent's drill — the answer was not applied (stale banner, offline server,
/// transport error), so the user sees the current prompt instead.
pub fn answer_needs_drill(result: &Result<u16, String>) -> bool {
    !matches!(result, Ok(204))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn agent(pane: &str) -> AgentRef {
        AgentRef { server_id: "prod".into(), pane_id: pane.into() }
    }

    fn ctx(actions: &[&str]) -> BannerContext {
        BannerContext {
            agent: agent("p1"),
            episode: Some("ep".into()),
            sig: Some("sig".into()),
            actions: actions
                .iter()
                .map(|id| BannerAction { id: id.to_string(), label: id.to_uppercase() })
                .collect(),
        }
    }

    #[test]
    fn meta_parses_agent_episode_and_answers() {
        let item = json!({
            "agent": {"server_id": "prod", "pane_id": "p1"},
            "event": "blocked",
            "episode": "ep",
            "sig": "s",
            "actions": [
                {"id": "approve", "label": "Approve"},
                {"id": "rm -rf", "label": "evil"},
                {"id": "deny", "label": "Deny"}
            ],
            "reply": "Reply…"
        });
        let meta = BannerMeta::from_item(&item);
        assert_eq!(meta.agent, Some(agent("p1")));
        assert_eq!(meta.event.as_deref(), Some("blocked"));
        assert_eq!(meta.episode.as_deref(), Some("ep"));
        let ids: Vec<_> = meta.actions.iter().map(|a| a.id.as_str()).collect();
        assert_eq!(ids, ["approve", "deny"]); // unknown ids are dropped
        assert_eq!(meta.reply.as_deref(), Some("Reply…"));
    }

    #[test]
    fn answers_need_an_agent_and_an_episode() {
        let item = json!({
            "agent": {"server_id": "prod", "pane_id": ""},
            "episode": "ep",
            "actions": [{"id": "approve", "label": "Approve"}],
            "reply": "r"
        });
        let meta = BannerMeta::from_item(&item);
        assert_eq!(meta.agent, None);
        assert!(meta.actions.is_empty() && meta.reply.is_none());
        assert_eq!(BannerMeta::from_item(&json!({"title": "usage"})), BannerMeta::default());
    }

    #[test]
    fn activation_types_map_to_intents() {
        let answers = ctx(&["approve", "deny"]);
        let plain = ctx(&[]);
        // 1 = banner body: open the drill (or just reveal an unknown banner).
        assert_eq!(banner_intent(1, Some(&plain), None, None), BannerIntent::Drill(agent("p1")));
        assert_eq!(banner_intent(1, None, None, None), BannerIntent::Reveal);
        // 2 = main action button: approve when the banner has answers.
        assert_eq!(
            banner_intent(2, Some(&answers), None, None),
            BannerIntent::Answer {
                agent: agent("p1"),
                episode: "ep".into(),
                sig: "sig".into(),
                choice: "approve".into()
            }
        );
        assert_eq!(banner_intent(2, Some(&plain), None, None), BannerIntent::Drill(agent("p1")));
        // 4 = additional action: the named answer only.
        match banner_intent(4, Some(&answers), Some("deny"), None) {
            BannerIntent::Answer { choice, .. } => assert_eq!(choice, "deny"),
            other => panic!("unexpected {other:?}"),
        }
        assert_eq!(
            banner_intent(4, Some(&answers), Some("stop"), None),
            BannerIntent::Drill(agent("p1"))
        );
        // 3 = inline reply (was ignored before): the trimmed text.
        assert_eq!(
            banner_intent(3, Some(&plain), None, Some("  go on \n")),
            BannerIntent::Reply { agent: agent("p1"), episode: "ep".into(), text: "go on".into() }
        );
        assert_eq!(banner_intent(3, Some(&plain), None, Some("   ")), BannerIntent::Ignore);
        assert_eq!(banner_intent(3, None, None, Some("hi")), BannerIntent::Reveal);
        assert_eq!(banner_intent(0, Some(&answers), None, None), BannerIntent::Ignore);
    }

    #[test]
    fn an_answer_without_sig_or_episode_falls_back_to_the_drill() {
        let mut c = ctx(&["approve", "deny"]);
        c.sig = None;
        assert_eq!(banner_intent(2, Some(&c), None, None), BannerIntent::Drill(agent("p1")));
    }

    #[test]
    fn the_book_is_bounded_and_indexed_by_agent() {
        let mut book = BannerBook::default();
        for i in 0..(BANNER_BOOK_MAX + 5) {
            let mut c = ctx(&[]);
            c.agent = agent(if i % 2 == 0 { "even" } else { "odd" });
            book.record(banner_identifier("g", i as u64), c);
        }
        assert_eq!(book.len(), BANNER_BOOK_MAX);
        assert!(book.get(&banner_identifier("g", 0)).is_none()); // oldest fell off
        assert!(book.get(&banner_identifier("g", BANNER_BOOK_MAX as u64)).is_some());
        let odd = book.take_agent(&agent("odd"));
        assert_eq!(odd.len(), BANNER_BOOK_MAX / 2);
        assert!(odd.iter().all(|id| book.get(id).is_none()));
        assert_eq!(book.len(), BANNER_BOOK_MAX / 2);
        book.forget(&banner_identifier("g", (BANNER_BOOK_MAX + 4) as u64));
        assert_eq!(book.len(), BANNER_BOOK_MAX / 2 - 1);
    }

    #[test]
    fn request_bodies_carry_exactly_the_runtime_fields() {
        let body: serde_json::Value = serde_json::from_str(&drill_body(&agent("p1"))).unwrap();
        assert_eq!(body, json!({"server_id": "prod", "pane_id": "p1"}));
        let reply = BannerIntent::Reply {
            agent: agent("p1"),
            episode: "ep".into(),
            text: "go \"on\"".into(),
        };
        let body: serde_json::Value = serde_json::from_str(&answer_body(&reply).unwrap()).unwrap();
        assert_eq!(body, json!({"server_id": "prod", "pane_id": "p1", "episode": "ep", "text": "go \"on\""}));
        assert_eq!(answer_body(&BannerIntent::Reveal), None);
        assert!(!answer_needs_drill(&Ok(204)));
        assert!(answer_needs_drill(&Ok(409)));
        assert!(answer_needs_drill(&Err("down".into())));
    }
}
