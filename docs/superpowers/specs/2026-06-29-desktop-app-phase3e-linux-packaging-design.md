# herdeck desktop app — Phase 3e: Linux packaging (design)

**Status:** design approved (brainstorming) · 2026-06-29
**Type:** phase design spec (součást [herdeck desktop app overview](2026-06-23-herdeck-desktop-app-overview.md), fáze 3 „Distribuce & polish")
**Depends on:** Phase 3a (frozen sidecar — PyInstaller pipeline + `build-sidecar.sh` + `smoke-sidecar.sh`), Phase 3d (icon set: PNG + `.icns`)

## Cíl

Vyrobit **instalovatelné Linux x86_64 artefakty** herdeck desktop appky —
Linux-frozen `herdeck.deckapp` sidecar zabalený do **AppImage + .deb + .rpm** —
stavěné a **ověřené v GitHub Actions CI**. macOS distribuce (arm64 `.app`/`.dmg`)
zůstává beze změny; cross-platform úpravy nesmí regresovat macOS build.

## Kontext

Jsme na macOS. Tauri Linux balení (webkit2gtk, appindicator, patchelf) ani
Linux PyInstaller freeze **nelze postavit ani ověřit lokálně** na Macu —
PyInstaller neumí cross-compile, mrazí vždy pro host arch. Jediná ověřitelná
cesta k Linux artefaktu vede přes **Linux CI runner**. Zelený CI job je proto
**gate** tohoto řezu (analogie macOS ručního `tauri build` gate).

## Scope

| # | Komponenta | Vrstvy |
|---|---|---|
| 1 | **Cross-platform freeze** — drop hard-coded arch z PyInstaller specu | `herdeck-deckapp.spec` + `build-sidecar.sh` (kosmetika) |
| 2 | **Cross-platform Tauri targets** — `"all"` místo macOS-only `["app","dmg"]` | `tauri.conf.json` |
| 3 | **Linux desktop metadata** — `.desktop` kategorie + tray runtime dep | `tauri.conf.json` (`bundle.category`, `bundle.linux.*`) |
| 4 | **CI Linux build workflow** — freeze + bundle AppImage/deb/rpm + artefakty | `.github/workflows/release.yml` (nový) |
| 5 | **Genericizace skriptů** — `build-app.sh` OS-neutrálně | `desktop/scripts/build-app.sh` (kosmetika) |

## Non-goals

- **arm64 Linux** — odloženo (private-repo GitHub arm64 hosted runnery jsou
  placené „larger runners"; PyInstaller arm64 freeze vyžaduje arm64 host nebo
  pomalou/křehkou QEMU emulaci). Zdokumentovaný follow-up.
- **Windows** — mimo scope celého produktu (herdeck nemá Win deck driver).
- **Signing / notarizace** — Linux ji zpravidla nevyžaduje; mimo scope (3b).
- **Flatpak / Snap** — jen AppImage/.deb/.rpm.
- **Wayland global hotkey** — Wayland principiálně blokuje global shortcuts;
  hotkey je na Linuxu best-effort (X11 funguje). Není to balicí problém.
- Změna runtime logiky sidecaru / Rust shellu — řez je build-system + CI.

## Global Constraints

Tato pravidla platí pro **každý** task plánu (kopíruj hodnoty verbatim):

- **Komunikace** lidská česky, **kód a commit messages anglicky**; conventional
  commits; **žádné `Co-Authored-By`**; po commitu zkontrolovat `roborev show <sha>`.
- **Push/PR/merge jen s explicitním souhlasem uživatele.**
- **macOS nesmí regresovat** — po změně PyInstaller specu MUSÍ na dev Macu projít
  `build-sidecar.sh` + `smoke-sidecar.sh` (host=arm64 freeze beze změny chování).
- **Cílová arch Linuxu = x86_64**; runner `ubuntu-24.04`. arm64 explicitně mimo.
- **Linux targets:** AppImage **+** .deb **+** .rpm (všechny tři).
- **CI trigger:** `workflow_dispatch` **+** `push: tags: ['v*']` (NE každý push —
  Linux build je 5–10 min a nesmí zpomalit rychlý test CI v `ci.yml`).
- **Token nikdy v JS / secret hodnoty jednosměrně** — beze změny (řez se jich
  netýká, ale platí).
- **Testy:** Python `.venv/bin/python -m pytest`, lint `.venv/bin/ruff check src tests`
  (OBĚ složky); Rust `cd desktop/src-tauri && cargo test`; freeze gate
  `bash desktop/scripts/build-sidecar.sh` + `smoke-sidecar.sh`.

---

## Komponenta 1: Cross-platform freeze

**Soubor:** `desktop/herdeck-deckapp.spec`

- `EXE(...)` má dnes natvrdo `target_arch="arm64"`. **Odstranit ho** (PyInstaller
  default `target_arch=None` → mrazí pro **host arch**). Na dev Macu je host arm64
  → identický výstup; na Linux x86_64 runneru → x86_64. Tím jeden spec slouží oběma
  OS bez větvení.
- `console=True` zůstává — na Linuxu neškodí (žádný Windows konzolový proces).
- `build-sidecar.sh`: verifikace (`test -x .../herdeck-deckapp`, kontrola
  `_internal/herdeck_assets`) je už OS-agnostická — PyInstaller 6 onedir layout je
  na Linuxu stejný, binárka je bezextenzní na obou. **Jen přeformulovat** macOS
  formulace v komentářích/echu („arm64 onedir" → „host-arch onedir") — žádná
  funkční změna.
- `HERDECK_PY` override už existuje → CI nastaví na Linux venv python.

**Pozn.:** Rust `resolve_frozen_sidecar` hledá `<resource_dir>/herdeck-deckapp/herdeck-deckapp`
(bezextenzní) — funguje na Linuxu beze změny. Onedir adresář (exe + `_internal/`)
Tauri zkopíruje přes `bundle.resources` do install tree; `resource_dir()` se na
Linuxu resolvuje stejně jako na macOS.

---

## Komponenta 2: Cross-platform Tauri targets

**Soubor:** `desktop/src-tauri/tauri.conf.json`

- `bundle.targets` je dnes `["app", "dmg"]` — to jsou **macOS-only** targety; na
  Linuxu `tauri build` s nimi selže.
- Změnit na **`"targets": "all"`** → Tauri vybere bundle targety platné pro host
  OS: na **macOS** = `app` + `dmg` (**identický výstup jako dnes**), na **Linux** =
  `appimage` + `deb` + `rpm`.
- `bundle.icon` ponechat (obsahuje `.icns` i PNG). Linux bundler `.icns` ignoruje
  a použije PNG ikony (`32x32`, `128x128`, `128x128@2x`, `icon.png` — už existují z 3d).
- **Dopad na macOS:** `"all"` na macOS = app+dmg, tedy beze změny. Ověří se
  freeze+smoke (Komponenta 1) + případně macOS `tauri build` v ručním gate.

**Pozn.:** `"all"` na Linuxu zahrne rpm → vyžaduje `rpmbuild` (balík `rpm`) v
prostředí. CI ho instaluje (Komponenta 4). Lokální Linux build bez `rpm` by na
rpm targetu selhal — to je akceptovatelné, CI je zdroj pravdy.

---

## Komponenta 3: Linux desktop metadata

**Soubor:** `desktop/src-tauri/tauri.conf.json`

- **`bundle.category`** — nastavit (např. `"Utility"`); Tauri z něj + `productName`
  + `identifier` generuje `.desktop` soubor pro deb/rpm/AppImage.
- **Tray runtime dependency** — floating deck má tray ikonu; na Linuxu ji kreslí
  `libayatana-appindicator3`. Tauri AppImage si appindicator lib bundluje sám
  (`TRAY_LIBRARY_PATH`):
  - **.deb:** `bundle.linux.deb.depends`: `["libayatana-appindicator3-1"]` —
    Tauri deb control file deklaruje pevný seznam depends (nespouští
    `dpkg-shlibdeps`), takže tray lib je vhodné doplnit explicitně. Debian/Ubuntu
    název je známý a správný.
  - **.rpm:** **bez explicitního `depends`** — `rpmbuild` automaticky generuje
    `Requires` pro linkované `.so` knihovny (scan ELF). Explicitní jméno balíku se
    na Fedora/RHEL liší od Debianu a špatný název by udělal **neinstalovatelný
    rpm** → raději spoléhat na auto-detekci. **Caveat:** tray appindicator lib je
    načítaná dynamicky (Tauri `TRAY_LIBRARY_PATH`), takže nemusí být ELF `NEEDED`
    a auto-Requires ji může minout → rpm tray-dep je v1 **best-effort**. CI proto
    loguje `rpm -qpR` (diagnostika, nefailuje) pro viditelnost; AppImage si
    appindicator bundluje, deb ji deklaruje explicitně — to jsou primární cesty.
  - webkit2gtk a core GTK libs doplňuje Tauri/rpmbuild sám.
- Žádné nové ikony — PNG z 3d stačí pro Linux.

---

## Komponenta 4: CI Linux build workflow

**Soubor:** `.github/workflows/release.yml` (nový; `ci.yml` se nemění)

```yaml
name: release
on:
  workflow_dispatch:
  push:
    tags: ["v*"]

jobs:
  build-linux:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - name: Install system deps
        run: |
          sudo apt-get update
          sudo apt-get install -y \
            libwebkit2gtk-4.1-dev libgtk-3-dev \
            libayatana-appindicator3-dev librsvg2-dev \
            patchelf rpm build-essential file desktop-file-utils
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - name: Freeze + smoke sidecar (Linux x86_64)
        run: |
          python -m venv .venv
          .venv/bin/pip install -e ".[packaging]"
          bash desktop/scripts/build-sidecar.sh
          bash desktop/scripts/smoke-sidecar.sh
      - uses: dtolnay/rust-toolchain@stable
      - uses: actions/setup-node@v4
        with: { node-version: "20" }
      - name: npm ci
        working-directory: desktop
        run: npm ci
      - name: tauri build (AppImage + deb + rpm)
        working-directory: desktop
        env:
          APPIMAGE_EXTRACT_AND_RUN: "1"   # runner nemá FUSE
        run: npm run tauri build
      - uses: actions/upload-artifact@v4
        with:
          name: herdeck-linux-x86_64
          path: |
            desktop/src-tauri/target/release/bundle/appimage/*.AppImage
            desktop/src-tauri/target/release/bundle/deb/*.deb
            desktop/src-tauri/target/release/bundle/rpm/*.rpm
          if-no-files-found: error
      # na tag připnout k GitHub Release (softprops/action-gh-release)
```

- **Trigger:** `workflow_dispatch` (ruční přes `gh workflow run release.yml`) +
  `push: tags: ['v*']`. NE každý push.
- **`HERDECK_PY`** se v `build-sidecar.sh` resolvuje na `$ROOT/.venv/bin/python`
  → CI vytvoří `.venv` přesně tam.
- **Stale-bundle clean** (krok před buildem) → `rm -rf .../target/release/bundle`;
  cargo cache zahrnuje `target`, takže warm-cache run by jinak mohl obnovit staré
  AppImage/deb/rpm a verify by prošel na zastaralém artefaktu (falešná zelená).
- **Per-format completeness check** (krok před uploadem) → ověří, že každý ze tří
  globů (`*.AppImage`, `*.deb`, `*.rpm`) matchl ≥1 soubor; chybějící jeden formát
  = červený job. (`if-no-files-found: error` failuje jen když nematchne NIC, tak
  by neodhalil samostatně chybějící rpm.)
- **`rpm -qpR` diagnostika** (nefailuje) → loguje runtime Requires rpm balíčku.
- **Release attach** jen na tag (na `workflow_dispatch` se jen nahrají artefakty).
- Caching (cargo/npm) je vítaný optimalizační detail, ne blocker.

---

## Komponenta 5: Genericizace build skriptů

**Soubor:** `desktop/scripts/build-app.sh`

- Dnes echuje „(.app + .dmg)" a je macOS-worded, ale fakticky jen volá
  `bash build-sidecar.sh` + `npm run tauri build` (OS-agnostické). S `targets:"all"`
  funguje i na Linuxu beze změny logiky.
- **Jen přeformulovat** komentář/echo OS-neutrálně („native bundles for the host
  OS"). Žádná funkční změna.

---

## Capabilities & dependencies

- **Žádné nové Rust/JS závislosti** — řez nemění runtime kód. (Autostart, tray,
  global-shortcut pluginy už jsou cross-platform z 3d; na Linuxu autostart =
  XDG `.desktop` v `~/.config/autostart/`, tray = appindicator.)
- **CI system deps** (jen v runneru): webkit2gtk-4.1, gtk-3, ayatana-appindicator3,
  librsvg2, patchelf, rpm, build-essential, file, desktop-file-utils.

## Testing

| Vrstva | Co | Jak |
|---|---|---|
| Freeze (macOS) | drop arch nerozbil arm64 host freeze | `build-sidecar.sh` + `smoke-sidecar.sh` na Macu (zelené) |
| Freeze (Linux) | x86_64 freeze + smoke projde | CI `build-linux` (freeze+smoke krok) |
| Bundle (Linux) | vzniknou AppImage + .deb + .rpm | CI `build-linux` (tauri build + upload-artifact `if-no-files-found: error`) |
| Config | `tauri.conf.json` validní JSON, `"all"` per-OS správně | macOS app+dmg (gate) + Linux 3 balíčky (CI) |
| Rust | beze změny logiky; Linux `cargo build` kompiluje | CI `build-linux` (tauri build zahrnuje cargo) |

- **Žádné nové Python/TS/Rust unit testy se nečekají** — logika beze změny.
  Pokud Linux `cargo build` odhalí `cfg(target_os)` potřebu, doplní se cílený
  test v rámci CI iterace.

## Manuální / CI gate

1. **macOS no-regrese (lokálně, teď):** `bash desktop/scripts/build-sidecar.sh &&
   bash desktop/scripts/smoke-sidecar.sh` → zelené (host=arm64).
2. **Linux build (CI, po merge+push):** `gh workflow run release.yml` → job
   `build-linux` zelený, artefakt `herdeck-linux-x86_64` obsahuje `.AppImage`,
   `.deb`, `.rpm`. (Lze ověřit i před merge: pre-release tag `v0.1.0-rc.1`
   pushnutý z branche spustí tag trigger; po ověření tag smazat.)
3. **(Volitelně) macOS `.app` gate:** `npm run tauri build` na Macu → app+dmg
   vznikají jako dřív (potvrzení, že `targets:"all"` nezměnil macOS výstup).

## Otevřené otázky

- Žádné blokující. arm64 Linux, Wayland hotkey, Flatpak/Snap, signing jsou
  vědomě mimo scope.
