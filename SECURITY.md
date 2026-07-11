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
  compared) provides authentication.
- The desktop sidecar binds to `127.0.0.1` only; its access token is injected by
  the Rust shell and is never exposed to the WebView / JavaScript.
- The browser simulator binds to loopback by default. For remote use, bind it to
  a trusted Tailscale interface only. Its URL token authorizes deck presses and
  live read-only terminal contents, so protect bookmarked/shared URLs as
  credentials; never bind the simulator to `0.0.0.0`, a public IP, or an
  untrusted LAN.
- Tokens live in the OS keychain or an environment variable, never in committed
  configuration.

Binding the bridge to a plain LAN or public interface is **outside** the
supported configuration: it exposes the bearer token and every forwarded
keystroke to passive network sniffing. The bridge exposes powerful primitives
(start an agent, send keystrokes), so treat its token as a full credential.
