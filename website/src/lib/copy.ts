export type Lang = 'en' | 'cs';
export const languages: Lang[] = ['en', 'cs'];
export const repository = 'https://github.com/vaclavik-xyz/herdeck';
export const releases = `${repository}/releases/latest`;
export const path = (lang: Lang, page = '') => `/${lang}/${page ? `${page}/` : ''}`;
export const text = (lang: Lang, en: string, cs: string) => (lang === 'cs' ? cs : en);

export const copy = {
  en: {
    nav: ['Features', 'Workflow', 'Docs'],
    download: 'Get Herdeck',
    source: 'View on GitHub',
    hero: 'Your agents.\nAt your fingertips.',
    intro:
      'A physical control room for your AI coding agents. See who needs you. Read the prompt. Make the call. Keep your flow.',
    watch: 'Watch the film',
    free: 'Open source. Yours to control.',
    compatibility: 'For the agents you already work with',
    firstTitle: 'Less tab hunting.\nMore making.',
    firstBody:
      'Claude is waiting for a decision. Codex is running tests. Gemini just finished. Bring it all into one view, without losing the thread.',
    demoTitle: 'Go ahead. Press a key.',
    demoBody:
      'Try the moment Herdeck was made for: an agent needs your approval. Select the amber tile, read the request, and decide.',
    demoLabel: 'Interactive demo · simulated agents',
    filmTitle: 'Find your rhythm.',
    filmBody:
      'Thirty seconds of fewer interruptions, physical controls, and a fleet that moves with you.',
    filmPlay: 'Play film with sound',
    filmSound: 'Original electronic score · 128 BPM',
    anywhere: 'One fleet.\nYour kind of cockpit.',
    anywhereBody:
      'On your desk, on your screen, or across your machines. Herdeck brings your Herdr sessions together.',
    hardware: 'On the deck',
    hardwareBody: 'Tactile LCD keys. A glance for the status. A press for the next decision.',
    desktop: 'On your desktop',
    desktopBody: 'A native app, a compact floating deck, and a live terminal preview.',
    remote: 'Across machines',
    remoteBody: 'Combine local sessions and remote bridges over your Tailscale network.',
    cta: 'Give your agents\na place on your desk.',
    ctaBody: 'Start with the desktop app. Add a deck when you want the satisfying part.',
    guide: 'Read the setup guide',
    install: 'Installation',
    foot: 'A control surface for AI coding agents. Built for Herdr.',
    credits: 'Visuals & credits',
    notice:
      'Product images are AI-created visualizations. The interactive demo uses simulated data.',
    skip: 'Skip to content',
    menu: 'Menu',
    close: 'Close',
    more: 'Explore all features',
  },
  cs: {
    nav: ['Funkce', 'Workflow', 'Dokumentace'],
    download: 'Stáhnout Herdeck',
    source: 'Kód na GitHubu',
    hero: 'Tvoji agenti.\nNa dosah ruky.',
    intro:
      'Fyzický ovládací pult pro tvoje AI agenty. Vidíš, kdo tě potřebuje. Přečteš požadavek. Rozhodneš. A pokračuješ v práci.',
    watch: 'Pustit film',
    free: 'Open source. Pod tvou kontrolou.',
    compatibility: 'Pro agenty, se kterými už pracuješ',
    firstTitle: 'Méně hledání oken.\nVíce hotové práce.',
    firstBody:
      'Claude čeká na rozhodnutí. Codex pouští testy. Gemini právě skončil. Měj je všechny na očích a neztrácej nit.',
    demoTitle: 'Zkus stisknout tlačítko.',
    demoBody:
      'Vyzkoušej chvíli, pro kterou Herdeck vznikl: agent potřebuje souhlas. Vyber oranžovou dlaždici, přečti si požadavek a rozhodni.',
    demoLabel: 'Interaktivní ukázka · simulovaní agenti',
    filmTitle: 'Chyť svůj rytmus.',
    filmBody:
      'Třicet sekund s méně přepínáním, fyzickými tlačítky a agenty, kteří drží tempo s tebou.',
    filmPlay: 'Přehrát film se zvukem',
    filmSound: 'Původní elektronická hudba · 128 BPM',
    anywhere: 'Jedna flotila.\nTvůj ovládací pult.',
    anywhereBody:
      'Na stole, na obrazovce i napříč stroji. Herdeck propojí tvoje Herdr sessions do jednoho přehledu.',
    hardware: 'Na decku',
    hardwareBody: 'Fyzická LCD tlačítka. Pohledem zjistíš stav. Stiskem rozhodneš, co dál.',
    desktop: 'Na desktopu',
    desktopBody: 'Nativní aplikace, malé plovoucí okno a živý náhled terminálu.',
    remote: 'Napříč stroji',
    remoteBody: 'Lokální sessions i vzdálené bridge přes tvou síť Tailscale.',
    cta: 'Dej agentům\nmísto na svém stole.',
    ctaBody: 'Začni desktopovou aplikací. Deck připoj, až budeš chtít ovládání vzít do ruky.',
    guide: 'Jak začít',
    install: 'Instalace',
    foot: 'Ovládací pult pro AI coding agenty. Postavený pro Herdr.',
    credits: 'Vizuály a licence',
    notice:
      'Produktové obrázky jsou vizualizace vytvořené pomocí AI. Interaktivní ukázka používá simulovaná data.',
    skip: 'Přeskočit na obsah',
    menu: 'Menu',
    close: 'Zavřít',
    more: 'Prohlédnout všechny funkce',
  },
};

export const features = [
  {
    icon: 'grid',
    title: ['A whole fleet at a glance', 'Celá flotila na očích'],
    body: [
      'Working, blocked, done, or waiting for CI. Color-coded tiles tell you what matters. Agents waiting for you come first.',
      'Pracuje, čeká na tebe, hotovo, nebo čeká na CI. Barevné dlaždice ukážou to podstatné. Agenti čekající na odpověď mají přednost.',
    ],
  },
  {
    icon: 'hand',
    title: ['Decisions with context', 'Rozhodnutí s kontextem'],
    body: [
      'Read the request before you approve, deny, stop, or continue. Herdeck moves to the next blocked agent after you answer.',
      'Přečti si požadavek, než ho schválíš, zamítneš, zastavíš nebo necháš pokračovat. Po odpovědi Herdeck přejde k dalšímu čekajícímu agentovi.',
    ],
  },
  {
    icon: 'terminal',
    title: ['The terminal, right here', 'Terminál přímo po ruce'],
    body: [
      'Open a live, read-only terminal preview in the desktop app. Focus the original pane when you need to take over.',
      'Otevři si živý náhled terminálu v desktopové aplikaci. Když potřebuješ zasáhnout, přepni se do původního okna agenta.',
    ],
  },
  {
    icon: 'network',
    title: ['Local and remote, together', 'Lokální i vzdálení pohromadě'],
    body: [
      'Bring multiple Herdr sessions and remote machines into the same deck. Remote bridges connect directly over Tailscale.',
      'Spoj více Herdr sessions a vzdálených strojů v jednom decku. Vzdálené bridge se připojují přímo přes Tailscale.',
    ],
  },
  {
    icon: 'sliders',
    title: ['A setup that fits you', 'Nastavení podle tebe'],
    body: [
      'Switch profiles, choose project or agent icons, set notification sounds, and create macros for the actions you repeat.',
      'Přepínej profily, zvol ikony projektů či agentů, nastav zvuky oznámení a vytvoř makra pro opakované akce.',
    ],
  },
  {
    icon: 'shield',
    title: ['You keep the controls', 'Ovládání zůstává u tebe'],
    body: [
      'Configurable confirmations, scoped agent actions, and no automatic retry of non-idempotent key sends. Your agents still ask. You still decide.',
      'Nastavitelné potvrzování, akce pro konkrétního agenta a žádné automatické opakování odeslaných stisků. Agenti se ptají. Rozhoduješ ty.',
    ],
  },
  {
    icon: 'bell',
    title: ['Know when you are needed', 'Včas víš, kdo tě potřebuje'],
    body: [
      'Desktop notifications and sound bring decisions to your attention. Optional Telegram notifications can reach you away from your desk.',
      'Desktopová oznámení a zvuk tě upozorní na čekající rozhodnutí. Volitelný Telegram tě zastihne i mimo pracovní stůl.',
    ],
  },
  {
    icon: 'chart',
    title: ['See where the time goes', 'Víš, kde se ztrácí čas'],
    body: [
      'Check usage limits and agent activity. With compatible bridges, statistics show waiting time and response time by agent or project.',
      'Sleduj limity využití a aktivitu agentů. S kompatibilními bridge uvidíš ve statistikách čekání a dobu odpovědí podle agenta nebo projektu.',
    ],
  },
];
export const local = (lang: Lang, pair: string[]) => pair[lang === 'cs' ? 1 : 0];
