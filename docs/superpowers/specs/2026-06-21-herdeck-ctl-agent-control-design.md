# herdeck-ctl — CLI pro ovládání agentů (design)

- **Datum:** 2026-06-21
- **Stav:** návrh k revizi
- **Větev:** `feat/herdeck-ctl`

## 1. Kontext a motivace

herdeck dnes umí ovládat agenty běžící pod herdr **jen přes deck** (D200 / Elgato / web simulátor): orchestrator mapuje stisk dlaždice na `Command` (Approve / Approve! / Deny / Stop + quick-send makra) → `Connector` → WebSocket → `bridge` → herdr socket → pane agenta. Akce i normalizované stavy (`working`/`idle`/`blocked`/`done`) už existují, jen nemají vstup z příkazové řádky.

Cíl: **`herdeck-ctl`** — CLI, kterým může jeden *lead* agent (Claude/codex v herdr panelu) řídit stádo ostatních agentů z terminálu, místo aby syrově mluvil s herdr socketem a pamatoval si key-sekvence. Primárně pro orchestraci agentů; sekundárně ho uživatel ovládá z mobilu (přes Tailscale na běžící bridge).

### Co herdr neumí sám a CLI reálně přidává
1. **Sémantické akce přes profily** — herdr má jen syrové `send_keys`; „approve" je u codexu jiná klávesa než u claude. herdeck centralizuje mapování *akce → key-sekvence per typ agenta* (`AnswerProfile`). Toto je hlavní nereduntantní přínos.
2. **`wait` primitiv** — herdr nemá „zablokuj, dokud agent nepotřebuje vstup"; bridge už dělá poll+diff a pushuje eventy, takže `wait` z toho padá.
3. **Remote / multi-server / auth** — získáno **zadarmo** tím, že CLI je klient existujícího bridge (varianta A níže), ne novou síťovou vrstvou.

## 2. Rozsah v1

**V1 = pozorování + akce na stávajících agentech:**
- `ls`, `wait`, `approve`, `deny`, `stop`, `send`, `focus`.

**Mimo v1 (vědomě, čistý follow-up):**
- `spawn` / `kill` agentů a worktree — `start` handler v bridge dnes neposílá `workspace_id`, takže neumí umístit agenta do worktree; potřebuje rozšíření bridge protokolu.
- `send --no-submit` (předvyplnit prompt bez odeslání pro ruční kontrolu na mobilu) — `bridge.send_text` (`bridge.py:331`) má `enter` zadrátovaný natvrdo; vyžádá si nový bridge příkaz. Nízká priorita.

## 3. Architektura — varianta A: CLI = tenký klient bridge

CLI čte stejný `~/.config/herdeck/config.toml` jako app, připojí se k nakonfigurovanému bridge (lokálně i přes Tailscale), stáhne `snapshot`/`event`, posílá `act`/`act_force`/`send_text`/`focus`. Když config chybí, spustí lokální loopback bridge proti herdr socketu (stejně jako app).

```
herdeck-ctl → (Connector/WebSocket) → bridge → herdr (Unix socket) → pane agenta
```

**Důsledky:** remote/auth/multi-server zadarmo; **identická sémantika s deckem** (jeden zdroj pravdy — profily i wire encoder se sdílejí); `wait` z event streamu; funguje dnes (běží launchd bridge na `100.86.178.12:8788`). Cena: jednorázový příkaz zaplatí ~vteřinu na connect + první snapshot.

Jiné varianty (zamítnuté): **B** přímo herdr socket — lokální only, duplikuje pane→agent mapování a normalizaci stavu; **C** sdílené jádro se dvěma transporty — předčasné (YAGNI).

## 4. Sada příkazů (CLI kontrakt)

**Pozorování**
- `herdeck-ctl ls [--json] [--server S] [--status blocked|working|idle|done]` — výpis agentů. Člověku tabulka (server, id, label, status, repo/branch); `--json` pole objektů.
- `herdeck-ctl wait (<agent> | --any) --until blocked|done|idle [--timeout S] [--json]` — **LEVEL**: zablokuje, dokud (konkrétní | jakýkoli) agent nedosáhne stavu; vrátí, který agent to splnil.

**Akce na stávajících agentech**
- `herdeck-ctl approve <agent> [--always] [--force] [--settle S | --no-settle]` — `--always` použije profil `approve_always`; default `guard=blocked` (bridge přeskočí, není-li agent blocked), `--force` = bezpodmínečně.
- `herdeck-ctl deny <agent> [--force] [--settle S | --no-settle]`
- `herdeck-ctl stop <agent> [--settle S | --no-settle]` — bezpodmínečně (`act_force`).
- `herdeck-ctl send <agent> "text"` — pošle text (`send_text`, submituje hned).
- `herdeck-ctl focus <agent>` — vytáhne pane do popředí (zavírá mobilní smyčku `wait --any → focus`).

**Společné přepínače:** `--json`, `--server S`, `--config PATH` (jinak `$HERDECK_CONFIG` / default), `--timeout` (default 10 s na connect+první snapshot a na request).

### Klíčový orchestrační primitiv
`wait --any --until blocked --json` → lead agent v cyklu: *„uspi mě, dokud někdo nepotřebuje vstup"*, vrátí kterého → `approve` / přečti / rozhodni / `focus`. Z pollingu se stává jeden řádek.

## 5. Identifikace agenta

`<agent>` = `server:pane_id` přesně, nebo fuzzy přes `label`/`repo`/`branch`. Nejednoznačnost → chyba (exit `4`) s výpisem kandidátů na stderr. `ls --json` dává kanonická id (skript nejdřív listne, pak jedná).

## 6. Mapování příkaz → `Command` → wire

| příkaz | `Command.kind` | wire `type` | klávesy / data |
|---|---|---|---|
| `approve` | `act_if_blocked` (default) / `act_force` (`--force`) | `act` (`guard` dle kind) | `profile.approve` / `profile.approve_always` (`--always`) |
| `deny` | `act_if_blocked` / `act_force` | `act` | `profile.deny` |
| `stop` | `act_force` | `act` (`guard=false`) | `profile.stop` |
| `send` | `send_text` | `send_text` | text |
| `focus` | `focus` | `focus` | — |
| `ls` | `list` | `list` | — (odpovědí je `snapshot`, ne `result`) |

Wire encoder = `commands.command_to_msg(cmd, req)` (úplný, vč. `read`/`start` kindů kvůli sdílení s appkou).

## 7. Profily — jeden zdroj pravdy

Profil se vybírá přes `agent_type` agenta: `config.profiles.get(agent_type, profiles["default"])`. Ověřeno, že `agent_type` je reálně `claude`/`codex` (ne `default`) — `bridge._herdr_pane_to_wire` mapuje herdr `agent` → `agent_type` (`bridge.py:54`).

Orchestrator už tento lookup má jako `_profile_for` (`orchestrator.py:289-291`). **Nepíše se vlastní** — vytáhne se do `commands.profile_for(config, agent_type)` a orchestrator ho začne volat. Tím profilová data zůstávají jediným zdrojem pravdy v configu a deck ani `ctl` se nerozejdou.

## 8. Jádro: `CtlSession`

`Connector` je stavěný pro dlouhožijící app; CLI je jednorázové. Tenká obálka to přemostí — **single-loop asyncio, žádné thread-bridging** jako v appce (callbacky Connectoru mutují stav `CtlSession` přímo).

Stav: `agents: dict[AgentKey, AgentState]`, `_pending: dict[req, Future]`, `_changed: asyncio.Event`.

- **`open()`** — pro každý cílový server spustí `connector.run()` jako task, počká na první `snapshot` (s `--timeout`; jinak exit `5`), naplní `agents`. Callbacky: `on_snapshot`/`on_event` přepíšou `agents` a pak `_changed.set()`; `on_result(req, data)` dořeší `_pending[req]`; `on_error` → uloží chybu.
- **`request(cmd) -> dict`** — alokuje `req`, zaregistruje `Future` do `_pending[req]`, pošle `command_to_msg(cmd, req)`, počká na `result` (Connector koreluje přes `msg.req`, `connector.py:123`). `list` nemá `req` — řeší se snapshot cestou.
- **`wait(predicate, timeout) -> AgentState | None`** — LEVEL, viz níže.
- **`close()`** — `connector.stop()`, zruší tasky, zavře handle lokálního bridge (pokud byl spuštěn).

### 8.1 `wait` — korektnost (W1)

`_changed` je **sdílený přes všechny status změny**, takže probudí i na cizím agentovi → nutný re-check po probuzení, ne jednorázový. `on_event` je *synchronní* callback Connectoru, nemůže držet async lock (proto **ne** `asyncio.Condition`; ta by potřebovala `loop.call_soon` bridge). Místo toho `asyncio.Event` s pořadím **arm → check → await**:

```python
while not predicate():
    self._changed.clear()      # arm PŘED re-checkem
    if predicate():            # re-check po clear: vidí stav zapsaný on_eventem
        break
    await asyncio.wait_for(self._changed.wait(), remaining_timeout)
```

Klíčové: `on_event` nejdřív přepíše `agents`, **pak** `set()`. V single-loop asyncio se callback spustí jen když waiter parkuje na `await`, takže žádný wakeup nepropadne, a check-po-clear vidí aktuální stav. CLI = 1 příkaz = single waiter, takže `clear()` nekrade probuzení. `remaining_timeout` se počítá z deadline.

Predikáty: konkrétní agent dosáhne stavu / (`--any`) jakýkoli agent v daném stavu.

### 8.2 Settle — anti-double-fire (W2)

**Problém:** CLI je one-shot, mezi invokacemi nedrží stav, a herdr má latenci než přepne `blocked`→`working`. Bez ošetření: `wait --until blocked` vrátí X → `approve X` pošle klávesy a vrátí ok → další `wait` LEVEL čte stav hned, X je možná *pořád* blocked (herdr nepřepnul) → vrátí zase X → druhý `approve` → dvojí poslání kláves. `guard=blocked` to **nechytne** (X reálně ještě je blocked).

**Řešení:** akce, co mají vyvést agenta z `blocked` (`approve`/`deny`/`stop`), po `{sent}` interně udělají `wait(X.status != BLOCKED, timeout=settle)`:
- X opustí blocked → exit `0` (akce potvrzená).
- settle vyprší (default **3 s**; `--settle S`, `--no-settle`) → exit `0`, ale `--json {"settled": false}` + warn na stderr.
- guard přeskočil (X nebyl blocked) → exit `3`, žádný settle.

`send`/`focus`/`ls`/`wait` settle nemají.

**Akceptovaný kompromis:** projede-li X rychle `blocked→idle→blocked`, settle uvidí `!=BLOCKED` v mezikroku a vrátí `settled:true`. V1 se neřeší.

## 9. Bootstrap, refaktory a rozpad souborů

### Nové soubory
- **`src/herdeck/commands.py`** — `Command` dataclass (přesun z `orchestrator.py:20`; má jen primitiva, žádné `model`/`AgentKey` závislosti → přesun bez rizika cyklu), `command_to_msg(cmd, req)` (úplný wire encoder vč. `read`/`start`), `profile_for(config, agent_type)`, `build_action_command(action, agent, profile, *, force, always) -> Command`.
- **`src/herdeck/bootstrap.py`** — `async resolve_runtime_config(...) -> (Config, aclose)` (rozhodnutí *file config vs lokální bridge*, vytaženo z `app.main`) **+ přesun `local_config` (`app.py:451`) a `resolve_mode` (`app.py:272`)**. Důvod přesunu: `bootstrap` je potřebuje a `app` má importovat `bootstrap` → jinak `app↔bootstrap` cyklus. Obě jsou čisté funkce bez UI závislostí, logicky patří sem.
- **`src/herdeck/ctl.py`** — argparse, `CtlSession`, handlery příkazů, `main()`.

### Změněné soubory (blast radius)
- `orchestrator.py` — `Command` importuje z `commands`; `_profile_for` deleguje na `commands.profile_for`.
- `app.py` — `Command`/`command_to_msg` z `commands` (signatura `(cmd, req)`, app předává svůj `next_req_for`); `main` přes `bootstrap.resolve_runtime_config`; **ztrácí `local_config` + `resolve_mode`** (přesunuty do `bootstrap`).
- `pyproject.toml` — `[project.scripts]` přidat `herdeck-ctl = "herdeck.ctl:main"`.

Oba refaktory zachovávají chování a jsou kryté `test_local_mode` / `test_connector` / `test_orchestrator*`.

## 10. Output a exit kódy

| kód | význam |
|---|---|
| `0` | ok (vč. settle timeout, viz `settled:false`) |
| `2` | usage (argparse) |
| `3` | přeskočeno guardem (`approve`/`deny` na non-blocked agentovi) |
| `4` | neznámý / nejednoznačný agent (kandidáti na stderr) |
| `5` | chyba spojení / configu / `error` frame z bridge / snapshot timeout |
| `124` | `wait` timeout |

`--json` výstupy: `ls` → pole agentů; akce → `{"result": "sent"|"skipped", "agent": "...", "settled": true|false}`; `wait` → `{"agent": "...", "status": "..."}`.

Stavy `idle`/`working`/`blocked`/`done` jsou konzistentní napříč `ls` i `wait` (ověřeno v `_AGENT_STATUSES`, `bridge.py:14`).

## 11. Error handling

- chybí config i herdr socket → `resolve_mode` „error" → stderr + exit `5`.
- timeout prvního snapshotu → exit `5`.
- neznámý/nejednoznačný agent → exit `4` + kandidáti.
- `error` frame z bridge → exit `5` + zpráva.
- settle timeout → exit `0` + `settled:false` + warn.

## 12. Testy (styl repa: `FakeHerdrClient` + `handle_client_message` + `start_local_bridge`)

1. **Unit `commands.py`** — `command_to_msg` pro každý kind (vč. `read`/`start`); `profile_for` fallback na `default`; `build_action_command` (approve→act_if_blocked, `--force`→act_force, `--always`→approve_always, stop→act_force).
2. **Unit `CtlSession`** s fake connectorem (bez WS, test krmí callbacky): `request()` koreluje `req`→`result`; `wait` LEVEL vrací hned když už splněno; `wait` blokuje→probudí na eventu; **W1** (event nastavený těsně před await stejně probudí, re-check po cizím eventu nevrací předčasně); **W2** (approve čeká na opuštění blocked; `--no-settle`; settle timeout → `settled:false`).
3. **Integrace** — `start_local_bridge` proti fake herdr (jako `test_local_mode`) + reálný `Connector`: e2e `ls`, `approve` (guard skip vs sent), `wait`; ověř, že herdr dostal správné `send_keys`.
4. **Regrese** — `test_local_mode` / `test_connector` / `test_orchestrator*` zůstávají zelené; celý suite.

## 13. Future (mimo v1)

- `send --no-submit` — nový bridge příkaz (oddělit „type text" od „enter").
- `spawn` / `kill` — rozšířit bridge `start` o `workspace_id` + proxy na `worktree.create`/`worktree.remove`.
- Případně sjednotit do jednoho transportu i lokální rychlou cestu (varianta C), až to bude potřeba.

## 14. Otevřené poznámky

- Profily pro typy mimo `claude`/`codex` spadnou na `default` (`["enter"]` / `["esc"]` / `["ctrl+c"]`) — uživatel je může přepsat v `config.toml` (`[answer_profiles]`).
- `wait --any` bez `--server` sleduje agenty napříč všemi nakonfigurovanými servery.
