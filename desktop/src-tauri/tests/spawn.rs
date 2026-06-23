//! Integration tests for the sidecar supervisor against a shell stub, so they
//! exercise the real spawn + first-stdout-line read + discovery parse + restart
//! path WITHOUT needing a Python environment.

use std::process::Child;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use herdeck_desktop_lib::sidecar::{
    next_backoff, spawn_and_read_discovery, supervise, CommandSpec, SupervisorConfig,
};

const DISCOVERY_JSON: &str = r#"{"url":"http://127.0.0.1:51999","host":"127.0.0.1","port":51999,"token":"tok-xyz","source":"mock"}"#;

fn sh_stub(script: &str) -> CommandSpec {
    CommandSpec {
        program: "/bin/sh".to_string(),
        args: vec!["-c".to_string(), script.to_string()],
        cwd: None,
        envs: vec![],
    }
}

#[test]
fn spawns_a_stub_and_reads_the_discovery_line() {
    // Print discovery, then stay alive so we control the child's lifetime.
    let script = format!("printf '%s\\n' '{DISCOVERY_JSON}'; sleep 5");
    let (discovery, mut child) =
        spawn_and_read_discovery(&sh_stub(&script), Duration::from_secs(3)).expect("discovery");
    assert_eq!(discovery.url, "http://127.0.0.1:51999");
    assert_eq!(discovery.token, "tok-xyz");
    assert_eq!(discovery.port, 51999);
    assert_eq!(discovery.source, "mock");
    let _ = child.kill();
    let _ = child.wait();
}

#[test]
fn errors_when_the_sidecar_closes_stdout_without_discovery() {
    let r = spawn_and_read_discovery(&sh_stub("exit 0"), Duration::from_secs(3));
    assert!(r.is_err(), "expected an error, got {r:?}");
}

#[test]
fn errors_on_a_malformed_discovery_line() {
    let r = spawn_and_read_discovery(&sh_stub("printf 'not json\\n'; sleep 2"), Duration::from_secs(3));
    assert!(r.is_err(), "expected a parse error, got {r:?}");
}

#[test]
fn times_out_when_no_line_is_printed() {
    let r = spawn_and_read_discovery(&sh_stub("sleep 5"), Duration::from_millis(300));
    assert!(r.is_err());
    assert!(r.unwrap_err().contains("timed out"));
}

#[test]
fn supervisor_restarts_a_crashing_sidecar() {
    // Print discovery then exit -> the supervisor must respawn it repeatedly.
    let script = format!("printf '%s\\n' '{DISCOVERY_JSON}'; exit 0");
    let cfg = SupervisorConfig {
        base_backoff: Duration::from_millis(10),
        max_backoff: Duration::from_millis(50),
        discovery_timeout: Duration::from_secs(2),
        ..SupervisorConfig::new(sh_stub(&script))
    };

    let child: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));
    let stop = Arc::new(AtomicBool::new(false));
    let count = Arc::new(AtomicUsize::new(0));

    let count_cb = count.clone();
    let sup_child = child.clone();
    let sup_stop = stop.clone();
    let handle = std::thread::spawn(move || {
        supervise(cfg, sup_child, sup_stop, move |_d| {
            count_cb.fetch_add(1, Ordering::SeqCst);
        });
    });

    // Wait for at least two (re)starts, within a generous budget.
    let deadline = Instant::now() + Duration::from_secs(4);
    while count.load(Ordering::SeqCst) < 2 && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(25));
    }

    stop.store(true, Ordering::SeqCst);
    if let Some(mut c) = child.lock().unwrap().take() {
        let _ = c.kill();
        let _ = c.wait();
    }
    let _ = handle.join();

    let observed = count.load(Ordering::SeqCst);
    assert!(observed >= 2, "expected >=2 restarts, observed {observed}");
}

#[test]
fn next_backoff_is_monotonic_up_to_cap() {
    let max = Duration::from_millis(100);
    let mut b = Duration::from_millis(10);
    b = next_backoff(b, max);
    assert_eq!(b, Duration::from_millis(20));
    b = next_backoff(b, max);
    assert_eq!(b, Duration::from_millis(40));
    b = next_backoff(b, max);
    assert_eq!(b, Duration::from_millis(80));
    b = next_backoff(b, max);
    assert_eq!(b, max);
}
