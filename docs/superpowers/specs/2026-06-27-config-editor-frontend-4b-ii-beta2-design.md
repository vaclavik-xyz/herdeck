# Config editor — Frontend řez 4b-ii-β2 (Tier-2/3 overlay + map-level explicit-empty) — design

## Kontext

Poslední frontend řez Phase 2 (GUI config editor). Staví na **β1** (merge `fb2dffe`), který dodal
overlay mechanismus (OverrideField inherit/override + chain-aware zděděné hodnoty + overlay
TriStateListField) a overlay-aware **Tier-1 sekce** (View / Deck / Safety / Theme).

Rodičovský design: `docs/superpowers/specs/2026-06-25-config-editor-frontend-design.md`. β1 design:
`docs/superpowers/specs/2026-06-26-config-editor-frontend-4b-ii-beta1-design.md`. β1 non-goals
explicitně vyjmenovaly tento řez:

- **Tier-2/3 overlay** — Notifications (telegram + per-profil secret), Macros (whole-list overlay),
  Start/Answer profiles (mapový per-entry overlay).
- **Map-level explicit-empty** — `start_profiles={}` (no-launchers), `profile.servers=[]` (serverless).
- ~~klik-to-jump preview~~ — **rozhodnuto odložit** do samostatného pozdějšího řezu (vyžaduje backend
  `/state` per-tile section-hint + embed deck preview do config okna; ortogonální naviga­ční vychytávka,
  nepatří k overlay editaci). **β2 zůstává frontend-only — žádná Python změna.**

Tímto řezem se uzavře overlay editace všech 8 `_OVERLAY_SECTIONS` + map-level explicit-empty.

## Klíčový princip (zděděný)

Žádná nová logika configu ve frontendu — tenká vrstva nad backend API. Token VALUE nikdy v JS / TOML /
response / logu (jen env-var NAME + `{set, source}`); keyring service literál „herdeck". Save model =
globální **Apply** (`POST /config` zapíše celé `{base,profiles,local}`). Overlay editace mutuje
`profiles[name][section]`, persistuje přes existující Apply. Žádné nové routes/commandy/Tauri commands.

## Backend grounding (proč to sedí)

`src/herdeck/settings.py`:
- `_merge_section(base, overlay)`: **dicty se mergují per-klíč rekurzivně, listy/skaláry nahrazují
  wholesale.** `_profile_overlays` aplikuje overlaye od base-most rodiče dolů přes `extends` řetězec.
- **Mapové sekce** `start_profiles` (`name → argv`, list) a `answer_profiles` (`name → {approve, deny,
  stop, approve_always}`, dict) se mergují **per-klíč**: overlay umí PŘEPSAT existující pojmenovanou
  položku nebo PŘIDAT novou. **Nemůže smazat zděděnou položku** — merge nezná „delete key". Tento limit
  se v UI promítne (zděděné položky nejdou v overlay smazat) + dokumentuje notem.
- **`start_profiles` / `macros` na BASE úrovni** mají tří-stav: `_launcher(None)` → `DEFAULT_START_PROFILES`,
  `_launcher({})` → `{}` (žádné launchery); `_macro_set(None)` → `DEFAULT_MACROS`, `[]` → žádná makra.
  `start_profiles = {}` jako overlay nad dict base je **no-op merge** (`dict(base)`), takže „no-launchers"
  je smysluplné jen v **base** módu.
- **`notifications`** dict merge per-klíč; `telegram` (nested dict) merge per-subklíč → per-subfield
  override (`token_env` / `chat_id`) je validní. Backend považuje telegram za platný jen když MERGED
  výsledek má OBA `token_env` + `chat_id`.
- **`profile.servers`** se NEmerguje — `_merged_sections` ho bere jako `selection = list(overlay["servers"])`
  (REPLACE wholesale). `servers = []` → `selection = []` → serverless; absent → zdědí base servery.

## Rozhodnutí (z brainstormu)

1. **klik-to-jump → odložit** (vlastní řez). β2 frontend-only.
2. **Jeden řez** — všechny 4 sekce + base map-empty v jednom subagent-driven plánu.
3. **Per-entry override** pro mapové sekce (Start/Answer) — zděděné položky zobrazeny s přepínačem
   Zdědit/Vlastní per pojmenovanou položku; override je celá pojmenovaná položka (pro `start_profiles`
   = argv list, pro `answer_profiles` = celý dict položky). Zděděnou položku nelze smazat → honest note.

## configClient (čisté, TDD)

Nové helpery (`desktop/src/lib/configClient.ts`). β1 helpery (`inheritedForPath`, `overrideValuePath`,
`overrideState`, `setOverridePath`, `clearOverridePath`, `inheritedChain` private) se reuse.

### Chain-aware mapový resolver (mirror backendu)

- **`mergeSection(base, overlay)`** — JS mirror `_merge_section`: oba dicty → merge per-klíč rekurzivně,
  jinak `overlay` nahrazuje. Čistá, tested.
- **`inheritedSection(payload, profile, section): Record<string, unknown>`** — efektivní zděděná mapa
  sekce = `mergeSection` přes `[base[section], ...parentOverlays[section]]` (parent overlaye z
  `inheritedChain`, EXCLUDING vlastní overlay profilu). Vrací `{}` když absent všude. Toto je věrný
  resolver pro zobrazení zděděných položek + seed při override (zabraňuje β1-stylu „undefined inherited").

### Per-entry override (Start/Answer)

Sekce čte živě z payloadu (žádné local rows v overlay módu):
- zděděné položky = `Object.keys(inheritedSection(...))`,
- vlastní overlay položky = klíče `profiles[prof][section]` (dict),
- efektivní = sjednocení; per položka `name`:
  - `state`: `name in ownOverlay ? "override" : "inherit"`,
  - `inheritedValue` = `inheritedSection[name]`, `overrideValue` = `ownOverlay[name]`.
- zápis override: `setOverridePath(profiles, prof, [section, name], value)`; clear:
  `clearOverridePath(profiles, prof, [section, name])` (smaže klíč, prořeže prázdné rodiče).
- přidání profilové položky: validace non-blank + non-duplicate jména → `setOverridePath` se seedem
  (prázdné argv / prázdný answer dict). Žádné rename existující overlay položky (klíč je fixní; změna =
  remove + re-add). Žádný delete na čistě zděděné položce (note).

### Whole-list / path varianty

- **`overrideStatePath(payload, profile, path): ListFieldState`** — path varianta `overrideState`
  (pro `macros` na 1 úrovni `["macros"]`): absent → `"default"`, `[]` → `"empty"`, jinak `"custom"`.
- **`macroRecords(raw: unknown): MacroRecord[]`** — extrakce `{label,text}[]` z libovolného list value
  (refactor: `macrosOf` ho použije na `payload.base.macros`; overlay ho použije na
  `overrideValuePath(payload, prof, ["macros"])`).

### Base map-level explicit-empty

- **`mapSectionState(payload, section): ListFieldState`** — `base[section]` absent → `"default"`,
  `{}` → `"empty"`, neprázdný dict → `"custom"`.
- **`setMapSectionState(payload, section, state): ConfigPayload`** — `"default"` → smaž klíč;
  `"empty"` → `setAt(base, section, {})`; `"custom"` → no-op (rows editor populuje). (Pro β2 použito jen
  pro `start_profiles`; `answer_profiles` base-empty není v rozsahu — backend nemá smysluplný empty default.)

### Serverless (profile.servers=[])

- **`profileServersState(payload, name): "inherit" | "explicit"`** — `servers` klíč absent → `"inherit"`,
  přítomen (vč. `[]`) → `"explicit"`.
- **`setProfileServersExplicit(payload, name, list): ConfigPayload`** — VŽDY zapíše klíč `servers = list`
  (i `[]` = serverless). **`clearProfileServers(payload, name)`** — OMIT klíč (zpět na inherit).
  Stávající `setProfileServers` (omit-when-empty) zůstává pro back-compat checkboxů; nový explicit setter
  ho doplní pro tri-state kontrolu.

## Widgety

Žádný nový generický widget — reuse `OverrideField` (skaláry) + `TriStateListField` overlay mód (listy)
z β1. Mapové per-entry řádky se skládají z `OverrideField` (per pojmenovanou položku) + vnořeného
editoru hodnoty (ListField pro argv, ListField×3+TriStateListField pro answer keys). Base map-empty
a serverless kontrola = malý in-section segmentovaný přepínač (vzor `TriStateListField`/`OverrideField`,
ne nový sdílený soubor — jednorázové).

## Sekce (overlay-aware)

Každá sekce dostane `editProfile: string | null`; `overlay = editProfile != null && editProfile !==
"default"`. **Base větev (`{:else}`) zůstává byte-equivalentní dnešku** (žádná regrese). Inner widgety
`label=""`. Default mirrory backendu s „keep in sync" komentářem (β1 lekce — frontend nezná backend
defaulty, takže při base-omitu skalárního klíče musí inherit display + override seed resolvnout efektivní
default).

- **Notifications** (`NotificationsSection.svelte`):
  - `enabled` / `sound` (bool) → `OverrideField` + `BooleanField`. Mirror `NOTIF_DEFAULTS =
    {enabled:false, sound:true}`.
  - `on` / `backends` (list) → overlay `TriStateListField`; inherit hint default `["blocked"]` /
    `["macos"]` (`NOTIF_LIST_DEFAULTS`).
  - `telegram.token_env` → `OverrideField` + `TokenSecretField` (path `[notifications, telegram,
    token_env]`); secret set/clear na env NAME (globální keychain) beze změny. `telegram.chat_id` →
    `OverrideField` + `TextField` (path `[notifications, telegram, chat_id]`). Inherit display token/chat
    spadá na „" (žádný default). Base větev = dnešní telegram editor (drop-empty-table logika) beze změny.
- **Macros** (`MacrosSection.svelte`):
  - overlay = whole-list override (backend nahrazuje list wholesale → per-entry nedává smysl).
    `OverrideField` [Zdědit | Vlastní] kolem celého editoru maker. Zdědit → dimmed „zděděno: N maker".
    Vlastní → editor maker (add/remove/edit) zapisující `profiles[prof].macros` přes `setOverridePath
    ["macros"]`; přepnutí na Vlastní seedne zděděný list (`macroRecords(inheritedForPath ["macros"])`).
    Zdědit → `clearOverridePath ["macros"]`. Base větev = dnešní editor beze změny.
- **Start profiles** (`StartProfilesSection.svelte`):
  - **base** větev: dnešní rows editor + **NOVÝ** segmentovaný [Výchozí | Vlastní | Vypnuto] (map-empty)
    s lokálním `mode` state (seed z `mapSectionState`, re-seed na `reloadRev`). Výchozí →
    `setMapSectionState "default"`; Vypnuto → `"empty"` (`{}`); Vlastní → odhalí rows editor.
  - **overlay** větev: per-entry (viz výše). Zděděné položky `OverrideField` [Zdědit | Vlastní]; Vlastní
    → `ListField` argv vázaný na override; přidat profilovou položku (name + button). Note „zděděné
    položky nelze v overlay smazat".
- **Answer profiles** (`AnswerProfilesSection.svelte`):
  - base větev beze změny (rows editor + reloadRev).
  - overlay větev: per-entry. Override položky = celý dict `{approve, deny, stop, approve_always}`
    (`ListField`×3 + overlay `TriStateListField` pro approve_always). Seed při override = zděděný dict
    (nebo prázdné listy pro novou). Note „zděděné nelze smazat".
- **Profiles** (`ProfilesSection.svelte`):
  - per-profil **servers** dostane `OverrideField` [Zdědit | Vybrat] (`profileServersState`):
    Zdědit → `clearProfileServers`; Vybrat → checkboxy (`setProfileServersExplicit`, **0 zaškrtnutých =
    serverless `[]`** s hintem „serverless: žádné servery"). Sekce NEdostává `editProfile` (meta-sekce,
    edituje per-profil přímo).

## ConfigApp (`ConfigApp.svelte`)

- Předá `{editProfile}` nově do **Macros / Notifications / Start profiles / Answer profiles** (dnes je
  nedostávají). Map sekce (Start/Answer) dál dostávají `{reloadRev}`.
- **Odstraní `BASE_ONLY_IN_OVERLAY` note** — po β2 jsou všechny `_OVERLAY_SECTIONS` overlay-aware, takže
  pole je prázdné a note se odstraní (Servers = base list, Profiles = meta — bez overlay, bez note, beze
  změny).
- Switcher / save / preview / banner / orphan-cleanup beze změny.

## Testing (TDD)

- **configClient** — čisté Vitest: `mergeSection` (dict per-key recursive, list/scalar replace);
  `inheritedSection` (přímý extends → base; vícevrstvý řetězec merge per-key; exclude vlastního overlaye;
  cyklus/neznámý cíl → base fallback přes `inheritedChain`); `overrideStatePath`; `macroRecords`;
  `mapSectionState` (absent/`{}`/non-empty); `setMapSectionState` (delete/`{}`/no-op, immutabilita);
  `profileServersState` (absent/`[]`/list); `setProfileServersExplicit` (zapíše i `[]`) +
  `clearProfileServers` (omit). Per-entry override pokrytí přes `setOverridePath`/`clearOverridePath`
  (existující testy) + `inheritedSection`.
- **Sekce** — build gate (`npm run build` exit 0) + compile-smoke (`widgets.smoke.test.ts` import).
  Base větve byte-equivalentní (žádná regrese). Repo nemá Svelte render harness — komponenty = build-gate
  (jako β1).
- Reálné chování proti reálným payload tvarům z `ConfigService.read()`.

## Architektura / jednotky

| Soubor | Status | Odpovědnost |
|---|---|---|
| `desktop/src/lib/configClient.ts` | modify | `mergeSection` + `inheritedSection` + `overrideStatePath` + `macroRecords` (refactor `macrosOf`) + `mapSectionState`/`setMapSectionState` + `profileServersState`/`setProfileServersExplicit`/`clearProfileServers` |
| `desktop/src/lib/configClient.test.ts` | modify | TDD pro všechny nové helpery |
| `desktop/src/lib/sections/NotificationsSection.svelte` | modify | overlay-aware (scalars + lists + telegram nested + secret) |
| `desktop/src/lib/sections/MacrosSection.svelte` | modify | overlay-aware whole-list override |
| `desktop/src/lib/sections/StartProfilesSection.svelte` | modify | overlay per-entry + base map-empty tri-state |
| `desktop/src/lib/sections/AnswerProfilesSection.svelte` | modify | overlay per-entry |
| `desktop/src/lib/sections/ProfilesSection.svelte` | modify | servers serverless (`[]`) authoring přes OverrideField + tri-state |
| `desktop/src/ConfigApp.svelte` | modify | wire `editProfile` do 4 sekcí; odstranit BASE_ONLY note |

Žádné nové runtime závislosti, žádné nové HTTP routes/Tauri commandy, žádná backend (Python) změna.

## Non-goals (β2)

- **klik-to-jump preview** (backend `/state` per-tile section-hint + embed deck preview) — vlastní řez.
- **Smazání zděděné mapové položky v overlay** — backend merge to neumí (honest note místo fake-delete).
- **`answer_profiles` base map-empty** — backend nemá smysluplný `{}` default (YAGNI).
- **Rename overlay mapové položky in-place** — klíč je fixní (remove + re-add).
- Zachování TOML komentářů, plný wizard, live preview neuložených editů, Windows/signing — Phase 3.

## Dekompozice

Jeden writing-plans plán, subagent-driven (jako β1). Pořadí: configClient helpery (TDD) → sekce
(každá overlay-aware, base beze změny) → ConfigApp wiring. Po něm je Phase 2 frontend overlay editace
kompletní; zbývá jen odložený klik-to-jump řez.
