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

Expose a browser-visible persOS handoff route on the public cockpit origin. That
route authenticates the persOS user, calls Herdeck privately with the persistent
credential, and forwards Herdeck's `Set-Cookie` header unchanged in its response
to the browser. These are two separate trust-boundary requests.

The browser calls the public persOS route using only its normal persOS session
and persOS CSRF protection. It never receives or sends the Herdeck credential:

```http
POST /api/cockpit/herdeck/session HTTP/1.1
Cookie: persos_session=<persOS session>
X-CSRF-Token: <persOS CSRF token>
```

After validating that request, the persOS backend calls the private Herdeck
route over Tailscale:

```http
POST /herdeck/api/v1/browser-sessions HTTP/1.1
X-Herdeck-Token: <persistent credential>
```

The private Herdeck `201` response alone cannot set a cookie in the user's
browser: the persOS backend must relay `Set-Cookie` through its browser-facing
response on `https://persos.example`. The relayed response sets an opaque
`HttpOnly` cookie scoped to `/herdeck/` and returns its lifetime as `expires_in`.
Neither backend may copy the cookie into a JSON body or log it. After that
browser-visible handoff completes, the browser may load the clean iframe URL
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

Without a configured base path, routes live at the server root. With
`HERDECK_WEB_BASE_PATH=/herdeck`, they live exclusively below `/herdeck`; the
unprefixed forms return 404. API responses use `application/json`,
`Cache-Control: no-store`, and include
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

If operator safety configuration also requires confirmation for `approve` or
`deny`, those actions use the same caller-, action-, and target-bound challenge
protocol. A confirmation can never cross actions or transports.

### Send text

`POST /api/v1/text` accepts the same stable target, `idempotency_key`, and a
`text` string. Text must be non-empty valid UTF-8, at most 4096 encoded bytes,
and single-line. C0 controls, DEL, tabs, carriage returns, and newlines are
rejected; input is never silently truncated. The endpoint cannot encode key
sequences, commands, or action variants.

### Blocked decisions

`POST /api/v1/decisions` accepts only `server_id`, `pane_id`, and
`terminal_id`. For a currently blocked agent it reads the current prompt and
returns at most 12 bounded numbered choices as `choices` records containing
only `key` and `label`. It never returns the question, terminal frame, or any
other prompt text. A non-blocked agent returns `outcome: "not_blocked"` with an
empty list.

`POST /api/v1/choices` accepts the same stable target, an `idempotency_key`,
and the numeric `choice` key. Herdeck reads and parses the current prompt again
before submitting the choice. If the agent is no longer blocked or the option
is no longer present, the request fails closed with `not_blocked` or
`stale_choice`; arbitrary text and stale menu keys are never submitted through
this endpoint.

### Outcomes and compatibility

Action responses use the stable `outcome` values `sent`, `skipped`,
`confirmation_required`, `confirmation_expired`, `stale_identity`,
`unavailable_target`, `not_blocked`, `stale_choice`, `timeout`, and
`backend_failure`. Validation errors use
`error.code: "validation_error"`; reusing an idempotency key for a different
request returns `idempotency_conflict`.
When all protected idempotency slots are occupied by unexpired requests, new
unique keys fail closed with HTTP 429 and `idempotency_capacity`; callers retry
after the oldest ten-minute window expires. Live results are never evicted
early.

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
handoff_headers=$(mktemp "${TMPDIR:-/tmp}/herdeck-handoff.XXXXXX")
chmod 600 "$handoff_headers"
trap 'rm -f "$handoff_headers"' EXIT HUP INT TERM
curl --fail --silent -X POST \
  -H "X-Herdeck-Token: $HERDECK_WEB_TOKEN" \
  -D "$handoff_headers" \
  "https://persos.example/herdeck/api/v1/browser-sessions"
grep -Eqi '^Set-Cookie: herdeck_session=[^;]+; Path=/herdeck/; HttpOnly;.*;[[:space:]]*Secure([;[:space:]]|$)' \
  "$handoff_headers"
```

Confirm `/api/v1/agents` outside `/herdeck` returns 404, the handoff response
cookie passed the attribute-only `grep` check, and ordinary output contains no
credential, browser-session value, prompt, or terminal content. Never print the
unredacted headers file.
