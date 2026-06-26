# Config editor — Frontend řez 4b-i (Profiles + switcher + deferred) — design

## Kontext

Phase 2 frontend (GUI config editor, 2. okno desktop appky) se staví po řezech nad hotovým backend
API (`ConfigService` + sidecar routes, merge `2fe46d4`). Rodičovský frontend design:
`docs/superpowers/specs/2026-06-25-config-editor-frontend-design.md`. Stav:

- **Řez 3** (transport + shell + Servers sekce) — MERGED do main.
- **Řez 4a** (base-mode editace všech 8 sekcí + widgety + onboarding + backend `env_locked`/
  `active_profile`) — MERGED do main (merge `6893a03`).
- **Řez 4** byl rozdělen na **4a** (hotovo) + **4b**. **4b** je dále rozděleno na **4b-i** (tento
  dokument) + **4b-ii** (samostatný pozdější design).

Tento dokument konkretizuje **řez 4b-i** a navazuje rozhodnutí, která rodičovský spec nechával otevřená
(dvojrole přepínače profilu; mechanika CRUD profilů; chování při dirty stavu).

## Klíčový princip (zděděný)

Žádná nová logika configu ve frontendu — tenká vrstva nad backend API. Token nikdy v JS (Rust commandy
ho injektují). Save model = explicitní globální **Apply** (`POST /config` zapíše celé `{base,profiles,
local}`); secret a `set_active` jsou okamžité side-effecty mimo Apply.

## Rozsah 4b-i

**V rozsahu:**
- **A — Profiles sekce + switcher wiring:** funkční přepínač aktivního profilu; Profiles sekce s
  list/create/delete + editací profilových polí `extends` a `servers`.
- **E — error/toast polish:** ConfigApp `notice` string → strukturovaný banner/toast.
- **F — deferred fixy z řezu 3:** přejmenování `token_env` osiří starý keychain záznam; `DELETE
  /secret/{env}` musí percent-encodovat non-identifier jména.

**Mimo rozsah (→ řez 4b-ii):** `OverrideField` třístav (default/set/explicit-empty), per-sekce overlay
editace profilů, autorování intentional explicit-empty (tile line off, `overview_order=[]`,
`approve_always:[]`), klik-to-jump preview. (B + C + D z rodičovského řezu 4.)

## Přepínač profilu = selektor AKTIVNÍHO profilu

Přepínač (dnes disabled skeleton z řezu 3) se zprovozní jako selektor **aktivního** profilu, NE jako
editační přepínač base/overlay (ten je 4b-ii).

- Dropdown nabízí `default (báze)` + pojmenované profily z **naposledy načteného/Applnutého** payloadu.
- Výběr volá `config_set_active` (`POST /profiles/active`) — **okamžitý** persistentní zápis
  `active_profile` do `local.toml` + backend reload; preview/deck se obnoví.
- **env-locked** (`HERDECK_PROFILE` nastaven, `payload.envLocked === true` z 4a): přepínač disabled +
  tooltip „profil zamčen přes HERDECK_PROFILE". `active_profile` z 4a se zobrazí jako vybraný.
- **Dirty-guard:** přepnutí je **blokované, dokud nejsou změny Applnuté nebo Discardnuté** — `set_active`
  spouští backend reload, který by jinak rozbil neuložené edity. Při dirty stavu switcher disabled +
  hint „máš neuložené změny — Apply nebo Discard před přepnutím".
- **Pořadí create→activate:** nově vytvořený profil žije nejdřív jen v modelu; aktivovatelný je až po
  Apply (do té doby není v perzistovaném seznamu, takže ho switcher nenabízí). `set_active` na neznámý
  profil backend odmítne (`changed=false`) — tomu se vyhneme tím, že switcher listuje jen perzistované.

## Profiles sekce

Sekce „Profiles" (dnes padá do 4b placeholderu v ladderu) edituje strukturu pojmenovaných profilů přes
`payload.profiles[name]`, persistuje přes **Apply** (žádné nové Rust commandy — `config_create_profile`/
`delete_profile` nemají HTTP route; jsou to čisté transformace a stejného efektu dosáhneme úpravou
modelu + zápisem přes existující `POST /config`).

- **List + create + delete:** create vyžaduje jméno; validace **reserved „default"** (rezervováno pro
  bázi) + **duplicita** se hlásí client-side přes `onError` (a backend write validace je druhá pojistka).
  delete odebere klíč z `payload.profiles`. Mirror `ServersSection` index-keying.
- **Per-profil `extends`:** `SelectField` (`default` + ostatní profily); cykly/neznámý cíl chytá backend
  write validace.
- **Per-profil `servers`:** výběr `server.id` (multi-select / list validovaný proti base servers).
- Edituje výhradně `payload.profiles[name].{extends, servers}` (+ holé vytvoření/smazání klíče);
  per-sekce overlay sekcí (view/deck/theme/…) je **mimo** 4b-i.

**Limitace 4b-i (dokumentovaná, jako absent≠empty u 4a):** když je aktivní pojmenovaný profil, formuláře
ostatních sekcí stále editují **base** (ne overlay daného profilu). Preview ukazuje resolvnutý profil, takže
base edity se v něm projeví přes dědičnost. Editace per-sekce **overrides** profilu je řez 4b-ii.

## E — error/toast polish

ConfigApp dnes drží jediný `notice` string. 4b-i ho povýší na strukturovaný stav s variantami
(success / error / warning) renderovaný jako banner/toast: validační `errors` z `validate`/`write` inline
u dotčených sekcí + souhrnný banner; ne-destruktivní toast pro selhání sidecaru/secretu (in-memory edity
zůstávají). Izolováno do ConfigApp (+ malá sdílená `Banner`/`Toast` komponenta).

## F — deferred fixy (řez 3)

- **token_env rename keychain orphan:** přejmenování (nebo smazání) `token_env` nechá starý keychain
  záznam osiřelý. Fix je **frontend-only, bez backend změny**: po úspěšném Apply spočítat osiřelé klíče =
  jména v `payload.secrets` s `source==="keychain"`, která už nereferencuje žádný `token_env` v configu
  (servery + base/telegram + profily), a nabídnout jejich smazání přes existující `config_secret_clear`.
  Hodnota se **nemigruje** (secrets jsou one-way / nečitelné) — uživatel token u nového názvu nastaví ručně.
  Detekce i akce žijí v ConfigApp/configClient (čistá `orphanedSecrets(payload)` helper + potvrzovací prompt).
- **DELETE /secret/{env} percent-encode:** `token_env` s mezerou/lomítkem rozbije request-line nebo
  `path.rsplit("/",1)` na sidecaru. **Oboustranný fix:** (1) Rust při stavbě cesty percent-encoduje
  segment `token_env`; (2) sidecar `do_DELETE` (`server.py`) musí segment `unquote`-nout, protože dnes
  bere `path.rsplit("/",1)[1]` bez dekódování — bez toho by enkódování hledalo klíč `%XX` a fix by sám
  o sobě škodil. Round-trip test na obou stranách.

## Architektura / jednotky

| Soubor | Status | Odpovědnost |
|---|---|---|
| `desktop/src/lib/configClient.ts` | modify | čisté helpery: `profileNames`, `createProfile` (reserved/duplicate validace), `deleteProfile`, `setProfileExtends`, `setProfileServers`; reuse `activeProfile`/`envLocked` z 4a |
| `desktop/src/lib/sections/ProfilesSection.svelte` | create | Profiles UI (list/create/delete + extends + servers) |
| `desktop/src/ConfigApp.svelte` | modify | switcher z disabled skeletonu → funkční (set_active, env-locked disabled, dirty-guard); wire Profiles větev; strukturovaný notice/banner |
| `desktop/src/lib/Banner.svelte` (nebo Toast) | create | sdílený banner/toast pro E |
| `desktop/src-tauri/src/http.rs` + `lib.rs` | modify | percent-encode `token_env` segmentu v `config_secret_clear` cestě (F-2) |
| `src/herdeck/deckapp/server.py` | modify | `do_DELETE` `unquote`-ne secret path segment (druhá strana F-2) |
| `configClient.ts` + `ConfigApp.svelte` | modify | `orphanedSecrets(payload)` helper + Apply-time prompt na úklid osiřelých keychain klíčů (F-1) — frontend-only |

Žádné nové runtime závislosti, žádné nové HTTP routes, žádné nové Tauri commandy (CRUD přes model+Apply;
`config_set_active`/`config_secret_clear` už existují z řezu 3). Backend (Python) změna je jediná: one-line
`unquote` v `do_DELETE` (druhá strana F-2 percent-encode). F-1 (orphan úklid) je frontend-only.

## Testing (TDD)

- **`configClient.ts` helpery** — čisté Vitest testy: create (reserved „default" → chyba, duplicate →
  chyba, jinak nový prázdný profil), delete (odebere klíč; neznámý → chyba/no-op), setProfileExtends/
  setProfileServers (immutabilní, vrací nový payload), profileNames.
- **ProfilesSection + switcher** — build gate (`npm run build`); switcher dirty-guard + env-locked
  disabled stav ověřitelný kompilací + případně lehkým smoke testem.
- **F fixy** — vlastní unit testy: `http_delete` percent-encode (Rust `tests/http.rs`); `orphanedSecrets`
  helper (čistý Vitest v `configClient.test.ts`).

## Non-goals (4b-i)

- `OverrideField` třístav, per-sekce overlay editace profilů, explicit-empty authoring — řez 4b-ii.
- klik-to-jump preview — řez 4b-ii.
- Editace per-sekce overrides profilu (jen `extends`/`servers` jsou v 4b-i) — řez 4b-ii.
- Zachování TOML komentářů, plný wizard, live preview neuložených editů, Windows/signing — Phase 3 / pozdější.

## Dekompozice

Řez 4b-i je jeden writing-plans plán, subagent-driven (jako řez 3 / 4a). Po něm následuje samostatný
brainstorm + plán pro **řez 4b-ii** (OverrideField overlay + třístav + klik-to-jump).
