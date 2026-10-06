# Art direction and asset provenance

## Design plan

Herdeck is a tactile control surface for AI coding agents. The memorable element
is the deck itself: the hero is a live, faithful miniature of the real D200 tile
UI (colours, anatomy and status words from the renderer), not a picture of it.
Surrounding typography and navigation stay quiet. Status colours are reserved
for agent state; site chrome never borrows them.

- Midnight navy `#09111f`: background, matching the dark-only desktop product.
- Blue steel `#101c2c`: panel surfaces.
- Brushed edge `#263549`: meaningful component boundaries.
- Cool white `#edf3fa`: primary text.
- Cool white is also the primary button, so no status colour doubles as "click me".
- Signal cyan `#78dfed`: text links and finished work.
- Waiting amber `#edb66b`: a request that needs a person.
- Deck tiles use the renderer's own fills (working `#0d3d17`/`#28b446`, done
  `#0f4449`/`#2dc8d7`, idle `#11284a`/`#3278dc`, blocked `#4e3906`/`#e6aa14`),
  with status words lifted slightly for WCAG AA contrast on the web.

Sora supplies the compact geometric headlines. Manrope carries readable body
copy. Both are self-hosted with Latin and Latin Extended coverage. Monospace
appears only in real commands. Inside the simulated deck only, Inter matches the
tile renderer's typeface.

Layout: left-aligned hero copy beside the interactive deck (press the amber key or
panel to answer); a quiet agent strip; three concrete capabilities; a large film
frame; a hardware / desktop / remote composition; installation CTA.
Docs use a narrow searchable sidebar and a readable single article column.

```
brand       features workflow docs        language / download
headline / action              interactive deck / request / decision
supported agent marks
capabilities in three columns
film title                         description
                  cinematic film
hardware photography       desktop / remote surfaces
                      download
```

## Review against the brief

Dark backgrounds and cyan can become generic software styling. We deliberately
avoid decorative glows, neon fog, gradient words and gratuitous status counters.
The color palette comes from the app and real agent statuses. The tactile hero,
a working request/decision loop, hardware photography and beat-cut product film
carry the identity. Numbering is reserved for actual workflow steps.

Czech and English have equally complete routes, documentation, captions and
rendered films. Page transitions are ordinary links; no scroll hijacking,
background sound, looping decorative movement or autoplay.

## Product images

The D200 has 13 square keys in a **5 + 5 + 3** arrangement and a horizontal
status display occupying the remaining two positions at the **bottom right**.
It has no rotary knobs. Do not confuse it with the D200X. Verify every future
product depiction against a real D200 reference before using it.

The initial text-generated images invented the enclosure and put a vertical
screen at the left. They were rejected and removed from the website and films.
Do not reuse those images or use them as generation references.

The corrected geometry was initially used with stock Ulanzi icons. That did not
communicate Herdeck, so the current assets replace the screen contents with
agent tiles and a status panel based on the actual application renderer.

### Hardware source images

Manufacturer sources, retrieved and visually checked on 2026-09-28:

- `design/ulanzi-d200-source.webp`: [original D200 studio image](https://www.ulanzi.com/cdn/shop/files/14_14_11zon_381246ba-6f9d-4199-a711-d5c63a6ecb9c.webp?v=1726212953), 800 × 800.
- `design/ulanzi-d200-desk-source.webp`: [original D200 desk photograph](https://www.ulanzi.com/cdn/shop/files/8_8_11zon_8c923c8c-cc9c-4bd5-b66d-d07d2156f287.webp?v=1726212953), 800 × 800.

Original Ulanzi imagery is excluded from the repository's MIT license.
No separate image redistribution license has been established; replace with
owner-provided photography or obtain permission before public distribution.
The old product page now redirects to D200X, so preserve these exact source URLs.

### Herdeck UI reference and compositing

`design/agent-ui-reference.png` is rendered by the real `IconProvider` and
`compose_panel` functions in `src/herdeck/icons.py`, using bundled agent glyphs
and Inter fonts, tint fills and synthetic project/session data. It contains
13 agent tiles and the bottom-right approval spotlight. It is reproducible:

```sh
PYTHONPATH=src python website/scripts/render-agent-reference.py
```

Use a Python environment with the root project's `dev` extra. This script has
no runtime, hardware or network connection and uses a temporary icon cache.

The built-in OpenAI `image_gen` tool edited each manufacturer's image using
that UI reference. The second edit also referenced the completed hero for
screen appearance. Full prompts and input roles are committed in
[design/screen-composite-prompts.md](design/screen-composite-prompts.md).

Selected exports (1254 × 1254 WebP, quality 92):

- `public/images/deck-hero.webp` — source generation `exec-97d78ea9-6360-4ba6-8c97-981e66dab610.png`.
- `public/images/deck-detail.webp` — source generation `exec-92ee16bc-6fda-45f4-a2dc-94cd25a39f7b.png`.

These are **edited product visualizations**, not photographs of a live Herdeck
session. The rendered UI is a reference for the edit, not a promise of exact
pixel transfer. Credits, alt text and the hardware guide identify the composites.
Both outputs were visually checked for the 5 + 5 + 3 square keys, bottom-right
horizontal panel, recognizable agent marks and absence of stock app icons.
The enclosure, stand, camera angle and hand placement follow the source D200.

The browser demo remains an interactive diagram with eight simulated agents,
five empty keys and the bottom-right display. Both localized Remotion films
use the same selected images and identify the hardware/UI sources.

## Film direction

A 30-second product film at 128 BPM. The 64-beat structure:

- beats 0–8: reveal the device and the promise;
- 8–16: waiting vs working;
- 16–24: the whole fleet;
- 24–32: read and approve, over the musical build;
- 32–40: D200 in use, return to flow, musical drop;
- 40–52: deck, desktop, remote surfaces;
- 52–64: identity, repository and resolution.

All movements are deterministic Remotion frame animations. The same composition
is rendered separately for Czech and English. The original synthesized score
is reproducible from `scripts/compose-score.py` and uses no third-party samples.
