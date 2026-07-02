# herdeck — pravidla pro agenty

## Jazyk UI
- Uživatelské texty v desktop aplikaci (desktop/src/**) i tray menu (desktop/src-tauri) jsou ČESKY.
- Labely polí v editoru nastavení zůstávají anglické = přesné klíče z TOML configu (např. `tile_fill`); vysvětluje je český tooltip.

## Vysvětlivky (help tooltips) — povinné
- Každé pole v editoru nastavení (desktop/src/lib/sections/*.svelte) MUSÍ mít český tooltip:
  prop `help` na field komponentě (desktop/src/lib/fields/*.svelte); texty sekce drží konstanta `HELP`.
- Text tooltipu piš podle SKUTEČNÉHO chování backendu (přečti konzumenta hodnoty v src/herdeck/), ne odhadem.
  Jedna věta, ~110 znaků, uživatelský jazyk, vyjmenuj hodnoty/jednotky/defaulty.
- Ikonová a nejasná tlačítka (×, ⚠, ⚙…) musí mít `title=`.
- Vynucuje test desktop/src/lib/sections/sections.help.test.ts — nové pole bez `help` test shodí.
  Nová field komponenta musí label renderovat se třídou `fieldlabel` + `title={help}` (viz TextField).
