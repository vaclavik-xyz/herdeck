# Config editor — Frontend řez 4b-ii-α (base-mode three-state + primitivum) — design

## Kontext

Phase 2 frontend (GUI config editor, 2. okno desktop appky) se staví po řezech nad hotovým backend
API (`ConfigService` + sidecar routes, merge `2fe46d4`). Rodičovský frontend design:
`docs/superpowers/specs/2026-06-25-config-editor-frontend-design.md`. Stav:

- **Řez 3** (transport + shell + Servers sekce) — MERGED do main.
- **Řez 4a** (base-mode editace všech 8 sekcí + widgety + onboarding + backend `env_locked`/
  `active_profile`) — MERGED do main (merge `6893a03`).
- **Řez 4b-i** (Profiles sekce + funkční přepínač aktivního profilu + deferred fixy) — MERGED do main
  (merge `74b774b`).
- **Řez 4b** byl rozdělen na **4b-i** (hotovo) + **4b-ii**. **4b-ii** je dále rozděleno na **4b-ii-α**
  (tento dokument) + **4b-ii-β** (samostatný pozdější design).

Tento dokument konkretizuje **řez 4b-ii-α**: znovupoužitelné *three-state* (tri-state) primitivum pro
listová pole a jeho nasazení v **base módu**, čímž odblokuje záměrný explicit-empty authoring deferred
z řezů 4a a 4b-i. Per-sekce **overlay** editace profilů (OverrideField inherit/override/clear) je
**4b-ii-β** a staví na tomto primitivu.

## Klíčový princip (zděděný)

Žádná nová logika configu ve frontendu — tenká vrstva nad backend API. Token nikdy v JS (Rust commandy
ho injektují). Save model = explicitní globální **Apply** (`POST /config` zapíše celé `{base,profiles,
local}`). Secret hodnoty jednosměrné, nikdy v modelu/odpovědi/logu.

## Problém, který α řeší (absent ≠ empty)

V tomto configu **chybějící** listový klíč znamená „použij backend default" (např. `DEFAULT_BOTTOM_ROW`,
všechny servery pro `overview_order`, defaultní tile-line tokeny), zatímco **explicitní `[]`** znamená
„nic" — a default se tím záměrně vypne. Řez 4a tuhle dvojznačnost vyřešil bezpečně, ale jednosměrně:
`putList` při vyprázdnění seznamu klíč **vynechá** (návrat k defaultu), takže **explicitní `[]` nešel
nikdy autorovat** z editoru. Tím zůstaly nedostupné legitimní konfigurace:

- `view.tile_primary = []` / `view.tile_secondary = []` — vypnout řádek textu na dlaždici.
- `deck.overview_order = []` — prázdný overview (žádné servery).
- `answer_profiles.<jméno>.approve_always = []` — žádné always-approve klíče (vs. absent → fallback
  na `approve`).
- (a obecně každý listový klíč s neprázdným backend defaultem).

α zavádí **tři rozlišitelné stavy** modelu pro listový klíč a UI, které je umí autorovat.

## Tři stavy listového klíče

Model nese pro listový klíč právě tři rozlišitelné stavy:

| Stav | Tvar v modelu | Význam | Zápis |
|---|---|---|---|
| **Výchozí** | klíč ABSENT | použij backend default | `removeAt` (vynechat klíč) |
| **Vlastní** | klíč = neprázdný list | editovaný seznam | `setAt(..., list)` |
| **Vypnuto** | klíč = `[]` | explicitní „nic" (default vypnut) | `setAt(..., [])` |

Detekce stavu je čistá funkce nad payloadem: absent → `default`, `[]` → `empty`, neprázdný → `custom`.
Model má jen tyhle tři rozlišitelné tvary, takže „Vlastní" s ručně vymazanými všemi položkami zapíše
`[]` a při příštím renderu se přečte jako **Vypnuto** (dokumentované, žádný čtvrtý stav).

## Primitivum — configClient helpery (čisté, TDD)

Do `desktop/src/lib/configClient.ts` (mirror stávajících `getAt`/`setAt`/`removeAt`/`putList`):

```ts
export type ListFieldState = "default" | "custom" | "empty";

/** Tri-state of a list key: absent → "default", [] → "empty", non-empty → "custom". */
export function listFieldState(
  payload: ConfigPayload, root: ConfigRoot, section: string, key: string,
): ListFieldState;

/** NEW payload writing the chosen tri-state:
 *  "default" → key OMITTED (removeAt); "empty" → key = []; "custom" → key = list
 *  (a "custom" list that happens to be empty is written as [] and reads back as "empty"). */
export function setListField(
  payload: ConfigPayload, root: ConfigRoot, section: string, key: string,
  state: ListFieldState, list: string[],
): ConfigPayload;
```

`setListField` skládá existující `setAt`/`removeAt` — žádná nová mutační cesta. **Nahrazuje** 4a
`putList` omit-on-empty u polí, která tri-state přijmou; `putList` zůstává pro pole, která explicitní
`[]` nepotřebují. Plně otestováno čistým Vitestem (tabulka 3 stavů + round-trip + immutabilita).

## Primitivum — `TriStateListField.svelte` (nový widget)

Wrapper kolem stávajícího `ListField`:

- Segmentovaný přepínač **[Výchozí | Vlastní | Vypnuto]** nad editorem; aktivní stav vždy viditelný.
- **Vlastní** → vykreslí `ListField` (editace seznamu).
- **Výchozí** → seznam skrytý; dimmed hint `defaultHint` (viz níže).
- **Vypnuto** → seznam skrytý; dimmed hint „prázdné — vypnuto".
- Props: `label`, `state: ListFieldState`, `list: string[]`, volitelný `defaultHint?: string`,
  callback `onchange(state, list)`. Sekce v callbacku zavolá `setListField(...)` + `onChange()`.
- Přechody segmentů: na „Vlastní" se zachová poslední neprázdný seznam (nebo prázdný k editaci); na
  „Výchozí"/„Vypnuto" se seznam jen schová (hodnota se odvodí ze stavu při zápisu).

Žádná nová runtime závislost; čistě kompozice nad `ListField`. Ověření = build gate (`npm run build`),
jako ostatní widgety (komponenty = build-gate, NE svelte-check).

## Default display (stav „Výchozí")

Dimmed hint generický **„(výchozí)"**; kde je default levně odvoditelný z modelu, ukáže se konkrétně:

- `deck.overview_order` → `defaultHint` = id všech serverů v pořadí (`serversOf(payload)`).
- Ostatní (`tile_primary`/`tile_secondary`/`bottom_row`/`tile_fields`/`notifications.on`/`backends`)
  → generické „(výchozí)". **Nebudeme duplikovat** backend token/konstantní defaulty do frontendu
  (drift). Reálnou výslednou hodnotu uživatel vidí v živém preview po Apply.

## Pole adoptující tri-state (base mód)

Adoptují **jen** listová pole s **neprázdným** backend defaultem (kde `default ≠ empty` má smysl):

| Sekce | Klíče | Pozn. |
|---|---|---|
| **View** | `tile_primary`, `tile_secondary`, `bottom_row`, `tile_fields` | dnes `putList` → `TriStateListField` |
| **Deck** | `overview_order` | dnes `putList` → `TriStateListField`; `defaultHint` z serverů |
| **Notifications** | `on`, `backends` | dnes plain list; default `["blocked"]` / `["macos"]` |
| **Answer profiles** | per-řádek `approve_always` | řádky už nesou `string[] | null`; tri-state mapuje null↔absent/Výchozí, `[]`↔Vypnuto, list↔Vlastní |

**Nepřijímají** (default už je `[]`, tri-state zbytečný): `safety.require_confirm_for` — zůstává plain
`ListField`. Skalární/enum/mapová pole se α netýkají.

**Dvě roviny zapojení** (důležité pro plán): sdílený **widget** `TriStateListField` je čistě UI nad
`(state, list)` a používá se ve všech čtyřech sekcích. Payload-level helpery `listFieldState`/
`setListField` se ale týkají jen **přímých** klíčů `payload[root][section][key]` (View, Deck,
Notifications). `answer_profiles.<jméno>.approve_always` je o úroveň hlouběji uvnitř mapového řádku —
tam widget řídí **řádkové** pole `approve_always: string[] | null` (null↔Výchozí, `[]`↔Vypnuto,
list↔Vlastní) a `serializeNamedRows` rozdíl null vs `[]` už zachovává (zavedeno v 4a). Žádný
payload-level helper se pro approve_always nepoužívá.

## Backend grounding (proč to sedí)

`src/herdeck/settings.py`: konzumenty čtou klíče přes `if key in raw` / `raw.get(key, <default>)`, takže
**absent vrací default, explicitní `[]` vrací `[]`** — přesně tři stavy, které α autoruje. Příklady:
`_view_config` (`if "tile_primary" in raw`), `_notifications_config` (`raw.get("on", ["blocked"])`),
answer-profil `approve_always` (absent → fallback na `approve`). Žádná backend změna v α — jen frontend
přestane omítat explicitní `[]` u adoptovaných polí.

## Architektura / jednotky

| Soubor | Status | Odpovědnost |
|---|---|---|
| `desktop/src/lib/configClient.ts` | modify | `ListFieldState` typ + `listFieldState` + `setListField` (čisté, nad `setAt`/`removeAt`) |
| `desktop/src/lib/fields/TriStateListField.svelte` | create | segmentovaný [Výchozí\|Vlastní\|Vypnuto] wrapper nad `ListField` |
| `desktop/src/lib/sections/ViewSection.svelte` | modify | 4 listová pole `putList` → `TriStateListField` |
| `desktop/src/lib/sections/DeckSection.svelte` | modify | `overview_order` → `TriStateListField` (+ `defaultHint` ze serverů) |
| `desktop/src/lib/sections/NotificationsSection.svelte` | modify | `on`, `backends` → `TriStateListField` |
| `desktop/src/lib/sections/AnswerProfilesSection.svelte` | modify | `approve_always` per-řádek → `TriStateListField` (null↔Výchozí) |
| `desktop/src/lib/configClient.test.ts` | modify | TDD testy `listFieldState` + `setListField` |

Žádné nové runtime závislosti, žádné nové HTTP routes, žádné nové Tauri commandy, žádná backend (Python)
změna. Čistě frontend nad existujícím modelem.

## Testing (TDD)

- **configClient helpery** — čisté Vitest: `listFieldState` (absent→default, `[]`→empty, list→custom),
  `setListField` (každý ze 3 stavů zapíše správný tvar; default omítne klíč; immutabilita; round-trip
  `listFieldState(setListField(...))`), interakce s `getAt`/`removeAt`.
- **TriStateListField + sekce** — build gate (`npm run build`); segment přepíná renderovaný stav,
  callback volá `setListField` se správným stavem.
- Reálné chování proti reálným payload tvarům z `ConfigService.read()`, žádné mocky logiky.

## Non-goals (4b-ii-α)

- **OverrideField / per-sekce overlay editace profilů** (inherit/override/clear nad `_OVERLAY_SECTIONS`)
  — **řez 4b-ii-β** (znovupoužije tohle tri-state primitivum: presence = override/inherit, empty =
  explicitní override na „nic").
- **Map-level explicit-empty** — `start_profiles = {}` (no-launchers), `profile.servers = []`
  (serverless profil) — shape-different (celá mapa/sekce), **→ β**.
- **klik-to-jump preview** — frontend nemá tile sémantiku (dlaždice jsou neprůhledné PNG; sémantika je
  v backend `layout.py`); čistá verze potřebuje malou backend změnu (per-tile section hint v `/state`),
  proto **přeřazeno do β**. (Rodičovský spec ho uváděl pod řezem 4; tady se reassignuje na β.)
- Zachování TOML komentářů, plný wizard, live preview neuložených editů, Windows/signing — Phase 3.

## Dekompozice

Řez 4b-ii-α je jeden writing-plans plán, subagent-driven (jako řezy 3 / 4a / 4b-i). Po něm následuje
samostatný brainstorm + spec + plán pro **řez 4b-ii-β** (OverrideField overlay + per-sekce inherit/
override/clear + map-level explicit-empty + klik-to-jump s backend tile-hintem).
