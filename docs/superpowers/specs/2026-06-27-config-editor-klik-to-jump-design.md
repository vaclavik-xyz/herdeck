# Config editor — klik-to-jump (preview tile → config section) — design

## Kontext

Poslední kus Phase 2 frontend config editoru (po řezech 3 / 4a / 4b-i / 4b-ii-α/β1/β2, vše MERGED).
Odložen z 4b-ii (β1/β2 byly frontend-only; tohle záměrně NEní — viz níže). Cíl: v config okně klik na
dlaždici v deck **preview** skočí editor na config sekci, která tu dlaždici řídí.

Rodičovský design: `docs/superpowers/specs/2026-06-25-config-editor-frontend-design.md`. Stav:
**všech 8 `_OVERLAY_SECTIONS` je base+overlay editovatelných** (β2, merge `d5ed3b1`). Tohle je poslední
UX vychytávka před Phase 3 (distribuce).

## Klíčový princip + proč to NENÍ frontend-only

Dlaždice jsou neprůhledné PNG; jejich **sémantika žije v backendu** (`orchestrator.render()` staví
`TileView`y podle aktuálního MÓDU). Frontend nemá jak z PNG odvodit, kterou config sekci dlaždice řídí.
Proto klik-to-jump vyžaduje **malou backend (Python) změnu**: orchestrator otaguje každý `TileView`
config-sekcí (`section` hint), deckapp `/state` ji vystaví, frontend ji na klik přečte. Toto je
dokumentovaná cena, proč byl řez odložen mimo β1/β2.

## Rozhodnutí (z brainstormu)

1. **Plain klik v config preview = skok na sekci** (NE press). Aktuace decku (POST `/press`) zůstává
   **plovoucímu deck-oknu** (`App.svelte`), které je dedikovaný remote control. Config preview se stává
   **config-mapou**, ne aktuátorem — čistě řeší konflikt „klik = press vs klik = jump". (Ztráta: nelze
   ovládat deck z config okna; od toho je plovoucí deck.)
2. **Dlaždice běžícího agenta → sekce „View"** (tile_primary/secondary/tile_fields řídí jejich vzhled —
   jejich nejrelevantnější jediná config sekce). Menu-módy mapují ostře.

## Tile → section mapování (kde orchestrator staví dlaždice)

`section` je **stabilní config-section KEY** (ne UI label), `None` = neskáče.

| Mód (`render()`) | Dlaždice | `section` |
|---|---|---|
| Overview | management „profiles" tlačítko (`new_agent`/`profiles` actions, bottom_row) | `profiles` (profiles action) / `start_profiles` (new_agent action) |
| Overview | „+ New" launcher dlaždice (management=launcher_menu) | `start_profiles` |
| Overview | dlaždice běžícího agenta | `view` |
| Overview | prázdná / dim | `None` |
| Profile menu | jméno profilu | `profiles` |
| Profile menu | „Back" | `None` |
| Launcher menu | typ start_profile (agent_type) | `start_profiles` |
| Launcher menu | „Profiles" položka | `profiles` |
| Launcher menu | „Back" | `None` |
| Drill (answer) | answer option | `answer_profiles` |
| Drill | „Stop" | `answer_profiles` |
| Drill | „Back" | `None` |
| (jakýkoli) | panel | `None` (v1; možné rozšíření → theme/view) |

Pozn.: management akce: `_management_indices` vrací `{index: action}`, `action ∈ {"profiles",
"new_agent"}` → `profiles`/`start_profiles`. Live preview je obvykle v overview (agent dlaždice → View);
ostatní sekce skočí, když uživatel deck navedl do menu (přes plovoucí deck / fyzický deck). Toto je
vlastnost designu (config preview je jump-mapa toho, co deck zrovna ukazuje), ne nedostatek.

## Architektura / jednotky

### Backend (Python, TDD pytest)

| Soubor | Status | Odpovědnost |
|---|---|---|
| `src/herdeck/driver/base.py` | modify | `TileView.section: str \| None = None` (nové volitelné pole, default None → žádná regrese existujících konstruktorů) |
| `src/herdeck/orchestrator.py` | modify | naplnit `section=` u každého `TileView` v `_render_overview`/`_render_profile_menu`/`_render_launcher`/`_render_drill` dle tabulky výše (čistě additivní kwarg) |
| `src/herdeck/deckapp/server.py` | modify | `_refresh_locked`: zachytit `self._tile_sections = {tile.index: tile.section for tile in rs.tiles if tile.index < slots and tile.section}`; `_state`: vystavit `"tile_sections": dict(self._tile_sections)` |

Žádná versioning komplikace — sekce se snapshotují per render (jako `_tiles`), `/state` je posílá celé
(malá mapa). Mock i live jdou přes stejný `Orchestrator.render()`, takže hinty fungují v obou.

### Frontend (TDD vitest + build gate)

| Soubor | Status | Odpovědnost |
|---|---|---|
| `desktop/src/lib/deckClient.ts` | modify | `DeckState.sections: Record<number,string>` + `parseState` parsuje `tile_sections` (string-key normalizace jako `parseTiles`, drop ne-string hodnot); `DeckViewModel.sections` + `initialView` (`{}`) + `stepDeck` fold (`sections: state.sections`) |
| `desktop/src/lib/DeckView.svelte` | modify | volitelný prop `onJump?: (section: string) => void`. Když je nastaven = **jump mód**: klik na dlaždici zavolá `onJump(view.sections[i])` (jen když section existuje), NIKDY nepostuje press; klávesnice vypnuta (config preview nikdy neaktuuje). Když není = dnešní press chování (plovoucí deck). Panel (index=slots) nemá section → bez akce v jump módu. |
| `desktop/src/ConfigApp.svelte` | modify | předat `onJump` do preview `<DeckView>`; mapovat section KEY → sidebar label (`SECTION_FOR_KEY = {view:"View", start_profiles:"Start profiles", answer_profiles:"Answer profiles", profiles:"Profiles"}`) a nastavit `active = SECTION_FOR_KEY[key] ?? active` |

`App.svelte` (plovoucí deck) DeckView NEdostává `onJump` → press chování beze změny (žádná regrese).

## Testing

- **Backend (pytest):** `TileView.section` default None; orchestrator render v každém módu produkuje
  správné `section` hinty per dlaždice (overview agent→view, management profiles→profiles /
  new_agent→start_profiles, „+ New"→start_profiles, prázdné→None; profile menu→profiles/Back None;
  launcher→start_profiles, „Profiles"→profiles; drill→answer_profiles/Stop answer_profiles/Back None);
  deckapp `_state` obsahuje `tile_sections` s nenull dlaždicemi (test proti DeckApp + mock source).
- **Frontend (vitest):** `parseState` parsuje `tile_sections` (string keys → number, drop junk);
  `stepDeck` propíše sekce do view modelu; `DeckView` jump mód = build gate + compile-smoke (žádný
  Svelte render harness — komponenta = build gate jako 4a/4b-i/α/β). Press chování beze změny když
  `onJump` chybí.
- Backend test runner: `.venv/bin/python -m pytest`; ruff `.venv/bin/ruff check src tests`. Frontend:
  `cd desktop && npx vitest run` / `npm run build`.

## Non-goals

- Panel klik-to-jump (theme/view) — v1 panel neskáče (možné rozšíření).
- Jump na konkrétní POLE v sekci (jen na sekci, ne na řádek/klíč) — sekce je dost.
- Static/synthetic config-map preview — preview zůstává LIVE deck (jump-mapa toho, co ukazuje).
- Modifier-klik dual press+jump — rozhodnuto plain-klik=jump, press zůstává plovoucímu decku.
- Zachování TOML komentářů, wizard, Windows/signing — Phase 3.

## Dekompozice

Jeden writing-plans plán, subagent-driven. Backend tasky (TileView field → orchestrator population →
deckapp /state, TDD pytest) pak frontend tasky (deckClient parse → DeckView jump mód → ConfigApp
mapping, vitest + build gate). Po něm je Phase 2 frontend KOMPLETNÍ (zbývá jen Phase 3 distribuce).
