# herdeck Runtime — Slice C (Tauri attach-or-spawn + deploy) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the desktop window ATTACH to an already-running headless `herdeck.runtime` (read its `runtime.json` + confirm `/health`) instead of always spawning its own Python sidecar — completing the convergence so the D200 and the window share ONE Orchestrator + ONE bridge + ONE clock — and switch the macbench launchd service to the converged runtime.

**Architecture:** The Tauri shell already decides between `SidecarPlan::External(Discovery)` (record the discovery, emit the `"discovery"` event, spawn NOTHING, kill nothing on quit) and `SidecarPlan::Spawn(CommandSpec)` (supervise a child). This slice adds a runtime.json read + a `/health` probe to `resolve_plan`: when a live runtime is found, produce `SidecarPlan::External` (= attach); otherwise fall through to `Spawn` exactly as today. The attach path reuses `External`'s existing "we don't own this process" semantics, so quitting the window never kills the launchd runtime. The frontend is unchanged — it already consumes discovery url+token through Rust proxy commands, transparent to attach vs spawn.

**Tech Stack:** Rust (Tauri v2, `std` only for the new logic — `std::fs`, `std::env`, `std::path`), `cargo test` (inline `#[cfg(test)]`), `cargo clippy`. No new crates. The Python runtime (`herdeck.runtime`) + its `runtime.json` (`{url,host,port,token,source}`, 0600) already exist from Slice B.

**Spec:** `docs/superpowers/specs/2026-06-30-herdeck-runtime-convergence-design.md`. This is **Slice C**, the final slice. Slices A (deckapp ticker) and B (converged Python runtime + `runtime.json` + `herdeck.runtime` entry) are already merged to main.

## Global Constraints

- Comms in Czech; code, comments, identifiers, and commit messages in English.
- Conventional Commits; NO `Co-Authored-By` trailer; never squash-merge. After each commit check `roborev show <sha>` and fix findings.
- Rust gate before "done": `cd desktop/src-tauri && ~/.cargo/bin/cargo test` green AND `~/.cargo/bin/cargo build` clean. (`cargo` is at `~/.cargo/bin`, NOT on PATH.) The repo does NOT enforce crate-wide fmt/clippy (no `rustfmt.toml`, no CI lint) — do NOT run `cargo fmt`/`cargo clippy --fix` across the crate or touch files this task does not modify; keep only the touched file tidy. `cargo clippy` on the touched file is advisory, not a blocking gate.
- **Attach reuses `SidecarPlan::External`** — it MUST NOT spawn or supervise anything and MUST NOT register a child (so the window-quit / `ExitRequested` handler never kills the launchd runtime we merely attached to).
- **Discovery contract:** the runtime.json the window reads is exactly `{url, host, port, token, source}` — the SAME shape `sidecar::Discovery` already deserializes (written by Slice B's `herdeck.deckapp.discovery`). Reuse `Discovery::parse`; do not define a second struct.
- **Token stays Rust-side:** the `/health` probe and all sidecar calls inject the token in Rust (`http::http_get(... "/health?token=" ...)`); the token never enters JS. Do not log it.
- **Decision is startup-only (v1):** `resolve_plan` runs once at window start. If the attached runtime dies mid-session the window shows offline UI (existing `/state` connection state) and recovers on the next window restart; auto-respawn-on-attach-death is a documented non-goal.
- **Priority order in `resolve_plan`:** (1) `HERDECK_DECKAPP_URL`+`_TOKEN` env override (existing dev hook) → External; (2) NEW: `runtime.json` present AND `/health` ok → External (attach); (3) else → Spawn (existing).
- **No frontend change:** `desktop/src/**` is untouched — attach vs spawn is invisible to the WebView.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `desktop/src-tauri/src/sidecar.rs` (modify) | `runtime_file_path()` + `read_runtime_discovery()` + pure `decide_runtime_attach()` decision helper + `#[cfg(test)]` tests | 1 |
| `desktop/src-tauri/src/lib.rs` (modify) | `resolve_plan` gains the runtime.json attach branch; `probe_runtime_health()` health probe | 2 |
| (macbench, not in repo) `~/Library/LaunchAgents/com.herdeck.app.plist` | launchd `ProgramArguments`: `-m herdeck.app` → `-m herdeck.runtime` | Manual |

---

### Task 1: runtime.json read + attach decision (framework-free, in `sidecar.rs`)

**Files:**
- Modify: `desktop/src-tauri/src/sidecar.rs` (add three functions after the `Discovery` impl at ~line 46; add tests to the existing `#[cfg(test)] mod tests` at ~line 325)

**Interfaces:**
- Produces:
  - `pub fn runtime_file_path() -> PathBuf` — `$HERDECK_RUNTIME_DIR/runtime.json` or `$HOME/.cache/herdeck/runtime.json`.
  - `pub fn read_runtime_discovery(path: &Path) -> Option<Discovery>` — parsed discovery, or `None` on missing/malformed.
  - `pub fn decide_runtime_attach<F: Fn(&Discovery) -> bool>(runtime_disco: Option<Discovery>, healthy: F) -> Option<Discovery>` — `Some(d)` iff a discovery was found AND `healthy(&d)`.
- Consumes: the existing `Discovery` + `Discovery::parse` (same file). Uses `std::fs`, `std::env`, `std::path::{Path, PathBuf}` (Path/PathBuf already imported at the top of the file).

- [ ] **Step 1: Write the failing tests**

Add to the existing `#[cfg(test)] mod tests` block in `desktop/src-tauri/src/sidecar.rs` (it already has a `scratch(name)` helper that makes a unique temp dir):

```rust
    fn sample_discovery() -> Discovery {
        Discovery {
            url: "http://127.0.0.1:8800".to_string(),
            host: "127.0.0.1".to_string(),
            port: 8800,
            token: "t0ken".to_string(),
            source: "live".to_string(),
        }
    }

    #[test]
    fn runtime_file_path_from_honors_dir_then_home() {
        let with_dir = runtime_file_path_from(Some("/run/hd".to_string()), "/home/x");
        assert_eq!(with_dir, Path::new("/run/hd/runtime.json"));
        let with_home = runtime_file_path_from(None, "/home/x");
        assert_eq!(with_home, Path::new("/home/x/.cache/herdeck/runtime.json"));
    }

    #[test]
    fn read_runtime_discovery_round_trips() {
        let dir = scratch("runtime-read");
        let path = dir.join("runtime.json");
        std::fs::write(
            &path,
            r#"{"url":"http://127.0.0.1:8800","host":"127.0.0.1","port":8800,"token":"t0ken","source":"live"}"#,
        )
        .unwrap();
        let d = read_runtime_discovery(&path).expect("should parse");
        assert_eq!(d.port, 8800);
        assert_eq!(d.token, "t0ken");
        assert_eq!(d.source, "live");
    }

    #[test]
    fn read_runtime_discovery_none_when_missing() {
        let dir = scratch("runtime-missing");
        assert!(read_runtime_discovery(&dir.join("runtime.json")).is_none());
    }

    #[test]
    fn read_runtime_discovery_none_when_malformed() {
        let dir = scratch("runtime-malformed");
        let path = dir.join("runtime.json");
        std::fs::write(&path, "{not json").unwrap();
        assert!(read_runtime_discovery(&path).is_none());
    }

    #[test]
    fn decide_runtime_attach_attaches_only_when_present_and_healthy() {
        assert!(decide_runtime_attach(Some(sample_discovery()), |_| true).is_some());
        assert!(decide_runtime_attach(Some(sample_discovery()), |_| false).is_none());
        assert!(decide_runtime_attach(None, |_| true).is_none());
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd desktop/src-tauri && cargo test runtime`
Expected: FAIL to COMPILE — `runtime_file_path_from`, `read_runtime_discovery`, `decide_runtime_attach` are not defined.

- [ ] **Step 3: Add the three functions**

In `desktop/src-tauri/src/sidecar.rs`, right after the `impl Discovery { ... }` block (which ends at ~line 46), add:

```rust
/// Resolve the runtime.json path, mirroring the Python side
/// (`herdeck.deckapp.discovery.runtime_file_path`): `HERDECK_RUNTIME_DIR` or
/// `~/.cache/herdeck`. The `_from` form is split out so it is unit-testable
/// without mutating process env (which would race parallel tests).
fn runtime_file_path_from(runtime_dir: Option<String>, home: &str) -> PathBuf {
    let base = runtime_dir
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(home).join(".cache").join("herdeck"));
    base.join("runtime.json")
}

/// The runtime.json discovery path for the current environment.
pub fn runtime_file_path() -> PathBuf {
    runtime_file_path_from(
        std::env::var("HERDECK_RUNTIME_DIR").ok(),
        &std::env::var("HOME").unwrap_or_default(),
    )
}

/// Read + parse the headless runtime's discovery file. `None` when the file is
/// absent or its contents are not a valid discovery object — so a missing or
/// stale/corrupt file cleanly falls through to spawning our own sidecar.
pub fn read_runtime_discovery(path: &Path) -> Option<Discovery> {
    let contents = std::fs::read_to_string(path).ok()?;
    Discovery::parse(&contents).ok()
}

/// Decide whether to ATTACH to an already-running runtime: attach only when a
/// discovery file was found AND its `/health` responds (via the injected probe).
/// A missing file or a failed probe (stale file) returns `None` → spawn instead.
pub fn decide_runtime_attach<F>(runtime_disco: Option<Discovery>, healthy: F) -> Option<Discovery>
where
    F: Fn(&Discovery) -> bool,
{
    match runtime_disco {
        Some(d) if healthy(&d) => Some(d),
        _ => None,
    }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd desktop/src-tauri && cargo test runtime`
Expected: PASS (the 5 new tests: `runtime_file_path_from_honors_dir_then_home`, `read_runtime_discovery_round_trips`, `read_runtime_discovery_none_when_missing`, `read_runtime_discovery_none_when_malformed`, `decide_runtime_attach_attaches_only_when_present_and_healthy`).

- [ ] **Step 5: Full sidecar test run + build**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo test`
Expected: PASS (all sidecar + http tests, no regression).
Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo build`
Expected: compiles clean. (Do NOT crate-wide `cargo fmt`/`clippy --fix` — the repo doesn't enforce them; keep only `sidecar.rs` tidy.)

- [ ] **Step 6: Commit**

```bash
git add desktop/src-tauri/src/sidecar.rs
git commit -m "feat(desktop): read runtime.json + decide attach-or-spawn"
```

---

### Task 2: wire the attach branch into `resolve_plan` (`lib.rs`)

**Files:**
- Modify: `desktop/src-tauri/src/lib.rs` (add `probe_runtime_health` near `current_discovery`/`check_health` ~line 108; insert the attach branch into `resolve_plan` at ~line 360, between the env-var `External` return and the `Spawn` fallback)

**Interfaces:**
- Consumes: `sidecar::runtime_file_path`, `sidecar::read_runtime_discovery`, `sidecar::decide_runtime_attach` (Task 1); `http::http_get`, `SIDECAR_TIMEOUT`, `Discovery`, `SidecarPlan::External`.
- Produces: `resolve_plan` returns `SidecarPlan::External(runtime_discovery)` when a live runtime is found — the existing `start_sidecar` `External` arm (record discovery, emit `"discovery"`, no spawn, no child registered) handles it unchanged.

- [ ] **Step 1: Add the health probe helper**

`resolve_plan` doing a network probe cannot be unit-tested under Tauri (lib.rs has no test harness); the DECISION logic is already covered by Task 1's `decide_runtime_attach` tests, and this wiring is verified by compile + clippy + the manual convergence gate. In `desktop/src-tauri/src/lib.rs`, add this helper right after `current_discovery` (ends ~line 108):

```rust
/// Probe an already-running headless runtime's token-authed `GET /health`
/// (Rust-side, so the token never enters JS). `true` iff it responds — the
/// signal that a `runtime.json` we found is live (not stale) and we should
/// ATTACH to it rather than spawn our own sidecar.
fn probe_runtime_health(d: &Discovery) -> bool {
    http::http_get(
        &d.host,
        d.port,
        &format!("/health?token={}", d.token),
        SIDECAR_TIMEOUT,
    )
    .is_ok()
}
```

- [ ] **Step 2: Insert the attach branch into `resolve_plan`**

In `desktop/src-tauri/src/lib.rs`, in `resolve_plan` (starts ~line 343), insert the attach branch AFTER the `HERDECK_DECKAPP_URL`/`_TOKEN` env block's closing `}` and BEFORE the final `SidecarPlan::Spawn(...)`:

```rust
    // Attach to an already-running headless runtime (herdeck.runtime) when its
    // discovery file is present AND /health responds: the window then shares the
    // runtime's Orchestrator + bridge + clock (D200 and window in lockstep) instead
    // of spawning its own sidecar. External == "we don't own it": quitting the
    // window never kills the launchd runtime. A missing/stale file falls through.
    if let Some(d) = sidecar::decide_runtime_attach(
        sidecar::read_runtime_discovery(&sidecar::runtime_file_path()),
        probe_runtime_health,
    ) {
        return SidecarPlan::External(d);
    }

    SidecarPlan::Spawn(sidecar::choose_spawn(
        resource_dir.as_deref(),
        &repo_root_from_manifest(),
    ))
```

(The final `SidecarPlan::Spawn(...)` shown is the EXISTING trailing expression — keep it; only the `if let ...` block above it is new.)

- [ ] **Step 3: Build + clippy + fmt**

Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo build`
Expected: compiles clean (the new `sidecar::` paths resolve; `probe_runtime_health` is used by `resolve_plan`, so no dead-code warning).
Run: `cd desktop/src-tauri && ~/.cargo/bin/cargo test`
Expected: tests PASS (Task 1's, unaffected). (Do NOT crate-wide `cargo fmt`/`clippy --fix`; keep only `lib.rs` tidy — the repo enforces neither.)

- [ ] **Step 4: Frontend regression (no change expected, just confirm green)**

Run: `cd desktop && npm test`
Expected: PASS — no frontend file changed; attach vs spawn is transparent to the WebView (it still pulls discovery via `get_discovery` / the `"discovery"` event).

- [ ] **Step 5: Commit**

```bash
git add desktop/src-tauri/src/lib.rs
git commit -m "feat(desktop): attach to a running herdeck.runtime via runtime.json + /health"
```

---

## Manual deploy + convergence gate (macbench — after both tasks, NOT an SDD task)

This is a runbook the human runs on the macbench (arm64, `user@host`); it touches the live D200 service and needs the Mac GUI, so it is not automatable.

**A. Switch the launchd service to the converged runtime.**
1. Deploy the current `src/herdeck` to macbench (the established path): `git archive HEAD src/herdeck | ssh MB 'tar -xf - -C ~/Projects/herdeck'`, clear `__pycache__`, gate on `~/Projects/herdeck/.venv/bin/python -c "import herdeck.runtime, herdeck.deckapp.sinks"`.
2. Edit `~/Library/LaunchAgents/com.herdeck.app.plist` `ProgramArguments`: change the module from `herdeck.app` to `herdeck.runtime` (keep `HERDECK_DECK=d200`, `HERDECK_CONFIG`, and the port env). Note a fixed `HERDECK_DECKAPP_PORT` (e.g. `8842`) so the window's `runtime.json` attach has a stable target — or rely on the ephemeral port written into `runtime.json`.
3. `launchctl unload` + `launchctl load` the plist (a plist change needs a full reload, not just `kickstart -k`). Confirm: `launchctl print gui/$(id -u)/com.herdeck.app` shows it running; `~/.cache/herdeck/runtime.json` exists with `{url,host,port,token,source}` and `0600`; the D200 paints and a WORKING agent animates; `curl "http://127.0.0.1:<port>/health?token=<token>"` returns ok.

**B. Rebuild + install the desktop `.app` with the attach change.**
1. Rebuild the frozen sidecar + Tauri bundle for arm64 (the existing desktop build path) so the `.app` carries the new `resolve_plan`.
2. Install/launch the `.app` on the macbench GUI.

**C. Convergence gate (visual, human):**
- [ ] The window **attaches** (no second sidecar spawned): its `source` matches the runtime's, and `~/.cache/herdeck/runtime.json` is unchanged (the window wrote nothing).
- [ ] A WORKING agent **animates in the window** AND on the D200 from the same spinner phase.
- [ ] **Elapsed matches** between the D200 and the window (one clock — no per-instance drift).
- [ ] A press on the D200 and a press in the window both act on the same deck (one Orchestrator).
- [ ] **Kill the runtime** (`launchctl stop com.herdeck.app`) and restart the window → it finds no live `/health`, **falls back to spawning its own sidecar** (web-only, no D200), so the `.app` stays usable standalone.
- [ ] Quit the window while the runtime runs → the **launchd runtime keeps running** (the window never owned it).

## Scope note — this completes the convergence

With Slice C merged + deployed, the macbench D200 and the desktop window run off ONE `herdeck.runtime` process (one Orchestrator + one bridge + one clock), the window attaches over `runtime.json`, and the `.app` still self-spawns when no runtime is up. Deliberately out of scope (documented non-goals): auto-respawn if the attached runtime dies mid-session (window shows offline until restart); a WS push protocol (still HTTP `/state` polling); remote/network clients; `.app` signing/universal2. The herdr-native migration remains a future single-seam swap of `LiveSource`.

## Self-Review (completed by plan author)

**Spec coverage:** attach-or-spawn reading `runtime.json` + `/health` (spec "Discovery" + "Tauri okno na startu soubor přečte + pingne /health → attach") → Tasks 1+2. Reuse of the External "don't-own-it" semantics so the window never kills the runtime (spec "Dvojí runtime" / self-sufficiency) → Task 2. Fallback spawn on stale/absent file (spec error handling "Stale runtime.json → /health selže → fallback spawn") → `decide_runtime_attach` returning None → Spawn. launchd plist switch + manual convergence gate (spec "Nasazení" + "Manuální gate") → the runbook. Frontend unchanged (spec "Frontend — beze změny tvaru") → no frontend task. Startup-only decision + mid-session offline (spec keeps hotplug/reconnect out of v1) → documented non-goal.

**Placeholder scan:** none — Task 1/2 carry complete Rust; the manual runbook is explicitly human-run (deploy + visual gate), not code with hidden TODOs.

**Type/name consistency:** `runtime_file_path`/`read_runtime_discovery`/`decide_runtime_attach` defined in Task 1 (sidecar.rs) and called by name in Task 2 (lib.rs). `decide_runtime_attach(Option<Discovery>, F: Fn(&Discovery)->bool) -> Option<Discovery>` matches the `probe_runtime_health(&Discovery)->bool` passed in Task 2. `SidecarPlan::External(Discovery)` reused (no new variant); `Discovery`/`Discovery::parse` reused (no second struct). Health path `/health?token=` + `SIDECAR_TIMEOUT` match the existing `check_health`.
