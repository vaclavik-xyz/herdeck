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

## Nastavení Herdecku

- Při žádosti o instalaci, připojení nebo změnu lokálních/vzdálených Herdr sessions postupuj podle `docs/agent-setup.md`; je to source of truth.
- Preferuj ověřitelné CLI a konfigurační soubory před Settings UI. Uživatel nemá ručně přepisovat TOML ani přenášet tokeny.
- Nejdřív zjisti skutečnou topologii (`herdr status --json`, `herdr session list --json`, Tailscale a existující config), zachovej nesouvisející nastavení a vytvoř zálohy.
- Tokeny nikdy nevypisuj ani neukládej do TOML, argumentů, logů nebo commitu. Remote bridge binduj jen na loopback/Tailscale adresu.
- Bez výslovného souhlasu nevytvářej DNS, Tailscale Funnel/Serve, Cloudflare tunnel, veřejný proxy ani firewall pravidla.
- Úkol nekončí zápisem konfigurace: ověř všechny lokální sessions, remote bridges, efektivní profil a runtime `connections`; výsledek předej bez secretů.
