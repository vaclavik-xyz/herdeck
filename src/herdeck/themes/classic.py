"""The classic deck look — the drawing code herdeck shipped before themes,
moved here verbatim (same draw calls, same order; pixel-identical, pinned by
tests/test_render_golden.py).

``IconProvider`` keeps the infrastructure these functions draw with (render
caches, glyph/logo/project layers, the comet overlay); they take it as their
first argument (``icons``). Built-in themes may import from here; user themes
must use ``herdeck.themes.api`` only.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from ..driver.base import COLORS, PanelView
from ..icons import (
    _BASE_CACHE_MAX,
    _P_BG,
    _P_CARD,
    _P_CHIP_H,
    _P_DOT_DIM,
    _P_FOOT_H,
    _P_INK,
    _P_MUTE,
    _P_NOTE,
    _P_PAD_X,
    _P_PAD_Y,
    _PANEL_SS,
    DARK_INK,
    ICON_SIZE,
    LIGHT_INK,
    PANEL_H,
    PANEL_W,
    PROJECT_BADGE,
    PROJECT_BADGE_XY,
    PROJECT_RADIUS,
    SPIN_DEG,
    SPINNER_FRAMES,
    STATUS_MAX_W,
    TILE_BG,
    _effective_animation,
    _fit_branch,
    _fit_font,
    _fit_project_name,
    _font,
    _ink_for,
    _mix,
    _panel_wrapped,
    _pulse_size,
    _soften,
    _tile_text_colors,
    _tint_bg,
    _truncate,
    _wrap,
)
from .api import API_VERSION, Theme


def _panel_palette(panel: PanelView) -> dict:
    """Background / ink / muted / chip colours for the panel's tone."""
    if panel.solid:
        bg = COLORS.get(panel.color, COLORS["grey"])
        dark_ink = _ink_for(bg) == (0, 0, 0)
        ink = _mix(bg, (0, 0, 0), 0.88) if dark_ink else _mix((255, 255, 255), bg, 0.06)
        mute = _mix(bg, ink, 0.72)
        return {"bg": bg, "ink": ink, "mute": mute, "chip": ink, "chip_ink": bg,
                "dim": _mix(bg, ink, 0.35), "note": ink, "aside": ink}
    chip = _P_CARD if panel.color == "grey" else COLORS.get(panel.color, _P_CARD)
    return {"bg": _P_BG, "ink": _P_INK, "mute": _P_MUTE, "chip": chip,
            "chip_ink": _P_INK if chip == _P_CARD else _ink_for(chip),
            "dim": _P_DOT_DIM, "note": _P_NOTE, "aside": COLORS["amber"]}


def _spaced_len(draw, text, font, spacing) -> float:
    return draw.textlength(text, font=font) + spacing * max(0, len(text) - 1)


def _draw_spaced(draw, xy, text, font, fill, spacing) -> None:
    """Letter-spaced text (the uppercase chip and card labels)."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing


def _truncate_spaced(draw, text, font, max_w, spacing) -> str:
    if _spaced_len(draw, text, font, spacing) <= max_w:
        return text
    while text and _spaced_len(draw, text + "…", font, spacing) > max_w:
        text = text[:-1]
    return text + "…" if text else ""


_ink_mid_cache: dict[tuple[int, str], float] = {}


def _ink_mid(font, sample: str = "Hg") -> float:
    """The vertical middle of ``sample``'s ink box (fonts are cached forever,
    so their id is a stable key; measuring per call doubled the panel's
    text-raster cost)."""
    key = (id(font), sample)
    if key not in _ink_mid_cache:
        top, bottom = font.getbbox(sample)[1::2]
        _ink_mid_cache[key] = (top + bottom) / 2
    return _ink_mid_cache[key]


def _text_mid(draw, x, cy, text, font, fill) -> None:
    """Draw ``text`` with its ink box vertically centred on ``cy``."""
    draw.text((x, cy - _ink_mid(font)), text, font=font, fill=fill)


def _draw_check(draw, x, cy, size, fill, width) -> None:
    pts = [(x, cy), (x + size * 0.36, cy + size * 0.36), (x + size, cy - size * 0.42)]
    draw.line(pts, fill=fill, width=width, joint="curve")


def _panel_header(draw, panel, pal, w, k) -> None:
    cy = (_P_PAD_Y + _P_CHIP_H / 2) * k
    left, right = _P_PAD_X * k, w - _P_PAD_X * k
    meta_w = 0
    if panel.meta:
        font = _font(15 * k)
        meta = _truncate(draw, panel.meta, font, (w - 2 * _P_PAD_X * k) * 0.45)
        meta_w = draw.textlength(meta, font=font)
        _text_mid(draw, right - meta_w, cy, meta, font, pal["ink"] if panel.solid else pal["mute"])
    room = right - left - meta_w - 14 * k
    if panel.sent:
        font = _font(16 * k)
        _draw_check(draw, left + 2 * k, cy, 15 * k, pal["ink"], 3 * k)
        x = left + 25 * k
        _text_mid(draw, x, cy, _truncate(draw, panel.sent, font, room - 25 * k), font, pal["ink"])
        return
    if not panel.title:
        return
    font, spacing = _font(13 * k), 1.2 * k
    pad = 11 * k
    dot = 8 * k if panel.chip_dot and not panel.solid else 0
    inner = room - 2 * pad - (dot + 7 * k if dot else 0)
    text = _truncate_spaced(draw, panel.title.upper(), font, inner, spacing)
    chip_w = 2 * pad + _spaced_len(draw, text, font, spacing) + (dot + 7 * k if dot else 0)
    top = _P_PAD_Y * k
    draw.rounded_rectangle(
        (left, top, left + chip_w, top + _P_CHIP_H * k), radius=_P_CHIP_H * k / 2, fill=pal["chip"]
    )
    x = left + pad
    if dot:
        draw.ellipse((x, cy - dot / 2, x + dot, cy + dot / 2), fill=COLORS.get(panel.chip_dot, _P_INK))
        x += dot + 7 * k
    _draw_spaced(draw, (x, cy - _ink_mid(font, "H")), text, font, pal["chip_ink"], spacing)


def _panel_footer(draw, panel, pal, w, h, k) -> None:
    cy = h - (_P_PAD_Y + _P_FOOT_H / 2) * k
    left, right = _P_PAD_X * k, w - _P_PAD_X * k
    font = _font(14 * k)
    right_w = 0
    if panel.aside:
        aside = _truncate(draw, panel.aside, font, (right - left) * 0.45)
        right_w = draw.textlength(aside, font=font)
        _text_mid(draw, right - right_w, cy, aside, font, pal["aside"])
    elif panel.page is not None and panel.page[1] > 1:
        index, count = panel.page
        label_font = _font(13 * k)
        label = f"{index % count + 1} / {count}"
        label_w = draw.textlength(label, font=label_font)
        _text_mid(draw, right - label_w, cy, label, label_font, pal["mute"])
        r, gap = 3.5 * k, 6 * k
        dots = min(count, 8)
        x = right - label_w - 8 * k - dots * 2 * r - (dots - 1) * gap
        right_w = right - x
        for i in range(dots):
            fill = pal["ink"] if i == index % count else pal["dim"]
            draw.ellipse((x, cy - r, x + 2 * r, cy + r), fill=fill)
            x += 2 * r + gap
    room = right - left - right_w - 14 * k
    if panel.note:
        x = left
        if not panel.solid:
            r = 4 * k
            draw.ellipse((x, cy - r, x + 2 * r, cy + r), fill=COLORS["red"])
            x += 2 * r + 7 * k
        note = _truncate(draw, panel.note, font, room - (x - left))
        _text_mid(draw, x, cy, note, font, pal["note"])
    elif panel.hint:
        _text_mid(draw, left, cy, _truncate(draw, panel.hint, font, room), font, pal["mute"])


def _panel_stats(draw, panel, pal, box, k) -> None:
    x0, y0, x1, y1 = box
    n = len(panel.stats)
    gap = 8 * k
    card_w = (x1 - x0 - gap * (n - 1)) / n
    card_h = min(74 * k, y1 - y0)
    top = y0 + (y1 - y0 - card_h) / 2
    value_font = _font(34 * k)
    label_room = card_w - 30 * k
    # The label font shrinks (11 -> 8 px) until the widest label fits the
    # narrow Elgato cards ("NEČINNÝ" at 392 px) instead of truncating them all.
    label_font, spacing = _font(11 * k), 0.8 * k
    for size in range(11, 7, -1):
        label_font, spacing = _font(size * k), (0.8 if size > 9 else 0.3) * k
        widest = max(_spaced_len(draw, st.label.upper(), label_font, spacing) for st in panel.stats)
        if widest <= label_room:
            break
    for i, stat in enumerate(panel.stats):
        cx = x0 + i * (card_w + gap)
        draw.rounded_rectangle((cx, top, cx + card_w, top + card_h), radius=10 * k, fill=_P_CARD)
        r = 4 * k
        dot_cy = top + 17 * k
        draw.ellipse(
            (cx + 11 * k, dot_cy - r, cx + 11 * k + 2 * r, dot_cy + r),
            fill=COLORS.get(stat.color, _P_INK),
        )
        label = _truncate_spaced(draw, stat.label.upper(), label_font, label_room, spacing)
        _draw_spaced(
            draw, (cx + 24 * k, dot_cy - _ink_mid(label_font, "H")), label, label_font,
            pal["mute"], spacing,
        )
        value = _truncate(draw, str(stat.value), value_font, card_w - 22 * k)
        draw.text((cx + 11 * k, top + card_h - 12 * k), value, font=value_font,
                  fill=pal["ink"], anchor="ls")


def _panel_gauges(draw, panel, pal, box, k) -> None:
    x0, y0, x1, y1 = box
    gauges = panel.gauges[:6]
    columns = 1 if len(gauges) <= 3 else 2
    rows = math.ceil(len(gauges) / columns)
    col_gap = 18 * k
    col_w = (x1 - x0 - col_gap * (columns - 1)) / columns
    row_h = 29 * k
    gap = min(10 * k, (y1 - y0 - rows * row_h) / max(1, rows - 1)) if rows > 1 else 0
    top = y0 + (y1 - y0 - rows * row_h - (rows - 1) * gap) / 2
    label_font, pct_font, hint_font = _font(14 * k), _font(14 * k), _font(12 * k, bold=False)
    pace_font = _font(12 * k)
    for i, gauge in enumerate(gauges):
        row, col = divmod(i, columns)
        gx = x0 + col * (col_w + col_gap)
        gy = top + row * (row_h + gap)
        cy = gy + 8 * k
        pct = f"{gauge.used_percent}%"
        pct_w = draw.textlength(pct, font=pct_font)
        _text_mid(draw, gx + col_w - pct_w, cy, pct, pct_font, pal["ink"])
        rx = gx + col_w - pct_w - 10 * k
        label = f"{gauge.label} · {gauge.window}"
        label_w = min(draw.textlength(label, font=label_font), col_w * 0.62)
        # A reset hint that does not fit whole drops its leading word
        # ("reset 14:09" -> "14:09") before it is dropped altogether.
        short_hint = gauge.hint.split(" ", 1)[-1] if gauge.hint else ""
        for options, font, fill in (((gauge.pace,), pace_font, COLORS["amber"]),
                                    ((gauge.hint, short_hint), hint_font, pal["mute"])):
            for text in options:
                if not text:
                    continue
                tw = draw.textlength(text, font=font)
                # the label keeps its full width (+ the 6k truncation margin):
                # the window name matters more than the hint
                if rx - tw - 16 * k < gx + label_w:
                    continue
                _text_mid(draw, rx - tw, cy, text, font, fill)
                rx -= tw + 10 * k
                break
        _text_mid(draw, gx, cy, _truncate(draw, label, label_font, rx - gx - 6 * k), label_font,
                  pal["ink"])
        bar_y = gy + row_h - 6 * k
        _draw_gauge_rail(draw, (gx, bar_y, gx + col_w, bar_y + 6 * k), gauge.used_percent,
                         _gauge_tone(gauge.color, gauge.used_percent), track=_P_CARD)


def _panel_text(draw, panel, pal, box, k) -> None:
    x0, y0, x1, y1 = box
    width = x1 - x0
    if panel.headline:
        head_font = _fit_font(draw, panel.headline, width, 36 * k, 22 * k)
        head = _truncate(draw, panel.headline, head_font, width)
        sub_font = _font(16 * k)
        sub_lh = 21 * k
        head_h = head_font.size * 1.05
        room = int((y1 - y0 - head_h - 9 * k) // sub_lh)
        sub = _panel_wrapped(draw, panel.lines, sub_font, width, max(0, min(2, room)))
        block = head_h + (9 * k + len(sub) * sub_lh if sub else 0)
        y = y0 + (y1 - y0 - block) / 2
        draw.text((x0, y + head_h), head, font=head_font, fill=pal["ink"], anchor="ls")
        y += head_h + 9 * k
        for line in sub:
            draw.text((x0, y + sub_lh * 0.78), line, font=sub_font, fill=pal["mute"], anchor="ls")
            y += sub_lh
        return
    body_font = _font(18 * k, bold=False)
    lh = 24 * k
    body = _panel_wrapped(draw, panel.lines, body_font, width, int((y1 - y0) // lh))
    y = y0
    for line in body:
        draw.text((x0, y + lh * 0.8), line, font=body_font, fill=pal["ink"], anchor="ls")
        y += lh


def _gauge_tone(color: str, used_percent: int) -> tuple[int, int, int]:
    if used_percent >= 85:
        return COLORS["red"]
    if used_percent >= 65:
        return COLORS["amber"]
    return COLORS.get(color, COLORS["violet"])


def _draw_gauge_rail(draw, box, used_percent: int, tone, track=_P_CARD) -> None:
    x0, y0, x1, y1 = box
    radius = max(1, (y1 - y0) / 2)
    draw.rounded_rectangle(box, radius=radius, fill=track)
    fill_w = (x1 - x0) * max(0, min(100, used_percent)) / 100
    if fill_w:
        draw.rounded_rectangle((x0, y0, x0 + max(fill_w, 2 * radius), y1), radius=radius, fill=tone)


def compose_classic_panel(panel: PanelView, width: int = PANEL_W) -> Image.Image:
    """Render a PanelView to a width x 196 image.

    The default width is the D200 small window's native 458px. The Elgato
    driver composes at 392 so the image splits into two exact 196x196 key
    images; the web simulator and desktop window display the PNG at its true
    aspect either way.
    """
    k = _PANEL_SS
    w, h = width * k, PANEL_H * k
    pal = _panel_palette(panel)
    img = Image.new("RGB", (w, h), pal["bg"])
    draw = ImageDraw.Draw(img)
    _panel_header(draw, panel, pal, w, k)
    has_footer = bool(panel.hint or panel.note or panel.aside or (panel.page and panel.page[1] > 1))
    top = (_P_PAD_Y + _P_CHIP_H + 10) * k
    bottom = h - (_P_PAD_Y + (_P_FOOT_H + 10 if has_footer else 0)) * k
    box = (_P_PAD_X * k, top, w - _P_PAD_X * k, bottom)
    if panel.stats:
        _panel_stats(draw, panel, pal, box, k)
    elif panel.gauges:
        _panel_gauges(draw, panel, pal, box, k)
    else:
        _panel_text(draw, panel, pal, box, k)
    _panel_footer(draw, panel, pal, w, h, k)
    return img.resize((width, PANEL_H), Image.LANCZOS)


def compose_label_tile(tile) -> Image.Image:
    if tile.color == "launcher":
        # A management tile, NOT a status: dark background + green accent
        # label. The old full-green launcher was pixel-identical to a
        # WORKING agent tile under solid fill — the deck read as having
        # one more running agent than it had.
        bg = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), TILE_BG + (255,))
        d = ImageDraw.Draw(bg)
        f = _font(30)
        t = _truncate(d, tile.label, f, ICON_SIZE - 16)
        w = d.textlength(t, font=f)
        bb = d.textbbox((0, 0), t, font=f)
        d.text(
            ((ICON_SIZE - w) / 2, (ICON_SIZE - (bb[3] - bb[1])) / 2 - bb[1]),
            t,
            font=f,
            fill=COLORS["green"],
        )
        return bg
    bg_col = COLORS.get(tile.color, COLORS["dim"])
    bg = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), bg_col + (255,))
    # Always-white text read at 2.1:1 on amber and 2.0:1 on cyan (a drill
    # choice on an amber approval tile); take the higher-contrast ink.
    ink = _ink_for(bg_col)
    if tile.subtext:
        # Drill choice tile: big number (label) up top, small wrapped choice
        # text underneath — readable instead of a truncated "1 Yes…" line.
        d = ImageDraw.Draw(bg)
        nf = _font(58)
        nw = d.textlength(tile.label, font=nf)
        d.text(((ICON_SIZE - nw) / 2, 16), tile.label, font=nf, fill=ink)
        sf = _font(22)
        sub_fill = _soften(ink, bg_col, max_mix=0.1)
        y = 92
        for line in _wrap(d, tile.subtext, sf, ICON_SIZE - 16, 3):
            lw = d.textlength(line, font=sf)
            d.text(((ICON_SIZE - lw) / 2, y), line, font=sf, fill=sub_fill)
            y += 26
    elif tile.label:
        d = ImageDraw.Draw(bg)
        f = _font(28)
        t = _truncate(d, tile.label, f, ICON_SIZE - 16)
        w = d.textlength(t, font=f)
        bb = d.textbbox((0, 0), t, font=f)
        d.text(
            ((ICON_SIZE - w) / 2, (ICON_SIZE - (bb[3] - bb[1])) / 2 - bb[1]),
            t,
            font=f,
            fill=ink,
        )
    return bg


def tile_bg(tile) -> tuple[str, tuple, tuple]:
    """(fill, accent, background colour) for an agent tile.

    tile_fill: how much of the tile the status colour covers.
      none  -> dark background (colour lives in the word + bottom bar)
      tint  -> whole tile a darkened shade of the colour + bright bottom edge
      solid -> whole tile the full colour"""
    accent = COLORS.get(tile.color, COLORS["dim"])
    fill = getattr(tile, "tile_fill", "none")
    if fill == "solid":
        return fill, accent, accent
    if fill == "tint":
        return fill, accent, _tint_bg(accent)
    return fill, accent, TILE_BG


def compose_agent_tile(icons, tile, spinner=None) -> Image.Image:
    """Compose an agent tile in two layers: the static base (background,
    text, bar — cached by the signature WITHOUT the animation phase) and
    the per-frame motion (logo, comet ring, sweep segment)."""
    anim = _effective_animation(tile)
    sweep = spinner is not None and anim == "sweep"
    key = (*icons._static_sig(tile), sweep)
    base = icons._base_cache.get(key)
    if base is None:
        base = compose_agent_base(icons, tile, static_bar=not sweep)
        icons._base_cache[key] = base
        while len(icons._base_cache) > _BASE_CACHE_MAX:
            icons._base_cache.popitem(last=False)
    else:
        icons._base_cache.move_to_end(key)
    img = base.copy()
    draw_agent_motion(icons, img, tile, spinner, anim)
    return img


def compose_agent_base(icons, tile, *, static_bar: bool) -> Image.Image:
    fill, accent, bg_col = tile_bg(tile)
    bg = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), bg_col + (255,))
    d = ImageDraw.Draw(bg)
    # text colours chosen for contrast against the fill (see _tile_text_colors)
    repo_fill, branch_fill, time_fill, word_fill = _tile_text_colors(fill, bg_col, accent)
    right = ICON_SIZE - 12
    # status word + elapsed time, top-right, beside the logo box (x < 66,
    # incl. the comet ring). Sized for a ~25mm physical key, where the old
    # 19px word shrank to ~7px at the D200's 72px key.
    if tile.status_text:
        fs = _fit_font(d, tile.status_text, STATUS_MAX_W, 24, 17)
        word = _truncate(d, tile.status_text, fs, STATUS_MAX_W)
        d.text((right - d.textlength(word, font=fs), 10), word, font=fs, fill=word_fill)
    if tile.time_text:
        ft = _fit_font(d, tile.time_text, STATUS_MAX_W, 20, 16)
        t = _truncate(d, tile.time_text, ft, STATUS_MAX_W)
        d.text((right - d.textlength(t, font=ft), 40), t, font=ft, fill=time_fill)
    # repo (primary) + branch (secondary) — spread down the tile so the
    # composition is optically centred between the logo row and the bottom
    # band instead of leaving a dead band across the bottom third.
    fr, project_lines = _fit_project_name(d, tile.repo or "", ICON_SIZE - 24)
    for i, line in enumerate(project_lines):
        d.text((12, 72 if len(project_lines) == 1 else 66 + i * 23),
               line, font=fr, fill=repo_fill)
    if tile.branch:
        fb = _font(18, bold=False)
        y = 116
        for line in _fit_branch(d, tile.branch, fb, ICON_SIZE - 24):
            d.text((12, y), line, font=fb, fill=branch_fill)
            y += 22
    # bottom band: backend tag (left) + pin (right), at a size that still
    # reads on a 72px key (the old 12px tag / 12px pin vanished there).
    pinned = getattr(tile, "pinned", False)
    badge_w = draw_subagent_badge(
        d, getattr(tile, "subagents", 0), right - (26 if pinned else 0), time_fill
    )
    if tile.server_tag:
        fc = _font(16, bold=False)
        tag_w = ICON_SIZE - 24 - (26 if pinned else 0) - (badge_w + 8 if badge_w else 0)
        tag = _truncate(d, tile.server_tag, fc, tag_w)
        d.text((12, 164), tag, font=fc, fill=time_fill)
    if pinned:
        # pin silhouette (head, collar, needle), independent of backend labels
        cx, top = ICON_SIZE - 20, 163
        d.rectangle((cx - 5, top, cx + 5, top + 8), fill=time_fill)
        d.line((cx - 8, top + 10, cx + 8, top + 10), fill=time_fill, width=3)
        d.line((cx, top + 11, cx, top + 22), fill=time_fill, width=2)
    # the plain static bottom bar — only when the fill isn't solid (on
    # solid it would be invisible) and no sweep is drawn over it per frame
    if static_bar and fill != "solid":
        d.rectangle([0, ICON_SIZE - 8, ICON_SIZE, ICON_SIZE], fill=accent)
    return bg


def draw_subagent_badge(d, count: int, right: int, ink) -> int:
    """The "⑂N" running-subagents badge, right-aligned at ``right`` in the
    bottom band; returns its width (0 when nothing is drawn).

    The fork is drawn as lines (the vendored Inter has no U+2442 glyph):
    a stem splitting into two prongs, then the count."""
    if count <= 0:
        return 0
    text = str(count) if count < 100 else "99+"
    font = _font(16)
    text_w = int(d.textlength(text, font=font))
    fork_w = 12
    width = fork_w + 3 + text_w
    x0 = right - width
    top, mid, bottom = 167, 174, 182
    left_x, right_x = x0 + 1, x0 + fork_w - 1
    stem_x = x0 + fork_w // 2
    d.line((stem_x, mid, stem_x, bottom), fill=ink, width=2)
    d.line((left_x, mid, right_x, mid), fill=ink, width=2)
    d.line((left_x, top, left_x, mid), fill=ink, width=2)
    d.line((right_x, top, right_x, mid), fill=ink, width=2)
    d.text((x0 + fork_w + 3, 164), text, font=font, fill=ink)
    return width


def draw_project_mark(icons, img, tile, spinner, anim, dark_fill, bg_col) -> None:
    """tile_icon = project: the favicon in the 46px logo box. It never
    rotates (spin arrives here as comet); pulse scales it."""
    working = spinner is not None
    if working and anim == "pulse":
        s = _pulse_size(spinner)
        off = 12 + (46 - s) // 2
        img.alpha_composite(icons._project_layer(tile, s, bg_col), (off, off))
        return
    img.alpha_composite(icons._project_layer(tile, 46, bg_col), (12, 12))
    if working and anim == "comet":
        ring = DARK_INK if dark_fill else LIGHT_INK
        img.alpha_composite(icons._comet_overlay(62, spinner, 2, 4, ring), (4, 4))


def draw_project_badge(icons, img, tile, bg_col) -> None:
    """tile_icon = both: a static badge on the logo box's bottom-right
    corner, ringed in the tile colour so it reads apart from the mark."""
    x = PROJECT_BADGE_XY
    ImageDraw.Draw(img).rounded_rectangle(
        [x - 2, x - 2, x + PROJECT_BADGE + 1, x + PROJECT_BADGE + 1],
        radius=round((PROJECT_BADGE + 4) * PROJECT_RADIUS),
        fill=tuple(bg_col) + (255,),
    )
    img.alpha_composite(icons._project_layer(tile, PROJECT_BADGE, bg_col), (x, x))


def draw_agent_motion(icons, img: Image.Image, tile, spinner, anim) -> None:
    """The per-frame layer: the logo (top-left; animated while working per
    the chosen style), the comet ring and the sweep segment. With
    tile_icon = project the favicon takes the mark's place; with both, the
    static project badge is drawn LAST so neither the mark nor the ring
    covers it (the mark is composited here, above the cached base)."""
    fill, accent, bg_col = tile_bg(tile)
    working = spinner is not None
    agent = tile.agent_type or "default"
    mode = getattr(tile, "tile_icon", "agent")
    # A white mark / ring washes out on bright solid fills (amber 2.1:1,
    # cyan 2.0:1 — below the 3:1 non-text minimum) while the text correctly
    # flips dark: recolour them to the same dark ink. Full-colour user
    # overrides are left as supplied (the flip would flatten them).
    dark_fill = fill == "solid" and _ink_for(bg_col) == DARK_INK
    if mode == "project":
        draw_project_mark(icons, img, tile, spinner, anim, dark_fill, bg_col)
    else:
        dark_logo = dark_fill and icons._is_light_glyph(agent)
        if working and anim == "pulse":
            # slow "breath": scale the mark between ~0.82x and 1.0x across the
            # PULSE_STATES frames (the spinner here is already the SLOW phase
            # from _anim_phase — one step per PULSE_SLOWDOWN ticks)
            s = _pulse_size(spinner)
            off = 12 + (46 - s) // 2  # keep the smaller mark centred in its 46px box
            img.alpha_composite(icons._logo(agent, dark_logo, s), (off, off))
        else:
            rotation = spinner * SPIN_DEG if working and anim == "spin" else 0
            img.alpha_composite(icons._logo(agent, dark_logo, 46, rotation), (12, 12))
            if working and anim == "comet":
                # thin comet ring orbiting the static mark; the 62px overlay is
                # centred over the 46px logo box at (12,12) -> composite at (4,4)
                ring = DARK_INK if dark_fill else LIGHT_INK
                img.alpha_composite(icons._comet_overlay(62, spinner, 2, 4, ring), (4, 4))
        if mode == "both":
            draw_project_badge(icons, img, tile, bg_col)
    # "sweep" is a moving segment along the bottom edge; it must stay
    # visible on any fill, so its colours adapt — on a solid tile
    # (background already = accent) a dark base + a bright segment; on
    # none/tint a dimmed base + the accent segment.
    if working and anim == "sweep":
        d = ImageDraw.Draw(img)
        y0 = ICON_SIZE - 8
        if fill == "solid":
            base = tuple(int(c * 0.45) for c in accent)
            seg_col = tuple(min(255, c + 90) for c in accent)
        else:
            base = tuple(int(c * 0.4) for c in accent)
            seg_col = accent
        d.rectangle([0, y0, ICON_SIZE, ICON_SIZE], fill=base)
        seg_w = ICON_SIZE // 4
        left = int((spinner / SPINNER_FRAMES) * ICON_SIZE)
        d.rectangle([left, y0, min(left + seg_w, ICON_SIZE), ICON_SIZE], fill=seg_col)
        if left + seg_w > ICON_SIZE:  # wrap the bright segment past the right edge
            d.rectangle([0, y0, (left + seg_w) - ICON_SIZE, ICON_SIZE], fill=seg_col)


class ClassicTheme(Theme):
    """Today's look; the end of every theme's base chain."""

    api_version = API_VERSION
    id = "classic"
    name = {"en": "Classic", "cs": "Klasický"}
    base = None
    animated = True  # spin / comet / pulse / sweep ([view].working_animation)

    def agent_tile(self, tile, ctx, frame):
        return compose_agent_tile(ctx._icons, tile, frame)

    def key_tile(self, tile, ctx):
        return compose_label_tile(tile)

    def panel(self, panel, ctx, width):
        return compose_classic_panel(panel, width)


THEME = ClassicTheme()
