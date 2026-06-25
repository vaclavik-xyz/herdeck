# Config Editor Frontend — Řez 3 (Transport + Shell) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the transport + shell of the herdeck config-editor GUI: Rust proxy commands for the config HTTP API (token injected Rust-side), a complete framework-free `configClient.ts` (parse/serialize/override/secret logic, fully unit-tested), a second Tauri window `"config"` with root-by-label selection, and a `ConfigApp.svelte` skeleton (profile switcher + sidebar + save bar + reused `DeckView` preview) with the **Servers** section as the one representative section proving the pattern.

**Architecture:** Mirror the proven floating-deck transport (`deckClient.ts` → `commandTransport(invoke)` → token-free Tauri commands → `http.rs` helpers inject the token Rust-side). The editor holds the whole config in memory and POSTs `{base, profiles, local}` on an explicit global Apply. `configClient.ts` carries ALL testable logic; Svelte components are thin templates over it. The `"config"` window shares `index.html`; `main.ts` mounts `App` or `ConfigApp` by `getCurrentWindow().label`.

**Tech Stack:** Rust (Tauri 2, stdlib TCP — no new crate), TypeScript + Svelte 5 (runes), Vitest, `cargo test`.

## Global Constraints

- The sidecar access token NEVER lives in JS. Every sidecar call goes through a token-free Tauri command; `http.rs` injects the token (query param for GET, `X-Herdeck-Token` header for POST/DELETE). Mirror the existing deck commands (`deck_state`/`deck_tile`/`deck_press`).
- `configClient.ts` is framework-free: no `@tauri-apps/api` import; the `invoke` function is injected (`InvokeFn`), exactly like `deckClient.ts`. It must be fully unit-testable under Vitest with a fake transport.
- Secret VALUES never enter the JS editor model. `read()` returns only `{set, source}` flags + env-var NAMES; a secret value is only ever sent one-way via `config_secret_set`.
- Backend API shapes are fixed (implemented in `src/herdeck/deckapp/server.py`), use them verbatim:
  - `GET /config?token=` → `200 {base, profiles, local, secrets}` (`secrets[name] = {set: bool, source: "env"|"keychain"|null}`); `404` when no config_service.
  - `POST /config/validate` (header token), body `{base, profiles, local}` → `200 {errors: [str]}`; `400` on non-dict body.
  - `POST /config` (header token), body `{base, profiles, local}` → `200 {errors: [str]}` (empty ⇒ write+reload); `400` on bad body.
  - `POST /profiles/active` (header token), body `{name}` → `200 {changed: bool}`; `400` on unknown/missing/non-string name.
  - `POST /secret` (header token), body `{token_env, value}` → `204`; `400` on missing keys.
  - `DELETE /secret/{token_env}` (header token) → `204`.
- The `"config"` window: label `"config"`, decorated, NOT always-on-top, hidden at startup (`visible: false`), opened on demand via the custom `open_config` command (Rust does `show()` + `set_focus()`).
- Save model = explicit global Apply (no auto-save). Řez 3 builds the save bar + wiring; the full per-section edit UX is řez 4.
- Spec: `docs/superpowers/specs/2026-06-25-config-editor-frontend-design.md`. Parent: `docs/superpowers/specs/2026-06-24-config-editor-design.md`.
- Work in `desktop/`. Run TS tests with `npm test` (Vitest), build/type-check with `npm run build`, from `desktop/`. Run Rust tests with `cargo test` from `desktop/src-tauri/`.
- Conventional commits, English. NEVER add `Co-Authored-By` or a "Generated with" trailer. A post-commit `roborev` hook runs automatically; fix anything it flags.

---

## File Structure

- `desktop/src-tauri/src/http.rs` — **modify**: add `build_post_json_request`, `http_post_json` (returns `(status, body)`), `build_delete_request`, `http_delete`.
- `desktop/src-tauri/tests/http.rs` — **modify**: integration tests for the two new helpers (one-shot loopback server, assert token/body + parsed result).
- `desktop/src-tauri/src/lib.rs` — **modify**: 6 config proxy commands + `open_config`; register in `invoke_handler`; tray "Settings…" item.
- `desktop/src-tauri/tauri.conf.json` — **modify**: add the `"config"` window.
- `desktop/src-tauri/capabilities/default.json` — **modify**: add `"config"` to `windows`.
- `desktop/src/lib/configClient.ts` — **new**: types, `parseConfig`, `parseValidate`, `ConfigTransport` + `commandTransport`, editor-model helpers (`toWriteBody`, `inheritedValue`, `setOverride`, `clearOverride`, `secretFlag`), server mutations (`addServer`, `removeServer`, `updateServer`).
- `desktop/src/lib/configClient.test.ts` — **new**: Vitest unit tests for all of the above.
- `desktop/src/main.ts` — **modify**: mount `App` or `ConfigApp` by window label.
- `desktop/src/ConfigApp.svelte` — **new**: editor shell skeleton.
- `desktop/src/lib/sections/ServersSection.svelte` — **new**: the Servers section.
- `desktop/src/lib/fields/TextField.svelte`, `desktop/src/lib/fields/TokenSecretField.svelte` — **new**: the two field widgets Servers needs.

---

### Task 1: Rust `http.rs` — `http_post_json` + `http_delete`

**Files:**
- Modify: `desktop/src-tauri/src/http.rs`
- Test: `desktop/src-tauri/tests/http.rs`

**Interfaces:**
- Produces:
  - `build_post_json_request(host: &str, path_and_query: &str, header_name: &str, header_value: &str, body: &str) -> String`
  - `http_post_json(host: &str, port: u16, path_and_query: &str, header: (&str, &str), body: &str, timeout: Duration) -> Result<(u16, String), String>` — returns `(status, body)` for ALL codes (caller reads `{errors}` on 200, distinguishes 400).
  - `build_delete_request(host: &str, path_and_query: &str, header_name: &str, header_value: &str) -> String`
  - `http_delete(host: &str, port: u16, path_and_query: &str, header: (&str, &str), timeout: Duration) -> Result<u16, String>` — returns the status code.

- [ ] **Step 1: Write the failing tests**

Add to the inline `#[cfg(test)] mod tests` in `desktop/src-tauri/src/http.rs` (mirroring `build_post_request_carries_header_and_zero_length`):

```rust
    #[test]
    fn build_post_json_request_carries_body_headers_and_length() {
        let req = build_post_json_request("127.0.0.1", "/config", "X-Herdeck-Token", "tok", "{\"a\":1}");
        assert!(req.starts_with("POST /config HTTP/1.0\r\n"));
        assert!(req.contains("X-Herdeck-Token: tok\r\n"));
        assert!(req.contains("Content-Type: application/json\r\n"));
        assert!(req.contains("Content-Length: 7\r\n")); // {"a":1} is 7 bytes
        assert!(req.ends_with("\r\n\r\n{\"a\":1}"));
    }

    #[test]
    fn build_delete_request_carries_token_header_and_zero_length() {
        let req = build_delete_request("127.0.0.1", "/secret/TOK", "X-Herdeck-Token", "tok");
        assert!(req.starts_with("DELETE /secret/TOK HTTP/1.0\r\n"));
        assert!(req.contains("X-Herdeck-Token: tok\r\n"));
        assert!(req.contains("Content-Length: 0\r\n"));
        assert!(req.ends_with("\r\n\r\n"));
    }
```

Add to `desktop/src-tauri/tests/http.rs` (mirroring `send_press_posts_with_token_header_and_returns_status`; update the `use` line to import the new helpers):

```rust
use herdeck_desktop_lib::http::{fetch_image, fetch_state, http_delete, http_get, http_post_json, send_press};

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
    assert!(req.starts_with("POST /config HTTP/1.0"), "request was: {req:?}");
    assert!(req.contains("X-Herdeck-Token: HDR\r\n"), "request was: {req:?}");
    assert!(req.ends_with("{\"base\":{}}"), "request was: {req:?}");
}

#[test]
fn http_post_json_returns_400_status_with_body() {
    let (port, _rx) = serve_once_capture(b"HTTP/1.0 400 Bad Request\r\n\r\nbad".to_vec());
    let (code, _body) =
        http_post_json("127.0.0.1", port, "/config", ("X-Herdeck-Token", "H"), "{", Duration::from_secs(2)).unwrap();
    assert_eq!(code, 400);
}

#[test]
fn http_delete_sends_token_header_and_returns_status() {
    let (port, rx) = serve_once_capture(b"HTTP/1.0 204 No Content\r\nContent-Length: 0\r\n\r\n".to_vec());
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
    assert!(req.starts_with("DELETE /secret/TOK HTTP/1.0"), "request was: {req:?}");
    assert!(req.contains("X-Herdeck-Token: HDR\r\n"), "request was: {req:?}");
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd desktop/src-tauri && cargo test http_post_json http_delete build_post_json build_delete 2>&1 | tail -20`
Expected: FAIL to compile — `cannot find function http_post_json` / `build_post_json_request` in module `http`.

- [ ] **Step 3: Write minimal implementation**

In `desktop/src-tauri/src/http.rs`, after `http_post` (around line 180), add:

```rust
/// Build an HTTP/1.0 POST with a JSON body and one extra header (the
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
        "POST {path_and_query} HTTP/1.0\r\n\
         Host: {host}\r\n\
         {header_name}: {header_value}\r\n\
         Content-Type: application/json\r\n\
         Content-Length: {len}\r\n\
         Connection: close\r\n\r\n\
         {body}",
        len = body.as_bytes().len()
    )
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
    let addr = format!("{host}:{port}");
    let mut stream = TcpStream::connect(&addr).map_err(|e| format!("connect {addr}: {e}"))?;
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));

    let req = build_post_json_request(host, path_and_query, header.0, header.1, body);
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("write to sidecar: {e}"))?;

    let mut raw = String::new();
    stream
        .read_to_string(&mut raw)
        .map_err(|e| format!("read from sidecar: {e}"))?;
    parse_http_response(&raw)
}

/// Build an HTTP/1.0 DELETE with one extra header and an empty body.
pub fn build_delete_request(
    host: &str,
    path_and_query: &str,
    header_name: &str,
    header_value: &str,
) -> String {
    format!(
        "DELETE {path_and_query} HTTP/1.0\r\n\
         Host: {host}\r\n\
         {header_name}: {header_value}\r\n\
         Content-Length: 0\r\n\
         Connection: close\r\n\r\n"
    )
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
    let addr = format!("{host}:{port}");
    let mut stream = TcpStream::connect(&addr).map_err(|e| format!("connect {addr}: {e}"))?;
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));

    let req = build_delete_request(host, path_and_query, header.0, header.1);
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd desktop/src-tauri && cargo test 2>&1 | tail -20`
Expected: PASS — all http tests (inline + integration) green, including the 5 new ones.

- [ ] **Step 5: Commit**

```bash
git add desktop/src-tauri/src/http.rs desktop/src-tauri/tests/http.rs
git commit -m "feat(desktop): http_post_json + http_delete loopback helpers (token-injecting)"
```

---

### Task 2: Rust config proxy commands + `open_config` + `"config"` window

**Files:**
- Modify: `desktop/src-tauri/src/lib.rs`
- Modify: `desktop/src-tauri/tauri.conf.json`
- Modify: `desktop/src-tauri/capabilities/default.json`

**Interfaces:**
- Consumes: `http::{http_get, http_post_json, http_delete}` (Task 1), `current_discovery`, `SIDECAR_TIMEOUT`, `AppState`.
- Produces (Tauri commands the frontend `invoke`s):
  - `config_read() -> Result<serde_json::Value, String>` (GET /config, parsed JSON)
  - `config_validate(body: serde_json::Value) -> Result<serde_json::Value, String>` (POST /config/validate → `{errors}`)
  - `config_write(body: serde_json::Value) -> Result<serde_json::Value, String>` (POST /config → `{errors}`)
  - `config_set_active(name: String) -> Result<serde_json::Value, String>` (POST /profiles/active → `{changed}`)
  - `config_secret_set(token_env: String, value: String) -> Result<u16, String>` (POST /secret → status)
  - `config_secret_clear(token_env: String) -> Result<u16, String>` (DELETE /secret/{env} → status)
  - `open_config() -> Result<(), String>` (show + focus the `"config"` window)

This task is wiring (thin commands over the Task-1 helpers + window config). It is verified by `cargo build` + `cargo test` staying green; the request/response logic itself is covered by Task 1's helper tests. Do NOT add a Tauri-runtime command test (the existing deck commands have none — the helper + spawn tests are the seam).

- [ ] **Step 1: Add the `"config"` window to `tauri.conf.json`**

In `desktop/src-tauri/tauri.conf.json`, add a second object to `app.windows` (after the `"main"` window object):

```json
      {
        "label": "config",
        "title": "herdeck — Config",
        "width": 900,
        "height": 680,
        "minWidth": 640,
        "minHeight": 480,
        "resizable": true,
        "decorations": true,
        "alwaysOnTop": false,
        "transparent": false,
        "visible": false,
        "shadow": true,
        "skipTaskbar": false
      }
```

- [ ] **Step 2: Allow the `"config"` window in capabilities**

In `desktop/src-tauri/capabilities/default.json`, change the `windows` list to include `"config"`:

```json
  "windows": ["main", "config"],
```

(The custom `config_*`/`open_config` commands need no capability — only core/plugin permissions are capability-gated; custom `invoke_handler` commands are callable from any window. `core:default` covers the events + window-label access the config WebView needs.)

- [ ] **Step 3: Add the commands to `lib.rs`**

In `desktop/src-tauri/src/lib.rs`, add a header-name constant near `SIDECAR_TIMEOUT` (line ~34):

```rust
/// The sidecar's mutating routes authenticate with this header (matches web.py
/// and the deck `/press`). GET routes use a `?token=` query param instead.
const HDR_TOKEN: &str = "X-Herdeck-Token";
```

Add the commands after `deck_press` (line ~137):

```rust
/// Proxy `GET /config` (token as query param) → the redacted config JSON
/// `{base, profiles, local, secrets}`. `Err` if the sidecar has no config
/// service (404) or is unreachable.
#[tauri::command]
fn config_read(state: tauri::State<'_, AppState>) -> Result<serde_json::Value, String> {
    let d = current_discovery(&state)?;
    let body = http::http_get(
        &d.host,
        d.port,
        &format!("/config?token={}", d.token),
        SIDECAR_TIMEOUT,
    )?;
    serde_json::from_str(&body).map_err(|e| format!("invalid /config JSON from sidecar: {e}"))
}

/// Proxy `POST /config/validate` (header token) with the proposed config body →
/// `{errors: [...]}`. The body is the JS `{base, profiles, local}` object.
#[tauri::command]
fn config_validate(
    state: tauri::State<'_, AppState>,
    body: serde_json::Value,
) -> Result<serde_json::Value, String> {
    config_post_json(&state, "/config/validate", &body)
}

/// Proxy `POST /config` (header token) — atomic write + reload on the sidecar
/// when `errors` is empty. Returns `{errors: [...]}`.
#[tauri::command]
fn config_write(
    state: tauri::State<'_, AppState>,
    body: serde_json::Value,
) -> Result<serde_json::Value, String> {
    config_post_json(&state, "/config", &body)
}

/// Proxy `POST /profiles/active` (header token) → `{changed: bool}`. A 400
/// (unknown/invalid profile name) surfaces as `Err` so the UI can show it.
#[tauri::command]
fn config_set_active(
    state: tauri::State<'_, AppState>,
    name: String,
) -> Result<serde_json::Value, String> {
    config_post_json(&state, "/profiles/active", &serde_json::json!({ "name": name }))
}

/// Proxy `POST /secret` (header token) — store `value` for `token_env` in the
/// OS keychain. Returns the HTTP status (204 ok, 400 missing fields). The value
/// is one-way: it is never read back.
#[tauri::command]
fn config_secret_set(
    state: tauri::State<'_, AppState>,
    token_env: String,
    value: String,
) -> Result<u16, String> {
    let d = current_discovery(&state)?;
    let body = serde_json::json!({ "token_env": token_env, "value": value }).to_string();
    let (code, _resp) = http::http_post_json(
        &d.host,
        d.port,
        "/secret",
        (HDR_TOKEN, &d.token),
        &body,
        SIDECAR_TIMEOUT,
    )?;
    Ok(code)
}

/// Proxy `DELETE /secret/{token_env}` (header token) → status (204 ok).
#[tauri::command]
fn config_secret_clear(
    state: tauri::State<'_, AppState>,
    token_env: String,
) -> Result<u16, String> {
    let d = current_discovery(&state)?;
    http::http_delete(
        &d.host,
        d.port,
        &format!("/secret/{token_env}"),
        (HDR_TOKEN, &d.token),
        SIDECAR_TIMEOUT,
    )
}

/// Shared POST-JSON-and-parse for the config routes that return a JSON object on
/// 200. A non-200 (e.g. 400 for a malformed body / bad profile name) is an `Err`
/// the command surfaces to JS.
fn config_post_json(
    state: &tauri::State<'_, AppState>,
    path: &str,
    body: &serde_json::Value,
) -> Result<serde_json::Value, String> {
    let d = current_discovery(state)?;
    let (code, resp) = http::http_post_json(
        &d.host,
        d.port,
        path,
        (HDR_TOKEN, &d.token),
        &body.to_string(),
        SIDECAR_TIMEOUT,
    )?;
    if code == 200 {
        serde_json::from_str(&resp).map_err(|e| format!("invalid JSON from {path}: {e}"))
    } else {
        Err(format!("sidecar returned HTTP {code} for {path}"))
    }
}

/// Show + focus the (hidden-at-startup) config editor window.
#[tauri::command]
fn open_config(app: tauri::AppHandle) -> Result<(), String> {
    let w = app
        .get_webview_window("config")
        .ok_or_else(|| "config window not found".to_string())?;
    w.show().map_err(|e| e.to_string())?;
    w.set_focus().map_err(|e| e.to_string())?;
    Ok(())
}
```

- [ ] **Step 4: Register the commands + tray "Settings…"**

In `lib.rs`, extend `tauri::generate_handler![...]` (line ~288) to add the new commands:

```rust
        .invoke_handler(tauri::generate_handler![
            get_discovery,
            check_health,
            deck_state,
            deck_tile,
            deck_panel,
            deck_press,
            config_read,
            config_validate,
            config_write,
            config_set_active,
            config_secret_set,
            config_secret_clear,
            open_config
        ])
```

In `build_tray` (line ~208), add a "Settings…" item and handle it. Replace the menu-item block + menu build:

```rust
    let settings = MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?;
    let show = MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "hide", "Hide", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&settings, &show, &hide, &quit])?;
```

and add a match arm in `.on_menu_event(...)` (alongside "show"/"hide"/"quit"):

```rust
            "settings" => {
                if let Some(w) = app.get_webview_window("config") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
```

- [ ] **Step 4b: Hide the config window on close (never destroy it)**

So `open_config` / tray "Settings…" can always re-show the window, intercept its close request and HIDE instead of letting Tauri destroy it. In the `.setup(...)` closure in `run()` (line ~296), after `build_tray(app)?;`, add:

```rust
            // The config window is hidden at startup and reopened on demand; if it
            // were allowed to close, Tauri would DESTROY it and open_config would
            // then fail with "config window not found". Intercept close -> hide, so
            // it persists for the app's lifetime (the floating deck + sidecar run on).
            if let Some(cfg_win) = app.get_webview_window("config") {
                let w = cfg_win.clone();
                cfg_win.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = w.hide();
                    }
                });
            }
```

(`open_config` keeps its `ok_or_else("config window not found")` as a defensive fallback, but with hide-on-close the window is never absent after startup.)

- [ ] **Step 5: Build + test**

Run: `cd desktop/src-tauri && cargo build 2>&1 | tail -20 && cargo test 2>&1 | tail -10`
Expected: `cargo build` succeeds (all commands compile, window/capabilities valid); `cargo test` all green (Task 1 helper tests included).

- [ ] **Step 6: Commit**

```bash
git add desktop/src-tauri/src/lib.rs desktop/src-tauri/tauri.conf.json desktop/src-tauri/capabilities/default.json
git commit -m "feat(desktop): config proxy commands + open_config + 2nd 'config' window + tray Settings"
```

---

### Task 3: `configClient.ts` — parse layer + transport

**Files:**
- Create: `desktop/src/lib/configClient.ts`
- Test: `desktop/src/lib/configClient.test.ts`

**Interfaces:**
- Consumes: an injected `InvokeFn` (same shape as `deckClient.ts`).
- Produces:
  - `interface SecretFlag { set: boolean; source: "env" | "keychain" | null }`
  - `interface ConfigPayload { base: Record<string, unknown>; profiles: Record<string, Record<string, unknown>>; local: Record<string, unknown>; secrets: Record<string, SecretFlag> }`
  - `interface WriteBody { base: Record<string, unknown>; profiles: Record<string, Record<string, unknown>>; local: Record<string, unknown> }`
  - `parseConfig(raw: unknown): ConfigPayload | null`
  - `parseValidate(raw: unknown): string[]`
  - `type InvokeFn = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>`
  - `interface ConfigTransport { read(): Promise<unknown>; validate(body: WriteBody): Promise<unknown>; write(body: WriteBody): Promise<unknown>; setActive(name: string): Promise<unknown>; setSecret(tokenEnv: string, value: string): Promise<number>; clearSecret(tokenEnv: string): Promise<number> }`
  - `commandTransport(invoke: InvokeFn): ConfigTransport`

- [ ] **Step 1: Write the failing tests**

Create `desktop/src/lib/configClient.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import {
  parseConfig,
  parseValidate,
  commandTransport,
  type ConfigPayload,
} from "./configClient";

function rawConfig(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    base: { servers: [{ id: "local", url: "ws://x", token_env: "TOK" }], deck: { grid: "5x3" } },
    profiles: { mobile: { view: { management: "bottom_row" } } },
    local: { active_profile: "mobile" },
    secrets: { TOK: { set: true, source: "env" } },
    ...over,
  };
}

describe("parseConfig", () => {
  it("shapes a well-formed /config payload", () => {
    const c = parseConfig(rawConfig())!;
    expect(c).not.toBeNull();
    expect(c.base.deck).toEqual({ grid: "5x3" });
    expect(c.profiles.mobile.view).toEqual({ management: "bottom_row" });
    expect(c.local.active_profile).toBe("mobile");
    expect(c.secrets.TOK).toEqual({ set: true, source: "env" });
  });

  it("defaults missing sections to empty objects (onboarding)", () => {
    const c = parseConfig({})!;
    expect(c).toEqual({ base: {}, profiles: {}, local: {}, secrets: {} });
  });

  it("normalizes a malformed secret flag", () => {
    const c = parseConfig(rawConfig({ secrets: { TG: { set: "yes", source: 9 } } }))!;
    expect(c.secrets.TG).toEqual({ set: false, source: null });
  });

  it("returns null for non-objects", () => {
    expect(parseConfig(null)).toBeNull();
    expect(parseConfig("nope")).toBeNull();
  });
});

describe("parseValidate", () => {
  it("extracts the errors array of strings", () => {
    expect(parseValidate({ errors: ["bad grid", "unknown server 'x'"] })).toEqual([
      "bad grid",
      "unknown server 'x'",
    ]);
  });
  it("returns [] for junk or missing errors", () => {
    expect(parseValidate({})).toEqual([]);
    expect(parseValidate(null)).toEqual([]);
    expect(parseValidate({ errors: [1, "ok", null] })).toEqual(["ok"]);
  });
});

describe("commandTransport", () => {
  it("maps each method to its invoke command with the right args", async () => {
    const calls: { cmd: string; args?: Record<string, unknown> }[] = [];
    const invoke = async (cmd: string, args?: Record<string, unknown>) => {
      calls.push({ cmd, args });
      if (cmd === "config_secret_set" || cmd === "config_secret_clear") return 204;
      return {};
    };
    const t = commandTransport(invoke);
    const body = { base: {}, profiles: {}, local: {} };
    await t.read();
    await t.validate(body);
    await t.write(body);
    await t.setActive("mobile");
    expect(await t.setSecret("TOK", "v")).toBe(204);
    expect(await t.clearSecret("TOK")).toBe(204);
    expect(calls).toEqual([
      { cmd: "config_read", args: undefined },
      { cmd: "config_validate", args: { body } },
      { cmd: "config_write", args: { body } },
      { cmd: "config_set_active", args: { name: "mobile" } },
      { cmd: "config_secret_set", args: { tokenEnv: "TOK", value: "v" } },
      { cmd: "config_secret_clear", args: { tokenEnv: "TOK" } },
    ]);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd desktop && npm test -- configClient 2>&1 | tail -20`
Expected: FAIL — cannot resolve `./configClient` (module does not exist yet).

- [ ] **Step 3: Write minimal implementation**

Create `desktop/src/lib/configClient.ts`:

```ts
// Framework-free parse / serialize / transport core for the config editor. A
// faithful sibling of deckClient.ts: pure functions + an injected `invoke`, so
// the whole client is unit-testable under Vitest without a Tauri WebView. The
// sidecar access token is NEVER here — the Rust shell injects it inside the
// token-free config_* commands (see src-tauri/src/lib.rs).

/** A redacted secret flag: presence + where it resolves, never a value. */
export interface SecretFlag {
  set: boolean;
  source: "env" | "keychain" | null;
}

/** The parsed `GET /config` payload. `secrets` carries only presence flags. */
export interface ConfigPayload {
  base: Record<string, unknown>;
  profiles: Record<string, Record<string, unknown>>;
  local: Record<string, unknown>;
  secrets: Record<string, SecretFlag>;
}

/** What `POST /config[/validate]` takes: the editable config minus `secrets`. */
export interface WriteBody {
  base: Record<string, unknown>;
  profiles: Record<string, Record<string, unknown>>;
  local: Record<string, unknown>;
}

function obj(v: unknown): Record<string, unknown> {
  return v != null && typeof v === "object" && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};
}

function parseSecretFlag(raw: unknown): SecretFlag {
  const v = obj(raw);
  const source = v.source === "env" || v.source === "keychain" ? v.source : null;
  return { set: v.set === true, source };
}

/** Shape a raw `/config` value into a ConfigPayload, or null when it is not an
 *  object. Missing sections default to `{}` (the onboarding / no-config case). */
export function parseConfig(raw: unknown): ConfigPayload | null {
  if (raw == null || typeof raw !== "object" || Array.isArray(raw)) return null;
  const v = raw as Record<string, unknown>;
  const profiles: Record<string, Record<string, unknown>> = {};
  for (const [name, overlay] of Object.entries(obj(v.profiles))) profiles[name] = obj(overlay);
  const secrets: Record<string, SecretFlag> = {};
  for (const [name, flag] of Object.entries(obj(v.secrets))) secrets[name] = parseSecretFlag(flag);
  return { base: obj(v.base), profiles, local: obj(v.local), secrets };
}

/** Extract the `errors` string list from a `{errors: [...]}` reply, dropping
 *  any non-string entry. Junk / missing → `[]`. */
export function parseValidate(raw: unknown): string[] {
  const v = obj(raw);
  if (!Array.isArray(v.errors)) return [];
  return v.errors.filter((e): e is string => typeof e === "string");
}

/** The Tauri `invoke` shape, injected so configClient stays framework-free. */
export type InvokeFn = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;

/** How the editor talks to the sidecar. Every call goes through a token-free
 *  Tauri command; the Rust shell injects the access token. Injectable so the
 *  editor is unit-testable with a fake. */
export interface ConfigTransport {
  read(): Promise<unknown>;
  validate(body: WriteBody): Promise<unknown>;
  write(body: WriteBody): Promise<unknown>;
  setActive(name: string): Promise<unknown>;
  setSecret(tokenEnv: string, value: string): Promise<number>;
  clearSecret(tokenEnv: string): Promise<number>;
}

export function commandTransport(invoke: InvokeFn): ConfigTransport {
  const asCode = (v: unknown) => (typeof v === "number" ? v : 0);
  return {
    read: () => invoke("config_read"),
    validate: (body) => invoke("config_validate", { body }),
    write: (body) => invoke("config_write", { body }),
    setActive: (name) => invoke("config_set_active", { name }),
    async setSecret(tokenEnv, value) {
      return asCode(await invoke("config_secret_set", { tokenEnv, value }));
    },
    async clearSecret(tokenEnv) {
      return asCode(await invoke("config_secret_clear", { tokenEnv }));
    },
  };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd desktop && npm test -- configClient 2>&1 | tail -20`
Expected: PASS — parseConfig (4), parseValidate (2), commandTransport (1).

- [ ] **Step 5: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat(desktop): configClient parse layer + token-free command transport"
```

---

### Task 4: `configClient.ts` — editor model helpers (write body, override/clear, secret flag)

**Files:**
- Modify: `desktop/src/lib/configClient.ts`
- Test: `desktop/src/lib/configClient.test.ts`

**Interfaces:**
- Consumes: `ConfigPayload`, `WriteBody`, `SecretFlag` (Task 3).
- Produces:
  - `toWriteBody(payload: ConfigPayload): WriteBody` — strips `secrets`, deep-copies `{base, profiles, local}`.
  - `inheritedValue(base: Record<string, unknown>, section: string, key: string): unknown` — the base value a profile overlay would inherit (`undefined` if absent).
  - `setOverride(profiles: Record<string, Record<string, unknown>>, name: string, section: string, key: string, value: unknown): Record<string, Record<string, unknown>>` — returns a NEW profiles map with `profiles[name][section][key] = value`.
  - `clearOverride(profiles: ..., name: string, section: string, key: string): Record<string, Record<string, unknown>>` — returns a NEW profiles map with that overlay key removed (and empties pruned).
  - `secretFlag(payload: ConfigPayload, name: string): SecretFlag` — the flag for `name`, defaulting to `{set: false, source: null}`.

- [ ] **Step 1: Write the failing tests**

Add to `desktop/src/lib/configClient.test.ts` (extend the imports):

```ts
import {
  toWriteBody,
  inheritedValue,
  setOverride,
  clearOverride,
  secretFlag,
} from "./configClient";

describe("toWriteBody", () => {
  it("drops secrets and deep-copies base/profiles/local", () => {
    const payload = parseConfig(rawConfig())!;
    const body = toWriteBody(payload);
    expect(body).toEqual({
      base: { servers: [{ id: "local", url: "ws://x", token_env: "TOK" }], deck: { grid: "5x3" } },
      profiles: { mobile: { view: { management: "bottom_row" } } },
      local: { active_profile: "mobile" },
    });
    expect("secrets" in body).toBe(false);
    // deep copy: mutating the body must not touch the source payload
    (body.base.deck as Record<string, unknown>).grid = "4x3";
    expect(payload.base.deck).toEqual({ grid: "5x3" });
  });
});

describe("inheritedValue", () => {
  it("returns the base section value, or undefined when absent", () => {
    const base = { view: { management: "launcher_menu" } };
    expect(inheritedValue(base, "view", "management")).toBe("launcher_menu");
    expect(inheritedValue(base, "view", "agent_slots")).toBeUndefined();
    expect(inheritedValue(base, "deck", "grid")).toBeUndefined();
  });
});

describe("setOverride / clearOverride", () => {
  it("setOverride writes a new profiles map without touching the input", () => {
    const profiles = { mobile: {} as Record<string, unknown> };
    const next = setOverride(profiles, "mobile", "view", "management", "bottom_row");
    expect(next.mobile.view).toEqual({ management: "bottom_row" });
    expect(profiles.mobile).toEqual({}); // input untouched
  });

  it("clearOverride removes the key and prunes empty section/profile", () => {
    const profiles = { mobile: { view: { management: "bottom_row" } } };
    const next = clearOverride(profiles, "mobile", "view", "management");
    expect(next.mobile.view).toBeUndefined(); // empty section pruned
    expect(profiles.mobile.view).toEqual({ management: "bottom_row" }); // input untouched
  });

  it("clearOverride on an absent key is a harmless no-op copy", () => {
    const profiles = { mobile: { view: { management: "bottom_row" } } };
    const next = clearOverride(profiles, "mobile", "deck", "grid");
    expect(next.mobile.view).toEqual({ management: "bottom_row" });
  });
});

describe("secretFlag", () => {
  it("returns the flag or a not-set default", () => {
    const payload = parseConfig(rawConfig())!;
    expect(secretFlag(payload, "TOK")).toEqual({ set: true, source: "env" });
    expect(secretFlag(payload, "MISSING")).toEqual({ set: false, source: null });
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd desktop && npm test -- configClient 2>&1 | tail -20`
Expected: FAIL — `toWriteBody` / `setOverride` / … are not exported.

- [ ] **Step 3: Write minimal implementation**

Add to `desktop/src/lib/configClient.ts` (a deep-clone helper + the model functions):

```ts
/** Structured deep copy for the JSON-shaped config model (no functions/dates). */
function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

/** The editable config (no secrets), deep-copied so edits never alias the
 *  fetched payload. This is exactly what `POST /config[/validate]` takes. */
export function toWriteBody(payload: ConfigPayload): WriteBody {
  return {
    base: clone(payload.base),
    profiles: clone(payload.profiles),
    local: clone(payload.local),
  };
}

/** The base value a profile overlay inherits for `section.key`, or undefined. */
export function inheritedValue(
  base: Record<string, unknown>,
  section: string,
  key: string,
): unknown {
  const sec = base[section];
  if (sec == null || typeof sec !== "object" || Array.isArray(sec)) return undefined;
  return (sec as Record<string, unknown>)[key];
}

/** New profiles map with `profiles[name][section][key] = value`. Input untouched. */
export function setOverride(
  profiles: Record<string, Record<string, unknown>>,
  name: string,
  section: string,
  key: string,
  value: unknown,
): Record<string, Record<string, unknown>> {
  const next = clone(profiles);
  const overlay = next[name] ?? (next[name] = {});
  const sec = (overlay[section] as Record<string, unknown> | undefined) ?? {};
  sec[key] = value;
  overlay[section] = sec;
  return next;
}

/** New profiles map with the overlay `section.key` removed; an emptied section
 *  (and an emptied profile body) is pruned so write() omits it. Input untouched. */
export function clearOverride(
  profiles: Record<string, Record<string, unknown>>,
  name: string,
  section: string,
  key: string,
): Record<string, Record<string, unknown>> {
  const next = clone(profiles);
  const overlay = next[name];
  if (overlay == null) return next;
  const sec = overlay[section];
  if (sec != null && typeof sec === "object" && !Array.isArray(sec)) {
    const s = sec as Record<string, unknown>;
    delete s[key];
    if (Object.keys(s).length === 0) delete overlay[section];
  }
  return next;
}

/** The presence flag for `name`, defaulting to not-set. */
export function secretFlag(payload: ConfigPayload, name: string): SecretFlag {
  return payload.secrets[name] ?? { set: false, source: null };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd desktop && npm test -- configClient 2>&1 | tail -20`
Expected: PASS — all configClient tests (Task 3 + Task 4) green.

- [ ] **Step 5: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts
git commit -m "feat(desktop): configClient editor-model helpers (write body, override/clear, secret flag)"
```

---

### Task 5: `main.ts` root-by-label + `ConfigApp.svelte` shell skeleton

**Files:**
- Modify: `desktop/src/main.ts`
- Create: `desktop/src/ConfigApp.svelte`

**Interfaces:**
- Consumes: `getCurrentWindow` (`@tauri-apps/api/window`), `App.svelte`, `ConfigApp.svelte`, `configClient.{commandTransport, parseConfig, parseValidate, toWriteBody, ConfigPayload}`, `deckClient.commandTransport` (preview), `sidecar.asDiscovery`, `DeckView.svelte`.
- Produces: a mounted `ConfigApp` when the window label is `"config"`; the editor shell (profile switcher + sidebar + section area + save bar + preview) bound to a loaded `ConfigPayload`. The Servers section content is Task 6 (this task leaves a placeholder area + the section list).

This is the Svelte shell. The testable logic already lives in `configClient.ts` (Tasks 3–4); verify this task with `npm run build` (svelte-check type-checks) and the existing Vitest suite staying green. Follow the discovery + `commandTransport` pattern in `App.svelte` exactly.

- [ ] **Step 1: Root-by-label in `main.ts`**

Replace `desktop/src/main.ts` with:

```ts
import { mount } from "svelte";
import App from "./App.svelte";
import ConfigApp from "./ConfigApp.svelte";

// Both windows load index.html; pick the root by window label. getCurrentWindow
// throws outside a Tauri WebView (plain browser) — default to the deck there.
let label = "main";
try {
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  label = getCurrentWindow().label;
} catch {
  /* not in a Tauri WebView */
}

const Root = label === "config" ? ConfigApp : App;
const app = mount(Root, { target: document.getElementById("app")! });

export default app;
```

- [ ] **Step 2: `ConfigApp.svelte` skeleton**

Create `desktop/src/ConfigApp.svelte` (Svelte 5 runes; mirror `App.svelte`'s discovery + `$state`/`$derived` and `DeckView` preview):

```svelte
<script lang="ts">
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { listen } from "@tauri-apps/api/event";
  import DeckView from "./lib/DeckView.svelte";
  import ServersSection from "./lib/sections/ServersSection.svelte";
  import { asDiscovery, type Discovery } from "./lib/sidecar";
  import { commandTransport as deckTransport } from "./lib/deckClient";
  import {
    commandTransport as cfgTransport,
    parseConfig,
    parseValidate,
    toWriteBody,
    type ConfigPayload,
  } from "./lib/configClient";

  const SECTIONS = [
    "Servers", "Deck", "View", "Theme", "Macros",
    "Start profiles", "Notifications", "Safety", "Answer profiles", "Profiles",
  ];

  let discovery = $state<Discovery | null>(null);
  let payload = $state<ConfigPayload | null>(null);
  let active = $state("Servers");
  let dirty = $state(false);
  let errors = $state<string[]>([]);
  let busy = $state(false);
  let notice = $state(""); // transient out-of-band message (e.g. a failed secret op)

  const cfg = cfgTransport((cmd, args) => invoke(cmd, args));
  const preview = $derived(discovery ? deckTransport((cmd, args) => invoke(cmd, args)) : null);
  const profiles = $derived(payload ? ["default (báze)", ...Object.keys(payload.profiles)] : ["default (báze)"]);

  async function load(): Promise<void> {
    try {
      payload = parseConfig(await cfg.read());
      dirty = false;
      errors = [];
    } catch {
      payload = null; // sidecar not ready / no config -> onboarding handled in řez 4
    }
  }

  function markDirty(): void {
    dirty = true;
  }

  async function apply(): Promise<void> {
    if (!payload) return;
    busy = true;
    try {
      const res = parseValidate(await cfg.write(toWriteBody(payload)));
      errors = res;
      if (res.length === 0) {
        dirty = false;
        await load(); // re-read saved state (preview refreshes itself via its own poll)
      }
    } catch (e) {
      errors = [String(e)];
    } finally {
      busy = false;
    }
  }

  async function discard(): Promise<void> {
    await load();
  }

  onMount(() => {
    let alive = true;
    void listen<Discovery>("discovery", (ev) => {
      const d = asDiscovery(ev.payload);
      if (d) discovery = d;
    });
    void (async () => {
      while (alive && !discovery) {
        try {
          const d = asDiscovery(await invoke("get_discovery"));
          if (d) discovery = d;
        } catch {
          /* not ready */
        }
        if (!discovery) await new Promise((r) => setTimeout(r, 400));
      }
      await load();
    })();
    return () => {
      alive = false;
    };
  });
</script>

<main>
  <header class="topbar">
    <label>
      Profil:
      <select disabled>
        {#each profiles as p}<option>{p}</option>{/each}
      </select>
    </label>
    {#if dirty}<span class="dirty">● neuložené změny</span>{/if}
  </header>

  <div class="body">
    <nav class="sidebar">
      {#each SECTIONS as s}
        <button class:active={s === active} onclick={() => (active = s)}>{s}</button>
      {/each}
    </nav>

    <section class="form">
      {#if payload == null}
        <p class="hint">Načítám config… (nebo sidecar zatím neběží)</p>
      {:else if active === "Servers"}
        <ServersSection {payload} onChange={markDirty} onError={(m) => (notice = m)} />
      {:else}
        <p class="hint">Sekce „{active}" — řez 4.</p>
      {/if}
    </section>

    <aside class="preview">
      <DeckView transport={preview} />
    </aside>
  </div>

  <footer class="savebar">
    <button onclick={discard} disabled={!dirty || busy}>Discard</button>
    {#if notice}<span class="notice">{notice}</span>{/if}
    <span class="errcount" class:bad={errors.length > 0}>⚠ {errors.length} chyb</span>
    <button onclick={apply} disabled={!dirty || busy}>Apply</button>
  </footer>
</main>

<style>
  :global(html, body) { margin: 0; background: #0b0b0d; color: #e8e8ea; font: 13px system-ui; }
  main { display: flex; flex-direction: column; height: 100vh; }
  .topbar { display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-bottom: 1px solid #222; }
  .dirty { color: #e0a030; margin-left: auto; }
  .body { flex: 1; display: grid; grid-template-columns: 160px 1fr 220px; min-height: 0; }
  .sidebar { display: flex; flex-direction: column; border-right: 1px solid #222; overflow: auto; }
  .sidebar button { text-align: left; background: none; border: 0; color: inherit; padding: 8px 12px; cursor: pointer; }
  .sidebar button.active { background: #1b1b1f; }
  .form { padding: 16px; overflow: auto; }
  .preview { border-left: 1px solid #222; padding: 8px; overflow: auto; }
  .hint { color: #888; }
  .savebar { display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-top: 1px solid #222; }
  .savebar button { margin: 0; }
  .notice { color: #e0a030; }
  .errcount { margin-left: auto; color: #888; }
  .errcount.bad { color: #e05050; }
</style>
```

(`ServersSection.svelte` is created in Task 6; until then this file will not type-check on its own — Task 5 and Task 6 land together as the shell+section pair. If implementing strictly task-by-task, stub `ServersSection.svelte` with an empty component in Task 5 and fill it in Task 6.)

- [ ] **Step 3: Stub `ServersSection.svelte` so the shell type-checks**

Create a minimal `desktop/src/lib/sections/ServersSection.svelte` (Task 6 replaces the body):

```svelte
<script lang="ts">
  import type { ConfigPayload } from "../configClient";
  let { payload, onChange, onError }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();
</script>

<p class="hint">Servers — řez 3 Task 6.</p>
```

- [ ] **Step 4: Build / type-check**

Run: `cd desktop && npm run build 2>&1 | tail -25`
Expected: build succeeds (svelte-check + vite), no type errors. (The `await import` in `main.ts` top-level needs the module target to allow top-level await — Vite's default esnext target does. If the build complains, the implementer switches to a non-await dynamic import with a `.then`.)

- [ ] **Step 5: Run the existing TS suite (no regressions)**

Run: `cd desktop && npm test 2>&1 | tail -8`
Expected: all green (deckClient, sidecar, configClient).

- [ ] **Step 6: Commit**

```bash
git add desktop/src/main.ts desktop/src/ConfigApp.svelte desktop/src/lib/sections/ServersSection.svelte
git commit -m "feat(desktop): root-by-label + ConfigApp shell skeleton (switcher/sidebar/savebar/preview)"
```

---

### Task 6: Servers section + field widgets + server mutations

**Files:**
- Modify: `desktop/src/lib/configClient.ts`
- Modify: `desktop/src/lib/configClient.test.ts`
- Create: `desktop/src/lib/fields/TextField.svelte`
- Create: `desktop/src/lib/fields/TokenSecretField.svelte`
- Modify: `desktop/src/lib/sections/ServersSection.svelte`

**Interfaces:**
- Consumes: `ConfigPayload`, `secretFlag`, the `ConfigTransport` (for `setSecret`/`clearSecret`).
- Produces (pure server mutations on `base.servers`, so the Svelte stays thin + testable):
  - `serversOf(payload: ConfigPayload): Array<{ id: string; url: string; token_env: string }>` — the base server list (always an array; `[]` when absent/malformed).
  - `addServer(payload: ConfigPayload): ConfigPayload` — NEW payload with a blank server appended.
  - `removeServer(payload: ConfigPayload, index: number): ConfigPayload` — NEW payload with server `index` removed.
  - `updateServer(payload: ConfigPayload, index: number, field: "id" | "url" | "token_env", value: string): ConfigPayload` — NEW payload with that field set.

- [ ] **Step 1: Write the failing tests (server mutations)**

Add to `desktop/src/lib/configClient.test.ts`:

```ts
import { serversOf, addServer, removeServer, updateServer } from "./configClient";

describe("server mutations", () => {
  it("serversOf returns the base server list or []", () => {
    expect(serversOf(parseConfig(rawConfig())!)).toEqual([
      { id: "local", url: "ws://x", token_env: "TOK" },
    ]);
    expect(serversOf(parseConfig({})!)).toEqual([]);
  });

  it("addServer appends a blank server without touching the input", () => {
    const p = parseConfig(rawConfig())!;
    const next = addServer(p);
    expect(serversOf(next)).toHaveLength(2);
    expect(serversOf(next)[1]).toEqual({ id: "", url: "", token_env: "" });
    expect(serversOf(p)).toHaveLength(1); // input untouched
  });

  it("updateServer sets one field on a copy", () => {
    const p = parseConfig(rawConfig())!;
    const next = updateServer(p, 0, "url", "ws://new");
    expect(serversOf(next)[0].url).toBe("ws://new");
    expect(serversOf(p)[0].url).toBe("ws://x"); // input untouched
  });

  it("removeServer drops the indexed server", () => {
    const p = addServer(parseConfig(rawConfig())!);
    const next = removeServer(p, 0);
    expect(serversOf(next)).toHaveLength(1);
    expect(serversOf(next)[0]).toEqual({ id: "", url: "", token_env: "" });
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd desktop && npm test -- configClient 2>&1 | tail -20`
Expected: FAIL — `serversOf` / `addServer` / … not exported.

- [ ] **Step 3: Implement the server mutations**

Add to `desktop/src/lib/configClient.ts`:

```ts
/** A base server record as the editor edits it. */
export interface ServerRecord {
  id: string;
  url: string;
  token_env: string;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** The base `servers` list as editable records (always an array). */
export function serversOf(payload: ConfigPayload): ServerRecord[] {
  const raw = payload.base.servers;
  if (!Array.isArray(raw)) return [];
  return raw.map((s) => {
    const r = obj(s);
    return { id: str(r.id), url: str(r.url), token_env: str(r.token_env) };
  });
}

function withServers(payload: ConfigPayload, servers: ServerRecord[]): ConfigPayload {
  return { ...payload, base: { ...clone(payload.base), servers } };
}

/** NEW payload with a blank server appended. */
export function addServer(payload: ConfigPayload): ConfigPayload {
  return withServers(payload, [...serversOf(payload), { id: "", url: "", token_env: "" }]);
}

/** NEW payload with server `index` removed. */
export function removeServer(payload: ConfigPayload, index: number): ConfigPayload {
  const servers = serversOf(payload).filter((_, i) => i !== index);
  return withServers(payload, servers);
}

/** NEW payload with one field of server `index` set. */
export function updateServer(
  payload: ConfigPayload,
  index: number,
  field: keyof ServerRecord,
  value: string,
): ConfigPayload {
  const servers = serversOf(payload).map((s, i) => (i === index ? { ...s, [field]: value } : s));
  return withServers(payload, servers);
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd desktop && npm test -- configClient 2>&1 | tail -20`
Expected: PASS — server-mutation tests green (plus all earlier configClient tests).

- [ ] **Step 5: Field widgets**

Create `desktop/src/lib/fields/TextField.svelte`:

```svelte
<script lang="ts">
  let { label, value, oninput }: { label: string; value: string; oninput: (v: string) => void } =
    $props();
</script>

<label class="field">
  <span>{label}</span>
  <input value={value} oninput={(e) => oninput((e.target as HTMLInputElement).value)} />
</label>

<style>
  .field { display: grid; grid-template-columns: 80px 1fr; align-items: center; gap: 8px; margin: 4px 0; }
  .field span { color: #aaa; }
  input { background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
</style>
```

Create `desktop/src/lib/fields/TokenSecretField.svelte` (name field + presence badge + inline set/clear):

```svelte
<script lang="ts">
  import type { SecretFlag } from "../configClient";
  let {
    label,
    value,
    flag,
    oninput,
    onset,
    onclear,
  }: {
    label: string;
    value: string;
    flag: SecretFlag;
    oninput: (v: string) => void;
    onset: (secretValue: string) => void;
    onclear: () => void;
  } = $props();

  let entering = $state(false);
  let secretValue = $state("");

  function submit(): void {
    if (secretValue) onset(secretValue);
    secretValue = "";
    entering = false;
  }
</script>

<label class="field">
  <span>{label}</span>
  <input value={value} oninput={(e) => oninput((e.target as HTMLInputElement).value)} />
  {#if value}
    {#if flag.set}
      <span class="ok" title={flag.source ?? ""}>🔑✓</span>
      <button type="button" onclick={onclear}>clear</button>
    {:else}
      <span class="missing">🔑✗</span>
      <button type="button" onclick={() => (entering = true)}>nastav</button>
    {/if}
  {/if}
</label>

{#if entering}
  <div class="setrow">
    <input type="password" placeholder="hodnota tokenu" bind:value={secretValue} />
    <button type="button" onclick={submit}>Uložit do keychain</button>
    <button type="button" onclick={() => (entering = false)}>Zrušit</button>
  </div>
{/if}

<style>
  .field { display: grid; grid-template-columns: 80px 1fr auto auto; align-items: center; gap: 8px; margin: 4px 0; }
  .field span:first-child { color: #aaa; }
  input { background: #141417; border: 1px solid #2a2a30; color: inherit; padding: 4px 6px; border-radius: 4px; }
  .ok { color: #4fa84f; } .missing { color: #e0a030; }
  .setrow { display: flex; gap: 8px; margin: 4px 0 8px 88px; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; cursor: pointer; }
</style>
```

- [ ] **Step 6: Servers section**

Replace `desktop/src/lib/sections/ServersSection.svelte` with the real component (binds the pure mutations + the secret transport):

```svelte
<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import TextField from "../fields/TextField.svelte";
  import TokenSecretField from "../fields/TokenSecretField.svelte";
  import {
    commandTransport as cfgTransport,
    serversOf,
    addServer,
    removeServer,
    updateServer,
    secretFlag,
    type ConfigPayload,
  } from "../configClient";

  let { payload = $bindable(), onChange, onError }:
    { payload: ConfigPayload; onChange: () => void; onError: (msg: string) => void } = $props();

  const cfg = cfgTransport((cmd, args) => invoke(cmd, args));
  const servers = $derived(serversOf(payload));

  function set(i: number, field: "id" | "url" | "token_env", v: string): void {
    payload = updateServer(payload, i, field, v);
    onChange();
  }
  function add(): void {
    payload = addServer(payload);
    onChange();
  }
  function remove(i: number): void {
    payload = removeServer(payload, i);
    onChange();
  }
  async function setSecret(name: string, value: string): Promise<void> {
    const code = await cfg.setSecret(name, value); // 204 on success
    if (code === 204) {
      payload = { ...payload, secrets: { ...payload.secrets, [name]: { set: true, source: "keychain" } } };
    } else {
      onError(`uložení tokenu '${name}' selhalo (HTTP ${code})`);
    }
  }
  async function clearSecret(name: string): Promise<void> {
    const code = await cfg.clearSecret(name); // 204 on success
    if (code === 204) {
      payload = { ...payload, secrets: { ...payload.secrets, [name]: { set: false, source: null } } };
    } else {
      onError(`smazání tokenu '${name}' selhalo (HTTP ${code})`);
    }
  }
</script>

<h2>Servers</h2>
{#each servers as s, i (i)}
  <fieldset>
    <legend>{s.id || "(nový server)"} <button type="button" onclick={() => remove(i)}>×</button></legend>
    <TextField label="id" value={s.id} oninput={(v) => set(i, "id", v)} />
    <TextField label="url" value={s.url} oninput={(v) => set(i, "url", v)} />
    <TokenSecretField
      label="token"
      value={s.token_env}
      flag={secretFlag(payload, s.token_env)}
      oninput={(v) => set(i, "token_env", v)}
      onset={(val) => setSecret(s.token_env, val)}
      onclear={() => clearSecret(s.token_env)}
    />
  </fieldset>
{/each}
<button type="button" onclick={add}>+ přidat server</button>

<style>
  fieldset { border: 1px solid #2a2a30; border-radius: 6px; margin: 8px 0; padding: 8px 12px; }
  legend { color: #ccc; } legend button { color: #e05050; background: none; border: 0; cursor: pointer; }
  button { background: #1b1b1f; border: 1px solid #2a2a30; color: inherit; border-radius: 4px; padding: 4px 8px; cursor: pointer; }
  h2 { margin: 0 0 8px; }
</style>
```

(`$bindable()` lets `ConfigApp`'s `payload` update from the section's reassignments; `ConfigApp` passes `payload` with `bind:` — see Step 7.)

- [ ] **Step 7: Wire `bind:payload` in `ConfigApp.svelte`**

In `desktop/src/ConfigApp.svelte`, change the Servers render to bind the payload back:

```svelte
      {:else if active === "Servers"}
        <ServersSection bind:payload onChange={markDirty} onError={(m) => (notice = m)} />
```

(`payload` is `$state<ConfigPayload | null>`; the `{#if payload == null}` guard above already narrows it to non-null in this branch.)

- [ ] **Step 8: Build / type-check + full TS suite**

Run: `cd desktop && npm run build 2>&1 | tail -25 && npm test 2>&1 | tail -8`
Expected: build clean (no type errors); all Vitest green (deckClient, sidecar, configClient incl. server mutations).

- [ ] **Step 9: Commit**

```bash
git add desktop/src/lib/configClient.ts desktop/src/lib/configClient.test.ts desktop/src/lib/fields/TextField.svelte desktop/src/lib/fields/TokenSecretField.svelte desktop/src/lib/sections/ServersSection.svelte desktop/src/ConfigApp.svelte
git commit -m "feat(desktop): Servers section + Text/TokenSecret field widgets + server mutations"
```

---

## Self-Review

**1. Spec coverage (řez 3 scope from `2026-06-25-config-editor-frontend-design.md`):**
- `http.rs` POST-JSON + DELETE helpers → Task 1. ✓
- 6 Rust proxy cmds (`config_read/validate/write/set_active/secret_set/secret_clear`) + token injection → Task 2. ✓
- 2nd `"config"` window + `open_config` + tray Settings + capabilities → Task 2. ✓
- `main.ts` root-by-label → Task 5. ✓
- `configClient.ts` complete pure logic (parse/serialize/override/clear/secret-presence/validate shaping) + unit tests → Tasks 3, 4, 6. ✓
- `ConfigApp.svelte` skeleton (switcher + sidebar + save bar + preview reuse) → Task 5. ✓
- One representative section (Servers, covering list-of-records + TokenSecret) → Task 6. ✓
- Global Apply save model + dirty + errors → Task 5 (`apply`/`discard`/`dirty`/`errors`). ✓
- Token never in JS; configClient framework-free (injected invoke); secret values one-way → Global Constraints, enforced in Tasks 2/3/6. ✓
- DEFERRED to řez 4 (correctly out of scope here): 9 other sections, `OverrideField` overlay UX, klik-to-jump preview, onboarding empty-state polish, error banners/toasts, profile create/delete + set_active wiring in the UI. Noted in the section-area fallback (`Sekce „{active}" — řez 4.`).

**2. Placeholder scan:** every code step shows complete code. The two "soft" notes (Task 5 stub→Task 6 fill of `ServersSection`; the `await import` top-level-await caveat in `main.ts`) are explicit, with the fallback spelled out — not "TBD".

**3. Type consistency:** `ConfigPayload`/`WriteBody`/`SecretFlag`/`ServerRecord`/`InvokeFn`/`ConfigTransport` names and shapes are identical across Tasks 3–6 and the Svelte consumers. Command names (`config_read`/`config_validate`/`config_write`/`config_set_active`/`config_secret_set`/`config_secret_clear`/`open_config`) match between the Rust `invoke_handler` (Task 2) and `commandTransport` (Task 3). `setSecret`/`clearSecret` return `number` (HTTP status) in both the transport interface and the Rust commands.
