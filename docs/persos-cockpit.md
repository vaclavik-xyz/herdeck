# persOS cockpit contract

Herdeck exposes a versioned semantic API for a same-origin persOS cockpit. The
supported browser topology is:

```text
browser -> https://persos.example/herdeck/* -> reverse proxy
        -> http://<tailscale-ip>:8800/herdeck/* -> herdeck-web
```

Keep the Herdeck listener bound to the host's Tailscale address, preserve the
`/herdeck` prefix, and expose it only through the cockpit's HTTPS origin. Do not
publish port 8800 to the public internet.

## Service and reverse proxy

Install the web runtime with an explicit private bind and public origin:

```sh
herdeck-service install web \
  --bind "$TAILSCALE_IP" \
  --port 8800 \
  --config ~/.config/herdeck/config.toml \
  --base-path /herdeck \
  --public-origin https://persos.example \
  --frame-ancestor https://persos.example
herdeck-service status web
```

The reverse proxy must strip no path components: `/herdeck/api/v1/agents` at
the public origin must reach the same path at Herdeck. Route the more-specific
`/herdeck/*` location before the persOS fallback. Forward the original `Host`
and scheme and disable caching for `/herdeck/api/*`.

`HERDECK_WEB_FRAME_ANCESTORS` is an allowlist for iframe ancestors, not an
authentication setting. Same-origin embedding is preferred. A cross-origin
ancestor is opt-in, requires HTTPS, changes the session cookie to
`SameSite=None; Secure`, and still requires exact `Origin` on browser writes.

## Credentials and browser sessions

The persistent web credential is stored in
`~/.local/state/herdeck/web-token` with mode `0600`. A persOS backend reads it
from its secret store and sends it only as `X-Herdeck-Token`. It must never put
the credential in an iframe URL, query string, redirect, browser storage, or
application log.

Mint a bounded browser session from the persOS backend:

```http
POST /herdeck/api/v1/browser-sessions HTTP/1.1
X-Herdeck-Token: <persistent credential>
```

The `201` response sets an opaque `HttpOnly` cookie scoped to `/herdeck/` and
returns its lifetime as `expires_in`. The body never contains the cookie or the
persistent credential. The browser may then load the clean iframe URL
`https://persos.example/herdeck/`.

On cockpit logout, revoke the current Herdeck session before removing the
iframe:

```http
DELETE /herdeck/api/v1/browser-sessions/current HTTP/1.1
Origin: https://persos.example
Cookie: herdeck_session=<opaque session>
```

Rotation procedure:

1. Replace the private web-token file atomically with a new mode-`0600` value.
2. Restart `dev.herdeck.web`.
3. Update the persOS secret and verify a new handoff.
4. Existing browser sessions are lost on restart and must be minted again.

The older `?token=` capability bootstrap and header-token automation remain
supported for compatibility, but new cockpit integrations use the handoff.

## API v1

All routes work both at the root and below the configured base path. API
responses use `application/json`, `Cache-Control: no-store`, and include
`api_version: "v1"`. Missing or invalid credentials return:

```json
{"api_version":"v1","error":{"code":"unauthorized","message":"missing or invalid credentials"}}
```

Server-to-server calls authenticate with `X-Herdeck-Token`. Browser writes
authenticate with the session cookie and must include the exact configured
`Origin`. Browser reads require the session cookie.

### Inventory

`GET /api/v1/agents` returns `agents` sorted by server and pane. Every record
contains stable `server_id`, `pane_id`, and `terminal_id`, normalized `status`,
`available`, bounded display metadata, and sanitized `work` metadata.

Stable v1 fields are:

- top level: `api_version`, `agents`
- identity/state: `server_id`, `pane_id`, `terminal_id`, `status`, `available`
- display: `agent_type`, `label`, `custom_status`, `repository`, `branch`,
  `project`, `workspace`, `tab`
- work: `source`, `item`, `run`, `url`

Prompt text, terminal frames, raw backend messages, credential values, and
unrecognized backend state labels are never included. Reconnects may keep an
agent record with `available: false`; the next authoritative snapshot replaces
the complete server slice, so removed panes and replaced terminal identities do
not remain as stale rows.

### Approve, deny, and stop

`POST /api/v1/actions` accepts only a semantic action and stable target:

```json
{
  "server_id": "local",
  "pane_id": "pane-1",
  "terminal_id": "terminal-1",
  "action": "approve",
  "idempotency_key": "persos-request-uuid"
}
```

`action` is `approve`, `deny`, or `stop`. Herdeck resolves the current target,
checks terminal identity, and executes through `RuntimeAgentControl` with the
configured answer profile. The API never accepts key names, forced approval,
approve-always, shell input, or arbitrary bridge messages.

Stop is a server-enforced two-step boundary. The first request returns HTTP 409
with `outcome: "confirmation_required"`, an opaque `confirmation`, and
`expires_in`; it sends no input. Repeat the target and action with that
confirmation and a new idempotency key.
The challenge is caller- and target-bound, expires after 60 seconds, is
single-use, and is invalidated by target state changes, reconnects, terminal
replacement, or configuration reload.

### Send text

`POST /api/v1/text` accepts the same stable target, `idempotency_key`, and a
`text` string. Text must be non-empty valid UTF-8, at most 4096 encoded bytes,
and single-line. C0 controls, DEL, tabs, carriage returns, and newlines are
rejected; input is never silently truncated. The endpoint cannot encode key
sequences, commands, or action variants.

### Outcomes and compatibility

Action responses use the stable `outcome` values `sent`, `skipped`,
`confirmation_required`, `confirmation_expired`, `stale_identity`,
`unavailable_target`, `timeout`, and `backend_failure`. Validation errors use
`error.code: "validation_error"`; reusing an idempotency key for a different
request returns `idempotency_conflict`.

Clients must ignore unknown response fields. New optional fields and new error
details may be added compatibly within v1. Existing fields do not change meaning
or type within v1; removals or semantic changes require `/api/v2`.

## Smoke checks

From a private host with the credential loaded without printing it:

```sh
curl --fail --silent "https://persos.example/herdeck/healthz"
curl --fail --silent \
  -H "X-Herdeck-Token: $HERDECK_WEB_TOKEN" \
  "https://persos.example/herdeck/api/v1/agents"
curl --fail --silent -X POST \
  -H "X-Herdeck-Token: $HERDECK_WEB_TOKEN" \
  -D /tmp/herdeck-handoff.headers \
  "https://persos.example/herdeck/api/v1/browser-sessions"
```

Confirm `/api/v1/agents` outside `/herdeck` returns 404, the handoff response
contains `Path=/herdeck/; HttpOnly; Secure`, and ordinary output contains no
credential, browser-session value, prompt, or terminal content.
