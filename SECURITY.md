# Security Policy

## Supported versions

herdeck is pre-1.0. Only the latest `main` receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities **privately** — do not open a public issue.

- **Preferred:** open a private advisory via GitHub Security Advisories
  ("Report a vulnerability" on the repository's **Security** tab).
- **Or** email `filip@vaclavik.xyz`.

Include a description, affected component, and steps to reproduce. You will get
an acknowledgement, and once a fix is available it will be released on `main`
with credit unless you prefer to remain anonymous.

## Threat model

herdeck runs a token-authenticated WebSocket bridge (`herdeck-bridge`) and, for
the desktop app, a loopback HTTP sidecar. The intended, supported deployment:

- The bridge binds to a **Tailscale / WireGuard interface only** (`HERDECK_BIND`),
  never `0.0.0.0` or a public IP. The transport is plain `ws://`, so the
  encrypted overlay provides confidentiality; a bearer token (constant-time
  compared) provides authentication. `herdeck-bridge` refuses to start on
  anything but loopback, `100.64.0.0/10` or a `*.ts.net` name unless
  `HERDECK_ALLOW_UNSAFE_BIND=1` is set explicitly.
- Rotate a bridge token with `herdeck-bridge --rotate-token` (it never prints
  the new token unless `--show` is given). An optional view-only token
  (`HERDECK_READONLY_TOKEN_FILE`) can watch the fleet — snapshots, icons, pane
  text, live previews, health — but every command that changes anything is
  rejected; give it to dashboards that should never press a key.
- `update` (bridge self-update) is a mutating message: it makes the bridge
  download and install a herdeck release into its own virtualenv and restart.
  It needs the full token (the read-only token is refused), is refused unless
  the bridge runs from a managed install (a `managed.json` marker at the root
  of the venv it runs from, herdeck installed inside that venv, not editable),
  and only takes a strict release version (`X.Y.Z` plus an optional
  `aN`/`bN`/`rcN`, `.postN`, `.devN`). The wheel comes from this
  repository's GitHub release over HTTPS and is installed only when its SHA-256
  matches the release's `SHA256SUMS`; a release without a wheel falls back to
  the `vX.Y.Z` git tag (TLS only, no checksum). No shell is involved and every
  step has a timeout; the bridge exits only after the new version verified.
  The full token therefore also authorizes installing any *published* herdeck
  release on the bridge host.
- The desktop sidecar binds to `127.0.0.1` only; its access token is injected by
  the Rust shell and is never exposed to the WebView / JavaScript.
- The browser simulator binds to loopback by default. For remote use, bind it to
  a trusted Tailscale interface only. Its URL token authorizes deck presses and
  live read-only terminal contents, so protect bookmarked/shared URLs as
  credentials; never bind the simulator to `0.0.0.0`, a public IP, or an
  untrusted LAN.
- Bridge and desktop-sidecar tokens live in the OS keychain or an environment
  variable. The browser simulator persists its bearer token in
  `~/.local/state/herdeck/web-token` with mode `0600`. Tokens are never stored
  in committed configuration.

Binding the bridge to a plain LAN or public interface is **outside** the
supported configuration: it exposes the bearer token and every forwarded
keystroke to passive network sniffing. The bridge exposes powerful primitives
(start an agent, send keystrokes), so treat its token as a full credential.
