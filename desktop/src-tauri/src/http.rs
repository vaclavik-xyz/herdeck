//! Minimal loopback HTTP GET for the sidecar, performed Rust-side.
//!
//! The WebView must NOT `fetch` the sidecar directly: it is a different origin
//! (`localhost:1420` in dev, the Tauri app origin in prod) and the loopback
//! sidecar — owned by the sidecar slice — does not send CORS headers, so the
//! browser would block the response and the shell would wrongly report the
//! sidecar unreachable. Doing the request here (Rust, no browser) sidesteps CORS
//! entirely and keeps the access token out of JS land.
//!
//! Plaintext HTTP/1.0 over a TCP socket to loopback; no TLS, no HTTP crate.
//!
//! Beyond `/health`, this also proxies the deck endpoints the WebView needs —
//! `/state` (JSON), `/tile/{i}` + `/panel` (PNG), and `POST /press/{i}` — with
//! the sidecar access token injected HERE (query param for GETs, `X-Herdeck-Token`
//! header for the press POST). The token therefore never crosses into JS: the
//! frontend invokes token-free Tauri commands (see lib.rs) that call these.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

/// Build an HTTP/1.0 GET request. `Connection: close` means the response ends at
/// EOF, so we can read it whole without parsing `Content-Length`.
pub fn build_get_request(host: &str, path_and_query: &str) -> String {
    format!(
        "GET {path_and_query} HTTP/1.0\r\n\
         Host: {host}\r\n\
         Accept: application/json\r\n\
         Connection: close\r\n\r\n"
    )
}

/// Split a raw HTTP response into (status_code, body).
pub fn parse_http_response(raw: &str) -> Result<(u16, String), String> {
    let (head, body) = raw
        .split_once("\r\n\r\n")
        .or_else(|| raw.split_once("\n\n"))
        .ok_or_else(|| "malformed HTTP response (no header/body split)".to_string())?;
    let status_line = head
        .lines()
        .next()
        .ok_or_else(|| "empty HTTP response".to_string())?;
    // e.g. "HTTP/1.0 200 OK" -> 200
    let code = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|c| c.parse::<u16>().ok())
        .ok_or_else(|| format!("could not parse HTTP status line: {status_line:?}"))?;
    Ok((code, body.to_string()))
}

/// GET `path_and_query` from `host:port`, returning the response body on a 2xx.
/// Body must be UTF-8 (fine for the JSON endpoints; PNG tiles are slice 2's job).
pub fn http_get(
    host: &str,
    port: u16,
    path_and_query: &str,
    timeout: Duration,
) -> Result<String, String> {
    let addr = format!("{host}:{port}");
    let mut stream = TcpStream::connect(&addr).map_err(|e| format!("connect {addr}: {e}"))?;
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));

    let req = build_get_request(host, path_and_query);
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("write to sidecar: {e}"))?;

    let mut raw = String::new();
    stream
        .read_to_string(&mut raw)
        .map_err(|e| format!("read from sidecar: {e}"))?;

    let (code, body) = parse_http_response(&raw)?;
    if (200..300).contains(&code) {
        Ok(body)
    } else {
        Err(format!("sidecar returned HTTP {code}"))
    }
}

/// Find the first occurrence of `needle` in `haystack` (tiny substring search;
/// the header/body separator is only a handful of bytes in).
fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || haystack.len() < needle.len() {
        return None;
    }
    haystack.windows(needle.len()).position(|w| w == needle)
}

/// Like `parse_http_response` but byte-preserving, so binary bodies (PNG tiles /
/// panel) survive. Only the header block is treated as text.
pub fn parse_http_response_bytes(raw: &[u8]) -> Result<(u16, Vec<u8>), String> {
    let (head_end, body_start) = find_subslice(raw, b"\r\n\r\n")
        .map(|i| (i, i + 4))
        .or_else(|| find_subslice(raw, b"\n\n").map(|i| (i, i + 2)))
        .ok_or_else(|| "malformed HTTP response (no header/body split)".to_string())?;
    let head = String::from_utf8_lossy(&raw[..head_end]);
    let status_line = head
        .lines()
        .next()
        .ok_or_else(|| "empty HTTP response".to_string())?;
    let code = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|c| c.parse::<u16>().ok())
        .ok_or_else(|| format!("could not parse HTTP status line: {status_line:?}"))?;
    Ok((code, raw[body_start..].to_vec()))
}

/// GET `path_and_query`, returning `(status, body-bytes)` even for non-2xx (so
/// the caller can distinguish a 404 — "no tile yet" — from a hard error).
pub fn http_get_bytes(
    host: &str,
    port: u16,
    path_and_query: &str,
    timeout: Duration,
) -> Result<(u16, Vec<u8>), String> {
    let addr = format!("{host}:{port}");
    let mut stream = TcpStream::connect(&addr).map_err(|e| format!("connect {addr}: {e}"))?;
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));

    let req = build_get_request(host, path_and_query);
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("write to sidecar: {e}"))?;

    let mut raw = Vec::new();
    stream
        .read_to_end(&mut raw)
        .map_err(|e| format!("read from sidecar: {e}"))?;
    parse_http_response_bytes(&raw)
}

/// Build an HTTP/1.0 POST with a single extra header and an empty body. Used for
/// `/press/{i}`, whose auth is the `X-Herdeck-Token` header (matching web.py).
pub fn build_post_request(
    host: &str,
    path_and_query: &str,
    header_name: &str,
    header_value: &str,
) -> String {
    format!(
        "POST {path_and_query} HTTP/1.0\r\n\
         Host: {host}\r\n\
         {header_name}: {header_value}\r\n\
         Content-Length: 0\r\n\
         Connection: close\r\n\r\n"
    )
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
    let addr = format!("{host}:{port}");
    let mut stream = TcpStream::connect(&addr).map_err(|e| format!("connect {addr}: {e}"))?;
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));

    let req = build_post_request(host, path_and_query, header.0, header.1);
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("write to sidecar: {e}"))?;

    let mut raw = String::new();
    stream
        .read_to_string(&mut raw)
        .map_err(|e| format!("read from sidecar: {e}"))?;
    let (code, _body) = parse_http_response(&raw)?;
    Ok(code)
}

/// Standard base64 (with padding). Inline to avoid a new crate dependency; used
/// to frame proxied PNG bytes as a `data:` URL the WebView `<img>` can render.
pub fn base64_encode(input: &[u8]) -> String {
    const ALPHABET: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
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

/// Proxy `GET /state`, returning the JSON body. The token is injected as a query
/// param exactly as the sidecar (and web.py) expects.
pub fn fetch_state(host: &str, port: u16, token: &str, timeout: Duration) -> Result<String, String> {
    http_get(host, port, &format!("/state?token={token}"), timeout)
}

/// Proxy a PNG endpoint (`/tile/{i}` or `/panel`) and frame it as a `data:` URL.
/// `Ok(None)` on 404 (no tile/panel yet) so the caller clears the cell.
pub fn fetch_image(
    host: &str,
    port: u16,
    path: &str,
    token: &str,
    timeout: Duration,
) -> Result<Option<String>, String> {
    let (code, body) = http_get_bytes(host, port, &format!("{path}?token={token}"), timeout)?;
    match code {
        200 => Ok(Some(format!(
            "data:image/png;base64,{}",
            base64_encode(&body)
        ))),
        404 => Ok(None),
        c => Err(format!("sidecar returned HTTP {c} for {path}")),
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_get_request_has_request_line_and_close() {
        let req = build_get_request("127.0.0.1", "/health?token=abc");
        assert!(req.starts_with("GET /health?token=abc HTTP/1.0\r\n"));
        assert!(req.contains("Host: 127.0.0.1\r\n"));
        assert!(req.contains("Connection: close\r\n"));
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
    fn build_post_request_carries_header_and_zero_length() {
        let req = build_post_request("127.0.0.1", "/press/3", "X-Herdeck-Token", "tok");
        assert!(req.starts_with("POST /press/3 HTTP/1.0\r\n"));
        assert!(req.contains("X-Herdeck-Token: tok\r\n"));
        assert!(req.contains("Content-Length: 0\r\n"));
        assert!(req.ends_with("\r\n\r\n"));
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
