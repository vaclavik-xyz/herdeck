//! Spawn + supervise the Python sidecar and parse its discovery contract.
//!
//! Mirrors `streamdeck/src/backend-process.ts`: spawn the child, treat any
//! terminal signal as "respawn with exponential backoff", and kill it on quit.
//! The one difference is the handshake: this sidecar prints exactly ONE JSON
//! line on stdout — the discovery contract `{url, host, port, token, source}`
//! (see `src/herdeck/deckapp/__main__.py`) — which we read and hand to the
//! WebView.
//!
//! Everything here is deliberately framework-free (no Tauri types) so it is unit-
//! and integration-testable without a GUI or a real Python environment.

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// The discovery contract emitted by `python -m herdeck.deckapp` on its first
/// stdout line. This is the only place the access token crosses the boundary.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Discovery {
    pub url: String,
    pub host: String,
    pub port: u16,
    pub token: String,
    pub source: String,
}

impl Discovery {
    /// Parse a single discovery JSON line. Rejects blank lines and malformed /
    /// incomplete JSON (the field-level errors come from serde).
    pub fn parse(line: &str) -> Result<Discovery, String> {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            return Err("empty discovery line".to_string());
        }
        serde_json::from_str::<Discovery>(trimmed)
            .map_err(|e| format!("invalid discovery JSON: {e}"))
    }
}

/// A fully-resolved spawn recipe for the sidecar. Kept as plain data so callers
/// (and tests) can inspect it before turning it into a `std::process::Command`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandSpec {
    pub program: String,
    pub args: Vec<String>,
    pub cwd: Option<PathBuf>,
    pub envs: Vec<(String, String)>,
}

impl CommandSpec {
    pub fn to_command(&self) -> Command {
        let mut cmd = Command::new(&self.program);
        cmd.args(&self.args);
        if let Some(cwd) = &self.cwd {
            cmd.current_dir(cwd);
        }
        for (k, v) in &self.envs {
            cmd.env(k, v);
        }
        cmd
    }
}

/// Resolve the dev-mode sidecar command: the repo `.venv` interpreter running
/// `-m herdeck.deckapp`, with `PYTHONPATH=<repo>/src` so it also works against a
/// non-installed checkout. (Frozen/bundled sidecar is a later phase.)
pub fn resolve_dev_sidecar(repo_root: &Path) -> CommandSpec {
    let python = repo_root.join(".venv").join("bin").join("python");
    let src = repo_root.join("src");
    CommandSpec {
        program: python.to_string_lossy().into_owned(),
        args: vec!["-m".to_string(), "herdeck.deckapp".to_string()],
        cwd: Some(repo_root.to_path_buf()),
        envs: vec![("PYTHONPATH".to_string(), src.to_string_lossy().into_owned())],
    }
}

/// Resolve the frozen/bundled sidecar command from the Tauri resource dir. The
/// PyInstaller onedir bundle lands at `<resource_dir>/herdeck-deckapp/` with the
/// executable inside it. Returns `Some` only when that binary actually exists, so
/// a dev build (no staged bundle) cleanly falls through to the `.venv`.
pub fn resolve_frozen_sidecar(resource_dir: &Path) -> Option<CommandSpec> {
    let bin = resource_dir.join("herdeck-deckapp").join("herdeck-deckapp");
    if bin.is_file() {
        Some(CommandSpec {
            program: bin.to_string_lossy().into_owned(),
            args: vec![],
            cwd: None,
            envs: vec![],
        })
    } else {
        None
    }
}

/// Pick the sidecar to spawn: the bundled frozen binary when present (production),
/// otherwise the dev `.venv` interpreter. `resource_dir` is `None` when Tauri
/// could not resolve one (then we always use the dev path).
pub fn choose_spawn(resource_dir: Option<&Path>, repo_root: &Path) -> CommandSpec {
    if let Some(dir) = resource_dir {
        if let Some(spec) = resolve_frozen_sidecar(dir) {
            return spec;
        }
    }
    resolve_dev_sidecar(repo_root)
}

/// Kill a child and reap it, so a failed spawn/handshake never leaves a zombie
/// that the supervisor's retries would accumulate.
fn kill_and_reap(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

/// Double the backoff up to a ceiling (matches the TS supervisor's policy).
pub fn next_backoff(current: Duration, max: Duration) -> Duration {
    let doubled = current.saturating_mul(2);
    if doubled > max {
        max
    } else {
        doubled
    }
}

/// Spawn the sidecar with piped stdout (null stdin; stderr inherited so Python
/// tracebacks surface in the dev console).
fn spawn_piped(spec: &CommandSpec) -> Result<Child, String> {
    let mut cmd = spec.to_command();
    cmd.stdin(Stdio::null());
    cmd.stdout(Stdio::piped());
    cmd.spawn()
        .map_err(|e| format!("failed to spawn sidecar `{}`: {e}", spec.program))
}

/// Read + parse the discovery line from an already-spawned child's stdout. A
/// background thread reads the first line (then drains the rest so the child
/// never blocks on a full pipe); the caller bounds the wait with `timeout`.
/// Takes the detached stdout by value so the caller can keep the `Child` handle
/// registered for the quit path while this blocks.
fn read_discovery_from_stdout(
    stdout: ChildStdout,
    timeout: Duration,
) -> Result<Discovery, String> {
    let (tx, rx) = mpsc::channel::<Result<String, String>>();
    thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut first = String::new();
        match reader.read_line(&mut first) {
            Ok(0) => {
                let _ = tx.send(Err("sidecar closed stdout before discovery".to_string()));
                return;
            }
            Ok(_) => {
                let _ = tx.send(Ok(first));
            }
            Err(e) => {
                let _ = tx.send(Err(format!("error reading sidecar stdout: {e}")));
                return;
            }
        }
        // Drain the rest so the child never blocks writing to a full pipe.
        let mut sink = String::new();
        while let Ok(n) = reader.read_line(&mut sink) {
            if n == 0 {
                break;
            }
            sink.clear();
        }
    });

    match rx.recv_timeout(timeout) {
        Ok(Ok(line)) => Discovery::parse(&line),
        Ok(Err(e)) => Err(e),
        Err(_) => Err("timed out waiting for sidecar discovery line".to_string()),
    }
}

/// Spawn the sidecar and read its first stdout line as the discovery contract.
///
/// On success returns the parsed `Discovery` and the live `Child` (so the caller
/// can supervise / kill it). On any failure — spawn error, closed stdout, parse
/// error, or timeout — the child is killed and reaped and an `Err` is returned.
///
/// The supervisor does NOT use this convenience (it can't register the kill
/// handle before reading); it composes `spawn_piped` + `read_discovery_from_stdout`
/// directly so the quit path can always reach an in-flight child.
pub fn spawn_and_read_discovery(
    spec: &CommandSpec,
    timeout: Duration,
) -> Result<(Discovery, Child), String> {
    let mut child = spawn_piped(spec)?;
    let stdout = match child.stdout.take() {
        Some(out) => out,
        None => {
            kill_and_reap(&mut child);
            return Err("sidecar stdout was not piped".to_string());
        }
    };
    match read_discovery_from_stdout(stdout, timeout) {
        Ok(discovery) => Ok((discovery, child)),
        Err(e) => {
            kill_and_reap(&mut child);
            Err(e)
        }
    }
}

/// Tunables for the supervise loop.
#[derive(Debug, Clone)]
pub struct SupervisorConfig {
    pub spec: CommandSpec,
    pub base_backoff: Duration,
    pub max_backoff: Duration,
    pub discovery_timeout: Duration,
}

impl SupervisorConfig {
    pub fn new(spec: CommandSpec) -> Self {
        SupervisorConfig {
            spec,
            base_backoff: Duration::from_millis(500),
            max_backoff: Duration::from_secs(30),
            discovery_timeout: Duration::from_secs(15),
        }
    }
}

/// Run the spawn → read-discovery → wait → respawn loop until `stop` is set.
///
/// `shared_child` holds the current child so the app's quit path can kill it.
/// `on_discovery` is invoked every time a (re)started sidecar reports its
/// discovery — the app uses it to update WebView state and emit an event.
/// Blocks the calling thread; run it on a dedicated thread.
pub fn supervise<F>(
    cfg: SupervisorConfig,
    shared_child: Arc<Mutex<Option<Child>>>,
    stop: Arc<AtomicBool>,
    on_discovery: F,
) where
    F: Fn(Discovery) + Send + 'static,
{
    let mut backoff = cfg.base_backoff;
    while !stop.load(Ordering::SeqCst) {
        match spawn_piped(&cfg.spec) {
            Ok(mut child) => {
                // Detach stdout, then register the child IMMEDIATELY (before the
                // potentially-long discovery read) so the quit path can always
                // kill an in-flight sidecar that hasn't reported in yet.
                let stdout = child.stdout.take();
                {
                    let mut slot = shared_child.lock().unwrap();
                    *slot = Some(child);
                }
                let result = match stdout {
                    Some(out) => read_discovery_from_stdout(out, cfg.discovery_timeout),
                    None => Err("sidecar stdout was not piped".to_string()),
                };
                match result {
                    Ok(discovery) => {
                        backoff = cfg.base_backoff; // healthy start resets backoff
                        on_discovery(discovery);
                        wait_for_child(&shared_child, &stop);
                    }
                    Err(_e) => {
                        // Discovery failed: reclaim + reap the child we registered
                        // (unless the quit path already took it).
                        if let Some(mut c) = shared_child.lock().unwrap().take() {
                            kill_and_reap(&mut c);
                        }
                    }
                }
            }
            Err(_e) => {
                // Spawn failed (e.g. no .venv yet) — fall through to back off.
            }
        }
        if stop.load(Ordering::SeqCst) {
            break;
        }
        thread::sleep(backoff);
        backoff = next_backoff(backoff, cfg.max_backoff);
    }
    // Make sure no child outlives us.
    if let Some(mut child) = shared_child.lock().unwrap().take() {
        kill_and_reap(&mut child);
    }
}

/// Poll the current child until it exits (or `stop` is set / the child is taken
/// out from under us by the quit path).
fn wait_for_child(shared: &Arc<Mutex<Option<Child>>>, stop: &Arc<AtomicBool>) {
    loop {
        if stop.load(Ordering::SeqCst) {
            return;
        }
        {
            let mut slot = shared.lock().unwrap();
            match slot.as_mut() {
                Some(child) => match child.try_wait() {
                    Ok(Some(_status)) => {
                        *slot = None;
                        return;
                    }
                    Ok(None) => {}
                    Err(_) => {
                        *slot = None;
                        return;
                    }
                },
                None => return,
            }
        }
        thread::sleep(Duration::from_millis(120));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        // Dependency-free temp dir keyed by the (unique) test name.
        let p = std::env::temp_dir().join(format!("herdeck-3a-{name}"));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    fn stage_frozen(resource_dir: &Path) -> PathBuf {
        let dir = resource_dir.join("herdeck-deckapp");
        std::fs::create_dir_all(&dir).unwrap();
        let bin = dir.join("herdeck-deckapp");
        std::fs::write(&bin, b"#!/bin/sh\n").unwrap();
        bin
    }

    #[test]
    fn resolve_frozen_sidecar_some_when_binary_exists() {
        let res = scratch("frozen-exists");
        let bin = stage_frozen(&res);
        let spec = resolve_frozen_sidecar(&res).expect("should resolve");
        assert_eq!(spec.program, bin.to_string_lossy());
        assert!(spec.args.is_empty());
        assert!(spec.cwd.is_none());
        assert!(spec.envs.is_empty());
    }

    #[test]
    fn resolve_frozen_sidecar_none_when_missing() {
        let res = scratch("frozen-missing");
        assert!(resolve_frozen_sidecar(&res).is_none());
    }

    #[test]
    fn choose_spawn_prefers_frozen_then_falls_back_to_dev_venv() {
        let res = scratch("choose-frozen");
        stage_frozen(&res);
        let frozen = choose_spawn(Some(&res), Path::new("/repo"));
        assert!(frozen.program.ends_with("/herdeck-deckapp/herdeck-deckapp"));

        // Empty resource dir -> no bundle -> dev venv.
        let empty = scratch("choose-empty");
        let dev = choose_spawn(Some(&empty), Path::new("/repo"));
        assert!(dev.program.ends_with("/.venv/bin/python"));

        // No resource dir at all -> dev venv.
        let none = choose_spawn(None, Path::new("/repo"));
        assert!(none.program.ends_with("/.venv/bin/python"));
    }

    #[test]
    fn parses_a_valid_discovery_line() {
        let line = r#"{"url":"http://127.0.0.1:51515","host":"127.0.0.1","port":51515,"token":"abc123","source":"mock"}"#;
        let d = Discovery::parse(line).expect("should parse");
        assert_eq!(d.url, "http://127.0.0.1:51515");
        assert_eq!(d.host, "127.0.0.1");
        assert_eq!(d.port, 51515);
        assert_eq!(d.token, "abc123");
        assert_eq!(d.source, "mock");
    }

    #[test]
    fn parses_a_line_with_trailing_newline_and_whitespace() {
        let line = "  {\"url\":\"http://127.0.0.1:1\",\"host\":\"127.0.0.1\",\"port\":1,\"token\":\"t\",\"source\":\"mock\"}\n";
        assert!(Discovery::parse(line).is_ok());
    }

    #[test]
    fn rejects_blank_and_malformed_lines() {
        assert!(Discovery::parse("").is_err());
        assert!(Discovery::parse("   \n").is_err());
        assert!(Discovery::parse("not json").is_err());
        // missing the required `token` field
        assert!(Discovery::parse(r#"{"url":"http://x","host":"h","port":1,"source":"mock"}"#).is_err());
        // port as a string is not a u16
        assert!(Discovery::parse(
            r#"{"url":"http://x","host":"h","port":"nope","token":"t","source":"mock"}"#
        )
        .is_err());
    }

    #[test]
    fn next_backoff_doubles_then_caps() {
        let max = Duration::from_secs(30);
        assert_eq!(next_backoff(Duration::from_millis(500), max), Duration::from_secs(1));
        assert_eq!(next_backoff(Duration::from_secs(1), max), Duration::from_secs(2));
        assert_eq!(next_backoff(Duration::from_secs(20), max), max); // 40 -> capped
        assert_eq!(next_backoff(max, max), max);
    }

    #[test]
    fn resolve_dev_sidecar_builds_the_expected_command() {
        let root = Path::new("/repo");
        let spec = resolve_dev_sidecar(root);
        assert!(spec.program.ends_with("/.venv/bin/python"));
        assert_eq!(spec.args, vec!["-m".to_string(), "herdeck.deckapp".to_string()]);
        assert_eq!(spec.cwd.as_deref(), Some(Path::new("/repo")));
        assert_eq!(
            spec.envs,
            vec![("PYTHONPATH".to_string(), "/repo/src".to_string())]
        );
    }

    #[test]
    fn command_spec_to_command_sets_program_and_args() {
        let spec = CommandSpec {
            program: "/bin/echo".to_string(),
            args: vec!["hi".to_string()],
            cwd: None,
            envs: vec![],
        };
        let cmd = spec.to_command();
        assert_eq!(cmd.get_program().to_string_lossy(), "/bin/echo");
        let args: Vec<_> = cmd.get_args().map(|a| a.to_string_lossy().into_owned()).collect();
        assert_eq!(args, vec!["hi".to_string()]);
    }
}
