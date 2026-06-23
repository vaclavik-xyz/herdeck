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
}
