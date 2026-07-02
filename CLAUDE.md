# herdeck — pravidla pro agenty

## Jazyk UI (dvojjazyčně: en default + cs)
- Aplikace je plně dvojjazyčná: **angličtina je default**, čeština se zapíná configem
  `[view].language = "cs"` (editor: sekce Zobrazení → `language`).
- KAŽDÝ nový uživatelsky viditelný text musí vzniknout V OBOU jazycích, nikdy natvrdo:
  - desktop UI: `defineMessages({en:{...},cs:{...}})` z `desktop/src/lib/i18n.svelte.ts`
    (lokálně v komponentě) nebo sdílený katalog tamtéž; typy vynucují stejné klíče en/cs.
  - vysvětlivky polí: centrální `desktop/src/lib/help.ts` (FIELD_HELP.en + FIELD_HELP.cs).
  - renderovaný text decku (dlaždice/panel/websim): `src/herdeck/i18n.py` STRINGS
    (en+cs, `tr(lang, key)`); jazyk teče z `config.view.language`.
  - tray menu (Rust): `tray_labels()` v `desktop/src-tauri/src/lib.rs`.
- Labely polí v editoru zůstávají anglické = přesné klíče z TOML configu (např. `tile_fill`);
  vysvětluje je tooltip.
- CLI, logy, README a komentáře v kódu zůstávají anglicky.

## Vysvětlivky (help tooltips) — povinné
- Každé pole v editoru nastavení (desktop/src/lib/sections/*.svelte) MUSÍ mít tooltip
  v obou jazycích: prop `help` na field komponentě, texty v `help.ts`, sekce je čte přes
  `fieldHelp("<sekce>")`.
- Text tooltipu piš podle SKUTEČNÉHO chování backendu (přečti konzumenta hodnoty
  v src/herdeck/), ne odhadem. Jedna věta, ~110 znaků, vyjmenuj hodnoty/jednotky/defaulty.
- Ikonová a nejasná tlačítka (×, ⚠, ⚙…) musí mít `title=` (v obou jazycích přes katalog).
- Vynucují testy: `desktop/src/lib/sections/sections.help.test.ts` (mount všech sekcí
  v en i cs + parita klíčů FIELD_HELP) a `tests/test_i18n.py` (parita klíčů STRINGS).
  Nová field komponenta musí label renderovat se třídou `fieldlabel` + `title={help}`.
