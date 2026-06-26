# Config editor — Frontend řez 4b-ii-β1 (overlay mechanismus + Tier-1 sekce) — design

## Kontext

Phase 2 frontend (GUI config editor, 2. okno desktop appky) se staví po řezech nad hotovým backend
API (`ConfigService` + sidecar routes, merge `2fe46d4`). Rodičovský frontend design:
`docs/superpowers/specs/2026-06-25-config-editor-frontend-design.md`. Stav:

- **Řezy 3 / 4a / 4b-i** — MERGED do main (4b-i merge `74b774b`).
- **Řez 4b-ii** rozdělen na **α** + **β**. **α** (base-mode three-state primitivum) — MERGED do main
  (merge `4a25572`): `listFieldState`/`setListField` + `TriStateListField` segmentovaný [Výchozí |
  Vlastní | Vypnuto].
- **Řez 4b-ii-β** rozdělen na **β1** (tento dokument) + **β2** (samostatný pozdější design) podle
  difficulty tierů overlay editace.

Tento dokument konkretizuje **řez 4b-ii-β1**: mechanismus per-sekce **overlay editace profilu**
(OverrideField inherit/override + chain-aware zděděné hodnoty) a jeho nasazení v **Tier-1 sekcích**
(View / Deck / Safety / Theme). Mapové/secret/list-replace sekce + map-level explicit-empty +
klik-to-jump jsou **β2**.

## Klíčový princip (zděděný)

Žádná nová logika configu ve frontendu — tenká vrstva nad backend API. Token nikdy v JS. Save model =
explicitní globální **Apply** (`POST /config` zapíše celé `{base,profiles,local}`). Overlay editace
mutuje `profiles[name][section]`, persistuje přes existující Apply — žádné nové routes/commandy.

## Sjednocený přepínač (active == edit-target)

Rozhodnuto v α brainstormu: přepínač profilu z 4b-i je ZÁROVEŇ editační cíl. Když je aktivní:

- **`default`** → **base mód**: Tier-1 formuláře editují `base` přímo (jako dnes / α).
- **pojmenovaný profil X** → **overlay mód**: Tier-1 formuláře editují `profiles[X][section]` jako
  override nad zděděnou hodnotou.

Přepínač (set_active + reload, env-lock + dirty-guard) zůstává z 4b-i beze změny — β1 jen přidá, že
sekce **sledují** aktivní profil. Preview = saved-state aktivního profilu, takže ukazuje přesně
editovaný overlay (čistá smyčka — důvod pro sjednocení). ConfigApp spočítá `editProfile`
(`activeProfile`, nebo `null` pro `default`) a předá ho Tier-1 sekcím + zobrazí v hlavičce indikátor
„edituješ overlay: X".

## Overlay mechanismus

### configClient (čisté, TDD)

- **chain-aware zděděná hodnota** — `inheritedFor(payload, profile, section, key)`: resolvne zděděnou
  hodnotu přes celý `extends` řetězec profilu (mirror backend `_profile_overlays`), **EXCLUDING vlastní
  overlay profilu**. Tj. od base-most rodiče dolů aplikuje overlaye až k bezprostřednímu rodiči X;
  pokud rodič klíč přepisuje, vrátí rodičovu hodnotu, jinak base. Walk s `seen` setem — cyklus/neznámý
  cíl ukončí walk a spadne na base (backend cyklus/unknown stejně odmítne při write). Řez-3 base-only
  `inheritedValue(base, section, key)` zůstává jako per-level primitivum (používá ho `inheritedFor`);
  jeho semantika se nemění (existující testy drží).
- **overlay field-state** — `overrideState(payload, profile, section, key)` → `"inherit" | "custom" |
  "empty"`: klíč v `profiles[profile][section]` absent → `inherit`; `[]` → `empty`; neprázdný/skalár →
  `custom`. Symetrické s α `listFieldState` (kde „default"↔„inherit").
- **overlay value reader** — `overrideValue(payload, profile, section, key)`: hodnota z
  `profiles[profile][section][key]` (nebo `undefined` když absent). `getAt` umí jen `base`/`local`
  roots, takže overlay hodnotu (zobrazenou ve widgetu při „Vlastní") čte tenhle helper. Pro 2-úrovňové
  klíče (`theme.colors.<status>`) path-varianta `overrideValuePath(payload, profile, path)`.
- **set/clear override** — řez-3 `setOverride(profiles, name, section, key, value)` + `clearOverride(...)`
  (1 úroveň `profiles[name][section][key]`) zůstávají. Pro **2-úrovňové** klíče (`theme.colors.<status>`)
  přidám path-variantu `setOverridePath(profiles, name, path: string[], value)` / `clearOverridePath`
  (vytvoří/promaže vnořenou cestu, prořeže zprázdné rodiče) — `theme.colors` se mergují per-status-klíč,
  takže per-status override musí žít na `profiles[X].theme.colors.<status>`, ne přepsat celý `colors`.

### Widgety

- **`OverrideField.svelte` (nový):** 2-segment [Zdědit | Vlastní] wrapper kolem skalárního widgetu.
  „Zdědit" → dimmed `zděděno: <inheritedDisplay>`; „Vlastní" → vykreslí předaný widget (Svelte 5
  `children` snippet). Props: `{ label, state: "inherit"|"override", inheritedDisplay: string,
  onstate: (s) => void, children }`. Přepnutí na „Vlastní" seedne override hodnotu zděděnou hodnotou
  (start z aktuálního stavu); „Zdědit" zavolá `clearOverride*`.
- **`TriStateListField` (rozšíření):** overlay mód přes props `inheritLabel?`/`inheritHint?` (default
  zachová α „Výchozí" + backend-default chování). V overlay módu první segment = „Zdědit", hint =
  zděděná hodnota. Stejný 3-stav (inherit/custom/empty) a stejné seedování `[""]` při přechodu na
  „Vlastní" z prázdna.

## Tier-1 sekce (overlay-aware)

Sekce dostanou `editProfile: string | null` a renderují podle módu:

- **View** — skaláry (management/agent_slots/show_profile_on_panel) přes `OverrideField`; listy
  (bottom_row/tile_fields/tile_primary/tile_secondary) přes `TriStateListField` v overlay módu.
- **Deck** — `grid` (OverrideField) + `overview_order` (overlay TriStateListField). **Hardware
  fieldset zůstává local-only i v overlay módu** — edituje `local.toml` bez ohledu na profil, NIKDY se
  neoverlayuje (machine-specific). V overlay módu jen vizuálně oddělen notem „hardware je vždy local".
- **Safety** — `approve_always` (bool, OverrideField) + `require_confirm_for` (overlay TriStateListField).
- **Theme** — `colors.<status>` per-status override (OverrideField nad TextField, zápis přes
  `setOverridePath` na `[theme, colors, status]`) + `server_accents` (overlay TriStateListField).

V **base módu** (`editProfile == null`) renderují přesně jako dnes/α (žádná regrese).

## Non-β1 sekce v overlay módu

Když je aktivní pojmenovaný profil, **non-β1 sekce** (Notifications / Macros / Start profiles / Answer
profiles / Profiles / Servers) **dál editují base** (= dokumentované α chování — base edity jsou
globální, aktivní profil je zdědí). Každá ukáže zvýrazněný **note**: „⚠ tato sekce zatím edituje base
(overlay editace přijde v řezu β2)". Žádná z nich se v β1 neoverlayuje. (Note se přidá lehce v ConfigApp
nebo per-sekci — implementační detail plánu.)

## Backend grounding (proč to sedí)

`src/herdeck/settings.py`:
- `_merged_sections` + `_profile_overlays`: overlaye od base-most rodiče dolů přes `extends` řetězec;
  `_merge_section` = **dicty se mergují per-klíč rekurzivně, listy/skaláry nahrazují wholesale**. Proto
  per-field (per-key) override sedí pro dict sekce; `theme.colors` (nested dict) se merguje per-status,
  takže per-status override je validní; listy (overview_order, tile_*, server_accents,
  require_confirm_for) override = nahradit celý list (vč. explicitního `[]` = vypnuto).
- `_OVERLAY_SECTIONS` = deck, answer_profiles, macros, start_profiles, notifications, theme, view,
  safety. β1 pokrývá overlay-aware editaci 4 z nich (view/deck/safety/theme); zbylé 4 (+ Servers výběr,
  který je 4b-i) jsou β2. Hardware NENÍ overlay sekce (local-only).

Žádná backend (Python) změna v β1.

## Architektura / jednotky

| Soubor | Status | Odpovědnost |
|---|---|---|
| `desktop/src/lib/configClient.ts` | modify | `inheritedFor` (chain-aware) + `overrideState` + `overrideValue`/`overrideValuePath` + `setOverridePath`/`clearOverridePath`; reuse řez-3 `inheritedValue`/`setOverride`/`clearOverride` |
| `desktop/src/lib/fields/OverrideField.svelte` | create | 2-segment [Zdědit\|Vlastní] skalární wrapper (children snippet) |
| `desktop/src/lib/fields/TriStateListField.svelte` | modify | overlay mód (inheritLabel/inheritHint) |
| `desktop/src/ConfigApp.svelte` | modify | spočítat `editProfile` z activeProfile, předat Tier-1 sekcím; overlay indikátor v hlavičce; note pro non-β1 sekce v overlay módu |
| `desktop/src/lib/sections/ViewSection.svelte` | modify | overlay-aware (OverrideField + overlay listy) |
| `desktop/src/lib/sections/DeckSection.svelte` | modify | overlay-aware grid+overview_order; Hardware zůstává local-only |
| `desktop/src/lib/sections/SafetySection.svelte` | modify | overlay-aware approve_always + require_confirm_for |
| `desktop/src/lib/sections/ThemeSection.svelte` | modify | overlay-aware colors (per-status, 2-level) + server_accents |
| `desktop/src/lib/configClient.test.ts` | modify | TDD: `inheritedFor` (chain + exclude self + cycle/unknown), `overrideState`, `setOverridePath`/`clearOverridePath` |

Žádné nové runtime závislosti, žádné nové HTTP routes, žádné nové Tauri commandy, žádná backend změna.

## Testing (TDD)

- **configClient resolvery** — čisté Vitest: `inheritedFor` (přímý extends → base; vícevrstvý řetězec
  rodič přepisuje vs ne; exclude vlastního overlaye; cyklus → fallback base; neznámý cíl → fallback);
  `overrideState` (absent→inherit, []→empty, list/skalár→custom); `setOverridePath`/`clearOverridePath`
  (vnořená cesta vytvořena/promazána, prázdní rodiče prořezáni, immutabilita).
- **OverrideField + overlay sekce** — build gate (`npm run build`) + compile-smoke; render overlay vs
  base módu ověřitelný kompilací. (Repo nemá Svelte render/interaction harness — komponenty = build-gate,
  jako řezy 4a/4b-i/α.)
- Reálné chování proti reálným payload tvarům z `ConfigService.read()`.

## Non-goals (4b-ii-β1)

- **Tier-2/3 overlay** — Notifications (telegram + per-profil secret), Macros (whole-list overlay),
  Start/Answer profiles (mapový per-entry overlay) — **řez β2**.
- **Map-level explicit-empty** — `start_profiles={}`, `profile.servers=[]` — **β2**.
- **klik-to-jump preview** (s backend per-tile section-hintem) — **β2**.
- Zachování TOML komentářů, plný wizard, live preview neuložených editů, Windows/signing — Phase 3.

## Dekompozice

Řez 4b-ii-β1 je jeden writing-plans plán, subagent-driven (jako řezy 3 / 4a / 4b-i / α). Po něm
následuje samostatný brainstorm + spec + plán pro **řez 4b-ii-β2** (Tier-2/3 overlay + map-level
explicit-empty + klik-to-jump).
