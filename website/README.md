# Herdeck website

Bilingual (English/Czech) static product website. Astro serves HTML and a small
amount of browser JavaScript for the menu, documentation search, simulated deck,
and user-initiated video. There is no runtime connection to Herdeck agents.

## Develop and verify

Requires Node >=22.12 (Node 22 LTS recommended).

```sh
cd website
npm ci
npm run dev
npm run build
npx playwright install chromium
npm test
```

The dev server binds to `0.0.0.0:4321`. Use the host's current Tailscale address
when sharing a preview. It serves only the public marketing website. It does
not expose the authenticated Herdeck runtime, bridges, or user configuration.
Astro 7 runs its dev server in the background; `npx astro dev stop` stops it.

`npm test` starts the production preview on port 4323. Browser coverage checks
both languages, deep-link language switches, all internal routes/assets, demo
approval and denial, accent-insensitive docs search, mobile layout/navigation,
reduced motion, accessibility, and actual MP4 playback.

## Routes and content

- `/` redirects to `/en/` without JavaScript.
- `/en/` and `/cs/` each have home, features, workflow, download, and credits.
- `/en/docs/` and `/cs/docs/` have seven guide pages each.
- `src/lib/copy.ts` holds marketing translations.
- `src/lib/docs.ts` holds both versions of the curated documentation. Commands
  are shared. Each guide links to its operational source in the repository.
- Releases are linked through GitHub's stable `releases/latest` page, without
  stale asset filenames, fabricated downloads, or build-time API dependence.

When app behavior changes, update both guide translations against `../README.md`
and `../docs/agent-setup.md`; those remain the operational sources of truth.

## Film and music

`remotion/root.tsx` contains both editable 1280×720, 30 fps compositions. The
30-second films cut on a 128 BPM musical grid. The website serves checked-in
H.264/AAC MP4 exports through a native video element; it does not send the
Remotion editor to every visitor. No video/audio autoplay. Captions, a text
transcript, native controls, and downloads are available in both languages.

```sh
npm run film:studio
npm run film:render
```

The original score uses no external samples. To reproduce it, install Python
3.12+, numpy and FFmpeg with `libmp3lame`, then run `npm run film:audio` before
rendering. `scripts/compose-score.py` records the arrangement, deterministic
instruments and mastering. Intermediate WAVs are ignored; the MP3 is committed.

Rendering uses a temporary public-assets directory to avoid bundling previous
MP4 exports. It is cleaned in a `finally` block. Remotion is pinned to one version.
Remotion itself has its own [license](https://www.remotion.dev/license).

## Assets and design

The photo-style images are AI-generated product **visualizations**, not verified
hardware photography. This is disclosed in the footer, hardware guide and credits.
Prompts and art direction are in [DESIGN.md](DESIGN.md). Optimized WebP assets
live in `public/images/`; no third-party image hotlinks or runtime font requests.
Agent icons and the Herdeck logo reuse repository assets. Font license files are
in `public/fonts/`; icon attribution is in `../src/herdeck/assets/ATTRIBUTION.md`.

The complete website and two film editions stay together as one deliverable:
shared translations, URLs, assets and playback tests cross those boundaries.
This is a self-contained new `website/` module; application behavior is unchanged.

## Hosting

Deploy `dist/` to a static host after `npm run build`. Configure ordinary directory
indexes and `404.html`; no SPA catch-all rewrite, functions, secrets or database.
No hosting, domain, DNS, tunnel or firewall is provisioned by this implementation.

Until a public domain is chosen, builds deliberately emit `noindex, nofollow`,
`Disallow: /`, and no production canonical URLs. For publication, set the actual
origin at build time:

```sh
SITE_URL=https://your-chosen-domain.example npm run build
```

This enables canonicals, language alternates, absolute social-image URLs, robots
indexing and the bilingual sitemap. Verify the origin before publishing. Use
long immutable cache lifetimes only on Astro's hashed `/_astro/` output; public
images and films use stable filenames and should revalidate between releases.

Primary framework references: [Astro static routing](https://docs.astro.build/en/reference/routing-reference/),
[Remotion rendering](https://www.remotion.dev/docs/cli/render),
[Remotion audio](https://www.remotion.dev/docs/html5-audio).
