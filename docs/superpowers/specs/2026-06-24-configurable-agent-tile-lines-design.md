# Konfigurovatelné řádky agent tile (workspace + tab label)

**Datum:** 2026-06-24
**Stav:** Návrh schválen, připraveno k plánu implementace

## 1. Účel

Agent tile (dlaždice agenta v overview) má dnes dva fixní textové řádky:
**primary** (repo, font 23) a **secondary** (branch, font 16, wrap). Cílem je
nechat uživatele nakonfigurovat, **co** je v těchto dvou řádcích — z pevné sady
tokenů (`repo`, `branch`, `workspace`, `tab`, `agent`) — přičemž jeden řádek smí
tokeny **kombinovat**.

Tím se řeší dva reálné scénáře:

- **Rozlišení víc agentů ve stejném repu.** Když ve workspace `herdeck` běží dva
  Claude agenti (`w2:p1` v tabu „1", `w2:p7` v tabu „2"), dnes vypadají oba tile
  identicky (repo=herdeck, branch=main, status). Token `tab` je odliší.
- **Smysluplnější jméno panům bez gitu.** Pane bez worktree dnes dostane jen
  basename `cwd`. Token `workspace` zobrazí lidský label workspace (např.
  `macdoktor-server`, `openclaw-server`).

## 2. Zdroj dat — co herdr přes socket reálně umí (živě ověřeno)

Stav herdr RPC API ověřen živě na běžící instanci (`pane.list`, `tab.list`,
`workspace.list`, `pane.get`, `agent.get`):

| Entita | RPC | Čitelný label? |
|---|---|---|
| **workspace** | `workspace.list` → `workspaces[].label` | ✅ ano (`"herdeck"`, `"macdoktor-server"`) |
| **tab** | `tab.list` → `tabs[].label` | ✅ ano (default číslo `"1"`/`"2"`, přejmenovatelné přes `tab.rename`) |
| **pane** | `pane.list` / `pane.get` | ❌ ne — i po `pane.rename` zůstává label **mimo** `pane.list`/`pane.get` výstup |
| **agent** | `agent.list` / `agent.get` | ❌ ne (jen typ `"claude"`) |

Mapování pane → workspace/tab: surová herdr pane nese `workspace_id`
(např. `w2`) a `tab_id` (např. `w2:t1`); label se dohledá v
`workspace.list`/`tab.list` podle těchto id.

**Důsledek pro scope:** zdrojem jména je **jen workspace label + tab label**.
Skutečné „pane jméno" není přes herdr API čitelné a je **mimo rozsah** tohoto
specu; vyžadovalo by zásah do herdr (vystavit pane label v `pane.list`) a je to
možné budoucí rozšíření.

## 3. Rozsah

**V rozsahu:**

- Dva nové config klíče `[view].tile_primary` a `[view].tile_secondary`.
- Načtení workspace + tab labelu z herdr a jeho protažení až do `AgentState`.
- Vykreslení konfigurovatelných řádků v **obou** render cestách:
  - **Orchestrator** (`orchestrator.py` → DeckDriver: web simulátor, deckapp, D200),
  - **ElgatoSession** (`elgato/session.py` → Stream Deck plugin).

**Mimo rozsah:**

- Akční tiles (approve / deny / stop / pager) — zůstávají beze změny
  (zobrazují cíl přes `repo=ident`).
- Badge pole a logo zůstávají beze změny podle dnešního chování každé render
  cesty. V Orchestrator cestě `tile_fields` dál řídí badge/texty `status`,
  `time` a `server`; logo `agent_type` se dál předává vždy. V Elgato cestě se
  stávající `status_text`/logo chování nemění; pokud nové primary/secondary
  klíče chybí, fallback zůstává dnešní pevné repo+branch chování.
- Skutečné pane jméno (viz sekce 2).

## 4. Config schéma

Dva nové klíče v sekci `[view]`. Hodnota je **seznam tokenů** (`list[str]`) —
konzistentní s tím, že `tile_fields` je rovněž seznam. **Žádný řetězcový DSL.**

```toml
[view]
tile_primary   = ["workspace"]      # default odvozený: ["repo"]
tile_secondary = ["tab", "branch"]  # default odvozený: ["branch"]
# příklad výše vykreslí primary = "herdeck", secondary = "▸2 · main"

# per-profil override — view volby patří pod [profiles.X.view]
[profiles.solo.view]
tile_primary   = ["repo"]
tile_secondary = ["branch"]
```

- **Povolené tokeny (přesně):** `repo`, `branch`, `workspace`, `tab`, `agent`.
- **Neznámý token → `ConfigError`** (validace při parsování v `settings.py`).
- Klíče lze uvést v **base** (`[view]`) i v profilu pod **`[profiles.X.view]`**
  (per-profil override) — `view` je overlay sekce (`_OVERLAY_SECTIONS`), takže se
  merguje field-by-field stejně jako `tile_fields`. Přímé klíče v `[profiles.X]`
  (mimo `view`) by se pro tile řádky **nemergovaly**.

### 4.1 Vykreslení řádku z tokenů

- Token se vyhodnotí na hodnotu z `AgentState`:
  - `repo` → `repo or label`
  - `branch` → `branch`
  - `workspace` → `workspace`
  - `tab` → **jen když má hodnotu**, vykresli jako `▸{tab}`
  - `agent` → `agent_type`
- Tokeny na řádku se spojí oddělovačem `" · "`.
- **Prázdné hodnoty se vynechají.** Když po vynechání nezbude nic, řádek se
  **vůbec nevykreslí** (předá se prázdný řetězec, který render už dnes zvládá).

### 4.2 Zpětná kompatibilita (přesně)

Odvození se počítá na **výsledné (po aplikaci profilu sloučené) view sekci** —
tj. až po merge base + `[profiles.X.view]`, ne před ním (v `_view_config`).

Pravidlo je **per-key** — každý ze dvou klíčů se vyhodnotí samostatně:

- **Klíč explicitně uvedený** (i prázdný `[]`) → **má přednost** a použije se
  doslova. Explicitní `[]` daný řádek **vypne** (žádné odvození).
- **Klíč chybí** (není ve sloučené view sekci) → použije se kompatibilní fallback
  podle render cesty:
  - **Orchestrator / D200 / web sim / deckapp:** odvoď ze sloučeného
    `tile_fields`, protože tato cesta dnes `repo`/`branch` přes `tile_fields`
    skutečně řídí.
    - `tile_primary`: `["repo"]` pokud `"repo" in tile_fields`, jinak `[]`
    - `tile_secondary`: `["branch"]` pokud `"branch" in tile_fields`, jinak `[]`
  - **ElgatoSession / Stream Deck plugin:** fallback je vždy
    `tile_primary = ["repo"]`, `tile_secondary = ["branch"]`, protože tato cesta
    dnes `tile_fields` pro branch/status text nečte a vždy zobrazuje repo+branch.

Klíče se vyhodnocují nezávisle: lze uvést jen `tile_primary` a `tile_secondary`
nechat fallbackovat (nebo naopak). Explicitní nový klíč tedy mění jen svůj řádek;
chybějící řádek zachová dnešní chování dané render cesty. Tím se každý existující
config chová přesně jako dnes: například `tile_fields = ["repo"]` bez nových
klíčů dál schová branch v Orchestrator cestě, ale dál ji zobrazí v Elgato cestě.

## 5. Datová cesta (workspace + tab label do `AgentState`)

Sdílená pro obě render cesty; liší se jen finální napojení do `TileView`.

1. **`bridge.py`**
   - `_wired_snapshot` dofetchuje `workspace.list` + `tab.list` vedle
     `pane.list` + `worktree.list`.
   - `HerdrClient` protokol dostane `workspaces()` a `tabs()`; `SocketHerdr` je
     mapuje na příslušné RPC; `StubHerdr` je doplní pro testy.
   - Indexy `{workspace_id: label}` a `{tab_id: label}`.
   - `_herdr_pane_to_wire` doplní do wire pane pole `workspace` a `tab` (label
     dohledaný podle `workspace_id` / `tab_id`).
   - **Když lookup chybí nebo je label prázdný → hodnota zůstane prázdná.**
     Raw id (`w2`, `w2:t1`) se **nikdy** nepoužije jako text na tile.
2. **`protocol.py`** `_pane_to_state` čte `workspace` a `tab` z wire pane.
3. **`model.py`** `AgentState` dostane pole `workspace: str = ""`, `tab: str = ""`.

## 6. Render

- **`layout.py`** — nový helper `compose_line(state, tokens) -> str`:
  mapuje tokeny na hodnoty dle 4.1, vynechá prázdné, spojí `" · "`.
  Toto je jediné místo s logikou skládání řádku (sdílené oběma cestami).
- **`orchestrator.py`** `_render_overview`: `TileView.repo = compose_line(s, primary)`,
  `TileView.branch = compose_line(s, secondary)`, kde `primary`/`secondary`
  pocházejí z (po backcompat odvození) `view.tile_primary` / `view.tile_secondary`.
- **`elgato/session.py`** `_slot_tile`: stejné napojení do `TileView`, ale musí
  zachovat indikaci vybraného agenta. Dnešní Stream Deck plugin prefixuje primary
  řádek vybraného tile jako `* {repo}`; po zavedení `compose_line` se stejný
  prefix aplikuje na složený primary text, případně se nahradí ekvivalentním
  explicitním selected-state renderem. Cíl je, aby approve/deny/stop target
  zůstal vizuálně jednoznačný.
- **`TileView`** pole `repo`/`branch` si **ponechávají názvy** — sémanticky jsou
  to teď „primary text slot" / „secondary text slot". Doplní se komentář.
  Přejmenování napříč kódem je zbytečně velký refactor (YAGNI).
- **`icons.py`** — **beze změny**; renderuje primary/secondary slot tak jak je.

## 7. Edge cases

- Prázdná hodnota tokenu se vynechá; úplně prázdný řádek se nevykreslí.
- Neznámý token v configu → `ConfigError`.
- Chybějící workspace/tab lookup nebo prázdný label → prázdná hodnota; raw id
  se nikdy nezobrazí.
- Token `tab` se renderuje jen když má hodnotu, jako `▸{tab}`.
- Ořezání (truncation) a zalomení (wrap) dlouhých řádků řeší **existující**
  render (`_truncate` / `_wrap`) beze změny.
- Duplicita `workspace == repo` (častý případ) je **legitimní uživatelská
  volba**, ne chyba.

## 8. Testy (TDD)

- **`bridge`**: workspace/tab label se doplní do wire pane; chybějící
  workspace/tab nebo prázdný label → prázdná hodnota (nikdy raw id).
- **`protocol`**: `workspace`/`tab` projdou do `AgentState`.
- **`compose_line`**: oddělovač `" · "`, vynechání prázdných, prefix `▸` u `tab`,
  prázdný výsledek pro prázdné vstupy.
- **`orchestrator`** a **`elgato session`**: config → očekávaný `TileView`
  primary/secondary text včetně rozdílného fallbacku při chybějících nových
  klíčích (`tile_fields = ["repo"]` schová branch jen v Orchestrator cestě, ne v
  Elgato cestě); Elgato test navíc ověří, že vybraný agent dál nese selected
  indikaci na primary řádku.
- **`settings`**: parsing nových klíčů; `ConfigError` u neznámého tokenu;
  per-key explicitnost na sloučené view (chybějící klíč fallbackuje až v render
  helperu podle render cesty, explicitní `[]` řádek vypne, částečný override jen
  jednoho klíče); merge přes `[profiles.X.view]`.
- **`config`**: defaults.

## 9. Dotčené soubory

`model.py` · `bridge.py` · `protocol.py` · `layout.py` · `config.py` ·
`settings.py` · `orchestrator.py` · `elgato/session.py` + odpovídající testy.
`icons.py` zůstává beze změny.

## 10. Otevřená rozhodnutí (vyřešená)

- **Zdroj jména:** jen tab + workspace label, bez zásahu do herdr.
- **Model customizace:** konfigurovatelné řádky (primary + secondary, kombinace
  tokenů), ne fixní default ani jednorázový subtitle přepínač.
- **Render cesty:** obě (Orchestrator i ElgatoSession).
- **Pane jméno:** odloženo (vyžaduje rozšíření herdr API).
