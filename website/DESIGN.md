# Art direction and asset provenance

## Design plan

Herdeck is a tactile control surface for AI coding agents. The memorable element
is the hardware itself: large, close, grounded on a desk. Surrounding typography
and navigation stay quiet. The website should feel like a well-made instrument.

- Midnight navy `#09111f`: background, matching the dark-only desktop product.
- Blue steel `#101c2c`: panel surfaces.
- Brushed edge `#263549`: meaningful component boundaries.
- Cool white `#edf3fa`: primary text.
- Signal cyan `#78dfed`: primary controls and finished work.
- Waiting amber `#edb66b`: a request that needs a person.

Sora supplies the compact geometric headlines. Manrope carries readable body
copy. Both are self-hosted with Latin and Latin Extended coverage. Monospace
appears only in real commands.

Layout: left-aligned hero copy beside a manufacturer product image; a
quiet agent strip; three concrete capabilities; the interactive deck; a large
film frame; a hardware / desktop / remote composition; installation CTA.
Docs use a narrow searchable sidebar and a readable single article column.

```
brand       features workflow docs        language / download
headline / action              physical deck
supported agent marks
capabilities in three columns
explanation          interactive deck / request / decision
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

The current locally hosted WebPs are unmodified manufacturer assets from Ulanzi,
retrieved and visually checked on 2026-09-28:

- `public/images/deck-hero.webp`: [original D200 studio image](https://www.ulanzi.com/cdn/shop/files/14_14_11zon_381246ba-6f9d-4199-a711-d5c63a6ecb9c.webp?v=1726212953), 800 × 800.
- `public/images/deck-detail.webp`: [original D200 desk photograph](https://www.ulanzi.com/cdn/shop/files/8_8_11zon_8c923c8c-cc9c-4bd5-b66d-d07d2156f287.webp?v=1726212953), 800 × 800.

These show stock Ulanzi software, **not Herdeck running on hardware**. Credits,
alt text, the hardware guide and film attribution keep that distinction explicit.
Images remain Ulanzi property, excluded from the repository's MIT license.
No separate image redistribution license has been established; replace with
owner-provided photography or obtain permission before public distribution.
The old product page now redirects to D200X, so preserve these exact source URLs.

Hero and film composition use CSS positioning without repainting, mirroring,
or synthesizing the device. The interactive demo is a diagram of the 13-key
layout with eight simulated agents, five empty keys and the bottom-right display.

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
