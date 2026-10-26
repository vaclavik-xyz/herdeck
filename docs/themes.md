# Deck themes

A deck theme decides how the deck **draws** its keys and its status window.
Everything else (which agents are shown, in what order, what a press does,
favicons, agent marks, the animation clock, the D200 / Elgato / desktop
drivers) is shared by every theme. Switching themes is an ordinary setting:

```toml
[view]
deck_theme = "classic"   # the default
```

In the desktop editor it is **Settings → View → Deck look** (`deck_theme`;
"Vzhled decku" in Czech). It is not `[theme.colors]`: the status colours stay
yours whatever the look, and every theme draws with them.

Built-in themes ship with herdeck (`classic` is today's look and the default).
You can also write your own: one Python file, described below.

> **A theme is code.** A user theme is a Python module that herdeck imports
> and runs with **your full user privileges**, exactly like any script you run.
> herdeck only loads themes you put in your themes folder yourself; it never
> downloads one. Read a theme before you install it.

## Install a user theme

1. Put the file in the `themes/` folder next to your `config.toml`
   (`~/.config/herdeck/themes/` by default; next to `$HERDECK_CONFIG` when you
   set it; or wherever `$HERDECK_THEMES_DIR` points). The **file name is the
   theme id**: `themes/minimal.py` is `deck_theme = "minimal"`. Ids are
   `[a-z0-9_-]+`; a file named like a built-in (`classic`) cannot shadow it: it
   is ignored and listed under **Not loaded**. A file whose name falls outside
   that set (say `MyTheme.py`) is skipped silently, without any message.
2. Set `[view].deck_theme` to the id (or pick it in the editor, which lists
   every theme that loaded; broken ones are reported under **Not loaded**).
3. Edit freely: the folder is rescanned every couple of seconds and the deck
   redraws when the file changes.

A file is rejected (and `classic` draws instead) when it fails to import,
has no `THEME` instance of a `Theme` subclass, declares an `api_version` other
than `1`, has an `id` that differs from its file stem, has a `name` that is not
a dict of language -> text, or a `base` that is neither a theme id nor `None`.
Such a file is shown in the editor under
**Not loaded** with the error, logged once, and the deck draws `classic`
instead. An unknown id draws `classic` too.

## A minimal theme

[`docs/themes/minimal.py`](themes/minimal.py) is a complete theme (the test
suite loads it, so it always works):

```python
class Minimal(Theme):
    api_version = 1
    id = "minimal"  # must match the file name
    name = {"en": "Minimal", "cs": "Minimální"}
    base = "classic"

    def agent_tile(self, tile, ctx, frame):
        if tile.kind != "agent":
            return None  # e.g. an Elgato action key: let the base draw it
        bg = ctx.status_color(tile.status)
        img = Image.new("RGB", (ctx.TILE, ctx.TILE), bg)
        draw = ImageDraw.Draw(img)
        ink = ctx.ink_for(bg)
        mark = ctx.project_mark(tile, 56)
        img.paste(mark, (14, 14), mark)
        text = tile.title or tile.workspace or tile.repo or tile.label
        font = ctx.font(26)
        y = 86
        for line in ctx.wrap(draw, text, font, ctx.TILE - 28, max_lines=3):
            draw.text((14, y), line, font=font, fill=ink)
            y += 32
        return img


THEME = Minimal()
```

It redraws agent tiles only. Every other key and the status window come from
its `base`, so a theme can start with one part and grow.

## The contract (`herdeck.themes.api`, `API_VERSION = 1`)

Import only from `herdeck.themes.api` (`Theme`, `ThemeContext`, `TileView`,
`PanelView`, `PanelAgent`, `API_VERSION`, `TILE`, `PANEL_H`) and Pillow.
Everything in `herdeck.icons` and the other modules is private and may change
in any release; `API_VERSION` changes only when this contract breaks.

The module must define `THEME`, an instance of a `Theme` subclass:

| attribute | meaning |
|---|---|
| `api_version` | the API the theme was written for: `1` |
| `id` | the theme id; must equal the file name |
| `name` | display names, `{"en": ..., "cs": ...}` |
| `base` | the theme that draws what this one does not (default `"classic"`); the chain always ends at `classic` |
| `animated` | `False` (default): `frame` is always `None` and a working tile is ONE cached image. `True`: you get the animation phase and every phase is its own image (on the D200 each is a full page reload) |

`name` must be a dict mapping language codes to text, and `base` must be a
theme id or `None`. With `animated = False` a tile gets one cache entry, so
the working-tile animation also freezes for tiles your `base` (classic) draws
on your behalf. Set `animated = True` only if your frames really differ.

Three parts, each optional. Return a Pillow image, or `None` to let `base`
draw that part:

- `agent_tile(tile, ctx, frame)` is called for every tile whose `repo` is set:
  the agent tiles, and the Elgato plugin's action keys (`tile.kind ==
  "control"`, with the target's name in `repo`). `frame` is the working
  animation phase (only when `animated = True`).
- `key_tile(tile, ctx)` draws every other key (`tile.kind` says which).
- `panel(panel, ctx, width)` draws the status window.

Sizes: tiles are `TILE` × `TILE` (196 × 196); the panel is `width` × `PANEL_H`
(196 high). `width` is 458 on the D200 (its native small window) and 392 on
the web simulator, the desktop window and Elgato decks; Elgato cuts the panel
into two keys at x = 196, so keep anything critical off that seam. RGB or
RGBA; a wrong size is resized (and logged once).

Rules:

- **Stateless.** Parts run on several render threads at once. Keep no state
  between calls; draw only from the view you are given.
- **Deterministic.** The output is cached by the view's content (a key is
  redrawn only when something it shows changes), so never read the clock,
  random numbers or files while drawing.
- **Readable on hardware.** A D200 key is ~25 mm and ~72 px physically: text
  below ~22 px (in the 196 px space) is unreadable there.
- **Failures are contained.** If a part raises, its `base` draws it and the
  error is logged once per theme and part.

### What a tile tells you (`TileView`)

| field | meaning |
|---|---|
| `kind` | `agent`, `option` (a drill answer: `label` = its number, `subtext` = its text), `control` (Back, Stop, Pin, Elgato action keys), `launcher` ("+ New"), `empty`, `idle_group` ("+N idle"), `pinned_hole` (a pinned agent that is gone), `menu` (profile / launcher / bottom-row entries) |
| `status` | agent tiles: `working`, `idle`, `blocked`, `done`, `waiting`, `unknown` or `offline` (server down) |
| `label`, `subtext` | the key's text (non-agent keys) |
| `title`, `workspace`, `repo`, `branch` | the agent's task title, herdr workspace, configured first/second tile line |
| `status_text`, `time_text` | the localized status word and the time in that status ("4m") |
| `wait_reason` | what holds a waiting agent ("CI", "REVIEW +1"); empty when the user hides the time line |
| `agent_type`, `project_icon`, `project_name` | for `ctx.agent_mark` / `ctx.project_mark` |
| `subagents`, `pinned`, `server_tag` | running subagents, pinned, backend label |
| `index`, `spinner` | the key position; the rotation phase of a working tile (`None` otherwise) |
| `color` | the colour name the user configured for this tile's state |
| `tile_fill`, `tile_icon`, `working_animation` | the user's classic look settings (use them or not) |

### What the status window tells you (`PanelView`)

| field | meaning |
|---|---|
| `kind` | `calm` (all clear), `spotlight` (someone needs you), `offline` (servers down), `config_error`, `detail` (a drilled agent's prompt), `usage` (limits detail), `menu` (launcher / profiles), `sent` (an answer just went out) |
| `queue` | blocked agents, longest-waiting first: `PanelAgent(label, title, elapsed, agent_type, project_icon, project_name)` |
| `counts` | overview panels: `blocked`, `working`, `idle`, `done`, `waiting`, `offline` |
| `title`, `headline`, `lines`, `meta`, `hint`, `note`, `aside`, `sent` | the localized texts classic draws (chip, big line, body, header right, footer) |
| `stats`, `gauges`, `page` | count cards, usage gauges, (page, pages) |
| `PanelStat` | `label`, `value` (int), `color` (colour name) |
| `PanelGauge` | `label`, `window`, `used_percent` (int), `hint`, `color`, `pace` (projection text, `""` = none) |
| `color`, `solid`, `chip_dot` | the tone colour name; fill the whole panel; the chip's status dot |

### Helpers (`ThemeContext`)

| helper | returns |
|---|---|
| `ctx.TILE`, `ctx.PANEL_H` | 196, 196 |
| `ctx.language` | `"en"` or `"cs"` (`[view].language`) |
| `ctx.tr(key, **fmt)` | a deck string from herdeck's catalog in that language |
| `ctx.font(size, bold=True)` | the bundled Inter font (the same on every OS) |
| `ctx.fit_text(draw, text, max_w, largest, smallest)` | `(font, text)`: the largest size that fits; cut with "…" at `smallest` |
| `ctx.wrap(draw, text, font, max_w, max_lines=2)` | wrapped lines; a cut-off tail ends with "…" |
| `ctx.ink_for(rgb)` | black or white, whichever reads better on `rgb` |
| `ctx.readable(rgb, bg)` | `rgb` lightened just enough to read on a dark `bg` |
| `ctx.color(name)` | RGB of a `[theme.colors]` key (`blocked`, ...) or a palette name (`amber`, ...) |
| `ctx.status_color(status)` | RGB of `tile.status`, honouring the user's colours |
| `ctx.project_mark(item, size)` | RGBA favicon of a `TileView` / `PanelAgent`; without one, a coloured monogram, or the agent's mark when `[view].project_icon_fallback = "agent"` |
| `ctx.agent_mark(agent_type, size, dark=False)` | RGBA agent logo; `dark` for bright backgrounds. It is a shared cached image: never draw on it in place, `.copy()` it first |

In the packaged desktop app and the Stream Deck plugin a theme can only import
modules bundled into the frozen app (stdlib modules such as `colorsys` may be
missing), so keep to Pillow and `herdeck.themes.api`.

## Trying a theme without a deck

The web simulator (`herdeck-web`) and the desktop window draw with the same
themes as the hardware, so the quickest loop is: save the file, watch the
simulator redraw. To render single images, use `herdeck.icons` (with your theme installed, or `HERDECK_THEMES_DIR` pointing at its folder):

```python
from herdeck.driver.base import PanelView, TileView
from herdeck.icons import IconProvider, compose_panel

icons = IconProvider(cache_dir="/tmp/herdeck-theme-try", slug_map={})
tile = TileView(0, "api", "amber", agent_type="claude", repo="api",
                status="blocked", title="approve rm -rf build", deck_theme="minimal")
open("/tmp/tile.png", "wb").write(icons.render_tile_bytes(tile))
compose_panel(PanelView("Needs you", deck_theme="minimal"), 458, icons).save("/tmp/panel.png")
```

## Built-in themes

| id | Draws | Everything else from |
|---|---|---|
| `classic` | every key and the status window (the original look, default) | none |
| `signal` | every key and every status-window kind | `classic` (kinds added later) |
| `headline` | agent tiles | `classic` |
| `quiet` | agent tiles | `signal` |

An unknown or broken theme id draws `classic`.

### `signal` (Signal / Signál)

A dark key per agent: the project's mark (its favicon, or a monogram; with
`[view].project_icon_fallback = "agent"` the agent's own logo instead of the
monogram) and under it the herdr task title (falling back to the workspace,
then the repo). The status is shown by colour AND by the frame's shape, so it
reads without colour vision and on the D200's 72 px keys:

- working: a frame open at the top-right corner
- idle: a thin, dimmed frame (dimmed text)
- done: a full frame with a dot (finished, unseen)
- waiting: a dashed frame; the holder label (CI, REVIEW) top-right, the
  waiting time top-left when both fit
- blocked: the whole key in the status colour, the mark on a white plate,
  the elapsed time in a pill
- offline: a full frame, the mark greyed and slashed
- unknown: a thin grey frame, dimmed like idle

A tile with no project data (no favicon, no repo) shows the agent's own mark.

Time shows only on blocked and waiting; there is no branch and no backend
tag; running subagents are a small `+N` pill. Drill choices are a big digit
with the choice text wrapped on the choice's colour; Back, Stop, Pin and menu
entries are dark keys with a coloured outline; "+ New" is a dashed empty slot
with a plus; the New-agent menu shows each agent's mark.

The status window: when agents wait for you it turns amber with the
longest-waiting agent's mark on a plate, its name and wait, the rest of the
queue as favicon chips with their ages and, during a partial outage, the
offline machine as a red chip. A calm fleet is a row of ring counters drawn in
the tiles' shapes plus the fullest usage window per provider. Offline, config
error and "sent" put a big glyph on the left with the words beside it; an
agent's detail shows its prompt with a status chip. At 392 px (two Elgato
keys) every word stays inside one key.

### `headline` (Headline / Titulek)

The project is the headline: the whole key is the status colour, the name line
(the project name, not the task title) is big at the bottom, the time in the
current status top-right and the project mark small top-left. A status word
appears only where it is news (blocked, waiting's holder label, offline).
Only agent tiles are drawn; other keys and the status window are `classic`.

### `quiet` (Quiet / Klid)

Only a blocked agent is loud: the whole key in the status colour with a big
wait time. A done agent is an outlined key that says DONE, an offline one a
dark red key; everything else is dark with a colour bar on the left, with the task
title as text like `signal`. Only agent tiles are drawn; other keys and the
status window are `signal`.

All built-ins keep every text at 22 px or more (in the 196 px key image) and
take their colours from `[theme.colors]`.
