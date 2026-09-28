import { local, type Lang } from './copy';

// Curated user documentation. Operational source of truth stays in the repository.
// Commands are shared between locales so translated guides cannot silently drift.
export type Section = {
  id: string;
  title: string[];
  paragraphs?: string[][];
  bullets?: string[][];
  code?: string;
  note?: string[];
};
export type Doc = {
  slug: string;
  title: string[];
  description: string[];
  source: string;
  sections: Section[];
};
export const docs: Doc[] = [
  {
    slug: 'getting-started',
    title: ['Getting started', 'Začínáme'],
    description: [
      'Install the app and connect your first agents. No deck required.',
      'Nainstaluj aplikaci a připoj první agenty. Deck nepotřebuješ.',
    ],
    source: 'README.md#install',
    sections: [
      {
        id: 'requirements',
        title: ['What you need', 'Co potřebuješ'],
        paragraphs: [
          [
            'Herdeck is a control surface for agents running under Herdr. For live agents, install Herdr 0.7.4 or newer and start an agent session. The desktop app includes its own Herdeck runtime; you do not need Python, Node.js, or Rust.',
            'Herdeck ovládá agenty běžící pod Herdr. Pro skutečné agenty nainstaluj Herdr 0.7.4 nebo novější a spusť session s agentem. Desktopová aplikace obsahuje vlastní Herdeck runtime; Python, Node.js ani Rust nepotřebuješ.',
          ],
        ],
        bullets: [
          [
            'macOS: Apple Silicon. The official DMG is signed and notarized.',
            'macOS: Apple Silicon. Oficiální DMG je podepsané a notarizované.',
          ],
          [
            'Linux: x86_64 or arm64, using AppImage, .deb, or .rpm.',
            'Linux: x86_64 nebo arm64, balíčky AppImage, .deb nebo .rpm.',
          ],
          [
            'A Ulanzi D200 is optional. D200 hardware control is verified on macOS.',
            'Ulanzi D200 je volitelný. Ovládání D200 je ověřené na macOS.',
          ],
        ],
      },
      {
        id: 'install',
        title: ['Install on macOS', 'Instalace na macOS'],
        bullets: [
          [
            'Open GitHub Releases from the download page and select the aarch64.dmg asset.',
            'Přes stránku Stažení otevři GitHub Releases a vyber soubor aarch64.dmg.',
          ],
          [
            'Open the DMG, drag Herdeck to Applications, and eject the disk image.',
            'Otevři DMG, přetáhni Herdeck do Aplikací a vysuň obraz disku.',
          ],
          [
            'If using a D200, quit Ulanzi Studio before connecting the deck. Only one app can own the device.',
            'Pokud používáš D200, před připojením ukonči Ulanzi Studio. Zařízení může ovládat jen jedna aplikace.',
          ],
          [
            'Open Herdeck and choose a local Herdr session in onboarding. Sessions under the same macOS user need no token or manual configuration.',
            'Otevři Herdeck a v úvodním nastavení vyber lokální Herdr session. Pod stejným uživatelem macOS nepotřebuješ token ani ruční konfiguraci.',
          ],
        ],
      },
      {
        id: 'linux',
        title: ['Install on Linux', 'Instalace na Linuxu'],
        paragraphs: [
          [
            'Download the package matching your architecture. Open the .deb or .rpm with your distribution package manager. For AppImage, make the downloaded file executable and run it. Substitute its actual filename below.',
            'Stáhni balíček pro svou architekturu. Soubor .deb nebo .rpm otevři ve správci balíčků své distribuce. AppImage nastav jako spustitelný a spusť. Níže dosaď skutečný název staženého souboru.',
          ],
        ],
        code: 'chmod +x ./herdeck_<version>_amd64.AppImage\n./herdeck_<version>_amd64.AppImage',
        note: [
          'AppImage supports signed in-app updates. Update .deb and .rpm by installing the newer package.',
          'AppImage podporuje podepsané aktualizace v aplikaci. Verze .deb a .rpm aktualizuj instalací novějšího balíčku.',
        ],
      },
      {
        id: 'demo',
        title: ['Try a hardware-free demo from source', 'Vyzkoušej demo ze zdrojového kódu'],
        paragraphs: [
          [
            'For a standalone demo, install Python 3.12 or 3.13 and run the following. No Herdr installation or hardware is required. Open the complete loopback URL printed in your terminal.',
            'Pro samostatné demo nainstaluj Python 3.12 nebo 3.13 a spusť následující příkazy. Herdr ani hardware nepotřebuješ. Otevři úplnou loopback URL, kterou vypíše terminál.',
          ],
        ],
        code: 'git clone https://github.com/vaclavik-xyz/herdeck.git\ncd herdeck\npython3 -m venv .venv\n.venv/bin/pip install -e ".[dev]"\nHERDECK_MOCK=1 HERDECK_SHOW_URL_TOKEN=1 \\\n  .venv/bin/herdeck-web run --allow-query-token',
        note: [
          'The printed query token is a credential. Keep it private. This quick start is for local loopback access only.',
          'Token ve vypsané URL je přihlašovací údaj. Uchovej ho v soukromí. Tento rychlý start slouží pouze k lokálnímu přístupu přes loopback.',
        ],
      },
    ],
  },
  {
    slug: 'hardware',
    title: ['Hardware & surfaces', 'Hardware a rozhraní'],
    description: [
      'Choose a D200, Elgato plugin, desktop app, or browser.',
      'Vyber D200, Elgato plugin, desktopovou aplikaci nebo prohlížeč.',
    ],
    source: 'README.md#the-deck-ulanzi-d200',
    sections: [
      {
        id: 'd200',
        title: ['Ulanzi D200', 'Ulanzi D200'],
        paragraphs: [
          [
            'The D200 has 13 LCD buttons and a status window. Herdeck fills the agent slots and reserves a launcher tile. When the fleet is larger than the available slots, the status window pages through it.',
            'D200 má 13 LCD tlačítek a stavové okno. Herdeck zaplní pozice agenty a jednu dlaždici vyhradí spouštěči. Pokud máš více agentů, stavové okno slouží k listování.',
          ],
          [
            'Connect by USB, close Ulanzi Studio, and open Herdeck. Reconnect after sleep or a USB unplug is handled automatically.',
            'Připoj zařízení přes USB, zavři Ulanzi Studio a otevři Herdeck. Opětovné připojení po uspání nebo odpojení USB probíhá automaticky.',
          ],
        ],
        note: [
          'D200 support is currently verified on macOS. Website product images combine Ulanzi D200 source photographs with Herdeck screen artwork based on the actual application renderer. They use sample data and are edited visualizations, not photographs of a live session.',
          'Podpora D200 je aktuálně ověřená na macOS. Produktové snímky kombinují podklady Ulanzi D200 s obrazovkami podle skutečného rendereru Herdecku. Používají ukázková data a jsou upravenou vizualizací, nikoliv fotografiemi živé session.',
        ],
      },
      {
        id: 'desktop',
        title: ['Desktop and browser', 'Desktop a prohlížeč'],
        paragraphs: [
          [
            'The native app includes a full control window, a compact floating deck, settings, and live terminal previews. Closing the window leaves Herdeck running in the menu bar. On macOS, Start at login brings it back after reboot.',
            'Nativní aplikace obsahuje hlavní ovládací okno, malé plovoucí okno decku, nastavení a živé náhledy terminálu. Po zavření okna Herdeck běží dál v liště. Na macOS ho volba Spustit při přihlášení spustí po restartu.',
          ],
          [
            'The browser dashboard is another control surface for the same runtime. A physical deck is not required to try the workflow.',
            'Dashboard v prohlížeči je další ovládací rozhraní stejného runtime. Pro vyzkoušení workflow fyzický deck nepotřebuješ.',
          ],
        ],
      },
      {
        id: 'elgato',
        title: ['Elgato Stream Deck', 'Elgato Stream Deck'],
        paragraphs: [
          [
            'The Elgato plugin includes a frozen backend, so the destination Mac does not need Python. The current package is a local arm64 macOS build and is not signed or notarized. Build instructions are in the repository README.',
            'Elgato plugin obsahuje přibalený backend, takže cílový Mac nepotřebuje Python. Aktuální balíček je lokální build pro arm64 macOS a není podepsaný ani notarizovaný. Postup sestavení je v README repozitáře.',
          ],
        ],
      },
    ],
  },
  {
    slug: 'connections',
    title: ['Sessions & connections', 'Sessions a připojení'],
    description: [
      'Combine local Herdr sessions and remote bridges.',
      'Spoj lokální Herdr sessions a vzdálené bridge.',
    ],
    source: 'docs/agent-setup.md',
    sections: [
      {
        id: 'local',
        title: ['Local sessions', 'Lokální sessions'],
        paragraphs: [
          [
            'Open Settings → Servers and select the discovered local sessions. Herdeck finds the default Herdr socket and named sessions. It starts an embedded, loopback-only bridge for each selected local socket.',
            'Otevři Nastavení → Servery a vyber nalezené lokální sessions. Herdeck najde výchozí Herdr socket i pojmenované sessions. Pro každý vybraný socket spustí vlastní bridge dostupný jen přes loopback.',
          ],
          [
            'No persistent bridge service or bearer token is needed for same-user local sessions. Changes reconnect in place without restarting Herdeck.',
            'Lokální sessions pod stejným uživatelem nepotřebují trvalou bridge službu ani bearer token. Změny připojení se projeví bez restartu Herdecku.',
          ],
        ],
        code: 'herdr status --json\nherdr session list --json',
      },
      {
        id: 'remote',
        title: ['Remote machines', 'Vzdálené stroje'],
        paragraphs: [
          [
            'Each remote Herdr session needs a Herdeck bridge on its host, a unique reachable port, and a bearer token. The deck connects directly to the bridge over your Tailscale network. SSH is for administration, not the runtime data path.',
            'Každá vzdálená Herdr session potřebuje na svém hostiteli Herdeck bridge, jedinečný dostupný port a bearer token. Deck se k bridge připojuje přímo přes tvou síť Tailscale. SSH slouží ke správě, nikoliv k provoznímu přenosu dat.',
          ],
          [
            'Use the linked agent setup runbook for a complete installation. It covers discovery, backups, token transfer, service installation, verification, and rollback.',
            'Pro úplnou instalaci použij odkazovaný agent setup runbook. Obsahuje zjištění topologie, zálohy, přenos tokenů, instalaci služeb, ověření i návrat zpět.',
          ],
        ],
        bullets: [
          [
            'Bind remote bridges only to loopback or a Tailscale address. Never expose them on a public interface.',
            'Vzdálené bridge binduj pouze na loopback nebo adresu Tailscale. Nikdy je nevystavuj na veřejném rozhraní.',
          ],
          [
            'Keep tokens in the OS keychain or owner-readable token files, never as values in TOML or command-line arguments.',
            'Tokeny uchovávej v klíčence OS nebo v souborech čitelných jen vlastníkem, nikdy jako hodnoty v TOML či argumentech příkazů.',
          ],
          [
            'Keep server IDs unique. Actions return to the bridge that supplied the agent.',
            'ID serverů musí být jedinečná. Akce se vracejí k bridge, který agenta poskytl.',
          ],
        ],
      },
      {
        id: 't3',
        title: ['Optional T3 Code adapter', 'Volitelný adaptér T3 Code'],
        paragraphs: [
          [
            'T3 Code is a separate, opt-in backend using an HTTP adapter. It is not a Herdr agent type. Consult the repository integration record for the tested version, setup procedure, and limitations before enabling it.',
            'T3 Code je samostatný volitelný backend přes HTTP adaptér. Není to typ agenta v Herdr. Před zapnutím ověř v integračním záznamu v repozitáři testovanou verzi, postup a omezení.',
          ],
        ],
      },
    ],
  },
  {
    slug: 'controls',
    title: ['Controls & agent states', 'Ovládání a stavy agentů'],
    description: [
      'Understand the colors and work through waiting requests.',
      'Pochop barvy a vyřiď čekající požadavky.',
    ],
    source: 'README.md#the-deck-ulanzi-d200',
    sections: [
      {
        id: 'states',
        title: ['Read the colors', 'Čti podle barev'],
        bullets: [
          [
            'Green: working. Blue: idle. Cyan: done.',
            'Zelená: pracuje. Modrá: nečinný. Azurová: hotovo.',
          ],
          [
            'Amber: blocked and needs your input. Red: error or disconnected.',
            'Oranžová: zablokovaný a potřebuje tvoji odpověď. Červená: chyba nebo odpojeno.',
          ],
          [
            'Violet: the agent has finished, but background work such as CI or review is still pending. This requires a waiting-state producer such as Herdwatch.',
            'Fialová: agent skončil, ale čeká se na práci na pozadí, například CI nebo review. To vyžaduje zdroj čekajícího stavu, například Herdwatch.',
          ],
        ],
      },
      {
        id: 'triage',
        title: ['Answer the waiting queue', 'Vyřiď frontu požadavků'],
        paragraphs: [
          [
            'Press the status window when it shows Needs you. Herdeck opens the agent that has waited longest. Read its prompt, choose the available answer, or stop it. After answering, the next blocked agent opens automatically.',
            'Když stavové okno ukazuje Čeká na tebe, stiskni ho. Herdeck otevře nejdéle čekajícího agenta. Přečti požadavek, vyber dostupnou odpověď nebo ho zastav. Po odpovědi se automaticky otevře další čekající agent.',
          ],
          [
            'Back exits the queue. When no agent is blocked, the deck returns to the overview. The optional desktop next_blocked hotkey opens the same flow.',
            'Zpět opustí frontu. Pokud už žádný agent nečeká, deck se vrátí do přehledu. Volitelná desktopová zkratka next_blocked spouští stejný postup.',
          ],
        ],
        note: [
          'Always read the actual request. Available answers depend on the agent and prompt; configurable confirmations still apply.',
          'Vždy si přečti skutečný požadavek. Dostupné odpovědi závisí na agentovi a promptu; nastavená potvrzení zůstávají v platnosti.',
        ],
      },
      {
        id: 'terminal',
        title: ['Look closer', 'Podívej se blíž'],
        paragraphs: [
          [
            'Open Live terminal in an agent card for a read-only preview of the pane. Use Focus to bring the original terminal forward when the situation needs direct interaction.',
            'V kartě agenta otevři Živý terminál pro náhled okna pouze ke čtení. Pokud potřebuješ přímou interakci, přepni se pomocí Focus do původního terminálu.',
          ],
        ],
      },
    ],
  },
  {
    slug: 'profiles',
    title: ['Profiles & notifications', 'Profily a oznámení'],
    description: ['Tune the deck for the way you work.', 'Přizpůsob deck svému způsobu práce.'],
    source: 'README.md#profiles-and-customization',
    sections: [
      {
        id: 'profiles',
        title: ['Switch your setup', 'Přepni si nastavení'],
        paragraphs: [
          [
            'Profiles combine view, theme, launcher, macros, notifications, and safety settings. Switch them from the desktop settings or from + New → Profiles on the deck.',
            'Profily spojují nastavení zobrazení, vzhledu, spouštěče, maker, oznámení a bezpečnosti. Přepínej je v nastavení aplikace nebo přes + Nový → Profily na decku.',
          ],
          [
            'Shared settings live in config.toml; device-specific preferences live in local.toml. A HERDECK_PROFILE environment override locks the profile for that process.',
            'Sdílená nastavení jsou v config.toml; preference konkrétního zařízení v local.toml. Proměnná HERDECK_PROFILE uzamkne profil pro daný proces.',
          ],
        ],
      },
      {
        id: 'appearance',
        title: ['Make status easy to read', 'Ať je stav dobře vidět'],
        paragraphs: [
          [
            'Choose a subtle status bar, a tinted tile, or a solid status color. Agent icons identify the tool; project favicons help you recognize the work. You can show both.',
            'Vyber nenápadný stavový pruh, tónovanou dlaždici nebo plnou barvu stavu. Ikona agenta označuje nástroj, favicon projektu pomůže poznat práci. Můžeš zobrazit obojí.',
          ],
        ],
      },
      {
        id: 'notifications',
        title: ['Choose how you are notified', 'Vyber si způsob upozornění'],
        paragraphs: [
          [
            'Use desktop banners and sounds for blocked or completed agents. Optional Telegram notifications can reach you away from the desk. Telegram setup requires a bot, a configured chat, and allowed user IDs.',
            'Používej desktopová oznámení a zvuky pro čekající nebo dokončené agenty. Volitelná oznámení přes Telegram tě zastihnou mimo stůl. Nastavení Telegramu vyžaduje bota, určený chat a povolená ID uživatelů.',
          ],
          [
            'Some compatible approval prompts can be answered through Telegram. Prompts requiring on-deck confirmation do not receive approval buttons. Keep the safety settings appropriate for your work.',
            'Některé kompatibilní schvalovací požadavky lze zodpovědět přes Telegram. Požadavky vyžadující potvrzení na decku schvalovací tlačítka nedostanou. Bezpečnostní nastavení přizpůsob své práci.',
          ],
        ],
      },
    ],
  },
  {
    slug: 'cli',
    title: ['CLI & automation', 'CLI a automatizace'],
    description: [
      'List, focus, and control agents from your terminal.',
      'Vypiš, otevři a ovládej agenty z terminálu.',
    ],
    source: 'README.md#controlling-agents-from-the-cli-herdeck-ctl',
    sections: [
      {
        id: 'inspect',
        title: ['Inspect first', 'Nejdřív zjisti stav'],
        paragraphs: [
          [
            'herdeck-ctl uses the same bridges and answer profiles as the deck. Start by listing live agents and use the actual server and pane IDs from that output.',
            'herdeck-ctl používá stejné bridge a odpovědní profily jako deck. Nejdřív vypiš skutečné agenty a použij ID serveru a panelu z tohoto výstupu.',
          ],
        ],
        code: 'herdeck-ctl ls --json\nherdeck-ctl wait --any --until blocked --timeout 60 --json',
      },
      {
        id: 'actions',
        title: ['Target an agent', 'Vyber konkrétního agenta'],
        paragraphs: [
          [
            'The examples below use local:w1:p1 as a placeholder. Replace it with a current target. approve changes agent state; send submits the text immediately.',
            'Následující příklady používají local:w1:p1 jako zástupný cíl. Nahraď ho aktuálním ID. approve mění stav agenta; send text ihned odešle.',
          ],
        ],
        code: 'herdeck-ctl focus local:w1:p1\nherdeck-ctl approve local:w1:p1\nherdeck-ctl send local:w1:p1 "run the tests"',
        note: [
          'Actions that clear a block wait for the agent to leave the blocked state. Use --settle or --no-settle only when you understand how your automation confirms success.',
          'Akce odstraňující blokování čekají, než agent opustí blokovaný stav. --settle nebo --no-settle použij, jen pokud víš, jak tvoje automatizace ověřuje úspěch.',
        ],
      },
      {
        id: 'exit-codes',
        title: ['Handle exit codes', 'Zpracuj návratové kódy'],
        bullets: [
          [
            '0: success. 2: usage error. 3: skipped because the agent is not blocked.',
            '0: úspěch. 2: chyba použití. 3: přeskočeno, protože agent není blokovaný.',
          ],
          [
            '4: unknown or ambiguous agent. 5: connection or configuration error. 124: wait timed out.',
            '4: neznámý nebo nejednoznačný agent. 5: chyba připojení nebo konfigurace. 124: vypršel čas čekání.',
          ],
        ],
      },
    ],
  },
  {
    slug: 'troubleshooting',
    title: ['Troubleshooting', 'Řešení problémů'],
    description: [
      'Diagnose a dark deck, missing agents, or a broken connection.',
      'Zjisti příčinu tmavého decku, chybějících agentů nebo připojení.',
    ],
    source: 'README.md#diagnosing-a-dark-deck',
    sections: [
      {
        id: 'maintenance',
        title: ['Start with Maintenance', 'Začni v Údržbě'],
        paragraphs: [
          [
            'Open Maintenance in the desktop app. Check the app, runtime, and bridge versions and the connection status. Use the specific recovery action offered for a reported problem.',
            'Otevři Údržbu v desktopové aplikaci. Zkontroluj verze aplikace, runtime a bridge a stav připojení. Použij konkrétní opravnou akci u nahlášeného problému.',
          ],
        ],
      },
      {
        id: 'doctor',
        title: ['Run the doctor', 'Spusť diagnostiku'],
        paragraphs: [
          [
            'For a source install, herdeck-doctor checks the Herdr socket, mode, configuration, device availability, and remote token resolution without printing token values.',
            'U instalace ze zdrojového kódu herdeck-doctor ověří socket Herdr, režim, konfiguraci, dostupnost zařízení a zdroj vzdáleného tokenu, aniž by vypsal jeho hodnotu.',
          ],
        ],
        code: 'herdeck-doctor\nherdr status --json\nherdr session list --json',
      },
      {
        id: 'dark-deck',
        title: ['The deck is dark', 'Deck nesvítí'],
        bullets: [
          [
            'Quit Ulanzi Studio. Check that another Herdeck runtime is not holding the D200. Reconnect the USB cable.',
            'Ukonči Ulanzi Studio. Zkontroluj, že D200 nedrží jiný Herdeck runtime. Znovu připoj USB.',
          ],
          [
            'Check Maintenance for configuration errors or version mismatches. A failed explicit config does not silently fall back to the demo fleet.',
            'V Údržbě zkontroluj chyby konfigurace nebo rozdílné verze. Neplatná explicitní konfigurace se potichu nepřepne na ukázkovou flotilu.',
          ],
          [
            'If only one remote server is offline, check that host and bridge; the other connections may still work.',
            'Pokud je offline jen jeden vzdálený server, zkontroluj jeho hostitele a bridge; ostatní připojení mohou stále fungovat.',
          ],
        ],
      },
      {
        id: 'missing-agents',
        title: ['Agents are missing', 'Chybí agenti'],
        paragraphs: [
          [
            'Check that the agents run inside the Herdr session selected in Settings → Servers. Named sessions and remote hosts are separate sources. A forced demo mode or an explicit socket environment override can change which sources are active.',
            'Zkontroluj, že agenti běží v Herdr session vybrané v Nastavení → Servery. Pojmenované sessions a vzdálení hostitelé jsou samostatné zdroje. Vynucený demo režim nebo proměnná s konkrétním socketem mohou změnit aktivní zdroje.',
          ],
        ],
        note: [
          'When reporting an issue, include versions, connection status, and the steps to reproduce it. Remove tokens, private prompts, and customer data from logs or screenshots.',
          'Při hlášení chyby přilož verze, stav připojení a postup reprodukce. Z logů a screenshotů odstraň tokeny, soukromé prompty a zákaznická data.',
        ],
      },
    ],
  },
];
export const searchText = (doc: Doc, lang: Lang) =>
  [
    local(lang, doc.title),
    local(lang, doc.description),
    ...doc.sections.flatMap((s) => [
      local(lang, s.title),
      ...(s.paragraphs || []).map((p) => local(lang, p)),
      ...(s.bullets || []).map((p) => local(lang, p)),
    ]),
  ].join(' ');
