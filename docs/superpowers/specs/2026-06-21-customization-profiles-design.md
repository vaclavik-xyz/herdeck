# Customization profiles - design

- **Date:** 2026-06-21
- **Status:** Approved (brainstorming)

## Goal

Add a full customization layer for Herdeck: portable user profiles, runtime
profile switching from the deck, configurable view/theme/macro/launcher/safety
and notification presets, and a clean settings core that can later power a web
GUI or desktop shell without rewriting the runtime.

## Product direction

Herdeck should stay a fast control surface, not become a full settings editor on
the device. The deck can switch already-defined modes, profiles, and quick
toggles. Detailed editing belongs in a future GUI.

The first implementation is app-ready core without GUI:

- New shareable config schema with named profiles and reusable preset blocks.
- Device-local overrides in a separate `local.toml`.
- Runtime profile switching from the deck.
- Optional management-row view for users who want management actions visible on
  the first overview page.
- A Python settings service boundary for future GUI and desktop app packaging.

Electron, Tauri, a browser settings editor, cloud sync, and import/export
wizards are out of scope for this implementation.

## Config files

Use a two-layer config model:

1. **Shareable config**: default `~/.config/herdeck/config.toml`.
   This is safe to sync or commit. It contains profiles, preset definitions,
   server ids/URLs, macro definitions, launcher definitions, themes, views,
   notification policies, and safety policies. It never contains secret values.
2. **Device-local override**: default `~/.config/herdeck/local.toml`.
   This is not meant to be shared. It stores active profile preference,
   startup-only device settings, local socket path, bind/port choices, local
   icon directories, and env-var names for secrets.

Environment variables remain supported:

- `HERDECK_CONFIG` points to the shareable config file.
- `HERDECK_LOCAL_CONFIG` points to the local override file.
- `HERDECK_PROFILE` selects the active profile for this process.
- Existing runtime env vars (`HERDECK_DECK`, `HERDECK_WEB_BIND`,
  `HERDECK_WEB_PORT`, `HERDR_SOCKET`, bridge token env vars) continue to work.

Precedence:

```text
built-in defaults
  < shareable config blocks
  < selected profile and inherited profiles
  < local.toml overrides
  < environment overrides
```

If `HERDECK_PROFILE` is set, active profile is env-locked for that process.
Switching profiles from the deck should show a clear "profile locked" result
and must not persist a different active profile to `local.toml`.

## Shareable schema

The top-level config defines named reusable blocks. Profiles compose them.

```toml
active_profile = "work"

[[servers]]
id = "workbox"
url = "ws://100.x.y.z:8788"
token_env = "HERDECK_WORKBOX_TOKEN"

[profiles.base]
theme = "default"
view = "dense"
notifications = "normal"
safety = "standard"
macros = "default"
launcher = "default"
servers = ["workbox"]

[profiles.work]
extends = "base"

[profiles.mobile]
extends = "work"
theme = "high_contrast"
view = "management"
notifications = "phone"

[themes.default.colors]
working = "green"
idle = "blue"
blocked = "amber"
done = "dim"
unknown = "grey"
offline = "red"

[views.dense]
management = "launcher_menu"
show_profile_on_panel = true
agent_slots = "max"

[views.management]
management = "bottom_row"
bottom_row = ["profiles", "notifications", "safety", "theme", "new_agent"]
show_profile_on_panel = true

[[macro_sets.default]]
label = "continue"
text = "continue"

[launchers.default]
claude = ["claude"]
codex = ["codex"]
cursor = ["cursor-agent"]
gemini = ["gemini"]
opencode = ["opencode"]

[notification_profiles.normal]
enabled = true
backends = ["macos"]
on = ["blocked"]
sound = true

[notification_profiles.phone]
enabled = true
backends = ["telegram"]
on = ["blocked", "offline"]
sound = false

[safety.standard]
approve_always = true
require_confirm_for = []

[safety.safe]
approve_always = false
require_confirm_for = ["act_force", "approve_always"]
```

`local.toml` stores device-specific values and active-profile preference:

```toml
active_profile = "mobile"

[local]
deck = "web"
herdr_socket = "~/.config/herdr/herdr.sock"
web_bind = "127.0.0.1"
web_port = 8800
icons_dir = "~/.config/herdeck/icons"

[hardware]
brightness = 80
debounce = 0.25
keep_alive_interval = 5.0
tick_interval = 0.4
```

## Profile inheritance

Profiles support single inheritance via `extends`.

Rules:

- A profile may extend one other profile.
- Missing fields inherit from the parent.
- Child fields replace parent scalar/list references.
- Preset blocks are not deep-merged through profile inheritance; the profile
  chooses named blocks such as `theme = "high_contrast"`.
- Cycles are config errors with the full profile chain in the error message.
- Unknown block references are config errors.
- Unknown active profile is a config error unless a legacy config is being used.

## Legacy compatibility

Existing configs remain loadable. If no `[profiles.*]` section exists, the
loader treats the file as legacy and maps it into a synthetic profile named
`default`:

- `servers` map through unchanged.
- `[deck].grid` and `[deck].overview_order` map to runtime grid/order.
- `[answer_profiles.*]` merge over built-in answer profiles as today.
- `[[macros]]` becomes `macro_sets.default`.
- `[start_profiles]` becomes `launchers.default`.
- `[notifications]` becomes `notification_profiles.default`.

The shipped `config.example.toml` should move to the new schema. Tests must keep
legacy config support covered so existing users are not broken.

## Runtime model

Add a resolver layer that produces the current runtime `Config` plus the active
customization metadata:

- Raw config dataclasses represent the new TOML schema.
- A resolver loads shareable config, local override, defaults, and env overrides.
- The resolver returns:
  - the existing runtime `Config` shape needed by the current app,
  - active profile name,
  - active view/theme/safety metadata,
  - startup-only local settings,
  - an `env_locked_profile` flag.

Runtime profile switching:

1. User opens the profile menu from the deck.
2. User selects a profile.
3. The app resolves that profile against the current config files.
4. View/theme/macros/launcher/notification/safety changes apply immediately.
5. Server set changes are diffed:
   - unchanged server ids keep their connectors,
   - removed server ids disconnect,
   - added server ids connect,
   - changed URL/token server definitions reconnect.
6. If not env-locked, the selected profile is persisted to `local.toml`.
7. The deck re-renders overview with the active profile visible on the panel
   when the active view requests it.

Startup-only settings such as deck kind, web bind/port, hardware brightness,
and local socket path are resolved at startup. If a runtime profile switch
changes only startup-only values, the app should not attempt a risky partial
restart. It should surface a short `restart required` status on the panel.

## Deck UX

Default view remains agent-first.

- Overview shows agents and keeps the final tile as `+ New`.
- `+ New` opens the launcher menu.
- Launcher includes agent types and a `Profiles` entry.
- `Profiles` opens a profile menu.
- Profile menu shows configured profile names as tiles.
- Active profile is visually marked.
- Selecting a profile switches immediately and returns to overview.
- Panel may show active profile, e.g. `work - 2/3`.

Optional management row:

```toml
[views.management]
management = "bottom_row"
bottom_row = ["profiles", "notifications", "safety", "theme", "new_agent"]
```

When enabled, the bottom row of the first overview page becomes management
actions. This costs agent slots, so it is not the default. Users who prefer a
control-panel style can opt in.

The deck does not edit full config values. It only switches named profiles or
small runtime toggles represented by existing config blocks.

## Customization surfaces

The first implementation covers these user-facing customization surfaces:

- **Profiles**: named whole-environment presets.
- **View**: management placement, profile label on panel, agent slot policy.
- **Theme**: status colors, offline color, server accent palette, basic high
  contrast option.
- **Tile content**: choose which fields appear when available: repo, branch,
  status, elapsed time, server tag.
- **Icons**: configurable Simple Icons slug map and local icon override
  directory.
- **Notifications**: named notification policies, including macOS and Telegram
  backends.
- **Macros**: named macro sets for non-blocked drill-in actions.
- **Launcher**: named launcher command sets.
- **Safety**: policy for `approve_always`, forced stop, and confirmation for
  destructive actions.
- **Hardware/runtime**: local startup/runtime knobs where safe: brightness,
  debounce, keep-alive interval, tick interval, web bind/port, deck kind.

Safety defaults remain conservative. Existing guarded `act_if_blocked` behavior
must not be weakened.

## App-ready settings boundary

Add a Python service layer that is useful now and can later be exposed through a
web GUI:

- `load_settings()` reads files and returns a structured settings snapshot.
- `resolve_profile(name)` returns the runtime config and metadata for a profile.
- `list_profiles()` returns profile names plus active/locked status.
- `set_active_profile(name, persist=True)` writes `local.toml` unless env-locked.
- `validate_settings()` returns user-facing validation errors without exposing
  secret values.

No HTTP server is needed in this first implementation. A future GUI can call the
same service through a local API or a desktop sidecar.

## Testing

Use TDD per task. Required coverage:

- Config parser/resolver:
  - new schema parses,
  - legacy config maps to a default profile,
  - profile inheritance works,
  - inheritance cycles fail clearly,
  - unknown block references fail clearly,
  - `local.toml` overrides active profile and local settings,
  - `HERDECK_PROFILE` locks the active profile.
- Runtime:
  - profile switch updates view/theme/macros/launcher without restart,
  - profile switch persists to `local.toml` when allowed,
  - env-locked switch does not persist and returns a locked status,
  - connector diff keeps unchanged servers, adds new servers, and removes old
    servers.
- Orchestrator:
  - launcher exposes `Profiles`,
  - profile menu lists profiles and marks the active one,
  - selecting a profile emits a profile-switch command,
  - management row view uses configured management actions,
  - default view remains agent-first.
- Rendering:
  - theme colors flow to `TileView`/panel rendering,
  - tile content preferences hide/show repo, branch, status, time, and server tag.
- Notifications/safety:
  - notification profile selection affects notifier construction,
  - safety policy disables or confirms risky actions.
- Docs:
  - new `config.example.toml` parses,
  - legacy README snippets remain accurate or are replaced.

## Out of scope

- Electron/Tauri packaging.
- Browser settings GUI.
- Editing config values from the deck.
- Cloud sync or account-backed profile sync.
- Import/export wizard.
- Automatic migration command that rewrites legacy config files.

