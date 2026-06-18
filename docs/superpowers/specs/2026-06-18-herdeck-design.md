# Herdeck — Stream Deck dashboard pro AI agenty

**Datum:** 2026-06-18
**Stav:** Návrh schválen, připraveno k plánu implementace

## 1. Účel

Fyzický „Stream Deck" (Ulanzi D200) jako řídicí panel pro AI coding agenty
(Claude Code, Codex, Cursor a další), kteří běží na vzdáleném serveru pod
[herdr](https://github.com/ogulcancelik/herdr). Na jeden pohled vidíš, který
agent **čeká na schválení**, a jedním stiskem odpovíš **Approve / Deny / Stop**,
aniž bys přepínal do terminálu.

Cílem je **spolehlivý, open-source-grade systém**: přežije uspání Macu bez
ručního reconnectu, je bezpečný (žádné secrets v kódu), generický (víc serverů
i agentů z configu) a otestovaný.

### Rozsah v1 (skupina B — reakce na agenty)
- Zobrazení stavu všech agentů (working / idle / blocked / done) barvou dlaždice.
- Drill-in na blokovaného agenta a odpověď Approve / Approve-always / Deny / Stop.
- Multi-server od začátku (servery i agenti z configu).
- Robustní chování přes uspání/probuzení/změnu sítě (resync-on-reconnect).

### Mimo rozsah v1 (možné rozšíření)
- Usage / zbývající limity (skupina jiná).
- Akce typu attach/SSH, spouštění agentů, tail logů na Macu.
- Server health (CPU/RAM/disk).

## 2. Architektura

Tři vrstvy, každá s jedním účelem a samostatně testovatelná.

```
  ┌─────────────────────── SERVER (remote) ───────────────────────┐
  │  agenti (claude/codex/cursor…) ── běží v ── herdr               │
  │                                              │ Unix socket      │
  │                                       herdr-bridge (daemon)     │
  │                                              │ WebSocket        │
  └──────────────────────────────────────────────┼────────────────┘
                                                  │ Tailscale (WireGuard)
  ┌──────────────────────────────────────────────┼────────────────┐
  │  MAC                                    connector (WS klient)   │
  │                                                │               │
  │                                          orchestrator          │
  │                                                │               │
  │                                          deck-driver ── USB HID ── D200
  └────────────────────────────────────────────────────────────────┘
```

### Důležité: SSH není v datové cestě
WebSocket jde po Tailscale (WireGuard). `herdr-bridge` mluví s herdr přes
**lokální Unix socket** na serveru. Žádný `ssh -L` / autossh → odpadá problém
s polomrtvým SSH tunelem po uspání. SSH zůstává jen pro tvůj ruční
`herdr attach` do terminálu (oddělená věc, systém na něm nestojí).

## 3. Komponenty

### 3.1 `herdr-bridge` (server, daemon)
Bezstavový relay mezi herdr socketem a Macem.

- **Vstup:** připojí se na lokální herdr Unix socket; `events.subscribe` na
  `pane.agent_status_changed`; na požádání volá `pane.list`, `pane.get`,
  `pane.read`, `pane.send-keys`, `pane.run`.
- **Výstup:** WebSocket server **bind jen na Tailscale interface** (ne 0.0.0.0),
  bearer token autentizace, JSON zprávy.
- **Protokol směrem k Macu:**
  - `event` — push změny stavu panelu (z herdr eventu).
  - `snapshot` — odpověď na `list` (plný stav všech panelů) pro resync.
  - `result` — odpověď na příkaz (read/send-keys/get).
- **Protokol směrem od Macu:**
  - `list` — vyžádá snapshot.
  - `read {pane_id, source}` — přečte prompt.
  - `act {pane_id, keys|text}` — pošle vstup do panelu.
  - `get {pane_id}` — aktuální stav panelu (pre-akce ověření).
- Idempotence: opakované `list`/`get` jsou bez vedlejších efektů; `act` se posílá
  až po ověření stavu (viz §6).
- Běží jako **systemd user/system unit**, restart on-failure.

### 3.2 `deck-driver` (Mac)
Tenký obal nad [`strmdck`](https://github.com/redphx/strmdck) (Python + hidapi),
izoluje HW od zbytku systému.

- API: `set_tile(index, label, color, icon=None)`, `clear()`, `on_press(cb)`,
  `device_info()`.
- Skládá obrázky dlaždic (label + barva + ikona stavu) a posílá je na D200.
- **Fake renderer** pro vývoj bez HW: stejné API, vykreslí dlaždice do lokálního
  okna / PNG. Umožní testovat orchestrator i běh bez zařízení.
- Retry při odpojení D200; detekuje zabrané zařízení (viz §6).

### 3.3 `connector` (Mac)
WS klient k jednomu nebo více `herdr-bridge`.

- Auto-reconnect s **exponenciálním backoffem** + jitter.
- **Heartbeat ping/pong**; při výpadku pongu spojení zahodí a reconnectne.
- **Po každém (re)connectu vyžádá `list` → plný snapshot** (resync-on-reconnect).
- Normalizuje eventy z více serverů do jednotného modelu `AgentState`
  (server_id, pane_id, agent_type, label, status, project).
- Emituje události do orchestratoru: `on_snapshot`, `on_agent_changed`,
  `on_connection_changed(server_id, up/down)`.

### 3.4 `orchestrator` (Mac)
Jádro logiky, bez I/O závislostí (testovatelné s mocky).

- Drží model `agents: dict[(server_id, pane_id) -> AgentState]`.
- **Mapování agent → dlaždice** podle configu (pořadí, filtry, který server).
- **Barvy stavů:** working = zelená, idle = modrá/šedá, blocked = pulzující žlutá,
  done = ztlumená, error/odpojeno = červená.
- **Stavový automat navigace:** `OVERVIEW ↔ DRILL_IN(agent)`.
- Zpracování stisků → akce; resolve **answer profilu** podle `agent_type`.
- Před `act` ověří přes `get`, že panel je stále `blocked` (viz §6).

### 3.5 `config` (Mac, TOML)
```toml
[[servers]]
id = "workbox"
url = "wss://workbox.tailnet.ts.net:8788"   # Tailscale jméno
token_env = "HERDECK_WORKBOX_TOKEN"          # token z env, ne v souboru

[deck]
grid = "auto"        # zjistí se z D200; lze přepsat na "5x3"
overview_order = ["workbox"]   # pořadí serverů v overview

[answer_profiles.claude]
approve        = ["1", "enter"]
approve_always = ["2", "enter"]
deny           = ["esc"]
stop           = ["ctrl+c"]

[answer_profiles.codex]
# doplní se empiricky
approve        = ["y", "enter"]
deny           = ["n", "enter"]
stop           = ["ctrl+c"]

[answer_profiles.default]
approve = ["enter"]
deny    = ["esc"]
stop    = ["ctrl+c"]
```
Žádné secrets v souboru — tokeny přes env proměnné.

## 4. Datový tok (Approve flow)

1. `bridge` odebírá herdr eventy; na `agent_status_changed` pushne `event` přes WS.
2. `connector` přijme → `orchestrator` přebarví dlaždici agenta.
3. Agent `blocked` → dlaždice **pulzuje žlutě**.
4. Uživatel klepne na dlaždici → orchestrator pošle `read {pane_id, source:"detection"}`
   → zobrazí drill-in se shrnutím requestu.
5. Klepne **Approve** → orchestrator: `get` (ověření `blocked`) → resolve profil
   → `act {pane_id, keys: profile.approve}`.
6. herdr vstup vloží → agent se odblokuje → `event` přiteče zpět → dlaždice zezelená
   → orchestrator se vrátí do OVERVIEW.

## 5. Layout dlaždic a interakce

- **OVERVIEW:** 1 dlaždice = 1 agent (label = `agent · projekt`, barva = stav).
  Systémové dlaždice: **„Další blokovaný"** (skok na nejstarší blocked),
  **Refresh** (vynucený resync), **stav spojení** (zelená/červená per server).
- **DRILL_IN(agent):** dlaždice **Approve**, **Approve-always**, **Deny**,
  **Stop**, **Back** + shrnutí čekajícího requestu (zkráceně).
- Layout je **parametrický** podle počtu buněk gridu (D200 ~5×3); přesný počet
  buněk a co jsou dotykové dlaždice vs. fyzická tlačítka se potvrdí, až
  `strmdck` zařízení vyčte.

## 6. Spolehlivost a ošetření chyb

| Situace | Chování |
|---|---|
| Uspání Macu / změna sítě / WS výpadek | backoff reconnect, dlaždice spojení červená, po obnově **plný resync** (`list`) |
| `bridge` nebo herdr restart | connector re-subscribe; změněná pane ID pokryje resync |
| Pane se mezi „blocked" a stiskem odblokuje | před `act` se přes `get` ověří stav; pokud už není `blocked`, akce se zruší a deck se resyncne |
| D200 odpojen | driver retryuje připojení |
| D200 zabraný oficiální Ulanzi appkou | detekce + jasná chybová hláška; setup vyžaduje vypnutý Ulanzi software |
| Výpadek jednoho serveru z více | degradace: ostatní servery fungují, jen jeho dlaždice červené |

**Bezpečnost (open-source-grade):** WS bind jen na Tailscale interface + bearer
token; doporučené Tailscale ACL; žádné secrets v kódu/configu (env); volitelně mTLS.

## 7. Stack a běh

- **Jazyk:** Python 3.12 + asyncio (jednotně bridge i Mac část).
- **Knihovny:** `strmdck`/hidapi (D200), `websockets`, TOML config.
- **Běh:** `bridge` jako systemd unit (server), Mac část jako launchd LaunchAgent
  (autostart po loginu).

## 8. Testování

- **Unit (orchestrator):** mapování agent→dlaždice, barvy, navigační automat,
  resolve answer profilu — s mock connectorem/driverem.
- **connector:** reconnect/resync proti **fake WS serveru** (simulace dropu,
  obnovy, ztráty pongu) — ověřit, že po obnově proběhne `list` a překreslení.
- **bridge:** integračně proti herdr socketu (nebo stubu socketu).
- **deck-driver:** fake renderer; HW cesta ověřena manuálně.
- **E2E manuálně:** reálný blokovaný `claude` v herdr → Approve z decku → odblokování.
- **CI:** lint + unit/reconnect testy v GitHub Actions.

## 9. Otevřené body / rizika (ověřit na začátku implementace)

1. Přesná velikost gridu D200 a rozlišení dotykové dlaždice vs. fyzické tlačítko
   (potvrdit přes `strmdck` enumeraci zařízení).
2. Spolehlivost herdr `blocked` detekce u permission promptů (herdr stav čte ze
   screenu — ověřit u claude i codex).
3. Přesné klávesy answer profilů pro claude/codex (postavit empiricky).
4. `strmdck` na macOS + konflikt s oficiální Ulanzi appkou.
5. Bind WS na konkrétní Tailscale interface/IP napříč platformami serveru.

## 10. Balení pro open source

- README (architektura, instalace, config příklad), LICENSE.
- Install skripty + ukázkové systemd / launchd unit soubory.
- `config.example.toml`, dokumentace answer profilů (jak přidat agenta).
