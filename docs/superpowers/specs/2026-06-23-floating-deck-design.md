# Fáze 1 — Floating softwarový deck (design spec)

**Status:** design approved (brainstorming) · 2026-06-23
**Parent:** [herdeck desktop app — product overview](2026-06-23-herdeck-desktop-app-overview.md)
**Phase:** 1/3 (MVP / tracer bullet)

## Cíl a hodnota

Nativní **floating always-on-top okno**, které renderuje deck a ovládá reálné
agenty přes bridge — náhrada fyzického decku, když ho nemáš u sebe (na cestách,
přes Tailscale). Zároveň je to **tracer bullet**: ověří celou osu Tauri shell →
Python sidecar → reuse jádra (render + connector) end-to-end, na které pak staví
fáze 2 (config editor) a 3 (distribuce).

## User scénáře

1. **Na cestách.** Uživatel je mimo stanici s HW deckem. Spustí herdeck desktop
   app; floating okno se připojí přes Tailscale k běžícímu bridge a ukáže živé
   agenty. Klikne na blocked agenta → approve jde do herdr.
2. **Hned po spuštění bez configu/bridge.** App ukáže **mock** deck (deterministická
   demo sada agentů), aby bylo vidět, že funguje, + hint jak nastavit server.
3. **Always-on-top přehled.** Okno zůstává v rohu nad ostatními; barvy dlaždic
   ukazují stav (working/idle/blocked/done), footer souhrn („4 agenti · ⚠ 1 blocked").

## Scope

**In:**
- Tauri app skeleton (jedno okno pro fázi 1: floating deck), tray ikona
  (show/hide/quit), always-on-top, kompaktní výchozí rozměr, spawn + supervize
  Python sidecaru.
- Python sidecar: token-auth loopback HTTP server (vzor `web.WebDeck`), který
  servíruje deck stav + `/tile` PNG a přijímá `/press`.
- Zdroj stavu: **mock** (default bez configu) a **živý** (`connector.Connector`
  proti bridge ze `config.toml`).
- DeckView komponenta (Svelte): poll `/state`, render PNG dlaždic + panel, klik =
  `POST /press`. Převzít poll+diff verzování z `web.WebDeck` JS.
- Spustitelné dev modem (`tauri dev` + sidecar z venv); frozen sidecar volitelně.

**Out (fáze 2/3):**
- Druhé okno (config editor), zápis configu, formuláře.
- Globální hotkeys, autostart, signing/notarizace, .app/.deb balíček.
- Onboarding UI pro zadání serveru/tokenu (fáze 1 čte existující `config.toml`).
- Drill-in / macro / launcher interakce nad rámec toho, co dnes dělá deck
  press path (fáze 1 zrcadlí chování existujícího orchestratoru).

## Architektura fáze 1

```
┌─ Tauri shell (Rust) ─────────────────────────────────────────┐
│  · spawn + supervize sidecaru (jako Elgato TS shell)          │
│  · floating okno: always-on-top, kompakt, bez chromu          │
│  · tray: show/hide/quit                                        │
│  · načte loopback URL+token od sidecaru, předá je WebView     │
│  ┌─ WebView: DeckView (Svelte) ─────────────────────────────┐ │
│  │  poll GET /state → diff → GET /tile/{i} PNG, /panel PNG   │ │
│  │  klik → POST /press/{i}                                    │ │
│  └──────────────────────────────────────────────────────────┘ │
└───────────────────────────┬──────────────────────────────────┘
                            │ loopback HTTP/JSON (token-auth)
        ┌───────────────────▼──────────────────────────────────┐
        │ Python sidecar `herdeck.deckapp` (frozen-able)         │
        │  HTTP server (vzor web.WebDeck): /state /tile /panel   │
        │                                  /press /health         │
        │  Orchestrator(config) ← render() → PNG                  │
        │  StateSource:                                           │
        │    · MockSource  (deterministická demo sada)            │
        │    · LiveSource  (Connector → on_snapshot/on_event)     │
        │  press → LiveSource: Connector.send(commands)           │
        └───────────────────┬───────────────────────────────────┘
                            │ (živý) WS, Bearer token
                  běžící herdeck-bridge ── herdr ── agenti
```

## Komponenty a rozhraní (jednotky)

### 1. Python sidecar — `herdeck.deckapp` (nový modul)
- **Vstup:** env/argv — cesta ke `config.toml` (volitelně), `HERDECK_MOCK`,
  loopback host/port (default 127.0.0.1:0 → vypíše skutečný port + token na
  stdout jako JSON, aby ho Tauri přečetl; vzor: `web.WebDeck` token + Elgato
  discovery contract).
- **Odpovědnost:** držet `Orchestrator`, vybraný `StateSource`, HTTP server;
  překládat HTTP požadavky na render/press.
- **HTTP API** (token-auth, loopback) — podmnožina vzoru `web.WebDeck`:
  ```
  GET  /state           → {version, slots, has_panel, panel, tiles:{i:ver},
                           summary:{agents, blocked, working, idle, done},
                           source:"mock"|"live", connected:bool}
  GET  /tile/{i}        → image/png
  GET  /panel          → image/png
  POST /press/{i}       → 204 (injektuje press do orchestratoru)
  GET  /health          → {ok, source, connected, server_id|null}
  ```
- **Závisí na:** `orchestrator`, `icons`, `connector`, `settings`/`config`,
  `model`.

### 2. StateSource (nový, dvě implementace)
- **`MockSource`** — deterministická demo sada `AgentState` (5–8 agentů napříč
  stavy a 2 servery), žádná náhoda. Press v mock režimu jen lokálně přepne stav
  (vizuální feedback), nikam neposílá.
- **`LiveSource`** — drží `Connector` (z fáze: `ServerConfig` z resolved configu),
  callbacky `on_snapshot/on_event/on_connection` aktualizují orchestrator stav;
  press → přeloží `Orchestrator.on_press(index)` na `Command`y → `Connector.send`.
  Reconnect/backoff řeší `Connector` sám.
- **Rozhraní:** `apply_to(orchestrator)`, `press(index) -> None`,
  `connected: bool`, `summary()`.

### 3. Tauri shell (Rust)
- Spawn sidecaru jako child proces (analogie `streamdeck/src/backend-process.ts`),
  čtení loopback URL+token z jeho stdout (JSON řádek), předání WebView přes Tauri
  config/IPC. Supervize: restart při pádu, kill při quit.
- Okno: `always_on_top=true`, `decorations=false` (nebo minimální), malý výchozí
  rozměr, pozice roh. Tray: show/hide/quit.

### 4. DeckView (Svelte komponenta)
- Port JS z `web.WebDeck._PAGE`: poll `/state` (300 ms), verzový diff, refetch
  jen změněných `/tile/{i}` a `/panel`, klik → `POST /press/{i}` s token
  hlavičkou. Plus footer souhrn ze `summary`. Komponenta je už psaná pro reuse
  ve fázi 2 (preview).

## Datové toky

- **Živý render:** bridge → `Connector.on_snapshot/on_event` →
  `Orchestrator.apply_snapshot/apply_event` → (poll) `render()` → `icons` PNG →
  `/tile` → DeckView.
- **Press (živý):** DeckView klik → `POST /press/{i}` → `Orchestrator.on_press` →
  `Command`y → `Connector.send` → bridge → herdr. Non-idempotentní sendy se
  neretryují (zachovat chování bridge/connector).
- **Mock:** `MockSource` plní orchestrator demo stavem; press lokálně přepne stav.

## Konfigurace a discovery

- Sidecar čte existující `config.toml`/`local.toml` přes `settings.load_settings`
  + `resolve_profile` (stejná pravda jako runtime). Server URL z configu, **token
  z env/keychain** (jako dnes `ServerConfig.token`). Chybí-li config nebo token →
  **mock režim** + `/health` to hlásí (UI ukáže hint).
- Sidecar↔Tauri token: jednorázový token vygenerovaný sidecarem (jako
  `web.WebDeck._press_token`), předaný Tauri přes stdout; loopback bind only.

## Error handling

- **Bridge nedostupný / výpadek:** `Connector` reconnectuje s backoffem; `/state`
  hlásí `connected:false`; DeckView ukáže offline indikaci (panel „OFFLINE",
  reuse `layout.panel_overview` chování).
- **Chybí token/config:** mock režim, `/health` `source:"mock"`, UI hint.
- **Sidecar spadne:** Tauri ho restartuje; WebView ukáže „reconnecting" dokud
  `/health` neodpovídá.
- **Neplatný press index / crafted request:** ignorovat (vzor `web.WebDeck.press`).

## Bezpečnost / secret hygiene

- Loopback bind only (127.0.0.1), token-auth na všech endpointech (constant-time
  compare, vzor `web.WebDeck`).
- Bridge token se nikdy neobjeví v UI, stdout logu ani v `/state`/`/health`
  odpovědi (jen `server_id`). Žádné tokeny do souborů.

## Testing

- **Python (pytest):** `MockSource` deterministická sada; sidecar HTTP endpointy
  (state/tile/press/health) s fake orchestratorem; token-auth (403 bez tokenu);
  press → command překlad přes `LiveSource` s fake connectorem.
- **Frontend (vitest):** DeckView verzový diff (refetch jen změněných), klik →
  POST, offline stav.
- **Rust (cargo):** spawn/supervize sidecaru (parsování stdout JSON), tray/okno
  config smoke (kde testovatelné).
- **E2E smoke:** `HERDECK_MOCK=1` sidecar + headless fetch `/state`+`/tile` →
  neprázdné PNG; (volitelně) proti dev bridge ukázat živý snapshot.

## Evidence gates (fáze 1 „done")

Každý gate = command + exit_code + cwd + truncated output (per company invariant):
- `pytest` (herdeck core + nové sidecar testy) zelené.
- `ruff`/lint zelené.
- `npm test` (vitest) v adresáři Tauri frontendu zelené.
- `cargo build` + `cargo test` Tauri shellu zelené.
- E2E mock smoke: sidecar v mock režimu vrátí neprázdné `/state` a `/tile/0` PNG
  (uložený truncated výstup).
- Manuální/skriptové ověření: `tauri dev` spustí floating okno, mock deck se
  vykreslí, klik vyvolá `POST /press` (log/trace).

## Návrh thin vertical slices (pro company Architect/slicing)

1. **Sidecar mock core:** `herdeck.deckapp` HTTP server + `MockSource` + render +
   `/state`/`/tile`/`/panel`/`/press`/`/health`. Gate: pytest + e2e mock smoke.
2. **DeckView (Svelte):** komponenta + poll/diff/press; běží proti sidecaru z
   slice 1. Gate: vitest.
3. **Tauri shell:** okno (always-on-top/tray) + spawn/supervize sidecaru +
   napojení WebView. Gate: cargo build/test + `tauri dev` smoke.
4. **LiveSource:** `Connector` napojení, press→command, offline handling; přepínač
   mock/live dle configu. Gate: pytest (fake connector) + ruční živý smoke.

Owned paths jsou disjunktní (Python `src/herdeck/deckapp*` + tests; frontend dir;
Tauri Rust dir), takže slices jdou převážně paralelně po slice 1.

## Otevřené otázky (fáze 1)

- Přesný název/umístění Tauri projektu v repu (např. `desktop/` vedle
  `streamdeck/`). Rozhodne Architect dle konvencí repa.
- Zda fázi 1 rovnou frozit sidecar (PyInstaller) nebo nechat na fázi 3 — default:
  dev mode (venv) stačí pro fázi 1, frozen je fáze 3.
- Mock press chování (přepínat stavy vs statické) — default: lehké přepnutí pro
  vizuální feedback.
