// Field help tooltips for the config editor, per language. One entry per
// labelled field in every section — sections.help.test.ts enforces that the
// en and cs key sets match exactly and that every rendered field carries one.
// Texts are grounded in the backend consumer of each value; keep them to one
// sentence (~110 chars) and update BOTH languages together.
import type { Lang } from "./i18n.svelte";

export type SectionHelp = Record<string, string>;

export const FIELD_HELP: Record<Lang, Record<string, SectionHelp>> = {
  en: {
    servers: {
      id: "Unique server name the deck uses to sort, color, and route its agents.",
      url: "WebSocket address of the herdr bridge on the server (e.g. ws://100.x.y.z:8788), ideally a Tailscale IP.",
      token: "Name of the env var or keychain item holding the access token; the value can be saved straight to the keychain.",
    },
    deck: {
      grid: "Deck grid size as columns×rows (e.g. 5x3); sets how many tiles fit on screen.",
      overview_order: "Which servers connect and in what order they appear in the overview; empty = all servers from the config.",
      deck: "Deck type on this machine: elgato, d200, web (browser simulator), or fake; empty = auto-detect.",
      herdr_socket: "Path to the herdr Unix socket for local mode (default ~/.config/herdr/herdr.sock).",
      web_bind: "Address the web simulator listens on; 127.0.0.1 local only, 0.0.0.0 also for other devices.",
      web_port: "Port of the browser web simulator (default 8800).",
      icons_dir: "Folder of custom icons (PNG) that override the built-in tile icons.",
      brightness: "Physical deck display brightness from 0–100 (default 80).",
      debounce: "Seconds during which a repeated press of the same D200 key is ignored (default 0.25 s).",
      keep_alive_interval: "How often, in seconds, the D200 is kept alive so it doesn't revert to its default screen (default 5 s).",
      tick_interval: "How often, in seconds, the deck redraws (clock, elapsed time, animations); default 0.4 s.",
    },
    view: {
      management: "Controls layout: launcher_menu = a '+ New' tile with a menu, bottom_row = a bottom row of buttons.",
      agent_slots: "Number of tiles reserved for agents ('max' = all free ones); not used by the app yet.",
      show_profile_on_panel: "Shows the active profile name on the status panel; not used when rendering yet.",
      working_animation: "Working-tile animation: pulse = calm slow breath (fewest repaints), spin/comet/sweep = per-tick motion, none = static.",
      tile_fill: "Fill the tile with the agent status color: none = text and bar only, tint = dimmed shade, solid = full color.",
      bottom_row: "Which buttons fill the bottom row in bottom_row mode; only profiles and new_agent (+ New) work for now.",
      tile_fields: "Which fields the agent tile shows: repo, branch, status, time (time in state), server (server label).",
      tile_primary: "First text line of the tile from repo/branch/workspace/tab/agent; unset = repo, empty = off.",
      tile_secondary: "Second text line of the tile from the same fields; unset = branch, an empty list turns the line off.",
      language: "Language of the deck and this app: en (English) or cs (Czech). Applies after Apply.",
    },
    theme: {
      working: "Tile color of an agent that is currently working (default green).",
      idle: "Tile color of an idle agent waiting for its next task (default blue).",
      blocked: "Tile color of an agent waiting for your approval or answer (default amber).",
      done: "Tile color of an agent that has finished its work (default cyan).",
      unknown: "Tile color of an agent whose status could not be determined (default grey).",
      offline: "Tile color of an agent whose server is disconnected or unreachable (default red).",
      server_accents: "Color palette for the server label on the tile; each server permanently picks one from it (#hex allowed).",
    },
    macros: {
      macros: "List of quick messages in a non-blocked agent's detail view; a profile overrides the whole list at once.",
      label: "Short macro label on the tile in the agent detail view (up to 14 characters shown).",
      text: "Text sent to the agent's terminal when the tile is pressed, as if you typed it yourself.",
    },
    start_profiles: {
      name: "Agent type name — shown on the tile in the '+ New' menu and sets both the icon and the answer profile.",
      argv: "Command and arguments that launch the agent in a new pane (each item = one word).",
    },
    notifications: {
      enabled: "Master switch for notifications — when off, no notifications are sent (default: off).",
      sound: "Whether notifications also play a sound; when off, macOS gets a silent notification and Telegram a silent message.",
      on: "Which agent states trigger a notification; only 'blocked' (agent waiting for your input) works for now.",
      backends: "Where notifications go: macos (Notification Center) and/or telegram (a bot message, e.g. to your phone).",
      token: "Name under which the Telegram bot token is stored (env var or keychain) — not the token itself.",
      chat_id: "Numeric ID of the Telegram chat or group where the bot sends blocked-agent notifications.",
    },
    safety: {
      approve_always: "Whether a blocked agent also offers an Approve! button (approve and don't ask again); turn off to hide it.",
      require_confirm_for: "Actions needing a second confirming press within 5 s (default act_force = Stop); empty list turns it off.",
    },
    answer_profiles: {
      name: "Profile name = agent type (claude, codex…) that selects the keys; default is the fallback.",
      approve: "Key sequence sent to a blocked agent on Approve (for Claude e.g. 1 + enter).",
      deny: "Key sequence sent to a blocked agent on Deny (e.g. esc).",
      stop: "Keys for Stop — forcibly interrupt the agent (e.g. ctrl+c); sent even when the agent isn't blocked.",
      approve_always: "Keys for 'Approve always' (for Claude 2 + enter); unset = the Approve keys are used.",
      keys: "Whether the profile layer inherits the whole entry from the base config or overrides it with its own keys.",
    },
    profiles: {
      extends: "Which profile this one inherits settings from; 'default' means the base config directly.",
      servers: "Which servers the profile uses; inheriting takes the parent's or base selection, empty = a profile with no servers.",
    },
    desktop: {
      window_mode: "Deck window appearance: normal = framed, floating = frameless, always_on_top = always on top (after restart).",
      toggle_deck: "Global shortcut to show/hide the deck; default Cmd/Ctrl+Shift+D, empty field = disabled.",
    },
  },
  cs: {
    servers: {
      id: "Jedinečný název serveru, podle kterého deck řadí, barví a směruje jeho agenty.",
      url: "WebSocket adresa herdr mostu na serveru (např. ws://100.x.y.z:8788), ideálně Tailscale IP.",
      token: "Název proměnné či položky klíčenky s přístupovým tokenem; hodnotu lze uložit rovnou do klíčenky.",
    },
    deck: {
      grid: "Rozměr mřížky decku ve tvaru sloupce×řádky (např. 5x3); určuje počet dlaždic na obrazovce.",
      overview_order: "Které servery se připojí a v jakém pořadí se řadí v přehledu; prázdné = všechny servery z konfigurace.",
      deck: "Typ decku na tomto stroji: elgato, d200, web (simulátor v prohlížeči) nebo fake; prázdné = autodetekce.",
      herdr_socket: "Cesta k unixovému socketu herdr pro lokální režim (výchozí ~/.config/herdr/herdr.sock).",
      web_bind: "Adresa, na které poslouchá webový simulátor; 127.0.0.1 jen lokálně, 0.0.0.0 i pro jiná zařízení.",
      web_port: "Port webového simulátoru v prohlížeči (výchozí 8800).",
      icons_dir: "Složka s vlastními ikonami (PNG), které přepíší vestavěné ikony na dlaždicích.",
      brightness: "Jas displeje fyzického decku v rozsahu 0–100 (výchozí 80).",
      debounce: "Doba v sekundách, po kterou se ignoruje opakovaný stisk téže klávesy na D200 (výchozí 0,25 s).",
      keep_alive_interval: "Jak často v sekundách se D200 udržuje při životě, aby se nepřepnul na výchozí obrazovku (výchozí 5 s).",
      tick_interval: "Jak často v sekundách se deck překresluje (hodiny, uplynulý čas, animace); výchozí 0,4 s.",
    },
    view: {
      management: "Rozložení ovládání: launcher_menu = dlaždice „+ New“ s menu, bottom_row = spodní řada tlačítek.",
      agent_slots: "Počet dlaždic vyhrazených agentům („max“ = všechny volné); zatím se v aplikaci nepoužívá.",
      show_profile_on_panel: "Ukáže název aktivního profilu na stavovém panelu; zatím se při vykreslování nepoužívá.",
      working_animation: "Animace pracující dlaždice: pulse = klidný pomalý tep (nejméně překreslení), spin/comet/sweep = pohyb každý tick, none = staticky.",
      tile_fill: "Vyplnění dlaždice barvou stavu agenta: none = jen text a proužek, tint = ztmavený odstín, solid = plná barva.",
      bottom_row: "Která tlačítka obsadí spodní řadu v režimu bottom_row; nyní fungují jen profiles a new_agent (+ New).",
      tile_fields: "Které údaje dlaždice agenta zobrazí: repo, branch, status, time (doba ve stavu), server (štítek serveru).",
      tile_primary: "První textový řádek dlaždice z polí repo/branch/workspace/tab/agent; nevyplněno = repo, prázdné = vypnuto.",
      tile_secondary: "Druhý textový řádek dlaždice ze stejných polí; nevyplněno = branch, prázdný seznam řádek vypne.",
      language: "Jazyk decku i této aplikace: en (angličtina) nebo cs (čeština). Projeví se po Použít.",
    },
    theme: {
      working: "Barva dlaždice agenta, který právě pracuje (výchozí green).",
      idle: "Barva dlaždice nečinného agenta, který čeká na další zadání (výchozí blue).",
      blocked: "Barva dlaždice agenta, který čeká na vaše schválení nebo odpověď (výchozí amber).",
      done: "Barva dlaždice agenta, který dokončil práci (výchozí cyan).",
      unknown: "Barva dlaždice agenta, jehož stav se nepodařilo zjistit (výchozí grey).",
      offline: "Barva dlaždice agenta, jehož server je odpojený nebo nedostupný (výchozí red).",
      server_accents: "Paleta barev pro štítek serveru na dlaždici; každý server si z ní natrvalo vylosuje jednu (lze i #hex).",
    },
    macros: {
      macros: "Seznam rychlých zpráv v detailu neblokovaného agenta; profil přepisuje celý seznam najednou.",
      label: "Krátký popisek makra na dlaždici v detailu agenta (zobrazí se max. 14 znaků).",
      text: "Text, který se po stisku dlaždice pošle agentovi do terminálu, jako bys ho napsal sám.",
    },
    start_profiles: {
      name: "Jméno typu agenta — zobrazí se na dlaždici v menu „+ New“ a určuje ikonu i profil odpovědí.",
      argv: "Příkaz a jeho argumenty, kterým se agent spustí v novém panelu (každá položka = jedno slovo).",
    },
    notifications: {
      enabled: "Hlavní vypínač upozornění — když je vypnutý, žádná oznámení se neposílají (výchozí: vypnuto).",
      sound: "Zda upozornění zazní i zvukem; při vypnutí přijde na macOS tiché oznámení a na Telegram tichá zpráva.",
      on: "Které stavy agenta spustí upozornění; zatím funguje jen „blocked“ (agent čeká na váš vstup).",
      backends: "Kam se upozornění doručí: macos (oznamovací centrum) a/nebo telegram (zpráva botem, třeba na mobil).",
      token: "Název, pod kterým je uložen token Telegram bota (proměnná prostředí či klíčenka) — ne token samotný.",
      chat_id: "Číselné ID Telegram chatu či skupiny, kam bot posílá upozornění na zablokované agenty.",
    },
    safety: {
      approve_always: "Zda se u blokovaného agenta nabízí i tlačítko Approve! (schválit a příště se neptat); vypnutím ho skryjete.",
      require_confirm_for: "Akce vyžadující druhý potvrzovací stisk do 5 s (výchozí act_force = Stop); prázdný seznam potvrzování vypne.",
    },
    answer_profiles: {
      name: "Jméno profilu = typ agenta (claude, codex…), podle kterého se vyberou klávesy; záložní je default.",
      approve: "Sekvence kláves poslaná blokovanému agentovi při akci Schválit (u Claude např. 1 + enter).",
      deny: "Sekvence kláves poslaná blokovanému agentovi při akci Zamítnout (např. esc).",
      stop: "Klávesy pro Stop — vynucené přerušení agenta (např. ctrl+c), pošle se i když agent není blokovaný.",
      approve_always: "Klávesy pro „Schválit napořád“ (u Claude 2 + enter); nevyplněné = použijí se klávesy pro Schválit.",
      keys: "Zda profilová vrstva dědí celou položku ze základní konfigurace, nebo ji přepíše vlastními klávesami.",
    },
    profiles: {
      extends: "Ze kterého profilu tento profil dědí nastavení; „default“ znamená přímo základní konfiguraci.",
      servers: "Které servery profil používá; při dědění přebírá výběr rodiče či báze, prázdný výběr = profil bez serverů.",
    },
    desktop: {
      window_mode: "Vzhled okna decku: normal = s rámečkem, floating = bez rámečku, always_on_top = vždy navrchu (po restartu).",
      toggle_deck: "Globální zkratka pro zobrazení/skrytí decku; výchozí Cmd/Ctrl+Shift+D, prázdné pole = vypnuto.",
    },
  },
};
