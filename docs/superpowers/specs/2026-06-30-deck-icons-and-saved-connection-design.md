# herdeck desktop — bundled agent icons + "saved connection" demo-exit — design

**Status:** design approved (brainstorming) · 2026-06-30
**Type:** two independent bugfix slices for the herdeck desktop app (post-window-UX polish), discovered on the macbench deploy
**Depends on:** Phase 3a (frozen sidecar + `_default_icons` offline-first), Phase 3c (onboarding card + `/setup` + `/setup/connect`)

## Problém

Dvě nezávislé chyby zjištěné při živém běhu na macbench (remote-only stroj připojený na m4 herdr):

1. **Q1 — jen Codex ikona, ostatní agenti mají písmeno.** Desktop deck renderuje ikony **čistě offline**: `server._default_icons()` konstruuje `IconProvider` s `fetch=lambda slug: None` (žádný runtime Simple-Icons fetch — ani v devu, ani frozen). `_base_glyph` precedence: override PNG → bundlovaný `assets/<safe_name(agent_type)>.svg` → Simple-Icons fetch (vypnutý) → **letter glyph**. V `src/herdeck/assets/` je dnes **jen `codex.svg`** (codex jako jediný nemá Simple-Icons slug, tak ho *museli* přibalit). Proto `codex` renderuje OpenAI logo a `claude`/`cursor`/`copilot`/`gemini`/`opencode` padají na první písmeno („C" pro claude).
2. **Q3 — demo je past.** „Prozkoumat demo" zapíše `choice="demo"` do `onboarding.toml`. `select_source_kind` dává explicitní volbě přednost před remote configem na disku (aby demo/local drželo přes restarty), takže demo **přebije** funkční `config.toml`. Marker se maže **jen úspěšným local/remote connectem** (`_commit_remote`→`clear_choice`, local connect přepíše na `"local"`). Na remote-only stroji (lokální herdr není) je jediná cesta zpět na live **znovu vypsat remote URL + token**. (Souvisí **Q2:** karta `⚙ Změnit připojení` hlásí „herdr nebyl lokálně nalezen" i když jsi připojený remote — neuznává existující uložené spojení.)

## Cíl

1. Nabundlovat monochromní SVG ikony pro všech 5 mapovaných typů agentů, ať se ve frozen `.app` i v devu renderují (codex beze změny).
2. Přidat v onboarding kartě **jednoklikové „Připojit k uloženému spojení"**, které smaže demo marker a přepne na live z disku (token z keychainu) — bez vypisování. Tím se vyřeší i matoucí Q2 hláška.

Dvě komponenty jsou **nezávislé** (assets/freeze vs onboarding flow) — sdílejí jen tento spec; SDD je rozdělí do oddělených tasků.

---

## Komponenta 1: Bundlované ikony agentů (Q1)

### Co

Přidat do `src/herdeck/assets/` pět monochromních bílých SVG, pojmenovaných **přesně podle `agent_type` klíčů** v `icons.DEFAULT_AGENT_SLUGS` (ne podle slugu — `_base_glyph` hledá `assets/{_safe_name(agent_type)}.svg`):

| soubor | agent_type | Simple Icons slug (zdroj `/white`) |
|---|---|---|
| `claude.svg` | `claude` | `claude` |
| `cursor.svg` | `cursor` | `cursor` |
| `copilot.svg` | `copilot` | `githubcopilot` |
| `gemini.svg` | `gemini` | `googlegemini` |
| `opencode.svg` | `opencode` | `opencode` |

- **Styl:** monochromní bílé (`fill="#ffffff"`), `viewBox="0 0 24 24"`, jako stávající `codex.svg`. Bílá značka je čitelná na každém status-barevném pozadí (vzor `_default_fetch` `/white`).
- **Zdroj:** `cdn.simpleicons.org/<slug>/white` (Simple Icons) — stažené při implementaci a **commitnuté jako statické SVG** (žádný build-time fetch). Nejde o AI-generovaná aktiva. **Licence:** ikonová data jsou CC0 1.0, ale **zobrazené značky zůstávají ochrannými známkami vlastníků** (Anthropic, Microsoft, Google, Cursor; existující codex.svg nese OpenAI) — viz attribution níže.
- **Attribution (nový soubor):** přidat `NOTICE` (nebo `src/herdeck/assets/ATTRIBUTION.md`) se záznamem: zdroj = Simple Icons (https://simpleicons.org), ikonová data pod CC0 1.0, značky jsou ochranné známky příslušných vlastníků, použité jen pro identifikaci, bez endorsementu. (Pokrývá i stávající `codex.svg`/OpenAI, který tuto poznámku dosud postrádal.) Opravit přespřesné „licence CC0".
- **Build:** `desktop/scripts/build-sidecar.sh` → `frozen.prerasterize_assets(ASSETS, ASSETS)` napeče **každé `*.svg`** v assets/ na content-keyed PNG **přímo do `src/herdeck/assets/`** (`out_dir == src_dir`, idempotentní). Přidáním 5 SVG se napečou → renderují se ve frozen `.app`; v devu `_base_glyph` najde asset → cairosvg rasterizace. Codex beze změny.
- **DŮLEŽITÉ — commitnout baked PNG:** baker zapisuje content-keyed PNG do source-tree assets dir (jako stávající `a6817b9c…png` pro codex); `.spec` bundluje CELÝ adresář (svg i png). **Oba — SVG i jeho baked PNG — musí být commitnuté**, jinak čistý checkout zabundluje SVG bez PNG a frozen rasterizer (`make_png_rasterizer`, bez cairosvg) glyf tiše degraduje na písmeno. Tj. po přidání SVG spustit freeze a commitnout i 5 nových PNG.

### Shoda `agent_type` — OVĚŘENO proti živému herdr

Název SVG souboru se MUSÍ rovnat živé `agent_type` hodnotě. Cesta je **bez normalizace**: `bridge.py:71` `p.get("agent","default")` → `protocol.py:23` `agent_type=pane.get("agent_type","default")` → `connector.py` (verbatim) → `icons._base_glyph` → `_safe_name(agent_type)` (identity passthrough pro bare alfanumeriku — žádný `.lower()`/alias/suffix-strip) → `assets/{name}.svg`.
**Ověřeno dotazem na živý herdr socket** (`pane.list`, 27 panes): emitované `agent` hodnoty jsou přesně `['claude','codex']` — bare lowercase. herdr má sice v `agent-detection` vstupní aliasy (`claude.toml`: `aliases=["claude-code"]`; cursor `cursor-agent`; copilot `github-copilot`/`ghcs`; opencode `open-code`), ale **normalizuje je na canonical id PŘED emisí** — herdeck na drátě variantu nikdy nevidí. Takže názvy `claude.svg`/`cursor.svg`/`copilot.svg`/`gemini.svg`/`opencode.svg` se trefí do asset větve a **žádný alias v `DEFAULT_AGENT_SLUGS` netřeba**. Manuální macbench gate (Claude dlaždice ukáže Claude značku, ne „C") = závěrečné potvrzení.

### Test

- **Real-asset baked-PNG test (load-bearing — guard proti tichému návratu Q1 bugu):** pro KAŽDÉ `*.svg` v `herdeck.icons._ASSETS_DIR` ověřit, že **commitnutý** baked PNG `os.path.join(_ASSETS_DIR, frozen.glyph_png_name(svg_text))` existuje a `Image.open(...).load()` dekóduje na 196×196. Toto hlídá invariant „commitnuté SVG ⇒ commitnutý dekódovatelný baked PNG" pro všech 5 nových glyfů + codex, v čistém pytestu (bez freeze). (Pozn.: stávající `test_deckapp_frozen_icons.py` testuje jen WIRING přes syntetický `"<svg>codex</svg>"` — nečte reálné assety, proto tenhle test navíc.)
- Provider test: pro frozen-style provider (PNG rasterizer + baked assets dir) `_base_glyph(t)` vrátí **bundlovanou značku, ne `_letter_glyph`** pro každý z 5 typů (asset větev se trefí). Mirror baked-PNG testu (monkeypatch `is_frozen`/`baked_assets_dir`).
- Aktivový sanity test: všech 5 SVG existuje v `_ASSETS_DIR`, parsuje jako XML a má `fill="#ffffff"` (monochromní).
- Freeze gate: `build-sidecar.sh` + `smoke-sidecar.sh` projdou. **Rozšířit `smoke-sidecar.sh`**, aby místo hardcoded codex.svg ověřil dekódování baked PNG pro VŠECHNA `*.svg` (jinak smoke gate nové glyfy nepokryje).

---

## Komponenta 2: „Připojit k uloženému spojení" (Q3, řeší i Q2)

### Backend — nový choice `"saved"` v `/setup/connect`

V `server.connect(app, body)` (dispatch demo/local/remote) přidat větev `choice == "saved"`, transakčně jako ostatní (prepare PŘED persistem, commit po):

```python
if choice == "saved":
    remote = select_live()  # (config, server) z disku, token z keychainu; None když nic neresolvuje
    if remote is None:
        return {"ok": False, "error": "no saved connection"}
    config, server = remote
    prior_choice = read_choice(config_path)
    new_source = None
    try:
        new_source = build_live_source_for_connect(config, server)   # build (fallible)
        prepared = app._prepare_swap(new_source, clock=time.monotonic)  # render-prepare (fallible)
        clear_choice(config_path)                                    # persist: drop demo/local marker
    except Exception:
        _restore_choice(config_path, prior_choice)                   # marker netknutý / vrácený
        if new_source is not None:
            new_source.close()
        return {"ok": False, "error": "could not restore saved connection"}
    app._commit_swap(new_source, prepared)   # assignment-only, nefalibilní
    app._set_local_bridge(None)              # saved cílí na remote; pustit případný local bridge
    app._reloader = _reloader_for(app, ("remote",), _select_source)
    return {"ok": True, "connected": app._source.connected}
```

- Reuse existujících: `select_live()` (server.py:569, čte config + resolvuje remote server + keychain token; vrací `(config, server)` | `None`), `build_live_source_for_connect`, `_prepare_swap`/`_commit_swap`/`_set_local_bridge`/`_restore_choice`/`_reloader_for`/`_select_source`. Přidat `clear_choice` do importů v `connect` (vedle `read_choice, write_choice`).
- **Bezpečnost transakce:** marker se maže (`clear_choice`) až PO úspěšném build+prepare; selhání → `_restore_choice` + `new_source.close()`, předchozí source netknutý (stejný vzor jako remote/local/demo). `clear_choice` (smazání markeru) je idempotentní.
- **Token:** nikdy se nečte zpět ani neloguje — `select_live` ho resolvuje z keychainu interně do `ServerConfig.token`.
- **Žádná watcher-suppression:** na rozdíl od remote větve (která jde přes `_commit_remote` + `_suppress_reload`, protože píše config.toml) saved větev píše **jen** smazání `onboarding.toml`, který watcher NEsleduje (sleduje config.toml/local.toml). Takže `_suppress_reload` NETŘEBA — stejně jako demo/local větve. **Nekopírovat** `_commit_remote` suppression (bylo by špatně). `_reloader_for(app, ("remote",), _select_source)` + `_set_local_bridge(None)` je správná a úplná adopce (saved používá `config.servers[0]` = persistovaný remote, nikdy nespouští `LocalBridgeRunner`).
- **Žádný probe (vědomě):** typovaná remote větev běží `_probe_sync` (validace zadaných creds). Saved větev **neprobuje** — `select_live()` ověří jen PŘÍTOMNOST tokenu, ne platnost. Proto `{"ok": True, "connected": app._source.connected}` může mít `connected=False`: čerstvý `LiveSource` startuje `_connected=False` a překlopí se async po dialnutí (live.py), stejně jako remote větev (její honest komentář). Stale-but-present token → swap na live source, který může zůstat „disconnected" (a auto-reconnectí, když server naběhne — shodné s chováním živého decku při výpadku bridge). **Manuální gate ověří, že deck dojde do connected**, ne jen že karta překlopí. (Probe nepřidáváme — odporoval by „jeden klik instant" a živý source se reconnectne sám.)

### Backend — `saved_remote_available` ve `/setup` statusu

**POZOR — `select_live()` NENÍ keychain-free.** `select_live`→`resolve_profile`→`settings._server_config`→`secrets.get_secret`→`keyring.get_password` je synchronní čtení systémového keychainu. Volat ho na hot pollu (`/setup` ~2.5 s, po celou dobu běhu okna) je drahé a footgun: frozen/re-signed `.app` může vyvolat keychain prompt — a ten by teď padal na background timeru, ne jen při explicitním connectu. Navíc `select_live()` vrací **None při `HERDECK_MOCK`** (server.py:578), takže by tlačítko v mock-env zmizelo i s platným configem na disku.

Proto `_setup_status()` (server.py:287) přidá klíč přes **keychain-free helper** (raw TOML read, BEZ resolvu tokenu, mock-gated):

```python
"saved_remote_available": _has_saved_remote(self._config_service),
```
```python
def _has_saved_remote(config_service) -> bool:
    """True když je na disku config s aspoň jedním [[servers]] záznamem (raw read,
    BEZ resolvu tokenu/keychainu). Autoritativní resoluce je až connect-time
    select_live() (fail-soft 'no saved connection'). Mock-env nemá saved button."""
    if os.environ.get("HERDECK_MOCK") or config_service is None:
        return False
    path = config_service._config_path
    if not path.exists():
        return False
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    servers = data.get("servers")
    return isinstance(servers, list) and len(servers) > 0
```
- Levně signalizuje „existuje uložený remote config"; jestli token reálně resolvuje, rozhodne až `select_live()` při connectu (fail-soft). Mock-gate je konzistentní se stávajícím `reason="mock_env"` special-casingem. Raw TOML read je stejně levný jako stávající `read_choice` na pollu.

### Frontend — tlačítko v kartě

- `onboardingClient.ts`:
  - `SetupStatus` + `savedRemoteAvailable: boolean`; `parseSetupStatus` přidá `savedRemoteAvailable: v.saved_remote_available === true`.
  - `ConnectRequest` union + `| { choice: "saved" }`.
- `Onboarding.svelte`: tlačítko **„Připojit k uloženému spojení"** (gated `status?.savedRemoteAvailable === true`) MUSÍ být **nahoře v OBOU view blocích** — `{#if view === "reconnect"}` (řádky ~69-77) i welcome blok — ne jen welcome. (V `local_unavailable`/reconnect je `savedRemoteAvailable` taky relevantní: `_has_saved_remote` ignoruje marker, takže remote config na disku dá jednoklikový únik.) Klik → `run({ choice: "saved" })` (stávající `run`: `transport.connect` → na `ok` `onConnected()`). Flip-back ověřen end-to-end: `onConnected` (App.svelte) nuluje `reonboard` + re-polluje status → nový status `reason=null` (live) → `shouldOnboard`→`"deck"`. Drobná hláška „Máš uložené spojení." adresuje Q2.
- `view` rozhodnutí (`shouldOnboard`/`onboardingDecision`) **se nemění**: demo má `reason="demo"` → `onboardingDecision`=`"deck"`, takže demo deck se ukáže jako deck; tlačítko je dostupné přes `⚙` (override→welcome) — kde `savedRemoteAvailable` rozhodne o jeho zobrazení.

### Test

- **Backend (pytest):** `connect(app, {"choice":"saved"})` s resolvovatelným remote configem → smaže demo marker (`read_choice`→None) + přepne source na live; bez configu (`select_live`→None) → `{"ok": False, "error": "no saved connection"}`, marker netknutý. Selhání buildu → marker vrácený (`_restore_choice`), žádný leak (mirror remote/demo transaction testů).
- **Backend status (pytest) — konkrétní vstupy, NE tautologie:** config s `[[servers]]` → `saved_remote_available` True; **tentýž config + `HERDECK_MOCK`** → False; bez configu / prázdné `servers` → False. (NIKDY netestovat `== select_live() is not None` — re-derivuje hodnotu ze stejné fce a neodhalí mock masking.)
- **Frontend (Vitest):** `parseSetupStatus` mapuje `saved_remote_available`→`savedRemoteAvailable` — test: **defaultně `false`** když klíč chybí/má špatný typ, **`true`** když `saved_remote_available===true` (mirror `local_herdr_available` casů). `ConnectRequest {choice:"saved"}` projde transportem (`connect` invoke). Onboarding compile-smoke (`onboarding.smoke.test.ts`) po přidání tlačítka.

---

## Manuální gate (macbench)

1. **Ikony:** Claude dlaždice ukáže **Claude značku** (ne „C"); codex pořád OpenAI; (cursor/copilot/gemini když je má). Rebuild `.app` (freeze napeče assets) + redeploy.
2. **Demo exit:** „Prozkoumat demo" → demo deck → `⚙` → v kartě je **„Připojit k uloženému spojení"** → klik → karta překlopí na deck bez vypisování URL/tokenu; `onboarding.toml` zmizí. **Ověřit, že deck dojde do `live · connected`** (saved neprobuje — source dialuje async; nestačí jen překlopení karty).

## Non-goals

- Auto-mazání dema přes „← zpět na deck" (jen explicitní tlačítko; dismiss jen schová overlay).
- Restore **lokálního** spojení přes „saved" (saved cílí na remote z disku; local řeší stávající „Zkusit znovu").
- Změna offline-first designu ikon (žádný runtime Simple-Icons fetch; jen víc bundlovaných assetů). Network fetch zůstává jen pro web/elgato/d200 drivery v non-frozen režimu.
- Per-agent override ikon z UI (přes `overrides_dir` jde už dnes; mimo rozsah).

## Vyřešené technické body (z adverzariální verifikace)

- **agent_type OVĚŘENO živě** = `['claude','codex']` bare lowercase; herdr normalizuje aliasy (`claude-code`…) před emisí → žádný alias v `DEFAULT_AGENT_SLUGS` netřeba. Asset soubor = `agent_type` (ne slug).
- `_default_icons` má `fetch=None` v OBOU režimech → deck je offline-only; bundlovaný asset je jediná cesta k reálné ikoně (mimo codex).
- **Baked PNG musí být commitnuté** s SVG (baker píše do source-tree assets dir; čistý checkout bez PNG → tichý letter fallback). Real-asset test hlídá invariant.
- **`saved_remote_available` je keychain-free** (`_has_saved_remote`, raw TOML, mock-gated) — NE `select_live()` (čte keychain na hot pollu + maskuje se HERDECK_MOCK). Autoritativní resoluce až connect-time.
- „saved" connect mirroruje remote/local/demo transakci (build→prepare→persist(`clear_choice`)→commit; rollback `_restore_choice`+`close`); BEZ `_suppress_reload` (nepíše config.toml); BEZ probe (token jen present, `connected` async).
- Licence: ikonová data CC0 1.0, značky = ochranné známky vlastníků → NOTICE/attribution (pokrývá i stávající codex/OpenAI).
- `shouldOnboard` se nemění; tlačítko se řídí `savedRemoteAvailable`, v OBOU view blocích (welcome i reconnect), dostupné v demu přes ⚙-override.
