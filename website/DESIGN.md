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

Layout: left-aligned hero copy beside a wide photographic visualization; a
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
hardware visualization       desktop / remote surfaces
                      download
```

## Review against the brief

Dark backgrounds and cyan can become generic software styling. We deliberately
avoid decorative glows, neon fog, gradient words and gratuitous status counters.
The color palette comes from the app and real agent statuses. The tactile hero,
a working request/decision loop, macro photography and beat-cut product film
carry the identity. Numbering is reserved for actual workflow steps.

Czech and English have equally complete routes, documentation, captions and
rendered films. Page transitions are ordinary links; no scroll hijacking,
background sound, looping decorative movement or autoplay.

## Product images

Generated with the built-in OpenAI image generation tool on 2026-09-28.
Images are explicitly disclosed as visualizations in the site footer, credits,
and hardware guide. They do not claim to document exact production hardware.

Files (optimized WebP, no external dependencies):
- `public/images/deck-hero.webp`
- `public/images/deck-detail.webp`

### Hero prompt

Use case: product-mockup. Create a premium photorealistic hero photograph for Herdeck, software that turns an Ulanzi D200 into a tactile command center for AI coding agents. Wide landscape 1792x1024 composition. Subject: a matte graphite desktop stream controller, low wide rectangular wedge, 5 columns by 3 rows of square glossy LCD keys; the first column upper two cells is one tall status screen, remaining 13 individual keys. Chunky rounded black beveled enclosure, subtle brushed metal desk, realistic USB-C cable leaving the back. Screens are midnight navy, tiny restrained white AI abstract symbols and miniature lines of UI, with cyan and muted green status indicators, one key glows warm amber. NO readable slogans, NO overlaid text, NO fake branding, NO decorative giant symbols. The controller occupies the RIGHT TWO THIRDS of the image, perspective view from 40 degrees above and slightly left, close enough to see surface detail. Left third stays deep clean navy negative space. Sophisticated high-end real product photography, like professional camera equipment campaign. Very dark navy seamless studio backdrop #080f1d and dark brushed slate tabletop. Narrow cool blue rim light across top edge, soft warm neutral key light bringing out physical materials; subtle cyan key light reflected on fingertips not needed since NO hand in this shot. Rich detailed blacks, crisp tactile glass keycaps, medium shallow depth of field, natural reflections and restrained film grain. No cyberpunk, no neon fog, no rainbow, no purple, no floating objects. Hardware visually grounded on desk, cinematic but believable.

### Detail prompt

Use case: product-mockup. Supporting reference is the Herdeck product visualization just generated. Make a SECOND, DISTINCT photorealistic editorial shot of this exact dark graphite 5-column LCD stream controller, with the same realistic 13 keys and taller upper-left status panel. Extreme low-angle macro close-up of a person's index finger pressing ONE warm amber lit glass key, nearby keys showing tiny cool cyan/green status dots and abstract minimal UI symbols. Strong tactile feeling, shallow depth of field, fingerprint ridges, glass reflections, fine dark metal/plastic texture. Wide 16:10 composition, the key and finger center-right. Edge of controller at bottom. Deep midnight blue studio setting and brushed dark desk matching reference. Color is dark slate blue with restrained amber and cyan only. Premium professional camera close-up, believable materials, not a sci-fi render. No overlaid text, no slogans, no brand logos, no watermarks. Make it emotionally about the satisfying physical button press.

The second generation used the hero image as a visual reference. The source
image was inspected before use. Original generated PNGs remain in the local
generation archive; committed WebP exports are the complete web inputs.

## Film direction

A 30-second product film at 128 BPM. The 64-beat structure:
- beats 0–8: reveal the device and the promise;
- 8–16: waiting vs working;
- 16–24: the whole fleet;
- 24–32: read and approve, over the musical build;
- 32–40: physical press, return to flow, musical drop;
- 40–52: deck, desktop, remote surfaces;
- 52–64: identity, repository and resolution.

All movements are deterministic Remotion frame animations. The same composition
is rendered separately for Czech and English. The original synthesized score
is reproducible from `scripts/compose-score.py` and uses no third-party samples.
