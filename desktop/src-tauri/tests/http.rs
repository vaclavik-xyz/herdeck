//! Integration tests for the Rust-side loopback HTTP GET, against a one-shot
//! local server (no sidecar / no Python needed).

use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

use herdeck_desktop_lib::http::http_get;

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
