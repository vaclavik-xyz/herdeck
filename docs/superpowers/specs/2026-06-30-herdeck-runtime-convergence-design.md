# herdeck as a runtime — convergence design

**Status:** design approved (brainstorming) · 2026-06-30
**Type:** architecture (unify two processes into one runtime + thin clients)
**Strategy:** A — build a herdeck-owned runtime now; designed to later migrate onto herdr's coming native client API (the herdr-coupling is isolated behind one interface).

## Problém / cíl

Dnes běží herdeck na macbench jako **dva oddělené procesy**, každý s vlastním Orchestrátorem, vlastním připojením k herdr bridge a vlastním životním cyklem:

- **D200 deck** — `python -m herdeck.app` (launchd služba): pohání fyzický USB displej (`D200Driver` + `RenderPump`) a má **ticker** (`app.py:handle_tick` → `orch.tick()`), takže spinner se plynule animuje.
- **Desktop okno** — Tauri `.app` spouští frozen sidecar `herdeck.deckapp`: serveruje PNG dlaždice + `/state` přes HTTP do Svelte/webview okna, ale **nemá ticker** — překresluje jen na události z bridge.

Důsledky (ověřeno laděním): v okně se **spinner neanimuje** a deck vypadá „zaseknutě" (mezi událostmi stojí), **elapsed čísla se liší** od D200 (každá instance si kotví „jak dlouho agent běží" k okamžiku, kdy ho **ona** uviděla — `orchestrator._since[key] = (status, self._clock())`), a běží **dvě připojení** k bridge.

Vize (po vzoru herdr — „herdr isn't an app, it's a runtime; clients attach over one protocol"): **jeden herdeck runtime** vlastní deck-stav a víc tenkých klientů se k němu připojuje. Cíl: synchronizované pohledy, **animace všude**, **konzistentní elapsed**, **jedno připojení** k bridge.

## Rozhodnutí (brainstorming)

1. **D200 musí běžet headless** (jako dnešní launchd služba) → **runtime = ta headless služba**, okno je jen klient.
2. **Desktop = nativní Tauri okno jako tenký klient** (zachová tray/borderless/floating/content-fit z window-ux fáze; přestane spouštět vlastní orchestrator/bridge).
3. **Rozsah v1 = runtime vlastní VŠECHNO** — deck + config editor + onboarding. Tauri je čistě tenký klient.
4. **Konvergence do jedné služby** (ne nový dedikovaný démon): vytáhne se sdílené Runtime jádro + sdílený HTTP povrch; jedna služba běží s víc sinky.

## Architektura

```
                    herdr bridge (m4, ws://…:8788)
                              │  (JEDNO připojení)
            ┌─────────────────▼─────────────────────────┐
            │   herdeck RUNTIME  (headless launchd, .venv)│
            │   1× Orchestrator + Connector + ticker +    │
            │   hodiny + ConfigWatcher                    │
            │   render fan-out do SINKŮ:                  │
            │     ├─ D200 USB sink (D200Driver+RenderPump)│
            │     └─ HTTP server (token-auth, localhost): │
            │          /state /tile /panel  (deck)        │
            │          /config /secret /profiles (editor) │
            │          /setup /setup/connect (onboarding) │
            │   → píše ~/.cache/herdeck/runtime.json       │
            │     {url,host,port,token} (0600)            │
            └───────▲───────────────────────▲─────────────┘
        D200 frames │                       │ HTTP poll /state + /press
        ┌───────────┴─────┐        ┌────────┴───────────────┐
        │ D200 fyzický deck│        │ Tauri okno (tenký klient)│
        │  (SINK, ne klient)│       │ čte runtime.json → attach│
        └──────────────────┘        └──────────────────────────┘
```

**Runtime** drží **jeden** Orchestrator + Connector k herdr + ticker + hodiny + ConfigWatcher. Renderuje **jednou** a fan-outuje `RenderState` do seznamu **sinků**: D200 USB + HTTP buffer. Stisk z D200 USB i z okna teče do **téhož** Orchestratoru pod jedním zámkem.

**Discovery (klíčový posun):** runtime běží jako headless launchd služba (ne Tauri-spawnutý sidecar), takže discovery `{url,host,port,token}` **zapíše do souboru** `~/.cache/herdeck/runtime.json` (perm 0600), na čistý exit ho smaže. Stdout discovery řádek zůstává pro fallback. Tauri okno na startu soubor přečte + pingne `/health` → **attach** (žádný vlastní sidecar). Když runtime neběží (stroj bez D200), Tauri **spustí vlastní** bundlovaný runtime web-only (jako dnes) → `.app` zůstane soběstačná.

**Secret hygiena:** herdr bridge token žije jen v `Connector` runtime, nikdy neopustí server-side. Přes discovery jde jen **lokální HTTP API token** (localhost auth) — přesně jako dnes přes stdout řádek.

## Komponenty / soubory

**Klíč: config + onboarding se NEstěhují.** `deckapp/server.py` je už dnes serveruje a má Orchestrator + bridge (`LiveSource`) + ConfigWatcher. Takže **deckapp server SE STANE runtime** — přidáme mu chybějící části a `herdeck.app` D200 cesta se do něj vstřebá.

**Python (runtime):**
- `src/herdeck/deckapp/sinks.py` (nové) — `RenderSink` protokol + `HttpTileSink` (zabalí stávající `/state` tile buffer + verzování) + `D200Sink` (zabalí `D200Driver` + `RenderPump`; full + working frames; vlastní press reader). `DeckApp` renderuje jednou a fan-outuje do seznamu sinků.
- `src/herdeck/deckapp/server.py` (změna) — `DeckApp` dostane seznam sinků (HTTP buffer = jeden sink) + background **ticker** (smyčka: `orch.tick()` → re-render → fan-out; working-only frames mezi tiky, full refresh každých N tiků kvůli elapsed — logika přebraná z `app.py:handle_tick`). D200 sink se postaví **jen** když je importovatelné `hid`/`strmdck` a zařízení je přítomno; jinak HTTP-only.
- `src/herdeck/runtime.py` (nové, headless entry) — postaví `DeckApp` s oběma sinky + tickerem + zapíše discovery soubor; běží jako launchd služba. `herdeck.app` D200-CLI role se sem vstřebá (sdílené části: Orchestrator, Connector, ticker, deck-kind detekce).
- Discovery soubor — writer (`~/.cache/herdeck/runtime.json`, 0600, smazat na exit) + čtečka sdílená s Tauri kontraktem.

**Tauri (klient):**
- `desktop/src-tauri/src/sidecar.rs` (+ discovery) (změna) — **attach-or-spawn**: na startu přečti `runtime.json` + pingni `/health`. Živé → použij `{url,token}` (žádný spawn). Mrtvé/není → spawn bundlovaný sidecar (web-only) jako dnes. Stávající stdout discovery zůstává pro spawn větev.
- Frontend (`desktop/src/...`) — **beze změny tvaru**: `deckClient` poll `/state`, config/onboarding klienti na `/config`+`/setup` — jen zdroj discovery se mění (soubor vs stdout). Tray/borderless/content-fit nedotčeno.

**Nasazení:**
- `~/Library/LaunchAgents/com.herdeck.app.plist` (macbench) → unified runtime entry (D200 sink + HTTP + discovery). Už má `HERDECK_DECK=d200` + token env. `.app` redeploy s attach-or-spawn.

## Data flow

1. **bridge → deck:** Connector callback (snapshot/event/connection) → `LiveSource` buffer → pod `DeckApp` zámkem `orch.apply_snapshot`/`set_connection` → render → fan-out (HTTP verze bump + D200 push).
2. **ticker → animace:** každý `tick_interval` → `orch.tick()` posune spinner fázi → render working dlaždic → fan-out → **okno i D200 animují ze stejné fáze**. Full refresh každých N tiků posune elapsed na všech sincích.
3. **press:** z D200 USB (reader) *nebo* z okna `/press/i` → **týž** `orch.on_press(i)` pod zámkem → `Command`y → bridge send (`LiveSource.press` / runner). Oba zdroje stisků ústí do jednoho Orchestratoru.
4. **config/onboarding:** okno → `/config` nebo `/setup` → `config_service` zápis `config.toml` → ConfigWatcher → `orch` reload → render → fan-out. Jeden zdroj pravdy.

## Error handling

- **Runtime dole při startu Tauri** → `runtime.json` chybí/stale nebo `/health` selže → Tauri spustí vlastní bundlovaný sidecar (web-only). Graceful.
- **D200 odpojen / není** → runtime D200 sink nepostaví (HTTP-only), okno jede dál. Hotplug = mimo v1 (restart runtime ho vezme).
- **bridge dole** → `LiveSource.connected=False` → offline UI na všech sincích (stávající chování).
- **Stale `runtime.json`** (runtime spadl) → Tauri `/health` ping selže → fallback spawn. Runtime soubor maže na čistý exit; po crashi ho detekuje selhaný `/health`.
- **Dvojí runtime** (launchd + omylem Tauri-spawnutý) → Tauri vždy nejdřív zkusí `/health`; když runtime odpoví, attachne a nespouští vlastní.

## Testy

- **Unit (pytest):** `RenderSink` fan-out (fake sink dostane týž `RenderState` jako HTTP sink); ticker posune spinner fázi a fanne do sinků; `D200Sink` press → `orch.on_press`; discovery write/read + perm 0600; deck-bez-D200 → HTTP-only (D200 sink se nepostaví).
- **Unit (Rust + vitest):** attach-or-spawn rozhodnutí — živý `/health` → attach (no spawn); mrtvý/chybí → spawn.
- **Integrace (pytest):** runtime s fake bridge + fake D200 sink + HTTP — working agent animuje (verze `/state` rostou bez bridge události) A D200 sink dostává working frames; config write → watcher → reload → fan-out.
- **Recyklace:** stávající `deckapp` + `app.py` + orchestrator/icons testy zůstávají zelené (Orchestrator/HTTP/config nezměněné).
- **Manuální gate (macbench D200):** launchd runtime nahoře → D200 animuje (ticker) + Tauri okno attachne + animuje + **stejný elapsed** jako D200; dropdown stylu v okně → obojí živě; zabij runtime → Tauri fallback na vlastní sidecar (web-only, bez D200).

## Migrace na herdr-native

herdr-coupling je izolovaný za rozhraním **`StateSource`** (`deckapp/live.py` `LiveSource`, drží `Connector` k bridge). Až herdr vydá native client API, vymění se **jen** `LiveSource` (bridge Connector → herdr-native klient); Orchestrator, sinky, HTTP povrch, klienti i ticker **zůstanou beze změny**. Spec proto drží veškerou herdr-závislost za tímto jedním švem.

## Non-goals (YAGNI)

- D200 hotplug za běhu (restart runtime); v1 staví D200 sink jen na startu.
- Multi-server fan-out v runtime (LiveSource je phase-1 single-server, jako dnes).
- Vzdálení klienti přes síť (telefon na tailnetu) — protokol to umožní, ale v1 cílí localhost (runtime + okno na téže mašině); remote klient je následný krok.
- WS push protokol — v1 drží stávající HTTP `/state` polling (300 ms, `DeckView`), na který je frontend už hotový; WS je optimalizace na později.
- Signing/universal2 `.app` (oddělená, deferred linie).

## Vyřešené technické body

- **Reuse, ne přepis:** config/onboarding/secret/HTTP už žijí v `deckapp/server.py`; runtime = ten server + D200 sink + ticker + discovery. `app.py` D200 logika (D200Driver+RenderPump wiring, `handle_tick`, deck-kind detekce) se vstřebá do sdílených částí.
- **RenderPump asyncio gotcha** je už vyřešená (worker-thread vlastní event loop, merge 6d7cc56) — `D200Sink` ji reuse-ne, žádná nová práce na loop modelu.
- **Jeden zámek:** `DeckApp._lock` už serializuje render/press; ticker, bridge callbacky i HTTP presses ho sdílí → fan-out do sinků pod ním je race-free.
- **Soběstačnost `.app`:** attach-or-spawn drží `.app` použitelnou i bez launchd runtime (web-only fallback) — distribuce se neláme.
- **Velikost:** velký, ale soudržný subsystém → jeden plán s víc tasky (sink abstrakce → ticker → D200 sink → discovery → runtime entry → Tauri attach-or-spawn → nasazení/manuální gate).
