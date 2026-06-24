//! Integration tests for the Rust-side loopback HTTP GET, against a one-shot
//! local server (no sidecar / no Python needed).

use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use herdeck_desktop_lib::http::{fetch_image, fetch_state, http_get, send_press};

/// Bind a loopback listener and, on one connection, reply with `response` then
/// close. Returns the bound port (already listening before we return).
fn serve_once(response: &'static str) -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    thread::spawn(move || {
        if let Ok((mut sock, _)) = listener.accept() {
            let mut buf = [0u8; 1024];
            let _ = sock.read(&mut buf); // consume the request line/headers
            let _ = sock.write_all(response.as_bytes());
            // drop closes the socket -> the client sees EOF
        }
    });
    port
}

/// Like `serve_once` but captures the raw request (so a test can assert the
/// token was injected) and serves an arbitrary byte response (for binary PNGs).
fn serve_once_capture(response: Vec<u8>) -> (u16, mpsc::Receiver<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        if let Ok((mut sock, _)) = listener.accept() {
            let mut buf = [0u8; 2048];
            let n = sock.read(&mut buf).unwrap_or(0);
            let _ = tx.send(String::from_utf8_lossy(&buf[..n]).into_owned());
            let _ = sock.write_all(&response);
        }
    });
    (port, rx)
}

#[test]
fn http_get_returns_body_on_200() {
    let port = serve_once(
        "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"ok\":true,\"source\":\"mock\",\"connected\":false,\"server_id\":null}",
    );
    let body = http_get("127.0.0.1", port, "/health?token=t", Duration::from_secs(2)).unwrap();
    assert!(body.contains("\"source\":\"mock\""));
    assert!(body.contains("\"connected\":false"));
}

#[test]
fn http_get_errors_on_403() {
    let port = serve_once("HTTP/1.0 403 Forbidden\r\n\r\nnope");
    let r = http_get("127.0.0.1", port, "/health?token=bad", Duration::from_secs(2));
    assert!(r.is_err());
    assert!(r.unwrap_err().contains("403"));
}

#[test]
fn http_get_errors_when_nothing_is_listening() {
    // Port 1 is privileged and not listening -> connect is refused promptly.
    let r = http_get("127.0.0.1", 1, "/health", Duration::from_millis(500));
    assert!(r.is_err());
}

// --- proxy layer: forwards the request and injects the token ---

#[test]
fn fetch_state_injects_token_as_query_param() {
    let (port, rx) = serve_once_capture(
        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"version\":7}".to_vec(),
    );
    let body = fetch_state("127.0.0.1", port, "SECRET123", Duration::from_secs(2)).unwrap();
    assert!(body.contains("\"version\":7"));
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(
        req.starts_with("GET /state?token=SECRET123 HTTP/1.0"),
        "request was: {req:?}"
    );
}

#[test]
fn fetch_image_frames_png_bytes_as_data_url_with_token() {
    // Binary PNG magic in the body proves bytes survive (not UTF-8 mangled).
    let mut resp = b"HTTP/1.0 200 OK\r\nContent-Type: image/png\r\n\r\n".to_vec();
    resp.extend_from_slice(&[0x89, 0x50, 0x4e, 0x47]); // -> base64 "iVBORw=="
    let (port, rx) = serve_once_capture(resp);
    let url = fetch_image("127.0.0.1", port, "/tile/2", "TKN", Duration::from_secs(2)).unwrap();
    assert_eq!(url, Some("data:image/png;base64,iVBORw==".to_string()));
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(
        req.starts_with("GET /tile/2?token=TKN HTTP/1.0"),
        "request was: {req:?}"
    );
}

#[test]
fn fetch_image_returns_none_on_404() {
    let (port, _rx) = serve_once_capture(b"HTTP/1.0 404 Not Found\r\n\r\n".to_vec());
    let url = fetch_image("127.0.0.1", port, "/panel", "T", Duration::from_secs(2)).unwrap();
    assert_eq!(url, None);
}

#[test]
fn send_press_posts_with_token_header_and_returns_status() {
    let (port, rx) =
        serve_once_capture(b"HTTP/1.0 204 No Content\r\nContent-Length: 0\r\n\r\n".to_vec());
    let code = send_press("127.0.0.1", port, 3, "HDR_TOK", Duration::from_secs(2)).unwrap();
    assert_eq!(code, 204);
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(
        req.starts_with("POST /press/3 HTTP/1.0"),
        "request was: {req:?}"
    );
    assert!(
        req.contains("X-Herdeck-Token: HDR_TOK\r\n"),
        "request was: {req:?}"
    );
}
