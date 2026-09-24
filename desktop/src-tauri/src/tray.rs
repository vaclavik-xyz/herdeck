//! The native tray icon and menu, the deck's right-click context menu (which
//! shares the tray's menu-event handler), and their EN/CS labels.

use std::sync::Mutex;

use tauri::menu::{CheckMenuItem, ContextMenu, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Emitter, Manager};

use crate::sync_util::LockExt;
use crate::window_roles::{
    deck_is_visible, hide_role_window, persist_deck_always_on_top, show_role_window,
    toggle_deck_window, APP_WINDOW, DECK_WINDOW,
};
use crate::{build_channel, AppState};

/// Told to the DECK window (never the app window) when a zoom item is picked
/// from the deck's own right-click context menu. The zoom level itself lives
/// entirely in the deck's WebView (`floatingScale.ts`'s `localStorage` value
/// plus a CSS variable), so Rust cannot change it directly — it just forwards
/// the chosen command ("in" | "out" | "reset") and `App.svelte` applies it via
/// the same `applyFloatingScale` the ⌘+/⌘-/⌘0 keydown handler already uses.
/// Mirrors the `DECK_VISIBILITY_EVENT` emit_to pattern above.
pub(crate) const FLOATING_ZOOM_EVENT: &str = "floating-zoom-command";

/// Every tray/context-menu item id: built once in `build_tray` (or, for
/// "show_app"/"deck_aot", also in `build_deck_context_menu`, which reuses
/// them so the two menus share one `on_menu_event` handler instead of each
/// carrying its own) and matched again in `on_menu_event`. A plain string
/// literal repeated across those sites has no compiler check tying them
/// together — a typo in any one copy (e.g. `MenuItem::with_id(app,
/// "zoom_ni", …)`) builds fine and produces a menu item that silently never
/// fires, which no test can catch either. These consts are the single
/// spelling every site refers to instead, so the whole handler reads one way.
pub(crate) const MENU_ID_SHOW_APP: &str = "show_app";
pub(crate) const MENU_ID_DECK_AOT: &str = "deck_aot";
pub(crate) const MENU_ID_HIDE_DECK: &str = "hide_deck";
pub(crate) const MENU_ID_ZOOM_IN: &str = "zoom_in";
pub(crate) const MENU_ID_ZOOM_OUT: &str = "zoom_out";
pub(crate) const MENU_ID_ZOOM_RESET: &str = "zoom_reset";
pub(crate) const MENU_ID_TOGGLE_DECK: &str = "toggle_deck";
pub(crate) const MENU_ID_RECONNECT: &str = "reconnect";
pub(crate) const MENU_ID_AUTOSTART: &str = "autostart";
pub(crate) const MENU_ID_CHECK_UPDATE: &str = "check_update";
pub(crate) const MENU_ID_QUIT: &str = "quit";

/// English/Czech texts for every tray item, keyed by the item order in
/// `TrayMenuItems::retitle`. The tray is native — the WebView retitles it via
/// the `tray_set_language` command when the deck's `[view].language` changes.
///
/// `toggle_deck` occupies TWO slots (show/hide) because its text also depends
/// on the deck's current visibility, not just the language — see
/// `toggle_deck_label`, which picks between them.
pub(crate) fn tray_labels(lang: &str) -> [&'static str; 8] {
    match lang {
        "cs" => [
            "Otevřít Herdeck",
            "Zobrazit deck",
            "Skrýt deck",
            "Deck vždy navrchu",
            "Spouštět po přihlášení",
            "Změnit připojení…",
            "Zkontrolovat aktualizace",
            "Ukončit",
        ],
        _ => [
            "Open Herdeck",
            "Show deck",
            "Hide deck",
            "Deck always on top",
            "Start at login",
            "Change connection…",
            "Check for updates",
            "Quit",
        ],
    }
}

/// The tray icon's id, for `tray_by_id` lookups after `build_tray`.
pub(crate) const TRAY_ID: &str = "herdeck-tray";

/// The blocked-agent part of the tray tooltip. A count-first "label: n" form in
/// Czech sidesteps plural agreement.
pub(crate) fn tray_blocked_label(lang: &str, blocked: u64) -> String {
    match lang {
        "cs" => format!("zablokováno: {blocked}"),
        _ => format!("{blocked} blocked"),
    }
}

/// The tray tooltip: the app's display name, plus how many agents are blocked
/// when any are (the count the deck's own `/state` poll just reported).
pub(crate) fn tray_tooltip(lang: &str, name: &str, blocked: Option<u64>) -> String {
    match blocked {
        Some(n) if n > 0 => format!("{name} — {}", tray_blocked_label(lang, n)),
        _ => name.to_string(),
    }
}

/// Which text the `toggle_deck` tray item (and its hotkey-driven refresh)
/// shows: it depends on both the language and whether the deck is currently
/// visible. Pulled out of `TrayMenuItems::retitle` as a pure function — the
/// interaction is the part worth pinning down with a test, and building a
/// real tray in a unit test isn't possible.
pub(crate) fn toggle_deck_label(lang: &str, deck_visible: bool) -> &'static str {
    let l = tray_labels(lang);
    if deck_visible {
        l[2] // "Hide deck" / "Skrýt deck"
    } else {
        l[1] // "Show deck" / "Zobrazit deck"
    }
}

/// English/Czech texts unique to the deck's right-click context menu — the
/// three zoom items, which the tray has no equivalent of. "Hide deck",
/// "Deck always on top" and "Open Herdeck" are NOT retyped here: see
/// `deck_context_menu_texts`, which pulls them from `tray_labels` instead so
/// the two menus can never describe the same action two different ways.
pub(crate) fn deck_context_zoom_labels(lang: &str) -> [&'static str; 3] {
    match lang {
        "cs" => ["Zvětšit", "Zmenšit", "Původní velikost"],
        _ => ["Zoom in", "Zoom out", "Actual size"],
    }
}

/// The six texts on the deck's right-click context menu, in on-screen order:
/// hide, the three zoom commands, the always-on-top checkbox, then open-app.
/// Split out of `build_deck_context_menu` (which needs a live `AppHandle` to
/// build real menu items and so cannot run in a unit test) purely so the
/// EN/CS mapping — and the fact that three of the six reuse `tray_labels`
/// byte-for-byte — has a test that does not need a display.
pub(crate) fn deck_context_menu_texts(lang: &str) -> [&'static str; 6] {
    let tray = tray_labels(lang);
    let zoom = deck_context_zoom_labels(lang);
    [
        tray[2], // "Hide deck" / "Skrýt deck" — identical to the tray's
        zoom[0], zoom[1], zoom[2],
        tray[3], // "Deck always on top" / "Deck vždy navrchu" — identical to the tray's
        tray[0], // "Open Herdeck" / "Otevřít Herdeck" — identical to the tray's
    ]
}

/// Which floating-scale command a context-menu zoom item's id names, or
/// `None` for any other id. Pulled out of the `on_menu_event` match so the
/// id→command mapping has a test that does not need a live tray or window.
pub(crate) fn zoom_command_for_menu_id(id: &str) -> Option<&'static str> {
    match id {
        MENU_ID_ZOOM_IN => Some("in"),
        MENU_ID_ZOOM_OUT => Some("out"),
        MENU_ID_ZOOM_RESET => Some("reset"),
        _ => None,
    }
}

/// Handles to every retitlable tray item, managed as Tauri state so the
/// `tray_set_language` command can reach them after setup.
#[derive(Default)]
pub(crate) struct TrayHandles(pub(crate) Mutex<Option<TrayMenuItems>>);

pub(crate) struct TrayMenuItems {
    show_app: MenuItem<tauri::Wry>,
    toggle_deck: MenuItem<tauri::Wry>,
    pub(crate) deck_aot: CheckMenuItem<tauri::Wry>,
    autostart: CheckMenuItem<tauri::Wry>,
    reconnect: MenuItem<tauri::Wry>,
    check_update: MenuItem<tauri::Wry>,
    quit: MenuItem<tauri::Wry>,
    /// The language `retitle` was last called with. Needed so a
    /// visibility-only refresh of `toggle_deck` (`sync_toggle_deck_label`,
    /// run from every path that shows or hides the deck) can pick the right
    /// string without its caller having to track the language too.
    lang: Mutex<String>,
    /// The blocked-agent count the tooltip last showed, so the tooltip is only
    /// touched when it actually changes (`/state` is polled many times a second).
    pub(crate) blocked: Mutex<Option<u64>>,
}

impl TrayMenuItems {
    pub(crate) fn retitle(&self, lang: &str, deck_visible: bool) {
        let l = tray_labels(lang);
        let _ = self.show_app.set_text(l[0]);
        let _ = self.toggle_deck.set_text(toggle_deck_label(lang, deck_visible));
        let _ = self.deck_aot.set_text(l[3]);
        let _ = self.autostart.set_text(l[4]);
        let _ = self.reconnect.set_text(l[5]);
        let _ = self.check_update.set_text(l[6]);
        let _ = self.quit.set_text(l[7]);
        *self.lang.lock_or_recover() = lang.to_string();
    }

    /// Refresh only `toggle_deck`'s text for a visibility change, in whichever
    /// language `retitle` was last called with. The counterpart to `retitle`,
    /// which instead needs the visibility handed in because IT runs on a
    /// language change.
    pub(crate) fn sync_toggle_deck_label(&self, deck_visible: bool) {
        let lang = self.lang.lock_or_recover().clone();
        let _ = self.toggle_deck.set_text(toggle_deck_label(&lang, deck_visible));
    }

    /// The language `retitle` was last called with. The single source of
    /// truth for "what language is the UI in right now" — the deck's own
    /// `[view].language` feeds it via `tray_set_language` (see `App.svelte`'s
    /// `locale` effect), so anything else that needs the current language
    /// (the context menu below) reads it from here instead of tracking it a
    /// second time.
    pub(crate) fn current_lang(&self) -> String {
        self.lang.lock_or_recover().clone()
    }
}

/// Retitle the native tray menu for `lang` ("en"/"cs"). Called by the WebView
/// whenever the deck-reported language changes; unknown values fall back to en.
#[tauri::command]
pub(crate) fn tray_set_language(app: tauri::AppHandle, lang: String, handles: tauri::State<'_, TrayHandles>) {
    let deck_visible = deck_is_visible(&app);
    if let Some(items) = handles.0.lock_or_recover().as_ref() {
        items.retitle(&lang, deck_visible);
        let blocked = *items.blocked.lock_or_recover();
        set_tray_tooltip(&app, &lang, blocked);
    }
}

pub(crate) fn set_tray_tooltip(app: &tauri::AppHandle, lang: &str, blocked: Option<u64>) {
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let text = tray_tooltip(lang, &build_channel::display_name(), blocked);
        let _ = tray.set_tooltip(Some(text));
    }
}

/// Record the blocked-agent count from a `/state` response and retitle the
/// tray tooltip — only when the count changed.
pub(crate) fn update_tray_blocked(app: &tauri::AppHandle, blocked: Option<u64>) {
    let Some(handles) = app.try_state::<TrayHandles>() else {
        return;
    };
    let lang = {
        let guard = handles.0.lock_or_recover();
        let Some(items) = guard.as_ref() else {
            return;
        };
        let mut last = items.blocked.lock_or_recover();
        if *last == blocked {
            return;
        }
        *last = blocked;
        items.current_lang()
    };
    set_tray_tooltip(app, &lang, blocked);
}

/// Build the deck's right-click context menu fresh for every popup, so its
/// "Deck always on top" checkbox always shows the CURRENT flag rather than a
/// snapshot from whenever the tray was last built.
///
/// "Hide deck", "Deck always on top" and "Open Herdeck" carry the SAME ids as
/// their tray counterparts ("deck_aot" and "show_app" ARE the tray's ids;
/// "hide_deck" is new but goes through the same `on_menu_event` closure as
/// `toggle_deck`'s hide path). `TrayIconBuilder::on_menu_event` is registered
/// ONCE for the whole app and — per its own doc comment — fires "for any menu
/// event, whether it is coming from this window, another window, or from the
/// tray icon menu". Reusing an id therefore reuses the tray's existing
/// handler outright; it does not add a second copy of that logic.
pub(crate) fn build_deck_context_menu(
    app: &tauri::AppHandle,
    lang: &str,
    always_on_top: bool,
) -> tauri::Result<Menu<tauri::Wry>> {
    let texts = deck_context_menu_texts(lang);
    let hide = MenuItem::with_id(app, MENU_ID_HIDE_DECK, texts[0], true, None::<&str>)?;
    let zoom_in = MenuItem::with_id(app, MENU_ID_ZOOM_IN, texts[1], true, Some("CmdOrCtrl+="))?;
    let zoom_out = MenuItem::with_id(app, MENU_ID_ZOOM_OUT, texts[2], true, Some("CmdOrCtrl+-"))?;
    let zoom_reset =
        MenuItem::with_id(app, MENU_ID_ZOOM_RESET, texts[3], true, Some("CmdOrCtrl+0"))?;
    let deck_aot = CheckMenuItem::with_id(
        app,
        MENU_ID_DECK_AOT,
        texts[4],
        true,
        always_on_top,
        None::<&str>,
    )?;
    let open_app = MenuItem::with_id(app, MENU_ID_SHOW_APP, texts[5], true, None::<&str>)?;
    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;
    Menu::with_items(
        app,
        &[
            &hide,
            &sep1,
            &zoom_in,
            &zoom_out,
            &zoom_reset,
            &sep2,
            &deck_aot,
            &open_app,
        ],
    )
}

/// Pop the deck's right-click context menu on the deck window, at the
/// cursor. Called by the deck's own `contextmenu` listener (never the app
/// window's — see `App.svelte`). Plain `fn`, not `async`: every other
/// command here that touches a window or the tray (`show_deck`, `tray_set_
/// language`, `reload_deck_always_on_top`, …) is sync for the same reason —
/// the native menu/window APIs it calls are main-thread-only, and Tauri's
/// `Menu::popup` already hops there itself.
#[tauri::command]
pub(crate) fn show_deck_context_menu(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    tray: tauri::State<'_, TrayHandles>,
) -> Result<(), String> {
    let window = app
        .get_webview_window(DECK_WINDOW)
        .ok_or_else(|| "deck window missing".to_string())?;
    let lang = tray
        .0
        .lock_or_recover()
        .as_ref()
        .map(TrayMenuItems::current_lang)
        .unwrap_or_else(|| "en".to_string());
    let always_on_top = *state.deck_always_on_top.lock_or_recover();
    let menu = build_deck_context_menu(&app, &lang, always_on_top).map_err(|e| e.to_string())?;
    let webview: &tauri::Webview<tauri::Wry> = window.as_ref();
    menu.popup(webview.window()).map_err(|e| e.to_string())
}

/// Whether a left click on the tray icon opens its menu. Left click with the
/// menu disabled and no click handler did NOTHING on macOS, where the menu bar
/// has no right-click habit to fall back on.
pub(crate) fn tray_menu_on_left_click() -> bool {
    cfg!(target_os = "macos")
}

/// Build the tray icon. `deck_always_on_top` and `deck_visible` are the
/// values `run()` already resolved at startup (config text + window state) —
/// handed in rather than re-read here, so the tray's initial checkbox and
/// `toggle_deck` label always agree with what actually got applied to the
/// windows.
pub(crate) fn build_tray(app: &tauri::App, deck_always_on_top: bool, deck_visible: bool) -> tauri::Result<()> {
    use tauri_plugin_autostart::ManagerExt;
    // Built with the English (default-language) labels; tray_set_language
    // retitles everything the moment the WebView learns the configured language.
    let l = tray_labels("en");
    let show_app = MenuItem::with_id(app, MENU_ID_SHOW_APP, l[0], true, None::<&str>)?;
    let toggle_deck = MenuItem::with_id(
        app,
        MENU_ID_TOGGLE_DECK,
        toggle_deck_label("en", deck_visible),
        true,
        None::<&str>,
    )?;
    let deck_aot = CheckMenuItem::with_id(
        app,
        MENU_ID_DECK_AOT,
        l[3],
        true,
        deck_always_on_top,
        None::<&str>,
    )?;
    let autostart = CheckMenuItem::with_id(
        app,
        MENU_ID_AUTOSTART,
        l[4],
        true,
        app.autolaunch().is_enabled().unwrap_or(false),
        None::<&str>,
    )?;
    let reconnect = MenuItem::with_id(app, MENU_ID_RECONNECT, l[5], true, None::<&str>)?;
    let check_update = MenuItem::with_id(app, MENU_ID_CHECK_UPDATE, l[6], true, None::<&str>)?;
    let quit = MenuItem::with_id(app, MENU_ID_QUIT, l[7], true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &show_app,
            &toggle_deck,
            &deck_aot,
            &autostart,
            &reconnect,
            &check_update,
            &quit,
        ],
    )?;
    let autostart_cb = autostart.clone();
    let deck_aot_cb = deck_aot.clone();
    if let Some(handles) = app.try_state::<TrayHandles>() {
        *handles.0.lock_or_recover() = Some(TrayMenuItems {
            show_app: show_app.clone(),
            toggle_deck: toggle_deck.clone(),
            deck_aot: deck_aot.clone(),
            autostart: autostart.clone(),
            reconnect: reconnect.clone(),
            check_update: check_update.clone(),
            quit: quit.clone(),
            lang: Mutex::new("en".to_string()),
            blocked: Mutex::new(None),
        });
    }

    let mut builder = TrayIconBuilder::with_id(TRAY_ID)
        .tooltip(build_channel::display_name())
        .menu(&menu)
        // macOS convention: a menu-bar extra opens its menu on a left click
        // too. Elsewhere the right click opens the menu and a left click
        // toggles the deck (see `on_tray_icon_event`).
        .show_menu_on_left_click(tray_menu_on_left_click())
        .on_tray_icon_event(|tray, event| {
            use tauri::tray::{MouseButton, MouseButtonState, TrayIconEvent};
            if tray_menu_on_left_click() {
                return;
            }
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_deck_window(tray.app_handle());
            }
        })
        .on_menu_event(move |app, event| match event.id.as_ref() {
            MENU_ID_SHOW_APP => {
                // Also the deck context menu's "Open Herdeck" item
                // (`build_deck_context_menu` gives it this SAME id, on
                // purpose, so it runs through this one arm instead of a copy).
                //
                // Mirrors what the pre-roles `normal` mode did: opening the app
                // dismisses a re-onboarding card the user never went through with.
                let _ = app.emit_to(APP_WINDOW, "open-settings", ());
                show_role_window(app, APP_WINDOW);
            }
            MENU_ID_TOGGLE_DECK => toggle_deck_window(app),
            MENU_ID_DECK_AOT => {
                // Also the deck context menu's "Deck always on top" checkbox,
                // for the same reason as "show_app" above.
                let Some(state) = app.try_state::<AppState>() else {
                    return;
                };
                let current = *state.deck_always_on_top.lock_or_recover();
                let target = !current;
                match persist_deck_always_on_top(&state, target) {
                    Ok(()) => {
                        if let Some(w) = app.get_webview_window(DECK_WINDOW) {
                            let _ = w.set_always_on_top(target);
                        }
                        *state.deck_always_on_top.lock_or_recover() = target;
                        let _ = deck_aot_cb.set_checked(target);
                    }
                    Err(e) => {
                        eprintln!("deck always-on-top: persist failed, not applying: {e}");
                        // Nothing changed on disk or on the window — force the
                        // checkbox back to that same unchanged value, in case
                        // the native widget already flipped itself on click.
                        let _ = deck_aot_cb.set_checked(current);
                    }
                }
            }
            MENU_ID_RECONNECT => {
                // Onboarding lives on the app surface, so the re-onboard event
                // and the window that has to be looking at it are the same one.
                let _ = app.emit_to(APP_WINDOW, "reonboard", ());
                show_role_window(app, APP_WINDOW);
            }
            MENU_ID_CHECK_UPDATE => {
                // The actual check runs in the WebView (`updateClient.ts`, via
                // `update_check`/`update_install`) — this just asks App.svelte
                // to run it and brings the app window forward so the result
                // (available / up to date / failed) is never rendered into a
                // hidden window. Unlike the silent mount-time check, a
                // user-requested one must report every outcome, including
                // failure — see App.svelte's "check-for-updates" listener.
                let _ = app.emit_to(APP_WINDOW, "check-for-updates", ());
                show_role_window(app, APP_WINDOW);
            }
            MENU_ID_AUTOSTART => {
                let mgr = app.autolaunch();
                let now = mgr.is_enabled().unwrap_or(false);
                let res = if now { mgr.disable() } else { mgr.enable() };
                if let Err(e) = res {
                    eprintln!("autostart toggle failed: {e}");
                }
                let _ = autostart_cb.set_checked(mgr.is_enabled().unwrap_or(false));
            }
            MENU_ID_QUIT => app.exit(0),
            // The deck's own right-click context menu (`build_deck_context_menu`).
            // MENU_ID_HIDE_DECK is new; MENU_ID_DECK_AOT and MENU_ID_SHOW_APP
            // above already cover the context menu's checkbox and open-app
            // items because those items are built with the SAME ids.
            MENU_ID_HIDE_DECK => hide_role_window(app, DECK_WINDOW),
            // Catch-all: anything left is either one of the three zoom ids
            // (routed through the SAME mapper `zoom_command_for_menu_id`
            // tests pin down) or truly unknown, in which case it is a no-op —
            // one arm instead of listing the zoom ids again here too.
            id => {
                if let Some(cmd) = zoom_command_for_menu_id(id) {
                    let _ = app.emit_to(DECK_WINDOW, FLOATING_ZOOM_EVENT, cmd);
                }
            }
        });

    // Reuse the embedded app icon for the tray (skip gracefully if absent).
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}
