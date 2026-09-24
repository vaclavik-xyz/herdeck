//! Proxy for the runtime's `/agent/*` routes (the desktop agent card).
//!
//! One command covers every card call: the WebView names the route and the
//! shell injects the access token Rust-side (query param for GET, the
//! `X-Herdeck-Token` header for POST), exactly like the deck's own proxies —
//! the token never enters JS. Only paths under `/agent/` are relayed, and a
//! caller-supplied `token` query key is refused so JS cannot override it.

use std::time::Duration;

use crate::proxy::{current_discovery, run_blocking};
use crate::{http, AppState, HDR_TOKEN};

/// Longest long-poll a card may ask the proxy to hold (the runtime clamps too).
pub const AGENT_WAIT_MAX_MS: u64 = 20_000;
/// A card action waits up to 6 s for the bridge's reply
/// (`agent_card.CARD_REPLY_TIMEOUT_S`); leave headroom on top of it.
pub const AGENT_CALL_TIMEOUT: Duration = Duration::from_secs(10);

/// Is `path` a relayable card route? `/agent/…`, URL-safe characters only, and
/// never a `token` query parameter of its own.
pub fn agent_path_allowed(path: &str) -> bool {
    if !path.starts_with("/agent/") || path.contains("..") {
        return false;
    }
    let safe = path
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b"/-_.~%?=&+".contains(&b));
    let query = path.split_once('?').map(|(_, q)| q).unwrap_or("");
    safe && !query.split('&').any(|kv| kv == "token" || kv.starts_with("token="))
}

/// The GET path with the access token appended as a query parameter.
pub fn agent_get_path(path: &str, token: &str) -> String {
    let sep = if path.contains('?') { '&' } else { '?' };
    format!("{path}{sep}token={token}")
}

/// Read timeout: a long-poll outlasts its wait by 6 s; everything else gets
/// `AGENT_CALL_TIMEOUT`.
pub fn agent_call_timeout(wait_ms: Option<u64>) -> Duration {
    match wait_ms {
        Some(w) => AGENT_CALL_TIMEOUT
            .max(Duration::from_millis(w.min(AGENT_WAIT_MAX_MS)) + Duration::from_secs(6)),
        None => AGENT_CALL_TIMEOUT,
    }
}

/// `{status, body}` where `body` is the parsed JSON (null when empty/invalid).
pub fn agent_response(status: u16, text: &str) -> serde_json::Value {
    let body = if text.trim().is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_str(text).unwrap_or(serde_json::Value::Null)
    };
    serde_json::json!({ "status": status, "body": body })
}

/// Relay one card call (`method` GET or POST) to the runtime → `{status, body}`.
/// HTTP errors come back as a status for the card to explain; `Err` only when
/// the runtime is unreachable or the request itself is not allowed.
#[tauri::command]
pub(crate) async fn agent_call(
    state: tauri::State<'_, AppState>,
    method: String,
    path: String,
    body: Option<serde_json::Value>,
    wait_ms: Option<u64>,
) -> Result<serde_json::Value, String> {
    if !agent_path_allowed(&path) {
        return Err(format!("agent_call: path not allowed: {path}"));
    }
    let d = current_discovery(&state)?;
    let timeout = agent_call_timeout(wait_ms);
    run_blocking(move || {
        let (code, text) = match method.as_str() {
            "GET" => {
                let (code, bytes) =
                    http::http_get_bytes(&d.host, d.port, &agent_get_path(&path, &d.token), timeout)?;
                (code, String::from_utf8_lossy(&bytes).into_owned())
            }
            "POST" => {
                let payload = body.unwrap_or_else(|| serde_json::json!({})).to_string();
                http::http_post_json(&d.host, d.port, &path, (HDR_TOKEN, &d.token), &payload, timeout)?
            }
            other => return Err(format!("agent_call: unsupported method {other}")),
        };
        Ok(agent_response(code, &text))
    })
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_agent_routes_without_a_token_param_are_relayed() {
        assert!(agent_path_allowed("/agent/detail?index=3"));
        assert!(agent_path_allowed("/agent/detail?server_id=prod&pane_id=w1-p2&refresh=1"));
        assert!(agent_path_allowed("/agent/answer"));
        assert!(!agent_path_allowed("/config"));
        assert!(!agent_path_allowed("/agentx/detail"));
        assert!(!agent_path_allowed("/agent/../config"));
        assert!(!agent_path_allowed("/agent/detail?token=evil"));
        assert!(!agent_path_allowed("/agent/detail?index=1&token=evil"));
        assert!(!agent_path_allowed("/agent/detail?index=1 HTTP/1.1\r\nX: y"));
        // A value that merely mentions "token" is fine (URLSearchParams encodes '=' and '&').
        assert!(agent_path_allowed("/agent/detail?pane_id=token%3Dx"));
    }

    #[test]
    fn get_path_appends_the_token() {
        assert_eq!(agent_get_path("/agent/detail?index=1", "t"), "/agent/detail?index=1&token=t");
        assert_eq!(agent_get_path("/agent/detail", "t"), "/agent/detail?token=t");
    }

    #[test]
    fn long_polls_outlast_their_wait_and_are_clamped() {
        assert_eq!(agent_call_timeout(None), AGENT_CALL_TIMEOUT);
        assert_eq!(agent_call_timeout(Some(15_000)), Duration::from_secs(21));
        assert_eq!(agent_call_timeout(Some(999_999)), Duration::from_secs(26));
        assert_eq!(agent_call_timeout(Some(0)), AGENT_CALL_TIMEOUT);
    }

    #[test]
    fn response_wraps_status_and_json_body() {
        assert_eq!(
            agent_response(200, r#"{"ok":true}"#),
            serde_json::json!({"status": 200, "body": {"ok": true}})
        );
        assert_eq!(agent_response(404, ""), serde_json::json!({"status": 404, "body": null}));
        assert_eq!(agent_response(500, "oops"), serde_json::json!({"status": 500, "body": null}));
    }
}
