# Pravidla pro agenty

## Komunikace

- Komunikuj česky; kód a commit messages piš anglicky.
- Buď stručný.

## Commity

- Po dokončení a ověření ucelené implementační změny ji automaticky commitni; nečekej na další výzvu uživatele.
- Review-only, diagnostické a plánovací úkoly necommituj.
- Používej Conventional Commits (`feat`, `fix`, `refactor`, `docs`, `test`, `chore`).
- Nepřidávej `Co-Authored-By`.
- Nikdy nepoužívej squash merge.
- Po každém commitu spusť `roborev show <sha>`, oprav všechny relevantní nálezy a případné opravy znovu commitni a zkontroluj.

## GitHub

- Pro GitHub operace používej lokální `gh` CLI.
