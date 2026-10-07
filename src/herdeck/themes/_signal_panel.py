"""signal's status window (the D200's wide window, the Elgato key pair, the
web/desktop panel strip), split from ``signal`` for size.

The window speaks the tiles' language: a blocked fleet floods it amber with
the head of the queue on a white plate, its wait, and the rest of the queue as
favicon chips (plus the offline machine chip); a calm fleet is a row of ring
counters in the tiles' shapes with two usage bars; offline, config error and
"sent" put a big glyph on the left with words beside it.

At 392 px (the Elgato key pair, split at x = 196) every text stays inside one
key: two-column text, a glyph on the left key and its words on the right one.
Every text is >= 22 px (``_kit.MIN_TEXT``).
"""

from __future__ import annotations

from . import _kit as k

S = k.S
COUNTER_R = 30


def draw(panel, ctx, width):
    """The panel image, or None for a kind signal does not draw (the base
    theme draws it)."""
    kind = getattr(panel, "kind", "")
    if panel.sent and kind in ("calm", "spotlight"):
        kind = "sent"  # the answer acknowledgement wins while it shows
    fn = PANELS.get(kind)
    return fn(panel, ctx, width) if fn else None


def _right_x0(width, wide: int = 176) -> int:
    """Where text beside a left-key glyph starts (the right key at 392)."""
    return k.SEAM + 10 if width < k.NARROW else wide


def _spotlight(panel, ctx, width):
    accent = ctx.color(panel.color)
    img, d = k.new_image((width, S), accent)
    ink = ctx.ink_for(accent)
    queue = list(getattr(panel, "queue", []) or [])
    head = queue[0] if queue else None
    d.rounded_rectangle((18, 18, 178, 178), 32, fill=k.PLATE)
    if head is not None:
        mark = k.queue_mark(ctx, head, 136)
        img.paste(mark, (30, 30), mark)
    else:
        font = ctx.font(110)
        d.text((98 - d.textlength("!", font=font) / 2, 22), "!", font=font, fill=accent)
    x0, xr = _right_x0(width, 198), width - 14
    name = (head.project_name or head.label) if head is not None else panel.headline
    nfont, name = k.fit(ctx, d, name or "", xr - x0, 52, 26)
    d.text((x0, 12), name, font=nfont, fill=ink)
    elapsed = head.elapsed if head is not None else panel.meta
    y = 12 + nfont.size + 10
    if elapsed:
        wfont, wait = k.fit(ctx, d, k.t(ctx, "signal.waiting_for", t=elapsed), xr - x0, 30)
        d.text((x0 + 2, y), wait, font=wfont, fill=ink)
    _queue_row(ctx, img, d, queue[1:], panel.note, x0, xr, ink)
    return img


GLYPH_CHIP = 42  # the offline chip collapsed to its glyph
NOTE_MIN = 96  # below this the offline chip shows only its glyph


def queue_row_layout(chip_ws, more_ws, x0, xr, note_w):
    """Place the spotlight's bottom row: queue chips from the left, the
    offline machine chip at the right, never overlapping.

    ``chip_ws[i]`` is chip i's width (mark + age), ``more_ws[i]`` the width
    of the "+N" left over after i + 1 chips (0 when none remain), ``note_w``
    the offline chip's natural width (0 = none). The first queue chip always
    keeps its place, with room kept for the "+N" after it; the offline chip
    takes what is left (cut, or just its glyph), and further chips fill the
    room before it, each leaving room for "+N".

    Returns (chips shown, note box left x or None, note box width)."""
    avail = xr - x0
    # the first chip, plus the gap and "+N" that trail it when others wait
    first = chip_ws[0] + (16 + more_ws[0] if more_ws[0] else 0) + 12 if chip_ws else 0
    note_x0, box_w = None, 0
    if note_w:
        room = avail - first
        if room >= note_w:
            box_w = note_w
        elif room >= NOTE_MIN:
            box_w = room
        elif room >= GLYPH_CHIP:
            box_w = GLYPH_CHIP
        if box_w:
            note_x0 = xr - box_w
    limit = (note_x0 if note_x0 is not None else xr) - 12
    x, shown = x0, 0
    for i, w in enumerate(chip_ws):
        # the first chip is always shown; every chip needs room for the
        # 16 px gap and "+N" that follow it when others remain
        need = w + (16 + more_ws[i] if more_ws[i] else 0)
        if i > 0 and x + need > limit + (12 if note_x0 is None else 0):
            break
        x += w + 16
        shown += 1
    return shown, note_x0, box_w


def machine_name(ctx, note: str) -> str:
    """"laptop offline" -> "laptop": the chip's glyph already says offline.
    Any other note ("2 servers offline") is kept whole."""
    template = ctx.tr("server_offline")
    prefix, found, suffix = template.partition("{name}")
    if found and note.startswith(prefix) and note.endswith(suffix) and len(note) > len(prefix + suffix):
        return note[len(prefix) : len(note) - len(suffix)].strip()
    return note


def _queue_row(ctx, img, d, others, note, x0, xr, ink) -> None:
    font = ctx.font(24)
    top = 126
    nfont = ctx.font(k.MIN_TEXT)
    note = machine_name(ctx, note) if note else ""
    note_w = d.textlength(note, font=nfont) + 50 if note else 0
    ages = [o.elapsed or "" for o in others]
    chip_ws = [58 + (8 + d.textlength(a, font=font) if a else 0) for a in ages]
    more_ws = [
        (d.textlength(f"+{len(others) - i - 1}", font=font) + 4) if len(others) - i - 1 else 0
        for i in range(len(others))
    ]
    shown, note_x0, box_w = queue_row_layout(chip_ws, more_ws, x0, xr, note_w)
    x = x0
    for other, age, w in zip(others[:shown], ages, chip_ws, strict=False):
        d.rounded_rectangle((x, top, x + 58, top + 58), 14, fill=k.PLATE)
        mark = k.queue_mark(ctx, other, 50)
        img.paste(mark, (int(x) + 4, top + 4), mark)
        if age:
            d.text((x + 66, top + 15), age, font=font, fill=ink)
        x += w + 16
    rest = len(others) - shown
    limit = (note_x0 if note_x0 is not None else xr + 12) - 12
    if rest and x + d.textlength(f"+{rest}", font=font) <= limit:
        d.text((x, top + 15), f"+{rest}", font=font, fill=ink)
    if note_x0 is not None:
        red = ctx.status_color("offline")
        on_red = ctx.ink_for(red)
        d.rounded_rectangle((note_x0, top + 8, xr, top + 50), 21, fill=red)
        k.offline_glyph(d, note_x0 + 21, top + 29, 10, on_red, 4)
        if box_w > GLYPH_CHIP:
            text = k.ellipsize(ctx, d, note, k.MIN_TEXT, box_w - 50)
            d.text((note_x0 + 38, top + 16), text, font=nfont, fill=on_red)


def ring_counter(ctx, d, cx, cy, r, status, n) -> None:
    """A status count drawn in the tiles' shape language."""
    col = ctx.status_color(status)
    box = (cx - r, cy - r, cx + r, cy + r)
    if status == "working":
        d.arc(box, -60, 240, fill=col, width=8)
    elif status == "waiting":
        for a in range(0, 360, 40):
            d.arc(box, a, a + 24, fill=col, width=8)
    elif status == "idle":
        d.ellipse(box, outline=k.dim(col, 0.75), width=4)
    elif status == "offline":
        d.ellipse(box, outline=col, width=8)
        bx, by = cx + r * 0.75, cy - r * 0.75
        d.ellipse((bx - 11, by - 11, bx + 11, by + 11), fill=k.DARK)
        k.offline_glyph(d, bx, by, 9, col, 3)
    else:  # done
        d.ellipse(box, outline=col, width=8)
        d.ellipse((cx + r * 0.55, cy - r - 2, cx + r * 0.55 + 14, cy - r + 12), fill=col)
    font, txt = k.fit(ctx, d, str(n), 2 * r - 14, 30)
    bb = d.textbbox((0, 0), txt, font=font)
    d.text((cx - (bb[2] + bb[0]) / 2, cy - (bb[3] + bb[1]) / 2), txt, font=font, fill=k.LIGHT)


def counter_slots(width, paged) -> list[int]:
    """Counter centres: five across the D200 window, two per key at 392 (the
    seam at x = 196 falls between them); paging gives the last slot to dots."""
    slots = [52, 144, 248, 340] if width < k.NARROW else [52, 140, 228, 316, 404]
    return slots[:-1] if paged else slots


def one_per_provider(gauges):
    """The window that runs out first, per provider (label)."""
    best: dict[str, object] = {}
    for g in gauges:
        if g.label not in best or g.used_percent > best[g.label].used_percent:
            best[g.label] = g
    return list(best.values())


def _calm(panel, ctx, width):
    img, d = k.new_image((width, S), k.DARK)
    d.rounded_rectangle((0, 0, width - 1, S - 1), 22, fill=k.EDGE)
    d.rounded_rectangle((6, 6, width - 7, S - 7), 18, fill=k.DARK)
    counts = getattr(panel, "counts", {}) or {}
    order = ("offline", "working", "waiting", "done", "idle")
    present = [(st, counts[st]) for st in order if counts.get(st)]
    paged = bool(panel.page and panel.page[1] > 1)
    slots = counter_slots(width, paged)
    gauges = one_per_provider(panel.gauges)[:2]
    rows = [("gauge", g) for g in gauges]
    if panel.note and len(rows) < 2:
        rows.append(("note", panel.note))
    cy = 58 if rows else 98
    for (status, n), cx in zip(present, slots, strict=False):
        ring_counter(ctx, d, cx, cy, COUNTER_R, status, n)
    if not present:
        font, title = k.fit(ctx, d, panel.title or "", (k.SEAM - 34) if width < k.NARROW else width - 48, 34)
        d.text((24, cy - font.size * 0.6), title, font=font, fill=k.LIGHT)
    if paged:
        _page_marker(ctx, d, panel.page, (slots[-1] + COUNTER_R + 14) if present else width // 2, width - 24, cy)
    y = 106
    for kind, row in rows:
        if kind == "gauge":
            _gauge_row(ctx, d, row, y, width)
        else:
            red = ctx.readable(ctx.status_color("offline"), k.DARK)
            d.ellipse((24, y + 7, 36, y + 19), fill=red)
            font, note = k.fit(ctx, d, row, (k.SEAM - 54) if width < k.NARROW else width - 72, 22)
            d.text((44, y), note, font=font, fill=red)
        y += 38
    return img


def _page_marker(ctx, d, page, x_min, x_max, cy) -> None:
    index, count = page
    shown = min(count, 6)
    need = shown * 22
    if x_max - x_min >= need:
        for i in range(shown):
            cx = x_max - 7 - (shown - 1 - i) * 22
            fill = k.LIGHT if i == index % count and count <= 6 else (80, 86, 96)
            d.ellipse((cx - 7, cy - 7, cx + 7, cy + 7), fill=fill)
        if count <= 6:
            return
    font = ctx.font(k.MIN_TEXT)
    label = f"{index % count + 1}/{count}"
    w = d.textlength(label, font=font)
    if x_max - x_min >= w:
        d.rectangle((x_max - need, cy - 8, x_max, cy + 8), fill=k.DARK)
        d.text((x_max - w, cy - 13), label, font=font, fill=k.MUTE)


def _gauge_tone(ctx, g):
    if g.used_percent >= 85:
        return ctx.color("red")
    if g.used_percent >= 65:
        return ctx.color("amber")
    return ctx.color(g.color)


def _gauge_row(ctx, d, g, y, width) -> None:
    font = ctx.font(k.MIN_TEXT)
    pct = f"{g.used_percent}%"
    pct_w = d.textlength(pct, font=font)
    d.text((width - 24 - pct_w, y), pct, font=font, fill=k.LIGHT)
    label_max = width - 24 - pct_w - 14 - 48 - 14 - 24
    if width < k.NARROW:
        label_max = min(label_max, k.SEAM - 10 - 24)
    label = k.ellipsize(ctx, d, f"{g.label} {g.window}", k.MIN_TEXT, label_max)
    d.text((24, y), label, font=font, fill=k.MUTE)
    bx0 = 24 + d.textlength(label, font=font) + 14
    bx1 = width - 24 - pct_w - 14
    # the bar may cross the Elgato seam: it is decoration, the numbers are not
    d.rounded_rectangle((bx0, y + 7, bx1, y + 19), 6, fill=k.TRACK)
    used = max(0, min(100, g.used_percent))
    if used:
        d.rounded_rectangle((bx0, y + 7, max(bx0 + 12, bx0 + (bx1 - bx0) * used / 100), y + 19), 6,
                            fill=_gauge_tone(ctx, g))


def _glyph_panel(ctx, width, frame_col):
    """A dark panel in a coloured frame (offline, config error, sent)."""
    img, d = k.new_image((width, S), k.DARK)
    d.rounded_rectangle((0, 0, width - 1, S - 1), 22, fill=frame_col)
    d.rounded_rectangle((14, 14, width - 15, S - 15), 12, fill=k.DARK)
    return img, d


def _offline(panel, ctx, width):
    col = ctx.color(panel.color)
    img, d = _glyph_panel(ctx, width, col)
    k.offline_glyph(d, 98, S // 2, 46, col, 12)
    x0, xr = _right_x0(width), width - 28
    tfont, title = k.fit(ctx, d, panel.title or k.t(ctx, "offline_title"), xr - x0, 46, 26)
    d.text((x0, 44), title, font=tfont, fill=k.LIGHT)
    waiting = len(getattr(panel, "queue", []) or []) or (getattr(panel, "counts", {}) or {}).get("blocked", 0)
    parts = [p for p in (panel.meta, k.t(ctx, "signal.n_waiting", n=waiting) if waiting else "") if p]
    if parts:
        sfont, sub = k.fit(ctx, d, " · ".join(parts), xr - x0, 28)
        d.text((x0 + 2, 44 + tfont.size + 16), sub, font=sfont, fill=k.MUTE)
    return img


def _columns(width):
    """Text columns: one across the panel, or one per Elgato key at 392."""
    if width < k.NARROW:
        return [(14, k.SEAM - 10), (k.SEAM + 10, width - 14)]
    return [(18, width - 18)]


def _flow(ctx, d, items, columns, top, bottom) -> int:
    """Lay ``items`` — (text, largest, bold, fill, max_lines) — down the
    columns: each wrapped to the column width, continuing in the next column
    when one is full. Returns how many rows stayed free in the last column."""
    col = 0
    y = top
    for text, largest, bold, fill, max_lines in items:
        if not text:
            continue
        x0, x1 = columns[col]
        font, lines = k.block(ctx, d, text, x1 - x0, largest=largest, max_lines=max_lines, bold=bold)
        lh = font.size + 6
        for line in lines:
            if y + lh > bottom:
                col += 1
                if col >= len(columns):
                    return 0
                x0, x1 = columns[col]
                y = top
            line = k.ellipsize(ctx, d, line, font.size, x1 - x0, bold=bold)
            d.text((x0, y), line, font=font, fill=fill)
            y += lh
    return int((bottom - y) // (k.MIN_TEXT + 6))


def _config_error(panel, ctx, width):
    col = ctx.color(panel.color)
    img, d = _glyph_panel(ctx, width, col)
    d.ellipse((52, 52, 144, 144), outline=col, width=12)
    font = ctx.font(60)
    d.text((98 - d.textlength("!", font=font) / 2, 62), "!", font=font, fill=col)
    x0, xr = _right_x0(width), width - 28
    items = [
        (panel.title, 34, True, k.LIGHT, 1),
        (panel.headline, 26, True, k.LIGHT, 2),
        *[(line, k.MIN_TEXT, False, k.MUTE, 2) for line in panel.lines],
    ]
    _flow(ctx, d, items, [(x0, xr)], 26, S - 24)
    return img


def _sent(panel, ctx, width):
    green = ctx.color("green")
    img, d = k.new_image((width, S), k.DARK)
    d.rounded_rectangle((0, 0, width - 1, S - 1), 22, fill=k.EDGE)
    d.rounded_rectangle((6, 6, width - 7, S - 7), 18, fill=k.DARK)
    d.ellipse((42, 42, 154, 154), fill=green)
    k.check_glyph(d, 98, 98, 44, ctx.ink_for(green), 12)
    x0, xr = _right_x0(width), width - 20
    items = [(panel.sent or panel.title, 34, True, k.LIGHT, 2), (panel.meta, 26, True, k.MUTE, 1)]
    _flow(ctx, d, items, [(x0, xr)], 40, S - 24)
    return img


def _detail(panel, ctx, width):
    """An agent's prompt: status chip, who it is, the prompt text; "answer on
    the keys" only when a whole free line is left for it."""
    tone = ctx.color(panel.color)
    img, d = k.new_image((width, S), k.DARK)
    cols = _columns(width)
    chip_max = (cols[0][1] - cols[0][0]) if len(cols) > 1 else (width - 28) * 0.6
    font = ctx.font(k.MIN_TEXT)
    chip, metas = panel.title or "", [panel.meta]
    if d.textlength(chip, font=font) > chip_max - 20 and " · " in chip:
        # "BLOCKED · 4m" too wide for the left key: the time joins the meta
        chip, extra = chip.split(" · ", 1)
        metas = [f"{extra} · {panel.meta}" if panel.meta else extra, extra]
    chip = k.ellipsize(ctx, d, chip, k.MIN_TEXT, chip_max - 20)
    chip_w = d.textlength(chip, font=font) + 20
    d.rounded_rectangle((14, 12, 14 + chip_w, 12 + font.size + 12), 17, fill=tone)
    d.text((24, 16), chip, font=font, fill=ctx.ink_for(tone))
    meta_x0 = cols[1][0] if len(cols) > 1 else 14 + chip_w + 16
    for meta in metas:
        if meta and d.textlength(meta, font=font) <= width - 14 - meta_x0:
            d.text((width - 14 - d.textlength(meta, font=font), 16), meta, font=font, fill=k.MUTE)
            break
    items = []
    if panel.headline:
        items.append((panel.headline, 26, True, k.LIGHT, 1))
    items += [(line, k.MIN_TEXT, False, k.LIGHT, 6) for line in panel.lines]
    bottom = S - 8
    free = _flow(ctx, d, items, cols, 58, bottom)
    if panel.hint and free >= 1:
        hfont = ctx.font(k.MIN_TEXT)
        x0, x1 = cols[-1]
        if d.textlength(panel.hint, font=hfont) <= x1 - x0:
            d.text((x0, bottom - k.MIN_TEXT - 6), panel.hint, font=hfont, fill=k.MUTE)
    return img


def _usage(panel, ctx, width):
    img, d = k.new_image((width, S), k.DARK)
    cols = _columns(width)
    tfont, title = k.fit(ctx, d, panel.title or k.t(ctx, "usage_title"), cols[0][1] - cols[0][0], 26)
    d.text((cols[0][0], 12), title, font=tfont, fill=k.LIGHT)
    if panel.page and panel.page[1] > 1:
        font = ctx.font(k.MIN_TEXT)
        label = f"{panel.page[0] % panel.page[1] + 1}/{panel.page[1]}"
        d.text((width - 14 - d.textlength(label, font=font), 14), label, font=font, fill=k.MUTE)
    gauges = panel.gauges[:3]
    if not gauges:
        return img
    font = ctx.font(k.MIN_TEXT)
    row_h = (S - 52 - 8) // len(gauges)
    for i, g in enumerate(gauges):
        y = 52 + i * row_h
        pct = f"{g.used_percent}%"
        pct_w = d.textlength(pct, font=font)
        d.text((width - 14 - pct_w, y), pct, font=font, fill=k.LIGHT)
        label_max = (k.SEAM - 10 - 14) if width < k.NARROW else (width - 28) * 0.5
        label = k.ellipsize(ctx, d, f"{g.label} · {g.window}", k.MIN_TEXT, label_max)
        d.text((14, y), label, font=font, fill=k.LIGHT)
        side = g.pace or g.hint
        if side:
            x_min = (k.SEAM + 10) if width < k.NARROW else 14 + d.textlength(label, font=font) + 16
            x_max = width - 14 - pct_w - 12
            sw = d.textlength(side, font=font)
            if x_max - sw >= x_min:
                d.text((x_max - sw, y), side, font=font, fill=ctx.color("amber") if g.pace else k.MUTE)
        bar_y = y + 30
        d.rounded_rectangle((14, bar_y, width - 14, bar_y + 10), 5, fill=k.TRACK)
        used = max(0, min(100, g.used_percent))
        if used:
            d.rounded_rectangle((14, bar_y, max(24, 14 + (width - 28) * used / 100), bar_y + 10), 5,
                                fill=_gauge_tone(ctx, g))
    return img


def _menu(panel, ctx, width):
    """Launcher / profile menus: what to pick, and where it will start."""
    img, d = k.new_image((width, S), k.DARK)
    d.rounded_rectangle((0, 0, width - 1, S - 1), 22, fill=k.EDGE)
    d.rounded_rectangle((6, 6, width - 7, S - 7), 18, fill=k.DARK)
    cols = [(x0 + 8, x1 - 8) for x0, x1 in _columns(width)]
    items = [
        (panel.title, 32, True, k.LIGHT, 1),
        (panel.headline, 26, True, k.LIGHT, 2),
        *[(line, k.MIN_TEXT, False, k.MUTE, 2) for line in panel.lines],
    ]
    _flow(ctx, d, items, cols, 22, S - 16)
    return img


PANELS = {
    "spotlight": _spotlight,
    "calm": _calm,
    "offline": _offline,
    "config_error": _config_error,
    "detail": _detail,
    "usage": _usage,
    "menu": _menu,
    "sent": _sent,
}
