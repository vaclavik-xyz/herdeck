# Bundled agent icons — attribution

The original agent marks in this directory (`claude.svg`, `codex.svg`,
`copilot.svg`, `cursor.svg`, `gemini.svg`, `opencode.svg`) are sourced from
[Simple Icons](https://simpleicons.org) (the `/white` monochrome variant).

- **Icon data** is distributed by Simple Icons under **CC0 1.0** (public domain).
- **The brand marks themselves remain trademarks of their respective owners**
  (Anthropic, OpenAI, Microsoft/GitHub, Cursor, Google, OpenCode). They are
  bundled solely to identify which agent a deck tile represents. Their use here
  does not imply any affiliation with or endorsement by those owners.

Additional monochrome marks are sourced from
[Lobe Icons 1.94.0](https://github.com/lobehub/lobe-icons/tree/v1.94.0)
(MIT): Amp, Antigravity (`agy`), Cline, Devin, Grok, Hermes Agent, Kilo Code,
Kimi, Kiro, Mastra (`mastracode`), Pi, Qoder (`qodercli`), and Qwen.

Three products use their official project artwork:

- Factory Droid: the monochrome mark from `https://factory.ai/favicon.svg`;
- OMP: `packages/collab-web/public/favicon.svg` from
  [unsigned-gg/omp](https://github.com/unsigned-gg/omp/tree/0c6c981ee5838d97700180383077eb0a3790637c)
  (MIT), converted to monochrome;
- Maki: `site/apple-touch-icon.png` from
  [tontinton/maki](https://github.com/tontinton/maki/tree/33b31d9ba2decaca31e387e9bda77c7b9b47b387)
  (MIT).

The brand marks remain trademarks of their respective owners. They are bundled
solely to identify the agent represented by a deck tile; their use does not
imply affiliation or endorsement. The sources and checksums used to refresh
them live in `scripts/sync-agent-icons.py`. All marks are committed as static
assets, so Herdeck renders the deck fully offline.

## Lucide (project-less T3 threads)

The "no project" mark on a T3 thread created without a project is Lucide's
`message-square-dashed` icon ([lucide-static 1.61.0](https://lucide.dev)),
the same icon T3 Code shows for it. Its path data is transcribed into
`src/herdeck/icons.py` (`_NO_PROJECT_STROKES`) and drawn with Pillow; no SVG
file is shipped. Lucide is distributed under the ISC License:

```
ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```
