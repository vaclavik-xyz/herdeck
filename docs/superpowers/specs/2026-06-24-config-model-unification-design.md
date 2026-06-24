# Config model unification — design

**Status:** design approved (brainstorming) · 2026-06-24
**Type:** refactor spec (precursor to the Phase 2 config editor)
**Branch:** `feat/config-unification`

## Problém

herdeck má dnes **dva paralelní config formáty** s **nepřekrývajícími se**
schopnostmi — ne jen syntaktická varianta, ale dvě schémata:

| Schopnost | Legacy flat (`config._load_legacy_config`) | Profiles (`settings._runtime_config`) |
|---|---|---|
| Více profilů + dědičnost (`extends`) | ❌ | ✅ |
| Pojmenované bloky (`[themes.X]`, `[views.X]`, `[launchers.X]`, `[macro_sets.X]`, `[notification_profiles.X]`) | ❌ | ✅ |
| Vlastní `answer_profiles` (klávesy per typ agenta) | ✅ | ❌ natvrdo `DEFAULT_PROFILES` |
| Vlastní `grid` | ✅ | ❌ natvrdo `5×3` |
| `theme` / `view` / `safety` | ❌ (jen defaulty) | ✅ |
| Názvy sekcí | `answer_profiles`, `notifications`, `macros`, `start_profiles` | `notification_profiles`, `macro_sets`, `launchers` |

**Ani jeden formát není nadmnožina druhého.** Legacy umí vlastní klávesy a grid,
ale ne profily ani theme/view/safety; profiles umí profily a theme/view/safety,
ale natvrdo bere defaultní klávesy a 5×3. Pro plánovaný GUI **config editor**
(Phase 2) je stavění nad dvěma rozcházejícími se schématy křehké a posvětilo by
matoucí model. Proto sjednocení **předchází** editoru.

### Zjištěný stav nasazení

- **Profiles formát se reálně nepoužívá nikde** — mini i macbench jedou legacy
  flat; repo nemá žádnou profiles-format config. Žádnou nasazenou config
  nemusíme migrovat (jen testy).
- Profilové API ze `settings.py` (`load_settings`, `resolve_profile`,
  `list_profiles`, `set_active_profile`, `validate_settings`) konzumují:
  `config.load_config`, `app.make_profile_switcher` a `app.main()` —
  tj. **Profiles menu na decku** (přepnutí aktivního profilu → reload). Tyto
  cesty musí dál fungovat.

## Cíl

**Jeden config model, jeden resolver.** Báze = všechny sekce naplocho. Profily =
volitelné overlaye přes **tytéž** sekce. „Jednoduchá" config (legacy flat) = báze
bez profilů. Profily získají i to, co dnes legacy umí navíc (custom klávesy, grid).

## Sjednocené schéma

### Báze (vždy přítomná = config když není aktivní žádný profil)

Sjednocení čte **všechny** sekce naplocho — superset dnešního legacy o
`theme`/`view`/`safety`:

```toml
[[servers]]
id = "local"
url = "ws://100.x.y.z:8788"
token_env = "HERDECK_TOKEN"          # hodnota z env/keychain, NIKDY v TOML

[deck]
grid = "5x3"
overview_order = ["local"]

[answer_profiles.claude]
approve = ["1", "enter"]
approve_always = ["2", "enter"]
deny = ["esc"]
stop = ["ctrl+c"]

[[macros]]
label = "continue"
text = "continue"

[start_profiles]
claude = ["claude"]

[notifications]
enabled = true
on = ["blocked"]
backends = ["macos"]

[theme]                              # NOVĚ čteno i ve flat configu
[theme.colors]
blocked = "amber"

[view]                               # NOVĚ čteno i ve flat configu
management = "launcher_menu"
tile_fields = ["repo", "branch", "status", "time", "server"]

[safety]                             # NOVĚ čteno i ve flat configu
approve_always = true
require_confirm_for = []
```

### Profily = volitelné overlaye přes tytéž sekce

```toml
[profiles.mobile]
extends = "default"                  # volitelné; báze je implicitní rodič
servers = ["local"]                  # podmnožina serverů (id), volí overview_order

[profiles.mobile.view]
management = "bottom_row"            # přepíše JEN tohle pole, ostatní z báze

[profiles.mobile.notifications]
backends = ["telegram"]

[profiles.mobile.deck]
grid = "4x3"                         # profil teď umí i grid

[profiles.mobile.answer_profiles.claude]
approve = ["y", "enter"]            # profil teď umí i custom klávesy
```

Profil smí přepsat libovolnou podmnožinu libovolné sekce, pod stejnými názvy
sekcí jen nested do `[profiles.<jméno>.<sekce>]`.

### `local.toml` (beze změny)

Strojově-specifické a netrackované do shared configu:

```toml
active_profile = "mobile"            # který profil je aktivní na tomto stroji
[local]
deck = "d200"
[hardware]
brightness = 80
```

## Resolve algoritmus

`resolve(data, local_data, *, name=None, env_profile) -> Config`:

1. **Aktivní profil** podle priority: `HERDECK_PROFILE` env (zamyká přepínání) >
   explicitní `name` argument > `local.toml active_profile` > `"default"`.
2. **Sestav řetěz overlayů** od báze:
   - `"default"` / nenastaveno / žádné `[profiles.*]` → jen báze. `"default"` je
     **rezervované jméno = báze**; `[profiles.default]` se nepodporuje (báze *je*
     default) — když existuje, `validate_settings` to nahlásí jako chybu.
   - jinak: `[profiles.<aktivní>]` + jeho `extends` řetěz (báze je vždy kořen);
     cyklus → `ConfigError`.
3. **Slouč po sekcích** (báze → postupně overlaye):
   - **tabulky** (`view`, `safety`, `theme`, `deck`, `notifications`,
     `answer_profiles.<typ>`): merge **field-by-field** (overlay přepíše jen
     uvedená pole).
   - **skaláry** (`grid`, `enabled`, …): replace.
   - **seznamy** (`servers`, `macros`, `bottom_row`, `tile_fields`,
     `on`, `start_profiles` hodnoty): replace celé.
   - `servers` v profilu = **výběr id** z bázových `[[servers]]` (určuje i
     `overview_order`), ne re-definice.
4. **Postav `Config` jednou** z výsledného slitého dictu (jeden builder sdílený
   bází i overlayem). Hardware z `local.toml`. Sekrety přes `token_env`.

Žádný profil → výsledek = báze (= dnešní legacy chování).

## Co se mění v kódu

- **`settings.py`**: `_resolve_legacy` vs `_runtime_config` split → **jeden**
  base-reader (servers, deck, answer_profiles, macros, start_profiles,
  notifications, theme, view, safety) + **overlay-applier** (merge po sekcích) +
  jeden `Config` builder. `resolve_profile`/`list_profiles`/`set_active_profile`/
  `validate_settings` zachovat (signatury stejné), uvnitř přepsat na nový model.
- **`config.py`**: `load_config` přestane větvit legacy vs profiles — jedna cesta
  přes nový resolver. `_load_legacy_config` se stane base-readerem (rozšířeným o
  theme/view/safety). `_parse_grid`, `_parse_profile`, `parse_notifications`
  zůstávají jako stavební bloky.
- **Konzumenti** (`app.make_profile_switcher`, `app.main`): beze změny API —
  jen jedou nad novým resolverem.

## Co se odstraňuje

- Pojmenované bloky `[themes.X]`, `[views.X]`, `[macro_sets.X]`, `[launchers.X]`,
  `[notification_profiles.X]` a jejich reference jménem z profilu.
- `settings._named_block`, `_runtime_config` (named-block varianta),
  `_resolve_legacy` (split). Žádný back-compat reader starého profiles formátu
  (rozhodnutí: zahodit — reálně se nepoužívá).

## Zpětná kompatibilita

- **Legacy flat configy** (mini, macbench, repo) se načtou jako báze →
  **identické runtime chování** (theme/view/safety zůstanou defaultní, když
  chybí; když je uživatel doplní, nově se projeví).
- **Deck Profiles menu** + `switch_profile` + `local.toml active_profile` +
  `HERDECK_PROFILE` env-lock fungují dál nad novým modelem. `"default"` je vždy
  platný profil (= báze), takže přepnutí zpět na bázi funguje.
- **Žádná migrace** reálných configů. Staré profiles-format **testy** se přepíšou
  na nový overlay model.

## Error handling

- neznámý aktivní profil (a není `"default"`) → `ConfigError("unknown profile …")`
- definovaný `[profiles.default]` → `ConfigError` (rezervované jméno = báze)
- `extends` cyklus → `ConfigError("profile inheritance cycle: …")`
- profil odkazuje na neznámé server-id → `ConfigError("unknown server …")`
- chybějící `token_env` hodnota → `ConfigError("env var … is not set")` (zachovat)
- neplatný `grid` → `ConfigError("invalid grid …")` (zachovat)
- `validate_settings` → seznam chyb: báze musí postavit + každý profil musí
  resolvovat (prefix jménem profilu).

## Testing (TDD)

- **Legacy-compat:** existující flat config (mini/macbench tvar) resolvuje na
  stejný `Config` jako dnes (servery, grid, answer_profiles, macros,
  notifications). Žádný `[profiles.*]` → báze.
- **Flat nově čte theme/view/safety:** `[theme]`/`[view]`/`[safety]` ve flat
  configu se projeví (dnes ignorováno).
- **Overlay merge:** profil přepíše jen uvedené pole sekce (`view.management`),
  ostatní pole z báze zůstanou; seznam (`servers`, `macros`) se nahradí celý.
- **Profil umí to, co dřív profiles formát neuměl:** `[profiles.X.deck] grid` a
  `[profiles.X.answer_profiles.claude]` se projeví.
- **`extends` řetěz** (default → work → mobile) + **cyklus** → chyba.
- **Priorita aktivního profilu:** env > `name` arg > `local.toml` > `"default"`.
- **`set_active_profile`** zapíše `active_profile` do `local.toml`; env-lock
  brání zápisu; `"default"` přepne zpět na bázi.
- **`validate_settings`** chytí vadnou bázi i vadný profil; konkrétní hlášky.
- Přepsat/odstranit staré named-block testy v `test_settings.py`.

## Non-goals

- GUI editor (to je Phase 2, navazuje na tento čistý model).
- Zachování komentářů v TOML (řeší se až u zápisu v editoru).
- Migrace reálných configů (žádné profiles-format nasazení neexistuje).
- Přejmenování `answer_profiles` (matoucí blízkost k `profiles`) — mimo scope,
  zachovat kvůli kompat.

## Návaznost

Po sjednocení stojí Phase 2 editor nad **jedním** modelem: edituje bázové sekce a
volitelně spravuje profilové overlaye přes tatáž pole — bez dvou kódových cest.
