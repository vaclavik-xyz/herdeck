//! Integration tests for the Rust-side loopback HTTP GET, against a one-shot
//! local server (no sidecar / no Python needed).

use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use herdeck_desktop_lib::http::{
    ack_notification, fallback_notification, fetch_image, fetch_notifications,
    fetch_notifications_status, fetch_png, fetch_setup, fetch_state, fetch_state_poll,
    http_delete, http_get, http_post_json, idle_connections, post_setup_connect, send_press,
};

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
    let r = http_get(
        "127.0.0.1",
        port,
        "/health?token=bad",
        Duration::from_secs(2),
    );
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
    let body = fetch_state(
        "127.0.0.1",
        port,
        "SECRET123",
        Duration::from_secs(2),
        false,
        None,
    )
    .unwrap();
    assert!(body.contains("\"version\":7"));
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(
        req.starts_with("GET /state?token=SECRET123 HTTP/1.1"),
        "request was: {req:?}"
    );
}

#[test]
fn fetch_notifications_carries_cursor_claim_and_generation() {
    let (port, rx) = serve_once_capture(
        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"generation\":\"g2\",\"seq\":3,\"items\":[]}".to_vec(),
    );
    let body = fetch_notifications(
        "127.0.0.1",
        port,
        "SECRET123",
        Duration::from_secs(2),
        Some("g1"),
        2,
        "shell-a",
    )
    .unwrap();
    assert!(body.contains("\"generation\":\"g2\""));
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(req.starts_with(
        "GET /notifications?token=SECRET123&after=2&wait_ms=25000&generation=g1 HTTP/1.1"
    ));
    assert!(req.contains("X-Herdeck-Shell: 1\r\n"));
    assert!(req.contains("X-Herdeck-Shell-Gen: shell-a\r\n"));
    assert!(req.contains("X-Herdeck-Shell-Features: withdraw\r\n"));
}

#[test]
fn ack_notification_posts_generation_and_seq() {
    let (port, rx) = serve_once_capture(
        b"HTTP/1.0 204 No Content\r\nContent-Length: 0\r\n\r\n".to_vec(),
    );
    let code = ack_notification(
        "127.0.0.1",
        port,
        "TOKEN",
        Duration::from_secs(2),
        "gen-1",
        7,
    )
    .unwrap();
    assert_eq!(code, 204);
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(req.starts_with("POST /notifications/ack HTTP/1.1"));
    assert!(req.contains("X-Herdeck-Token: TOKEN\r\n"));
    assert!(req.ends_with("{\"generation\":\"gen-1\",\"seq\":7}"));
}

#[test]
fn fallback_notification_identifies_shell_and_item() {
    let (port, rx) = serve_once_capture(
        b"HTTP/1.0 204 No Content\r\nContent-Length: 0\r\n\r\n".to_vec(),
    );
    let code = fallback_notification(
        "127.0.0.1",
        port,
        "TOKEN",
        Duration::from_secs(2),
        "gen-1",
        7,
        "shell-a",
        "no permission",
    )
    .unwrap();
    assert_eq!(code, 204);
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(req.starts_with("POST /notifications/fallback HTTP/1.1"));
    assert!(req.contains("X-Herdeck-Token: TOKEN\r\n"));
    assert!(req.ends_with(
        "{\"error\":\"no permission\",\"generation\":\"gen-1\",\"seq\":7,\"shell_gen\":\"shell-a\"}"
    ));
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
        req.starts_with("GET /tile/2?token=TKN HTTP/1.1"),
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
fn http_post_json_sends_body_and_returns_status_and_body() {
    let (port, rx) = serve_once_capture(
        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"errors\":[]}".to_vec(),
    );
    let (code, body) = http_post_json(
        "127.0.0.1",
        port,
        "/config",
        ("X-Herdeck-Token", "HDR"),
        "{\"base\":{}}",
        Duration::from_secs(2),
    )
    .unwrap();
    assert_eq!(code, 200);
    assert_eq!(body, "{\"errors\":[]}");
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(
        req.starts_with("POST /config HTTP/1.1"),
        "request was: {req:?}"
    );
    assert!(
        req.contains("X-Herdeck-Token: HDR\r\n"),
        "request was: {req:?}"
    );
    assert!(req.ends_with("{\"base\":{}}"), "request was: {req:?}");
}

#[test]
fn http_post_json_returns_400_status_with_body() {
    let (port, _rx) = serve_once_capture(b"HTTP/1.0 400 Bad Request\r\n\r\nbad".to_vec());
    let (code, _body) = http_post_json(
        "127.0.0.1",
        port,
        "/config",
        ("X-Herdeck-Token", "H"),
        "{",
        Duration::from_secs(2),
    )
    .unwrap();
    assert_eq!(code, 400);
}

#[test]
fn http_delete_sends_token_header_and_returns_status() {
    let (port, rx) =
        serve_once_capture(b"HTTP/1.0 204 No Content\r\nContent-Length: 0\r\n\r\n".to_vec());
    let code = http_delete(
        "127.0.0.1",
        port,
        "/secret/TOK",
        ("X-Herdeck-Token", "HDR"),
        Duration::from_secs(2),
    )
    .unwrap();
    assert_eq!(code, 204);
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(
        req.starts_with("DELETE /secret/TOK HTTP/1.1"),
        "request was: {req:?}"
    );
    assert!(
        req.contains("X-Herdeck-Token: HDR\r\n"),
        "request was: {req:?}"
    );
}

#[test]
fn send_press_posts_with_token_header_and_returns_status() {
    let (port, rx) =
        serve_once_capture(b"HTTP/1.0 204 No Content\r\nContent-Length: 0\r\n\r\n".to_vec());
    let code = send_press("127.0.0.1", port, 3, "HDR_TOK", Duration::from_secs(2)).unwrap();
    assert_eq!(code, 204);
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(
        req.starts_with("POST /press/3 HTTP/1.1"),
        "request was: {req:?}"
    );
    assert!(
        req.contains("X-Herdeck-Token: HDR_TOK\r\n"),
        "request was: {req:?}"
    );
}

#[test]
fn fetch_setup_injects_token_as_query_param() {
    let (port, rx) = serve_once_capture(
        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"mode\":\"mock\",\"reason\":\"first_run\"}".to_vec(),
    );
    let body = fetch_setup("127.0.0.1", port, "SECRET", Duration::from_secs(2)).unwrap();
    assert!(body.contains("\"reason\":\"first_run\""));
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(
        req.starts_with("GET /setup?token=SECRET HTTP/1.1"),
        "request was: {req:?}"
    );
}

#[test]
fn post_setup_connect_sends_header_token_and_body() {
    let (port, rx) = serve_once_capture(
        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"ok\":true,\"connected\":true}".to_vec(),
    );
    let (code, body) = post_setup_connect(
        "127.0.0.1",
        port,
        "HDR",
        "{\"choice\":\"demo\"}",
        Duration::from_secs(2),
    )
    .unwrap();
    assert_eq!(code, 200);
    assert!(body.contains("\"ok\":true"));
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(
        req.starts_with("POST /setup/connect HTTP/1.1"),
        "request was: {req:?}"
    );
    assert!(
        req.contains("X-Herdeck-Token: HDR\r\n"),
        "request was: {req:?}"
    );
    assert!(
        req.ends_with("{\"choice\":\"demo\"}"),
        "request was: {req:?}"
    );
}

// --- keep-alive connection pool ---

/// Read one request head (up to the blank line) off a server-side socket.
fn read_request_head(sock: &mut std::net::TcpStream) -> Option<String> {
    let mut buf = Vec::new();
    let mut byte = [0u8; 1];
    while !buf.ends_with(b"\r\n\r\n") {
        match sock.read(&mut byte) {
            Ok(0) | Err(_) => return None,
            Ok(_) => buf.push(byte[0]),
        }
    }
    Some(String::from_utf8_lossy(&buf).into_owned())
}

#[test]
fn keep_alive_responses_reuse_one_connection() {
    // The server accepts exactly ONE connection and answers every request on
    // it. A client that reconnected per request would hang on the second call.
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let (mut sock, _) = listener.accept().unwrap();
        let mut served = 0;
        while let Some(req) = read_request_head(&mut sock) {
            served += 1;
            let _ = tx.send(req);
            let body = format!("{{\"n\":{served}}}");
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
                body.len()
            );
            if sock.write_all(resp.as_bytes()).is_err() {
                break;
            }
        }
    });
    let t = Duration::from_secs(2);
    assert_eq!(http_get("127.0.0.1", port, "/health", t).unwrap(), "{\"n\":1}");
    assert_eq!(http_get("127.0.0.1", port, "/health", t).unwrap(), "{\"n\":2}");
    assert_eq!(
        fetch_state("127.0.0.1", port, "T", t, false, None).unwrap(),
        "{\"n\":3}"
    );
    assert_eq!(rx.try_iter().count(), 3);
    assert_eq!(idle_connections("127.0.0.1", port), 1);
}

#[test]
fn a_pooled_connection_the_server_closed_is_replaced_transparently() {
    // First connection: one keep-alive response, then the server hangs up
    // (runtime restart / idle reap). The next request must reconnect instead
    // of surfacing "connection closed" to the deck.
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let (closed_tx, closed_rx) = mpsc::channel();
    thread::spawn(move || {
        for (i, conn) in listener.incoming().take(2).enumerate() {
            let mut sock = conn.unwrap();
            let _ = read_request_head(&mut sock);
            let body = if i == 0 { "one" } else { "two" };
            let resp = format!("HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n{body}");
            let _ = sock.write_all(resp.as_bytes());
            drop(sock);
            if i == 0 {
                let _ = closed_tx.send(());
            }
        }
    });
    let t = Duration::from_secs(2);
    assert_eq!(http_get("127.0.0.1", port, "/a", t).unwrap(), "one");
    closed_rx.recv_timeout(t).unwrap();
    thread::sleep(Duration::from_millis(50));
    assert_eq!(http_get("127.0.0.1", port, "/b", t).unwrap(), "two");
}

#[test]
fn a_connection_close_response_is_not_pooled() {
    let port = serve_once("HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\nok");
    assert_eq!(
        http_get("127.0.0.1", port, "/x", Duration::from_secs(2)).unwrap(),
        "ok"
    );
    assert_eq!(idle_connections("127.0.0.1", port), 0);
}

#[test]
fn fetch_notifications_status_reports_a_404_as_a_status_not_an_error() {
    let port = serve_once("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n");
    let (code, _) = fetch_notifications_status(
        "127.0.0.1",
        port,
        "T",
        Duration::from_secs(2),
        None,
        0,
        "shell",
    )
    .unwrap();
    assert_eq!(code, 404);
}

#[test]
fn fetch_state_poll_sends_the_long_poll_cursor() {
    let (port, rx) = serve_once_capture(
        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"version\":8}".to_vec(),
    );
    let body = fetch_state_poll(
        "127.0.0.1",
        port,
        "TOK",
        Duration::from_secs(2),
        false,
        None,
        Some(7),
        Some(20_000),
    )
    .unwrap();
    assert!(body.contains("\"version\":8"));
    let req = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(
        req.starts_with("GET /state?token=TOK&after=7&wait_ms=20000 HTTP/1.1"),
        "request was: {req:?}"
    );
}

#[test]
fn fetch_png_returns_raw_bytes() {
    let mut resp =
        b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 4\r\n\r\n".to_vec();
    resp.extend_from_slice(&[0x89, 0x50, 0x4e, 0x47]);
    let (port, _rx) = serve_once_capture(resp);
    let png = fetch_png("127.0.0.1", port, "/panel", "T", Duration::from_secs(2)).unwrap();
    assert_eq!(png, Some(vec![0x89, 0x50, 0x4e, 0x47]));
}
