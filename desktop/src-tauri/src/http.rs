//! Minimal loopback HTTP client for the sidecar, performed Rust-side.
//!
//! The WebView must NOT `fetch` the sidecar directly: it is a different origin
//! (`localhost:1420` in dev, the Tauri app origin in prod) and the loopback
//! sidecar — owned by the sidecar slice — does not send CORS headers, so the
//! browser would block the response and the shell would wrongly report the
//! sidecar unreachable. Doing the request here (Rust, no browser) sidesteps CORS
//! entirely and keeps the access token out of JS land.
//!
//! Plaintext HTTP/1.1 over a TCP socket to loopback; no TLS, no HTTP crate.
//! Connections are kept alive and pooled per `host:port` (see `send_request`):
//! the deck polls `/state` and fetches every changed tile, and a fresh TCP
//! handshake + server thread per request measured ~0.98 ms/req against
//! ~0.22 ms/req on a reused connection.
//!
//! Beyond `/health`, this also proxies the deck endpoints the WebView needs —
//! `/state` (JSON), `/tile/{i}` + `/panel` (PNG), and `POST /press/{i}` — with
//! the sidecar access token injected HERE (query param for GETs, `X-Herdeck-Token`
//! header for the press POST). The token therefore never crosses into JS: the
//! frontend invokes token-free Tauri commands (see lib.rs) that call these.

use std::collections::HashMap;
use std::io::{ErrorKind, Read, Write};
use std::net::TcpStream;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

// --- request builders --------------------------------------------------------

/// Build an HTTP/1.1 GET request. The connection stays open (keep-alive) so the
/// pool can reuse it; the response is framed by its `Content-Length`.
pub fn build_get_request(host: &str, path_and_query: &str) -> String {
    build_get_request_with_headers(host, path_and_query, &[])
}

/// Same as `build_get_request` with extra headers (`(name, value)` pairs).
pub fn build_get_request_with_headers(
    host: &str,
    path_and_query: &str,
    headers: &[(&str, &str)],
) -> String {
    let mut req = format!(
        "GET {path_and_query} HTTP/1.1\r\n\
         Host: {host}\r\n\
         Accept: application/json\r\n"
    );
    for (name, value) in headers {
        req.push_str(&format!("{name}: {value}\r\n"));
    }
    req.push_str("\r\n");
    req
}

/// Build an HTTP/1.1 POST with a single extra header and an empty body. Used for
/// `/press/{i}`, whose auth is the `X-Herdeck-Token` header (matching web.py).
pub fn build_post_request(
    host: &str,
    path_and_query: &str,
    header_name: &str,
    header_value: &str,
) -> String {
    format!(
        "POST {path_and_query} HTTP/1.1\r\n\
         Host: {host}\r\n\
         {header_name}: {header_value}\r\n\
         Content-Length: 0\r\n\r\n"
    )
}

/// Build an HTTP/1.1 POST with a JSON body and one extra header (the
/// `X-Herdeck-Token` auth header). Content-Type/-Length frame the body; the
/// sidecar reads exactly Content-Length bytes.
pub fn build_post_json_request(
    host: &str,
    path_and_query: &str,
    header_name: &str,
    header_value: &str,
    body: &str,
) -> String {
    format!(
        "POST {path_and_query} HTTP/1.1\r\n\
         Host: {host}\r\n\
         {header_name}: {header_value}\r\n\
         Content-Type: application/json\r\n\
         Content-Length: {len}\r\n\r\n\
         {body}",
        len = body.len()
    )
}

/// Build an HTTP/1.1 DELETE with one extra header and an empty body.
pub fn build_delete_request(
    host: &str,
    path_and_query: &str,
    header_name: &str,
    header_value: &str,
) -> String {
    format!(
        "DELETE {path_and_query} HTTP/1.1\r\n\
         Host: {host}\r\n\
         {header_name}: {header_value}\r\n\
         Content-Length: 0\r\n\r\n"
    )
}

// --- response parsing --------------------------------------------------------

/// Split a raw (complete) HTTP response into (status_code, body).
pub fn parse_http_response(raw: &str) -> Result<(u16, String), String> {
    let (code, body) = parse_http_response_bytes(raw.as_bytes())?;
    Ok((code, String::from_utf8_lossy(&body).into_owned()))
}

/// Find the first occurrence of `needle` in `haystack` (tiny substring search;
/// the header/body separator is only a handful of bytes in).
fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || haystack.len() < needle.len() {
        return None;
    }
    haystack.windows(needle.len()).position(|w| w == needle)
}

/// Where the header block ends: `(head_end, body_start)`.
fn header_split(raw: &[u8]) -> Option<(usize, usize)> {
    find_subslice(raw, b"\r\n\r\n")
        .map(|i| (i, i + 4))
        .or_else(|| find_subslice(raw, b"\n\n").map(|i| (i, i + 2)))
}

/// The parts of a response head the transport needs to frame the body and to
/// decide whether the connection may carry another request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResponseHead {
    pub status: u16,
    pub content_length: Option<usize>,
    pub chunked: bool,
    /// The server allows this connection to carry another request.
    pub keep_alive: bool,
}

/// Parse a response head (status line + headers, without the blank line).
pub fn parse_response_head(head: &str) -> Result<ResponseHead, String> {
    let mut lines = head.lines();
    let status_line = lines
        .next()
        .ok_or_else(|| "empty HTTP response".to_string())?;
    let mut parts = status_line.split_whitespace();
    let version = parts.next().unwrap_or("");
    // e.g. "HTTP/1.1 200 OK" -> 200
    let status = parts
        .next()
        .and_then(|c| c.parse::<u16>().ok())
        .ok_or_else(|| format!("could not parse HTTP status line: {status_line:?}"))?;
    let mut content_length = None;
    let mut chunked = false;
    // HTTP/1.1 defaults to persistent connections, HTTP/1.0 to close.
    let mut keep_alive = version.eq_ignore_ascii_case("HTTP/1.1");
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        let (name, value) = (name.trim(), value.trim());
        if name.eq_ignore_ascii_case("content-length") {
            content_length = value.parse::<usize>().ok();
        } else if name.eq_ignore_ascii_case("transfer-encoding") {
            chunked = value.to_ascii_lowercase().contains("chunked");
        } else if name.eq_ignore_ascii_case("connection") {
            let v = value.to_ascii_lowercase();
            if v.contains("close") {
                keep_alive = false;
            } else if v.contains("keep-alive") {
                keep_alive = true;
            }
        }
    }
    Ok(ResponseHead {
        status,
        content_length,
        chunked,
        keep_alive,
    })
}

/// Like `parse_http_response` but byte-preserving, so binary bodies (PNG tiles /
/// panel) survive. Only the header block is treated as text. A body longer than
/// a declared `Content-Length` is cut to it.
pub fn parse_http_response_bytes(raw: &[u8]) -> Result<(u16, Vec<u8>), String> {
    let (head_end, body_start) = header_split(raw)
        .ok_or_else(|| "malformed HTTP response (no header/body split)".to_string())?;
    let head = parse_response_head(&String::from_utf8_lossy(&raw[..head_end]))?;
    let body = &raw[body_start..];
    let body = match head.content_length {
        Some(n) if n <= body.len() => &body[..n],
        _ => body,
    };
    Ok((head.status, body.to_vec()))
}

// --- pooled keep-alive transport ---------------------------------------------

/// Idle connections kept per `host:port`. The deck's steady state is a `/state`
/// long poll, the notification long poll and a short burst of tile fetches, so
/// a handful is plenty.
const MAX_IDLE_PER_HOST: usize = 4;

/// An idle connection older than this is dropped instead of reused: every idle
/// keep-alive socket parks a server thread, and a runtime restarted on the same
/// port would only answer a stale socket with a reset anyway.
const MAX_IDLE_AGE: Duration = Duration::from_secs(30);

type IdlePool = HashMap<String, Vec<(TcpStream, Instant)>>;

fn pool() -> &'static Mutex<IdlePool> {
    static POOL: OnceLock<Mutex<IdlePool>> = OnceLock::new();
    POOL.get_or_init(|| Mutex::new(HashMap::new()))
}

fn take_idle(key: &str) -> Option<TcpStream> {
    let mut pool = pool().lock().unwrap_or_else(|p| p.into_inner());
    let conns = pool.get_mut(key)?;
    while let Some((stream, since)) = conns.pop() {
        if since.elapsed() < MAX_IDLE_AGE {
            return Some(stream);
        }
    }
    None
}

fn put_idle(key: &str, stream: TcpStream) {
    let mut pool = pool().lock().unwrap_or_else(|p| p.into_inner());
    let conns = pool.entry(key.to_string()).or_default();
    conns.retain(|(_, since)| since.elapsed() < MAX_IDLE_AGE);
    if conns.len() < MAX_IDLE_PER_HOST {
        conns.push((stream, Instant::now()));
    }
}

/// Number of idle pooled connections for `host:port` (test/diagnostic aid).
pub fn idle_connections(host: &str, port: u16) -> usize {
    let pool = pool().lock().unwrap_or_else(|p| p.into_inner());
    pool.get(&format!("{host}:{port}")).map_or(0, Vec::len)
}

enum ExchangeError {
    /// A REUSED connection the server had already closed: no byte of a response
    /// arrived, so the request was never processed and is safe to resend once
    /// on a fresh connection.
    Stale(String),
    Fatal(String),
}

fn is_disconnect(kind: ErrorKind) -> bool {
    matches!(
        kind,
        ErrorKind::ConnectionReset
            | ErrorKind::ConnectionAborted
            | ErrorKind::BrokenPipe
            | ErrorKind::UnexpectedEof
            | ErrorKind::NotConnected
    )
}

/// Read into `buf` until it holds `len` bytes.
fn read_exact_into(
    stream: &mut TcpStream,
    buf: &mut Vec<u8>,
    len: usize,
    chunk: &mut [u8],
) -> Result<(), String> {
    while buf.len() < len {
        match stream.read(chunk) {
            Ok(0) => return Err("read from sidecar: connection closed mid-body".to_string()),
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(e) => return Err(format!("read from sidecar: {e}")),
        }
    }
    Ok(())
}

/// Write one request and read exactly one response. Returns the status, the
/// body, and the stream back when it may carry another request.
fn exchange(
    mut stream: TcpStream,
    req: &[u8],
    timeout: Duration,
    reused: bool,
) -> Result<(u16, Vec<u8>, Option<TcpStream>), ExchangeError> {
    let fail = |received: usize, msg: String, kind: Option<ErrorKind>| {
        if reused && received == 0 && kind.map_or(true, is_disconnect) {
            ExchangeError::Stale(msg)
        } else {
            ExchangeError::Fatal(msg)
        }
    };
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));
    if let Err(e) = stream.write_all(req) {
        return Err(fail(0, format!("write to sidecar: {e}"), Some(e.kind())));
    }

    let mut buf: Vec<u8> = Vec::with_capacity(8192);
    let mut chunk = [0u8; 16 * 1024];
    let (head_end, body_start) = loop {
        if let Some(split) = header_split(&buf) {
            break split;
        }
        match stream.read(&mut chunk) {
            Ok(0) => {
                let msg = if buf.is_empty() {
                    "read from sidecar: connection closed before a response"
                } else {
                    "malformed HTTP response (no header/body split)"
                };
                return Err(fail(buf.len(), msg.to_string(), None));
            }
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(e) => {
                return Err(fail(
                    buf.len(),
                    format!("read from sidecar: {e}"),
                    Some(e.kind()),
                ))
            }
        }
    };
    let head = parse_response_head(&String::from_utf8_lossy(&buf[..head_end]))
        .map_err(ExchangeError::Fatal)?;
    let mut body = buf.split_off(body_start);
    let reusable = if matches!(head.status, 100..=199 | 204 | 304) {
        // Bodiless by definition, whatever the head says.
        body.clear();
        head.keep_alive
    } else if let Some(len) = head.content_length {
        read_exact_into(&mut stream, &mut body, len, &mut chunk).map_err(ExchangeError::Fatal)?;
        // Surplus bytes would be misread as the next response's head.
        let clean = body.len() == len;
        body.truncate(len);
        head.keep_alive && clean
    } else if head.chunked {
        return Err(ExchangeError::Fatal(
            "sidecar sent a chunked response (unsupported)".to_string(),
        ));
    } else {
        // Unframed body: it ends at EOF, so the connection cannot be reused.
        stream
            .read_to_end(&mut body)
            .map_err(|e| ExchangeError::Fatal(format!("read from sidecar: {e}")))?;
        false
    };
    Ok((head.status, body, reusable.then_some(stream)))
}

/// Send one complete request (head + body bytes) to `host:port` and return
/// `(status, body)` for EVERY status; `Err` only on connect/transport failure.
///
/// Reuses an idle pooled connection when there is one. A pooled connection the
/// server has meanwhile closed (runtime restarted, idle reap) fails before any
/// response byte arrives; that one case is retried once on a fresh connection
/// — the server never read the request, so even a POST is not duplicated.
pub fn send_request(
    host: &str,
    port: u16,
    req: &[u8],
    timeout: Duration,
) -> Result<(u16, Vec<u8>), String> {
    let key = format!("{host}:{port}");
    if let Some(stream) = take_idle(&key) {
        match exchange(stream, req, timeout, true) {
            Ok((code, body, back)) => {
                if let Some(s) = back {
                    put_idle(&key, s);
                }
                return Ok((code, body));
            }
            Err(ExchangeError::Fatal(e)) => return Err(e),
            Err(ExchangeError::Stale(_)) => {} // fall through to a fresh connection
        }
    }
    let stream = TcpStream::connect(&key).map_err(|e| format!("connect {key}: {e}"))?;
    let _ = stream.set_nodelay(true);
    match exchange(stream, req, timeout, false) {
        Ok((code, body, back)) => {
            if let Some(s) = back {
                put_idle(&key, s);
            }
            Ok((code, body))
        }
        Err(ExchangeError::Fatal(e)) | Err(ExchangeError::Stale(e)) => Err(e),
    }
}

fn body_string(body: Vec<u8>) -> String {
    String::from_utf8(body).unwrap_or_else(|e| String::from_utf8_lossy(e.as_bytes()).into_owned())
}

/// Issue an already-built GET request and return the body on a 2xx.
pub fn http_get_request(
    host: &str,
    port: u16,
    req: &str,
    timeout: Duration,
) -> Result<String, String> {
    let (code, body) = send_request(host, port, req.as_bytes(), timeout)?;
    if (200..300).contains(&code) {
        Ok(body_string(body))
    } else {
        Err(format!("sidecar returned HTTP {code}"))
    }
}

/// GET `path_and_query` from `host:port`, returning the response body on a 2xx.
/// Body must be UTF-8 (fine for the JSON endpoints; PNGs use `http_get_bytes`).
pub fn http_get(
    host: &str,
    port: u16,
    path_and_query: &str,
    timeout: Duration,
) -> Result<String, String> {
    http_get_request(host, port, &build_get_request(host, path_and_query), timeout)
}

/// GET `path_and_query`, returning `(status, body-bytes)` even for non-2xx (so
/// the caller can distinguish a 404 — "no tile yet" — from a hard error).
pub fn http_get_bytes(
    host: &str,
    port: u16,
    path_and_query: &str,
    timeout: Duration,
) -> Result<(u16, Vec<u8>), String> {
    let req = build_get_request(host, path_and_query);
    send_request(host, port, req.as_bytes(), timeout)
}

/// POST `path_and_query` with one extra header, returning the HTTP status code.
/// 4xx (e.g. 403 bad token, 400 bad index) are returned as codes, NOT errors —
/// the caller relays them; `Err` is reserved for connect/read failures.
pub fn http_post(
    host: &str,
    port: u16,
    path_and_query: &str,
    header: (&str, &str),
    timeout: Duration,
) -> Result<u16, String> {
    let req = build_post_request(host, path_and_query, header.0, header.1);
    send_request(host, port, req.as_bytes(), timeout).map(|(code, _)| code)
}

/// POST a JSON body with one extra header, returning `(status, body)` for ALL
/// status codes — the caller reads `{errors}` on 200 and distinguishes 400 (a
/// malformed body the sidecar rejected). `Err` is reserved for connect/read
/// failures, matching `http_post`/`http_get`.
pub fn http_post_json(
    host: &str,
    port: u16,
    path_and_query: &str,
    header: (&str, &str),
    body: &str,
    timeout: Duration,
) -> Result<(u16, String), String> {
    let req = build_post_json_request(host, path_and_query, header.0, header.1, body);
    send_request(host, port, req.as_bytes(), timeout).map(|(code, body)| (code, body_string(body)))
}

/// DELETE `path_and_query` with one extra header, returning the HTTP status
/// code (204 ok, 403 bad token). `Err` only on connect/read failure.
pub fn http_delete(
    host: &str,
    port: u16,
    path_and_query: &str,
    header: (&str, &str),
    timeout: Duration,
) -> Result<u16, String> {
    let req = build_delete_request(host, path_and_query, header.0, header.1);
    send_request(host, port, req.as_bytes(), timeout).map(|(code, _)| code)
}

/// Percent-encode a single URL path segment per RFC 3986: keep the unreserved
/// set (`A-Z a-z 0-9 - . _ ~`), emit `%XX` (upper-hex) for every other byte.
/// Used so a `token_env` with a space or slash can't break the DELETE request
/// line or the sidecar's `path.rsplit('/')`. The sidecar `unquote`s it back.
pub fn percent_encode_segment(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// Standard base64 (with padding). Inline to avoid a new crate dependency; used
/// to frame proxied PNG bytes as a `data:` URL the WebView `<img>` can render.
pub fn base64_encode(input: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[((n >> 18) & 63) as usize] as char);
        out.push(ALPHABET[((n >> 12) & 63) as usize] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[((n >> 6) & 63) as usize] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[(n & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}

// --- token-injecting proxy layer (the sidecar token is added here, never in JS) ---

/// The runtime clamps a `/state` long poll to this many milliseconds (C2).
pub const STATE_MAX_WAIT_MS: u64 = 25_000;

/// The `/state` path + query for a plain poll (`after == None`) or a long poll
/// (`?after=<version>&wait_ms=<ms>`, `wait_ms` clamped to `STATE_MAX_WAIT_MS`).
pub fn state_path(token: &str, after: Option<u64>, wait_ms: Option<u64>) -> String {
    let mut path = format!("/state?token={token}");
    if let Some(after) = after {
        path.push_str(&format!("&after={after}"));
        if let Some(wait) = wait_ms {
            path.push_str(&format!("&wait_ms={}", wait.min(STATE_MAX_WAIT_MS)));
        }
    }
    path
}

/// Proxy `GET /state`, returning the JSON body. The token is injected as a query
/// param exactly as the sidecar (and web.py) expects. `claim` adds the
/// `X-Herdeck-Shell` header — the shell's "I post the banners" heartbeat.
pub fn fetch_state(
    host: &str,
    port: u16,
    token: &str,
    timeout: Duration,
    claim: bool,
    shell_gen: Option<&str>,
) -> Result<String, String> {
    fetch_state_poll(host, port, token, timeout, claim, shell_gen, None, None)
}

/// `fetch_state` with the optional long-poll cursor (see `state_path`). The
/// caller's `timeout` must outlast `wait_ms`.
#[allow(clippy::too_many_arguments)]
pub fn fetch_state_poll(
    host: &str,
    port: u16,
    token: &str,
    timeout: Duration,
    claim: bool,
    shell_gen: Option<&str>,
    after: Option<u64>,
    wait_ms: Option<u64>,
) -> Result<String, String> {
    let mut headers: Vec<(&str, String)> = Vec::new();
    if claim {
        headers.push(("X-Herdeck-Shell", "1".to_string()));
        if let Some(gen) = shell_gen {
            headers.push(("X-Herdeck-Shell-Gen", gen.to_string()));
        }
    }
    let owned: Vec<(&str, &str)> = headers.iter().map(|(k, v)| (*k, v.as_str())).collect();
    let req = build_get_request_with_headers(host, &state_path(token, after, wait_ms), &owned);
    http_get_request(host, port, &req, timeout)
}

/// Long-poll the shell-owned notification feed, returning `(status, body)` for
/// every status: the pump tells a 404 ("this source has no feed", e.g. the
/// demo/mock source) apart from a failure. A live request is also the
/// banner-duty claim, so an event wakes this connection instead of waiting for
/// a fixed heartbeat interval.
pub fn fetch_notifications_status(
    host: &str,
    port: u16,
    token: &str,
    timeout: Duration,
    generation: Option<&str>,
    after: u64,
    shell_gen: &str,
) -> Result<(u16, String), String> {
    let mut path = format!("/notifications?token={token}&after={after}&wait_ms=25000");
    if let Some(generation) = generation {
        path.push_str("&generation=");
        path.push_str(&percent_encode_segment(generation));
    }
    let req = build_get_request_with_headers(
        host,
        &path,
        &[("X-Herdeck-Shell", "1"), ("X-Herdeck-Shell-Gen", shell_gen)],
    );
    send_request(host, port, req.as_bytes(), timeout).map(|(code, body)| (code, body_string(body)))
}

/// `fetch_notifications_status` that returns the body on a 2xx and an `Err`
/// for every other status.
pub fn fetch_notifications(
    host: &str,
    port: u16,
    token: &str,
    timeout: Duration,
    generation: Option<&str>,
    after: u64,
    shell_gen: &str,
) -> Result<String, String> {
    let (code, body) =
        fetch_notifications_status(host, port, token, timeout, generation, after, shell_gen)?;
    if (200..300).contains(&code) {
        Ok(body)
    } else {
        Err(format!("sidecar returned HTTP {code}"))
    }
}

/// Confirm one generation-scoped delivery after the shell posted the banner.
pub fn ack_notification(
    host: &str,
    port: u16,
    token: &str,
    timeout: Duration,
    generation: &str,
    seq: u64,
) -> Result<u16, String> {
    let body = serde_json::json!({"generation": generation, "seq": seq}).to_string();
    let (code, _) = http_post_json(
        host,
        port,
        "/notifications/ack",
        ("X-Herdeck-Token", token),
        &body,
        timeout,
    )?;
    Ok(code)
}

/// Ask the runtime to deliver exactly one queued alert through osascript after
/// native notification delivery failed. The shell keeps pumping later alerts.
pub fn fallback_notification(
    host: &str,
    port: u16,
    token: &str,
    timeout: Duration,
    generation: &str,
    seq: u64,
    shell_gen: &str,
    error: &str,
) -> Result<u16, String> {
    // `error` is the native failure, logged by the runtime as the fallback's
    // reason (`reason=shell_native_failed error=...`).
    let body = serde_json::json!({
        "generation": generation,
        "seq": seq,
        "shell_gen": shell_gen,
        "error": error,
    })
    .to_string();
    let (code, _) = http_post_json(
        host,
        port,
        "/notifications/fallback",
        ("X-Herdeck-Token", token),
        &body,
        timeout,
    )?;
    Ok(code)
}

/// Proxy a PNG endpoint (`/tile/{i}` or `/panel`) and return its raw bytes.
/// `Ok(None)` on 404 (no tile/panel yet).
pub fn fetch_png(
    host: &str,
    port: u16,
    path: &str,
    token: &str,
    timeout: Duration,
) -> Result<Option<Vec<u8>>, String> {
    let (code, body) = http_get_bytes(host, port, &format!("{path}?token={token}"), timeout)?;
    match code {
        200 => Ok(Some(body)),
        404 => Ok(None),
        c => Err(format!("sidecar returned HTTP {c} for {path}")),
    }
}

/// Proxy a PNG endpoint (`/tile/{i}` or `/panel`) and frame it as a `data:` URL.
/// `Ok(None)` on 404 (no tile/panel yet) so the caller clears the cell. Kept as
/// the fallback transport beside the `herdeck://` image protocol (lib.rs).
pub fn fetch_image(
    host: &str,
    port: u16,
    path: &str,
    token: &str,
    timeout: Duration,
) -> Result<Option<String>, String> {
    Ok(fetch_png(host, port, path, token, timeout)?
        .map(|body| format!("data:image/png;base64,{}", base64_encode(&body))))
}

/// Proxy `GET /setup`, injecting the token as a query param. Returns the JSON body.
pub fn fetch_setup(
    host: &str,
    port: u16,
    token: &str,
    timeout: Duration,
) -> Result<String, String> {
    http_get(host, port, &format!("/setup?token={token}"), timeout)
}

/// Proxy `POST /setup/connect` with the token in the `X-Herdeck-Token` header and a
/// JSON body. Returns `(status, body)` for all statuses (200 carries `{ok,…}`, 400 a
/// malformed body), matching `http_post_json`.
pub fn post_setup_connect(
    host: &str,
    port: u16,
    token: &str,
    body: &str,
    timeout: Duration,
) -> Result<(u16, String), String> {
    http_post_json(
        host,
        port,
        "/setup/connect",
        ("X-Herdeck-Token", token),
        body,
        timeout,
    )
}

/// Proxy `POST /press/{index}` with the token in the `X-Herdeck-Token` header,
/// returning the sidecar's HTTP status code (204 ok, 403 bad token, 400 bad index).
pub fn send_press(
    host: &str,
    port: u16,
    index: u32,
    token: &str,
    timeout: Duration,
) -> Result<u16, String> {
    http_post(
        host,
        port,
        &format!("/press/{index}"),
        ("X-Herdeck-Token", token),
        timeout,
    )
}

/// The runtime route behind the "next blocked agent" hotkey.
pub const TRIAGE_PATH: &str = "/triage";

/// Proxy `POST /triage` (open the longest-blocked agent's drill) with the token
/// in the `X-Herdeck-Token` header, returning the HTTP status code (204 ok,
/// 403 bad token, 404 a source without drills, e.g. the demo mock).
pub fn send_triage(host: &str, port: u16, token: &str, timeout: Duration) -> Result<u16, String> {
    http_post(host, port, TRIAGE_PATH, ("X-Herdeck-Token", token), timeout)
}

/// The runtime routes behind an actionable banner (click / answer / reply).
pub const AGENT_DRILL_PATH: &str = "/agents/drill";
pub const AGENT_ANSWER_PATH: &str = "/agents/answer";

/// POST a banner action's JSON body (`banners::drill_body` / `answer_body`)
/// with the token header. Returns the HTTP status: 204 applied, 409 stale
/// banner, 404 unknown agent, 503 server offline.
pub fn post_agent_action(
    host: &str,
    port: u16,
    token: &str,
    timeout: Duration,
    path: &str,
    body: &str,
) -> Result<u16, String> {
    let (code, _) = http_post_json(host, port, path, ("X-Herdeck-Token", token), body, timeout)?;
    Ok(code)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_get_request_with_headers_appends_custom_headers() {
        let req = build_get_request_with_headers(
            "127.0.0.1",
            "/state?token=abc",
            &[("X-Herdeck-Shell", "1")],
        );
        assert!(req.contains("X-Herdeck-Shell: 1\r\n"));
        assert!(req.ends_with("X-Herdeck-Shell: 1\r\n\r\n"));
    }

    #[test]
    fn build_get_request_is_keep_alive_http_1_1() {
        let req = build_get_request("127.0.0.1", "/health?token=abc");
        assert!(req.starts_with("GET /health?token=abc HTTP/1.1\r\n"));
        assert!(req.contains("Host: 127.0.0.1\r\n"));
        assert!(!req.contains("Connection: close"));
        assert!(req.ends_with("\r\n\r\n"));
    }

    #[test]
    fn parse_http_response_extracts_code_and_body() {
        let raw = "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"ok\":true}";
        let (code, body) = parse_http_response(raw).unwrap();
        assert_eq!(code, 200);
        assert_eq!(body, "{\"ok\":true}");
    }

    #[test]
    fn parse_http_response_handles_403() {
        let raw = "HTTP/1.0 403 Forbidden\r\n\r\nnope";
        let (code, body) = parse_http_response(raw).unwrap();
        assert_eq!(code, 403);
        assert_eq!(body, "nope");
    }

    #[test]
    fn parse_http_response_rejects_garbage() {
        assert!(parse_http_response("no headers no body").is_err());
    }

    #[test]
    fn parse_http_response_bytes_preserves_binary_body() {
        let mut raw = b"HTTP/1.0 200 OK\r\nContent-Type: image/png\r\n\r\n".to_vec();
        raw.extend_from_slice(&[0x89, 0x50, 0x4e, 0x47, 0x00, 0xff]); // PNG-ish, non-UTF8
        let (code, body) = parse_http_response_bytes(&raw).unwrap();
        assert_eq!(code, 200);
        assert_eq!(body, vec![0x89, 0x50, 0x4e, 0x47, 0x00, 0xff]);
    }

    #[test]
    fn response_head_framing_and_keep_alive_rules() {
        let h = parse_response_head("HTTP/1.1 200 OK\r\nContent-Length: 12").unwrap();
        assert_eq!(
            h,
            ResponseHead {
                status: 200,
                content_length: Some(12),
                chunked: false,
                keep_alive: true
            }
        );
        // HTTP/1.0 closes unless it says otherwise; 1.1 persists unless told to close.
        assert!(!parse_response_head("HTTP/1.0 200 OK").unwrap().keep_alive);
        assert!(
            parse_response_head("HTTP/1.0 200 OK\r\nConnection: keep-alive")
                .unwrap()
                .keep_alive
        );
        assert!(
            !parse_response_head("HTTP/1.1 200 OK\r\nconnection: Close")
                .unwrap()
                .keep_alive
        );
        assert!(
            parse_response_head("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked")
                .unwrap()
                .chunked
        );
    }

    #[test]
    fn build_post_request_carries_header_and_zero_length() {
        let req = build_post_request("127.0.0.1", "/press/3", "X-Herdeck-Token", "tok");
        assert!(req.starts_with("POST /press/3 HTTP/1.1\r\n"));
        assert!(req.contains("X-Herdeck-Token: tok\r\n"));
        assert!(req.contains("Content-Length: 0\r\n"));
        assert!(req.ends_with("\r\n\r\n"));
    }

    #[test]
    fn send_triage_posts_the_token_to_the_triage_route() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut sock, _) = listener.accept().unwrap();
            let mut buf = [0u8; 1024];
            let n = sock.read(&mut buf).unwrap();
            sock.write_all(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
                .unwrap();
            String::from_utf8_lossy(&buf[..n]).to_string()
        });
        let code = send_triage("127.0.0.1", port, "tok", Duration::from_secs(2)).unwrap();
        let req = server.join().unwrap();
        assert_eq!(code, 204);
        assert!(req.starts_with("POST /triage HTTP/1.1\r\n"));
        assert!(req.contains("X-Herdeck-Token: tok\r\n"));
    }

    #[test]
    fn build_post_json_request_carries_body_headers_and_length() {
        let req = build_post_json_request(
            "127.0.0.1",
            "/config",
            "X-Herdeck-Token",
            "tok",
            "{\"a\":1}",
        );
        assert!(req.starts_with("POST /config HTTP/1.1\r\n"));
        assert!(req.contains("X-Herdeck-Token: tok\r\n"));
        assert!(req.contains("Content-Type: application/json\r\n"));
        assert!(req.contains("Content-Length: 7\r\n")); // {"a":1} is 7 bytes
        assert!(req.ends_with("\r\n\r\n{\"a\":1}"));
    }

    #[test]
    fn build_delete_request_carries_token_header_and_zero_length() {
        let req = build_delete_request("127.0.0.1", "/secret/TOK", "X-Herdeck-Token", "tok");
        assert!(req.starts_with("DELETE /secret/TOK HTTP/1.1\r\n"));
        assert!(req.contains("X-Herdeck-Token: tok\r\n"));
        assert!(req.contains("Content-Length: 0\r\n"));
        assert!(req.ends_with("\r\n\r\n"));
    }

    #[test]
    fn state_path_adds_the_long_poll_cursor_and_clamps_the_wait() {
        assert_eq!(state_path("T", None, Some(5000)), "/state?token=T");
        assert_eq!(state_path("T", Some(7), None), "/state?token=T&after=7");
        assert_eq!(
            state_path("T", Some(7), Some(1500)),
            "/state?token=T&after=7&wait_ms=1500"
        );
        assert_eq!(
            state_path("T", Some(7), Some(90_000)),
            "/state?token=T&after=7&wait_ms=25000"
        );
    }

    #[test]
    fn percent_encode_segment_encodes_unsafe_and_keeps_unreserved() {
        assert_eq!(percent_encode_segment("TOK"), "TOK");
        assert_eq!(percent_encode_segment("My_Tok-1.0~x"), "My_Tok-1.0~x");
        assert_eq!(percent_encode_segment("MY TOK"), "MY%20TOK");
        assert_eq!(percent_encode_segment("a/b"), "a%2Fb");
        assert_eq!(percent_encode_segment("é"), "%C3%A9"); // UTF-8 bytes, upper-hex
    }

    #[test]
    fn base64_encode_matches_rfc_vectors() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foob"), "Zm9vYg==");
        assert_eq!(base64_encode(b"fooba"), "Zm9vYmE=");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
        // PNG magic -> the familiar "iVBORw" data-URL prefix
        assert_eq!(base64_encode(&[0x89, 0x50, 0x4e, 0x47]), "iVBORw==");
    }
}
