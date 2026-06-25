# Config editor — Frontend (desktop app Phase 2, part 2 of 2) — design

## Kontext

Phase 2 = GUI config editor jako druhé okno desktop appky (Tauri + Svelte + `herdeck.deckapp`
sidecar). **Backend (část 1/2) je hotový a mergnutý do main** (merge `2fe46d4`): `ConfigService`
(read/validate/write + profily + keychain secrets), config HTTP routes na sidecaru, A/B reload,
`secrets` modul (env-first keychain). Tento dokument je **frontend (část 2/2)** — tenká GUI vrstva
nad tím API.

Rodičovský (kombinovaný) design: `docs/superpowers/specs/2026-06-24-config-editor-design.md`.
Odtud zděděno a stále platné: layout varianta A (sidebar + formulář + trvalý preview), sekce,
profil přepínač (base/overlay), preview = saved-state, error handling, onboarding minimální,
non-goals (žádné zachování TOML komentářů, žádný plný wizard, žádné live preview neuložených editů,
žádný Windows). Tento dokument ta rozhodnutí **konkretizuje** pro implementaci a přidává nová.

## Klíčový princip

**Žádná nová logika configu ve frontendu.** Editor je tenká vrstva nad hotovým backend API. Stejný
sidecar, stejný token-auth, stejný Tauri proxy vzor jako floating deck (`deckClient.ts` →
`configClient.ts`). Token nikdy nežije v JS — Rust commandy ho injektují.

## Backend API (implementované, proti čemu se staví)

Sidecar `DeckApp` (loopback HTTP, token-auth) — viz `src/herdeck/deckapp/server.py`:

| Metoda + cesta | Auth | Tělo | Odpověď |
|---|---|---|---|
| `GET /config?token=` | query token | — | `200 {base, profiles, local, secrets}`; `404` když sidecar nemá config_service |
| `POST /config/validate` | header `X-Herdeck-Token` | `{base, profiles, local}` | `200 {errors: [str]}`; `400` na nevalidní/non-dict tělo |
| `POST /config` | header token | `{base, profiles, local}` | `200 {errors: [str]}` — prázdné errors ⇒ atomický zápis + reload; `400` na nevalidní tělo |
| `POST /profiles/active` | header token | `{name}` | `200 {changed: bool}`; `400` na neznámý/chybějící/non-string name |
| `POST /secret` | header token | `{token_env, value}` | `204`; `400` na chybějící klíče |
| `DELETE /secret/{token_env}` | header token | — | `204` |

**Tvar `read()`:**
- `base` — dict přítomných bázových sekcí (`servers`, `deck`, `view`, `theme`, `macros`,
  `start_profiles`, `notifications`, `safety`, `answer_profiles`).
- `profiles` — dict pojmenovaných profilů (overlay sekce nad bází + `extends`/`servers`).
- `local` — dict `local.toml` (`active_profile`, `hardware` overrides).
- `secrets` — `{token_env_name: {set: bool, source: "env"|"keychain"|null}}` — **nikdy žádná
  hodnota secretu**, jen jméno env varu + příznak přítomnosti.

Editor model = přesně tahle struktura. Edituje se in-memory, zpět se POSTuje `{base, profiles, local}`.

## Architektura

```
Tauri shell (Rust, desktop/src-tauri/src/lib.rs)
 ├─ Okno "main"   (floating, 360×600, always-on-top)   → App.svelte      → DeckView
 └─ Okno "config" (dekorované, ~900×680, ne-on-top)     → ConfigApp.svelte → sekce + DeckView preview
        proxy cmds (token v Rustu): config_read / config_validate / config_write /
        config_set_active / config_secret_set / config_secret_clear
                                              ↓ loopback HTTP + token
                            herdeck.deckapp sidecar (ConfigService + routes)
```

Root komponenta se vybírá podle **window labelu** (`getCurrentWindow().label`): `"main"` → `App.svelte`
(deck), `"config"` → `ConfigApp.svelte` (editor). `main.ts` větví mount podle labelu.

### Nové / dotčené jednotky

| Vrstva | Soubor | Odpovědnost |
|---|---|---|
| Rust | `desktop/src-tauri/tauri.conf.json` | přidat 2. okno `"config"` (dekorované, ne-on-top, skryté při startu, otevírané on-demand) |
| Rust | `desktop/src-tauri/src/lib.rs` | 6 proxy cmds (token injektuje Rust); tray „Settings…" + příkaz `open_config` (show+focus okna) |
| Rust | `desktop/src-tauri/src/http.rs` | nové helpery `http_post_json` (X-Herdeck-Token, vrací status+body), `http_delete` |
| Frontend | `desktop/src/lib/configClient.ts` (nový) | čistá logika bez Tauri: parse `read()` → editor model, serialize model → `{base,profiles,local}`, override/clear logika, secret-presence mapping, validate/write call shaping, error parsing. Transport injektovaný (mirror `deckClient.ts`). |
| Frontend | `desktop/src/ConfigApp.svelte` (nový) | editor shell: profil přepínač, sidebar nav, aktivní sekce, save bar (Apply/Discard/dirty/chyby), preview pane (reuse `DeckView`) |
| Frontend | `desktop/src/lib/fields/*.svelte` (nové) | sdílené field widgety: `TextField`, `NumberField`, `SelectField`, `ListField`, `ServerRefField`, `TokenSecretField`, `KeyValueField`; `OverrideField` wrapper pro overlay mód |
| Frontend | `desktop/src/lib/sections/*.svelte` (nové) | per-sekce formuláře (10×) skládající field widgety nad modelem |

Hranice: `configClient.ts` testovatelný čistě (bez Tauri/DOM, jako `deckClient.test.ts`); Rust cmds
testovatelné jako `tests/http.rs`/`spawn.rs`; Svelte komponenty tenké nad klientem.

## Save model

**Explicitní globální Apply.** Editor drží celý editovaný model v paměti. Změny značí **dirty**
indikátor. **Apply** POSTne `/config` (celý `{base,profiles,local}`) — když `errors==[]`, backend
atomicky zapíše + spustí reload → preview se obnoví; když `errors` neprázdné, zobrazí se inline u
sekcí + souhrnný banner a nic se nezapíše. **Discard** zahodí neuložené změny (re-fetch `read()`).
Žádný auto-save (thrashoval by reload / flapnul živý bridge). Před Apply lze volat
`POST /config/validate` pro živou validaci bez zápisu (on-blur / před povolením Apply).

## Form architektura

Sdílená sada field widgetů + per-sekce kompozice (žádný generický schema-renderer — sekce jsou
heterogenní). Widgety:

- `TextField` / `NumberField` — skalární pole.
- `SelectField` — enum (např. `view.management`, `theme.*`).
- `ListField` — seznam stringů (např. `view.tile_primary`/`tile_secondary` token listy z tile-lines
  featury, `overview_order`, `notifications.on`).
- `ServerRefField` — výběr `server.id` (validuje proti existujícím serverům).
- `TokenSecretField` — pole pro `token_env` jméno + odznak přítomnosti (`🔑✓` set / `🔑✗ nastav`),
  inline prompt na zadání hodnoty → `POST /secret` (keychain); hodnota se nikdy nezobrazuje ani neukládá do modelu.
- `KeyValueField` — mapy (`macros`, `answer_profiles`, `start_profiles`).
- `OverrideField` — wrapper kolem libovolného widgetu pro overlay mód: dimmed zděděná hodnota jako
  placeholder, badge „override" + `[clear]` na přepsaných polích; clear odebere klíč z overlay sekce.

## Sekce (sidebar)

Všech 10, editovatelných (uživatel zvolil kompletní editor): **Servers** (id/url/token_env, add/remove)
· **Deck** (grid, hardware) · **View** (management, tile_primary/secondary token listy, overview_order)
· **Theme** · **Macros** (KV) · **Start profiles** (KV/list) · **Notifications** (enabled/on/backends +
telegram token_env/chat_id) · **Safety** · **Answer profiles** (KV map profilů: approve/deny/stop/...) ·
**Profiles** (seznam pojmenovaných profilů, `extends`, výběr `servers`, create/delete přes
`create_profile`/`delete_profile`).

## Profil přepínač (base / overlay)

Nahoře přepínač profilu (`default (báze)` + pojmenované profily z modelu) → volá `POST /profiles/active`
pro perzistentní aktivní profil v `local.toml`.

- **Base mód** (`default`): formuláře editují `base` přímo (plné widgety).
- **Overlay mód** (pojmenovaný profil): pole obalená `OverrideField` — ukazují zděděnou bázovou
  hodnotu jako dimmed placeholder; per-field override/clear. `extends` a výběr `servers` profilu jsou
  v sekci Profiles.
- **env-locked profil** (`HERDECK_PROFILE` nastaven): přepínač zamčený, `set_active` disabled s
  vysvětlením (backend `set_active` vrací `changed=false`).

## Preview

Reuse `DeckView` + `commandTransport` (deck proxy cmds už existují). **Saved-state**: ukazuje
resolvnutý config pro aktivní profil (mock|živý) tak, jak ho sidecar renderuje. Tok: edit → Apply →
backend reload → sidecar `/state` verze povýší → preview se sám obnoví (DeckView už pollne). Žádná
druhá render cesta ve frontendu.

**Klik-to-jump (lehká v1):** klik na dlaždici v preview → skok do relevantní sekce (mapa
tile-index → sekce; default Servers/View). Pouze navigační pomůcka, žádná direct-manipulace.

## Onboarding

Bez configu `read()` vrátí prázdné sekce. Editor ukáže prázdné formuláře s defaulty + inline „přidej
první server"; první Apply zapíše čerstvý `config.toml` (+ `local.toml` když je potřeba). Plný
first-run wizard je Phase 3 (non-goal).

## Error handling

- **Validace:** `validate`/`write` `errors` → inline u dotčené sekce + souhrnný banner. Apply
  blokován jen na strukturálních chybách (backend `write` už pouští missing-secret přes).
- **Missing secret:** `secrets[name].set==false` → `TokenSecretField` ukáže `🔑✗ nastav` + inline
  prompt → `POST /secret`. Není to Apply-blocker, jen varování.
- **Sidecar nedostupný / write selže:** ne-destruktivní toast; in-memory edity zůstanou (žádná ztráta dat).
- **No-config:** viz Onboarding.

## Otevření okna

- Tray menu položka **„Settings…"** → `open_config` (show + focus okna `"config"`).
- **Ozubené kolečko** na floating decku (`App.svelte`) → stejný `open_config`.
- Okno startuje skryté; zavření okna ho jen skryje (sidecar + floating deck běží dál).

## Testing (TDD)

- **`configClient.ts`** — čisté unit testy (Vitest, mirror `deckClient.test.ts`, bez Tauri):
  `read()` payload → editor model a zpět (round-trip, `{base,profiles,local}` tvar); override/clear
  logika (set override → klíč v overlay; clear → klíč pryč, vrací zděděné); secret-presence mapping;
  validate/write call shaping; error parsing; profil přepnutí shaping.
- **Rust** — proxy cmd testy (token injection, status mapping) mirroring `tests/http.rs`/`spawn.rs`;
  `http_post_json`/`http_delete` helper testy.
- **Svelte komponenty** — lehké testy klíčových widgetů (`OverrideField` override/clear, `TokenSecretField`
  presence stav) kde to dává smysl; UI shell ne nutně E2E ve v1.
- Reálné chování, ne mocky: configClient proti reálným payloadům z `ConfigService.read()` tvaru.

E2E přes reálný sidecar + Tauri WebView je mimo v1 (jako u floating decku — TS/Rust suite se ověřuje
za vývoje, ne nutně v CI).

## Decompozice (2 plány)

Frontend se rozpadá na dva samostatně funkční + testovatelné řezy (= slice 3 + 4 z rodičovského specu):

1. **Řez 3 — Transport + shell:** `http.rs` helpery (POST-JSON/DELETE) + 6 Rust proxy cmds + jejich
   testy; 2. okno `"config"` + `open_config` cmd + tray/gear vstup; `main.ts` root-by-label;
   `configClient.ts` (kompletní čistá logika + unit testy); `ConfigApp.svelte` skeleton (přepínač +
   sidebar + save bar + preview reuse) + **jedna reprezentativní sekce** (Servers, protože pokrývá
   list-of-records + ServerRef + TokenSecret). Deliverable: funkční okno, které načte/zvaliduje/zapíše
   config a edituje Servers.
2. **Řez 4 — UI completion:** zbylých 9 sekcí + všechny field widgety + `OverrideField` overlay UX +
   secrets UX (set/clear) + onboarding + klik-to-jump preview + error bannery/toasty. Deliverable:
   kompletní editor.

Každý řez = vlastní writing-plans plán, subagent-driven v izolovaném worktree.

## Non-goals (Phase 2 frontend)

- Zachování TOML komentářů (tomlkit) — pozdější.
- Plný onboarding wizard — Phase 3.
- Live preview neuložených editů (`/config/preview`) — pozdější.
- Auto-save / per-section save — explicitní globální Apply.
- Generický schema-driven form renderer — per-sekce formuláře.
- Direct-manipulace na decku (drag/drop tile↔server) — klik-to-jump je jen navigace.
- E2E přes reálný Tauri WebView v CI; Windows build; signing/distribuce (Phase 3).

## Závislosti

Žádné nové runtime závislosti nad rámec Phase 1 + backend (Tauri, Svelte 5, Vitest už jsou). Nové
soubory, žádné nové balíčky.
