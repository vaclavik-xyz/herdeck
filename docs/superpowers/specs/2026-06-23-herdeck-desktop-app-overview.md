# herdeck desktop app — product overview

**Status:** design approved (brainstorming) · 2026-06-23
**Type:** product overview (frames 3 phases; each phase gets its own design spec)

## Problém

herdeck dnes ovládá AI coding agenty přes fyzický Stream Deck (Ulanzi D200 /
Elgato) nebo přes web simulátor v prohlížeči. Konfigurace se dělá ručně v
`config.toml`/`local.toml` nebo přes tlačítka na decku — pro větší customizaci
nepohodlné a bez náhledu, co změna udělá. A když je uživatel mimo svoji stanici
(bez připojeného decku), nemá nativní způsob, jak agenty pohodlně sledovat a
ovládat.

## Produkt

**herdeck desktop app** — nativní desktopová aplikace (Tauri) s **dvěma okny** a
**tray ikonou**, postavená nad existujícím Python jádrem herdecku:

1. **Floating softwarový deck** — kompaktní always-on-top okno v rohu monitoru.
   Renderuje stejné dlaždice jako fyzický deck, klik = press přes bridge do
   reálného herdr. Náhrada HW decku na cestách (přes Tailscale), tray + autostart.
2. **Config editor** — plný GUI editor celého configu (servery, profily, themes,
   views, makra, launchery, notifikace, safety, hardware) s **live preview**
   decku (mock když nejsou agenti, živý když běží bridge).

Obě okna sdílejí jednu komponentu **DeckView** (render dlaždic + klik = press) a
jeden **Python sidecar** (render, config-service, bridge klient).

## Architektura (high-level)

```
Tauri shell (Rust): tray · autostart · always-on-top · 2 okna · hotkeys · balení
  ├─ Okno 1: Floating deck ─┐
  └─ Okno 2: Config editor ─┤→ DeckView (Svelte): render dlaždic + klik=press
                             │  loopback HTTP/JSON + /tile PNG (token-auth)
                             ▼
  Python sidecar (frozen):  render (orchestrator + icons)
                            · config-service (read/validate/write TOML)
                            · state source: mock | živý (bridge WS klient = connector)
                             ▼
                    běžící herdeck-bridge ── herdr socket ── agenti
```

**Klíčový princip: maximální reuse jádra.** Sidecar nereimplementuje logiku —
volá existující moduly. Mapa znovupoužití:

| Potřeba | Existující modul |
|---|---|
| render dlaždic z config+stavu | `orchestrator.Orchestrator`, `icons.render_tile_bytes`, `icons.compose_panel` |
| živý stav agentů + odeslání press | `connector.Connector` (WS klient, Bearer token, reconnect) |
| load/resolve/validace configu | `settings.load_settings/resolve_profile/validate_settings`, `config.*` |
| token-auth loopback HTTP + /tile PNG | vzor `driver/web.WebDeck` |
| frozen Python bundle | PyInstaller pipeline z Elgato packaging (`streamdeck/scripts/build-plugin.sh`, `*.spec`) |

## Fáze

Každá fáze = samostatný design spec → plán → implementace.

| Fáze | Náplň | Hodnota | Závisí na |
|---|---|---|---|
| **1 — Floating softwarový deck** (MVP/tracer) | Tauri skeleton + sidecar + DeckView + floating okno; mock + živé ovládání přes bridge; tray; spustitelné dev modem | Ovládání agentů bez HW, ověří celou osu end-to-end | — (reuse jádra) |
| **2 — Config editor okno** | TOML writer (`tomli-w`), formuláře všech sekcí, validace, preview (reuse DeckView), apply/reload, secret hygiene (keychain) | GUI nahradí ruční TOML | DeckView z f.1 |
| **3 — Distribuce & polish** | cross-platform build (macOS+Linux), signing/notarizace, autostart, globální hotkeys, ikony, onboarding | Instalovatelné koncovým uživatelům | f.1 + f.2 |

## Klíčová rozhodnutí

- **Stack:** Tauri (Rust shell) + web frontend (Svelte) + Python sidecar (frozen
  přes PyInstaller). Důvod: nejnativnější/nejmenší distribuovatelná binárka,
  always-on-top/tray/autostart, Python zůstává „mozkem" (reuse jádra).
- **Samostatná appka**, ne integrace do běžícího herdecku — funguje nad všemi
  front-endy (D200, web, Elgato plugin), čisté oddělení, živý náhled přes
  existující bridge WS.
- **Cílové OS:** macOS primárně (tam běží deck), Linux druhotně. Windows mimo
  scope (herdeck nemá Win deck driver).
- **Layout config okna:** sidebar + formulář + trvalý preview (varianta A) +
  lehká klikací vrstva (klik na dlaždici → skok do relevantní sekce). Jemná
  direct-manipulace je out-of-scope.
- **Preview:** mock (deterministická demo sada agentů) jako default; živý
  (read-only WS snapshot z bridge) když je dostupný. Stejná render pipeline,
  jen jiný zdroj stavu.
- **Secret hygiene:** server/telegram tokeny zůstávají jako `token_env` + hodnota
  v OS keychain; nikdy plaintext do TOML, nikdy do UI/logu.
- **TOML zápis** (fáze 2): `tomli-w`, generovaný soubor s hlavičkou „Managed by
  herdeck-config"; ruční komentáře zaniknou. Zachování komentářů (`tomlkit`) je
  možné pozdější vylepšení.

## Non-goals (celý produkt)

- Windows build.
- Jemná direct-manipulace (inline editace konkrétního pole přímo na decku).
- Zachování komentářů v TOML.
- GUI pro custom answer profiles (profile-schema je dnes podporuje jen omezeně).
- Nahrazení fyzického decku ve smyslu HID/USB — softwarový deck jde přes bridge,
  ne přes USB.

## Otevřené otázky (k vyřešení v jednotlivých fázích)

- Apply/reload mechanismus běžícího herdecku (mtime watch vs IPC signál) — fáze 2.
- Onboarding bez existujícího configu (první spuštění, zadání serveru/tokenu) —
  fáze 2/3.
- Strategie signing/notarizace a autostart per OS — fáze 3.
