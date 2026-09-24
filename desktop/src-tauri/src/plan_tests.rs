//! Unit tests for the shell glue (runtime plan, notifications, window
//! placement, tray labels), moved out of the inline `plan_tests` module.

use super::*;
use crate::banner_native::*;
use crate::notifications::*;
use crate::notify_pump::*;
use crate::tray::*;
use crate::window_roles::*;

#[test]
fn health_carries_the_shell_version_for_mismatch_warnings() {
    let health = serde_json::json!({"ok": true, "version": "1.2.3"});
    let stamped = with_app_version(health, "1.2.4");
    assert_eq!(stamped["app_version"], "1.2.4");
    assert_eq!(stamped["version"], "1.2.3");
    assert_eq!(with_app_version(serde_json::json!(null), "1"), serde_json::json!(null));
}

#[test]
fn notification_generation_change_delivers_low_sequence_item() {
    let cursor = NotifyCursor {
        generation: Some("old".into()),
        seq: 10,
    };
    let state = serde_json::json!({
        "generation": "new",
        "seq": 1,
        "acked_seq": 0,
        "items": [{
            "id": "new:1", "seq": 1, "title": "done", "body": "p1",
            "sound": "Hero", "created_at_ms": 1
        }]
    });
    let (generation, _, items) = notification_batch(&state, &cursor).unwrap();
    assert_eq!(generation, "new");
    assert_eq!(items.len(), 1);
    assert_eq!(items[0].seq, 1);
}

#[test]
fn notification_item_carries_its_banner_icon() {
    let state = serde_json::json!({
        "generation": "g",
        "seq": 2,
        "acked_seq": 0,
        "items": [
            {"id": "g:1", "seq": 1, "title": "done", "body": "p1",
             "icon": "/c/notification-icons/v1-p-ab12.png"},
            {"id": "g:2", "seq": 2, "title": "done", "body": "p2", "icon": null}
        ]
    });
    let (_, _, items) = notification_batch(&state, &NotifyCursor::default()).unwrap();
    assert_eq!(
        items[0].icon.as_deref(),
        Some("/c/notification-icons/v1-p-ab12.png")
    );
    assert_eq!(items[1].icon, None);
}

#[test]
fn banner_image_path_accepts_only_runtime_icon_files() {
    let ok = "/Users/a/.cache/herdeck/notification-icons/v1-m-0f3a.png";
    assert_eq!(banner_image_path(ok), Some(ok));
    for bad in [
        "notification-icons/v1-p-ab.png",                 // relative
        "/Users/a/.ssh/id_ed25519",                       // not a png
        "/Users/a/Pictures/v1-p-ab.png",                  // wrong directory
        "/c/notification-icons/../../etc/v1.png",         // parent is not the dir
        "/c/notification-icons/V1 P.png",                 // outside [a-z0-9-]
        "/c/notification-icons/.png",                     // empty stem
        "",
    ] {
        assert_eq!(banner_image_path(bad), None, "{bad}");
    }
}

#[test]
fn notification_cursor_filters_an_already_delivered_item() {
    let cursor = NotifyCursor {
        generation: Some("same".into()),
        seq: 1,
    };
    let state = serde_json::json!({
        "generation": "same",
        "seq": 1,
        "acked_seq": 0,
        "items": [{"id": "same:1", "seq": 1, "title": "done", "body": "p1"}]
    });
    let (_, _, items) = notification_batch(&state, &cursor).unwrap();
    assert!(items.is_empty());
}

fn discovery_on(port: u16) -> Discovery {
    Discovery {
        url: format!("http://127.0.0.1:{port}"),
        host: "127.0.0.1".into(),
        port,
        token: "t".into(),
        source: "mock".into(),
    }
}

// A bare `continue` on either of these spun the pump thread at 100% CPU
// for as long as the sidecar had not reported in.
#[test]
fn the_pump_idles_instead_of_spinning_without_permission_or_discovery() {
    assert_eq!(notify_target(true, None), Err(NOTIFY_RETRY_DELAY));
    assert_eq!(notify_target(false, Some(discovery_on(1))), Err(NOTIFY_RETRY_DELAY));
    assert_eq!(notify_target(false, None), Err(NOTIFY_RETRY_DELAY));
    assert!(NOTIFY_RETRY_DELAY > Duration::ZERO);
    assert_eq!(notify_target(true, Some(discovery_on(1))), Ok(discovery_on(1)));
}

#[test]
fn a_feedless_source_is_unsupported_not_a_failure() {
    assert_eq!(
        classify_notify_poll(Ok((200, "{}".into()))),
        NotifyPoll::Deliver("{}".into())
    );
    assert_eq!(classify_notify_poll(Ok((404, String::new()))), NotifyPoll::Unsupported);
    assert_eq!(classify_notify_poll(Ok((409, String::new()))), NotifyPoll::Failed);
    assert_eq!(classify_notify_poll(Err("down".into())), NotifyPoll::Failed);
    assert!(NOTIFY_UNSUPPORTED_BACKOFF >= Duration::from_secs(30));
}

#[test]
fn the_unsupported_backoff_wakes_when_discovery_changes() {
    let start = std::time::Instant::now();
    assert!(backoff_until(&SystemClock, Duration::from_secs(30), || true));
    assert!(start.elapsed() < Duration::from_secs(1));
    // And it does wait out the full period when nothing changes.
    let start = std::time::Instant::now();
    assert!(!backoff_until(&SystemClock, Duration::from_millis(30), || false));
    assert!(start.elapsed() >= Duration::from_millis(30));
    assert_ne!(discovery_key(&discovery_on(1)), discovery_key(&discovery_on(2)));
}

fn sound_scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("herdeck-sounds-{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn sound_names_are_sorted_unique_playable_stems() {
    let user = sound_scratch("list-user");
    let system = sound_scratch("list-system");
    for f in ["Glass.aiff", "Zap.wav", "notes.txt", "bad.name.aiff", "Ping.caf"] {
        std::fs::write(user.join(f), b"").unwrap();
    }
    for f in ["Glass.aiff", "Basso.aiff"] {
        std::fs::write(system.join(f), b"").unwrap();
    }
    std::fs::create_dir_all(user.join("Folder.aiff")).unwrap();
    let names = list_sound_names(&[user, system, PathBuf::from("/nonexistent-herdeck")]);
    assert_eq!(names, vec!["Basso", "Glass", "Ping", "Zap"]);
}

// Every listed sound must be one playback can find: a user sound from
// ~/Library/Sounds used to be listed-but-unplayable (only the system folder
// and .aiff were searched).
#[test]
fn sound_resolution_searches_the_user_folder_first_and_any_extension() {
    let user = sound_scratch("resolve-user");
    let system = sound_scratch("resolve-system");
    std::fs::write(user.join("Glass.m4a"), b"").unwrap();
    std::fs::write(system.join("Glass.aiff"), b"").unwrap();
    std::fs::write(system.join("Basso.aiff"), b"").unwrap();
    let dirs = [user.clone(), system.clone()];
    assert_eq!(resolve_sound_path("Glass", &dirs), Some(user.join("Glass.m4a")));
    assert_eq!(resolve_sound_path("Basso", &dirs), Some(system.join("Basso.aiff")));
    assert_eq!(resolve_sound_path("Missing", &dirs), None);
    assert_eq!(resolve_sound_path("../Basso", &dirs), None);
}

// The banner carries its own sound (no separate afplay): silence stays
// silence, and only a name the OS can find is attached.
#[test]
fn banner_sound_is_attached_only_for_a_findable_name() {
    use serde_json::json;
    let dir = sound_scratch("banner");
    std::fs::write(dir.join("Hero.aiff"), b"").unwrap();
    std::fs::write(dir.join("Glass.aiff"), b"").unwrap();
    let dirs = [dir];
    assert_eq!(banner_sound_name(&json!("Hero"), &dirs), Ok(Some("Hero".into())));
    assert_eq!(
        banner_sound_name(&json!(true), &dirs),
        Ok(Some(DEFAULT_NOTIFICATION_SOUND.into()))
    );
    assert_eq!(banner_sound_name(&json!(""), &dirs), Ok(None));
    assert_eq!(banner_sound_name(&json!(false), &dirs), Ok(None));
    assert!(banner_sound_name(&json!("Missing"), &dirs).is_err());
    assert!(banner_sound_name(&json!("../Hero"), &dirs).is_err());
}

#[test]
fn banners_present_while_frontmost_unless_the_inner_delegate_says_no() {
    assert!(should_present_banner(None));
    assert!(should_present_banner(Some(true)));
    assert!(!should_present_banner(Some(false)));
}

#[test]
fn a_feed_item_carries_its_agent_and_answers_into_the_batch() {
    use serde_json::json;
    let state = json!({"generation": "g", "acked_seq": 0, "items": [
        {"seq": 1, "id": "g:1", "title": "t", "body": "b", "sound": false,
         "agent": {"server_id": "prod", "pane_id": "p1"}, "event": "blocked",
         "episode": "ep", "sig": "s", "actions": [{"id": "approve", "label": "Approve"}]},
    ]});
    let (_, _, items) = notification_batch(&state, &NotifyCursor::default()).unwrap();
    let meta = &items[0].meta;
    assert_eq!(meta.agent.as_ref().map(|a| a.pane_id.as_str()), Some("p1"));
    assert_eq!(meta.actions.len(), 1);
    assert_eq!(banners::banner_identifier(&items[0].generation, items[0].seq), "herdeck:g:1");
}

#[test]
fn requested_sound_maps_true_to_the_default_and_false_to_silence() {
    use serde_json::json;
    assert_eq!(requested_sound_name(&json!(true)), Some(DEFAULT_NOTIFICATION_SOUND));
    assert_eq!(requested_sound_name(&json!("Hero")), Some("Hero"));
    assert_eq!(requested_sound_name(&json!(false)), None);
    assert_eq!(requested_sound_name(&json!("")), None);
    assert_eq!(requested_sound_name(&json!(null)), None);
}

#[test]
fn test_notification_text_exists_in_both_languages() {
    let (en_t, en_b) = test_notification_texts("en");
    let (cs_t, cs_b) = test_notification_texts("cs");
    assert!(!en_t.is_empty() && !en_b.is_empty());
    assert_ne!(en_t, cs_t);
    assert_ne!(en_b, cs_b);
    // Unknown languages fall back to English.
    assert_eq!(test_notification_texts("de"), (en_t, en_b));
}

#[test]
fn a_long_poll_state_request_outlasts_its_wait() {
    assert_eq!(state_request_timeout(None, None), SIDECAR_TIMEOUT);
    assert_eq!(state_request_timeout(None, Some(20_000)), SIDECAR_TIMEOUT);
    // C2: read timeout must exceed wait_ms + 5 s.
    assert!(state_request_timeout(Some(3), Some(20_000)) > Duration::from_millis(25_000));
    // The runtime clamps to 25 s; so does the timeout, instead of hanging on.
    assert_eq!(
        state_request_timeout(Some(3), Some(600_000)),
        state_request_timeout(Some(3), Some(25_000))
    );
    assert!(state_request_timeout(Some(3), Some(0)) >= SIDECAR_TIMEOUT);
}

#[test]
fn state_peek_validates_and_reads_the_blocked_count() {
    let peek = peek_state(r#"{"version":3,"summary":{"blocked":2,"working":1},"tiles":{}}"#)
        .unwrap();
    assert_eq!(peek.blocked(), Some(2));
    assert_eq!(peek_state(r#"{"version":3}"#).unwrap().blocked(), None);
    assert!(peek_state("{\"version\":").is_err());
    assert!(peek_state("not json").is_err());
}

#[test]
fn tray_tooltip_names_blocked_agents_in_both_languages() {
    assert_eq!(tray_tooltip("en", "Herdeck", None), "Herdeck");
    assert_eq!(tray_tooltip("en", "Herdeck", Some(0)), "Herdeck");
    assert_eq!(tray_tooltip("en", "Herdeck", Some(3)), "Herdeck — 3 blocked");
    assert_eq!(tray_tooltip("cs", "Herdeck", Some(3)), "Herdeck — zablokováno: 3");
    assert_ne!(tray_blocked_label("en", 1), tray_blocked_label("cs", 1));
}

#[test]
fn tray_left_click_opens_the_menu_on_macos() {
    assert_eq!(tray_menu_on_left_click(), cfg!(target_os = "macos"));
}

#[test]
fn the_image_scheme_only_proxies_tiles_and_the_panel() {
    assert_eq!(image_proxy_path("/panel").as_deref(), Some("/panel"));
    assert_eq!(image_proxy_path("/tile/0").as_deref(), Some("/tile/0"));
    assert_eq!(image_proxy_path("/tile/14").as_deref(), Some("/tile/14"));
    assert_eq!(image_proxy_path("/tile/007").as_deref(), Some("/tile/7"));
    for bad in ["/", "/state", "/config", "/tile/", "/tile/-1", "/tile/1/x", "/tile/99999", "/panel/x", "/tile/1%2F"] {
        assert_eq!(image_proxy_path(bad), None, "{bad} must not be proxied");
    }
}

#[test]
fn image_responses_carry_type_and_cache_policy() {
    let ok = image_response(200, vec![1, 2]);
    assert_eq!(ok.status(), 200);
    assert_eq!(ok.headers()["Content-Type"], "image/png");
    assert_eq!(ok.headers()["Cache-Control"], "no-cache");
    assert_eq!(ok.body(), &vec![1, 2]);
    let missing = image_response(404, Vec::new());
    assert_eq!(missing.status(), 404);
    assert_eq!(missing.headers()["Cache-Control"], "no-store");
}

#[test]
fn main_window_capability_allows_compact_window_control() {
    let capability: serde_json::Value = serde_json::from_str(include_str!(
        "../capabilities/default.json"
    ))
    .expect("default capability must be valid JSON");
    let permissions = capability["permissions"]
        .as_array()
        .expect("default capability must declare permissions");

    assert!(permissions.iter().any(|permission| {
        permission.as_str() == Some("core:window:allow-start-dragging")
    }));
    assert!(permissions.iter().any(|permission| {
        permission.as_str() == Some("core:window:allow-set-position")
    }));
}

// Placement geometry. The deck is pinned to the top-right of the monitor's
// USABLE area — the screen rect would put it under the macOS menu bar.
#[test]
fn floating_deck_sits_inside_the_work_area_not_the_screen() {
    // A 1920x1080 display whose top 37 points belong to the menu bar.
    let (x, y) = floating_origin((0.0, 37.0), (1920.0, 1043.0), (328.0, 300.0), 16.0);
    assert_eq!((x, y), (1576.0, 53.0));
}

// The whole point of following the pointer: a monitor left of the primary one
// has a NEGATIVE origin, and placement must be relative to it.
#[test]
fn floating_deck_places_relative_to_its_own_monitor_origin() {
    let (x, y) = floating_origin((-1920.0, 240.0), (1920.0, 1080.0), (328.0, 300.0), 16.0);
    assert_eq!((x, y), (-344.0, 256.0));
}

// A deck zoomed past a small external display must still land ON it: an
// off-screen borderless window has no titlebar to drag it back with.
#[test]
fn floating_deck_never_starts_off_the_screen_it_is_placed_on() {
    let (x, y) = floating_origin((100.0, 50.0), (300.0, 200.0), (328.0, 300.0), 16.0);
    assert_eq!((x, y), (100.0, 50.0));
}

// A Retina built-in beside a 1x external is the setup that breaks if the
// cursor reading is handed to the lookup untouched: tao scales the logical
// point by the PRIMARY monitor's factor, but the lookup wants logical.
#[test]
fn pointer_lookup_undoes_the_primary_monitors_scaling() {
    // Pointer at logical (1000, 400) on a 2x primary reads back as (2000, 800).
    assert_eq!(cursor_in_lookup_space((2000.0, 800.0), 2.0), (1000.0, 400.0));
}

#[test]
fn pointer_lookup_leaves_an_unscaled_reading_alone() {
    assert_eq!(cursor_in_lookup_space((3000.0, 120.0), 1.0), (3000.0, 120.0));
}

// A monitor tao could not inspect reports 0; dividing by it would hand the
// lookup an infinity, and `usable_scale` guards every other divisor too.
#[test]
fn a_nonsense_scale_factor_never_produces_an_infinity() {
    let (x, y) = cursor_in_lookup_space((640.0, 480.0), 0.0);
    assert_eq!((x, y), (640.0, 480.0));
    assert_eq!(usable_scale(-2.0), 1.0);
    assert_eq!(usable_scale(2.0), 2.0);
}

// Wayland hands back a hard (0, 0) instead of an error, so a believed
// pointer reading would pin the deck to whatever monitor owns the origin —
// worse than the current_monitor() fallback it would be overriding. Detection
// is asserted on every host so the shipped Linux behaviour is not only
// covered by the one CI runner that compiles this branch.
#[test]
fn a_wayland_session_is_recognised_by_either_signal() {
    assert!(is_wayland_session(Some("wayland"), None, None));
    assert!(is_wayland_session(Some("Wayland"), None, None));
    assert!(is_wayland_session(None, Some("wayland-0"), None));
    assert!(is_wayland_session(Some("x11"), Some("wayland-0"), None));
}

#[test]
fn an_x11_session_is_not_mistaken_for_wayland() {
    assert!(!is_wayland_session(Some("x11"), None, None));
    assert!(!is_wayland_session(None, None, None));
    // Exported-but-empty is how a cleared variable survives in a session env.
    assert!(!is_wayland_session(Some("x11"), Some(""), None));
}

// GDK_BACKEND=x11 in a Wayland session is the standard WebKitGTK workaround:
// WAYLAND_DISPLAY stays exported, but GDK — and so tao — really is on X11 and
// reports a usable pointer. Believing the socket there would discard it.
#[test]
fn a_forced_backend_outranks_the_session_variables() {
    // The workaround this exists for: x11 forced in a Wayland session.
    assert!(!is_wayland_session(
        Some("wayland"),
        Some("wayland-0"),
        Some("x11")
    ));
    assert!(is_wayland_session(Some("wayland"), None, Some("wayland")));
    assert!(!is_wayland_session(None, None, Some(" x11 ")));
}

// A list that forbids x11 leaves nothing to fall through to, so it decides
// alone — wl_display_connect(NULL) finds wayland-0 with WAYLAND_DISPLAY
// unset, and believing the session variables there would trust a (0, 0).
// The rule is which backends are named, not how many entries there are.
#[test]
fn a_list_that_forbids_x11_settles_it() {
    assert!(is_wayland_session(None, None, Some("wayland")));
    assert!(is_wayland_session(Some("x11"), None, Some("wayland")));
    assert!(is_wayland_session(Some("x11"), None, Some("wayland,broadway")));
}

// A list naming BOTH permits backends without naming the one that connected,
// so there it filters the session signals instead of replacing them. Reading
// entry zero as the answer got both orders wrong, in opposite directions.
#[test]
fn a_backend_list_naming_both_filters_rather_than_decides() {
    // Belt-and-braces value in an X11 session: wayland is permitted but the
    // session is not offering it, so the real pointer must survive.
    assert!(!is_wayland_session(Some("x11"), None, Some("wayland,x11")));
    // Wayland session with no Xwayland: x11 is preferred but unreachable, so
    // GDK falls through to wayland and the (0, 0) reading must not be believed.
    assert!(is_wayland_session(
        Some("wayland"),
        Some("wayland-0"),
        Some("x11,wayland")
    ));
}

#[test]
fn an_unusable_backend_value_leaves_the_session_signals_standing() {
    // Exported-but-empty, whitespace, and a name we do not recognise are all
    // "no filter" — not a definitive "this is not Wayland".
    assert!(is_wayland_session(Some("wayland"), None, Some("")));
    assert!(is_wayland_session(None, Some("wayland-0"), Some("  ")));
    assert!(is_wayland_session(Some("wayland"), None, Some("gdk")));
    // And the other direction: an unusable value must not manufacture one.
    assert!(!is_wayland_session(Some("x11"), None, Some("gdk")));
    assert!(!is_wayland_session(None, None, Some("")));
}

#[test]
#[cfg(all(unix, not(target_os = "macos")))]
fn the_gtk_backend_drops_the_pointer_reading_under_wayland() {
    assert!(!pointer_is_locatable(Some("wayland"), None, None));
    assert!(pointer_is_locatable(Some("x11"), None, None));
}

// The variables mean nothing off the GTK backend, and a stray export must
// not cost a macOS user the pointer preference this path exists for.
#[test]
#[cfg(not(all(unix, not(target_os = "macos"))))]
fn wayland_variables_are_ignored_off_the_gtk_backend() {
    assert!(pointer_is_locatable(
        Some("wayland"),
        Some("wayland-0"),
        None
    ));
}

// The space decision is what broke once already, and it is the one part that
// cannot be reached from a test on this host — so it takes the platform as an
// argument and both shapes are asserted everywhere.
#[test]
fn logical_placement_divides_by_each_factor_and_keeps_the_margin() {
    let p = placement_units(false, 2.0, 2.0);
    assert_eq!((p.screen_div, p.window_div, p.margin), (2.0, 2.0, 16.0));
    // A window still on a 1x display, moving to a 2x one.
    let mixed = placement_units(false, 2.0, 1.0);
    assert_eq!((mixed.screen_div, mixed.window_div), (2.0, 1.0));
    // A monitor tao could not inspect must not become a divide by zero.
    let broken = placement_units(false, 0.0, -1.0);
    assert_eq!((broken.screen_div, broken.window_div), (1.0, 1.0));
}

#[test]
fn physical_placement_leaves_the_screen_rect_and_scales_the_margin() {
    // Windows measures coordinates in one global physical space, so the
    // screen rect is untouched — but a fixed 16 would be 8 points at 200%.
    let p = placement_units(true, 2.0, 2.0);
    assert_eq!((p.screen_div, p.window_div, p.margin), (1.0, 1.0, 32.0));
    assert_eq!(placement_units(true, 0.0, 0.0).margin, 16.0);
}

// Sizes are not DPI-invariant anywhere: a window measured on a 1x display
// occupies twice the pixels once the OS rescales it onto a 2x one, and
// anchoring to the right edge with the old width misses by half a deck.
#[test]
fn a_window_is_measured_in_the_pixels_of_the_monitor_it_moves_to() {
    assert_eq!(placement_units(true, 2.0, 1.0).window_div, 0.5);
    assert_eq!(placement_units(true, 1.0, 2.0).window_div, 2.0);
    assert_eq!(placement_units(true, 2.0, 2.0).window_div, 1.0);
    assert_eq!(placement_units(true, 0.0, 0.0).window_div, 1.0);
}

#[test]
fn placement_hands_set_position_the_space_it_was_measured_in() {
    assert!(matches!(
        placement_position(false, 100.5, -20.5),
        tauri::Position::Logical(p) if p.x == 100.5 && p.y == -20.5
    ));
    assert!(matches!(
        placement_position(true, 100.6, -20.4),
        tauri::Position::Physical(p) if p.x == 101 && p.y == -20
    ));
}

// The labels read backwards — `main` is the DECK — so the mapping from a
// label to the flag it owns is worth pinning down rather than eyeballing.
#[test]
fn visibility_is_recorded_against_the_role_the_label_names() {
    let mut s = window_state::WindowState::default();
    assert_eq!((s.app_visible, s.deck_visible), (true, false));
    set_role_visible(&mut s, DECK_WINDOW, true);
    assert_eq!((s.app_visible, s.deck_visible), (true, true));
    set_role_visible(&mut s, APP_WINDOW, false);
    assert_eq!((s.app_visible, s.deck_visible), (false, true));
    // A label that owns neither flag must change nothing, rather than be
    // quietly filed under "the app".
    set_role_visible(&mut s, "somewhere-else", true);
    assert_eq!((s.app_visible, s.deck_visible), (false, true));
}

// The role is read by the frontend before first paint; a typo here is a
// window that silently renders the other surface.
#[test]
fn the_role_script_sets_the_attribute_the_frontend_reads() {
    assert_eq!(
        window_role_script("deck"),
        "document.documentElement.dataset.windowRole='deck'"
    );
}

// The single-coordinate half of the same space decision, and unreachable
// from a test on this host for the same reason — so it too takes the
// platform as an argument and both shapes are asserted everywhere.
#[test]
fn a_coordinate_is_divided_off_windows_and_left_alone_on_it() {
    assert_eq!(placement_divisor(false, 2.0), 2.0);
    assert_eq!(placement_divisor(true, 2.0), 1.0);
    // A monitor tao could not inspect must not become a divide by zero.
    assert_eq!(placement_divisor(false, 0.0), 1.0);
    assert_eq!(placement_divisor(false, -1.0), 1.0);
}

// A deck the user dragged somewhere deliberate comes back there — but only
// if "there" still exists. The numbers are a real two-display desk: a Retina
// built-in and an external one up and to the left of it.
#[test]
fn a_remembered_position_off_every_monitor_is_rejected() {
    let monitors = [((0, 0), (2514u32, 1410u32)), ((-1343, -1050), (1680, 1050))];
    assert!(position_is_on_any(&monitors, (2170, 46)));
    assert!(position_is_on_any(&monitors, (-1000, -900)));
    // The unplugged display: remembered, but nothing is there any more.
    assert!(!position_is_on_any(&monitors, (4000, 300)));
    // Off in Y alone is off as well — the same two displays restacked
    // vertically leave every X in range and no Y anywhere near it.
    assert!(!position_is_on_any(&monitors, (2170, 5000)));
    // Exactly on the far edge is off: a window placed there is invisible.
    assert!(!position_is_on_any(&monitors, (2514, 0)));
}

// Monitor choice. `pick_monitor` is generic, so a &str stands in for a Monitor.
#[test]
fn monitor_choice_prefers_the_pointers_screen() {
    let picked = pick_monitor(
        Some((-800.0, 400.0)),
        |_, _| Some("cursor"),
        || panic!("consulted the window's monitor despite a located pointer"),
        || panic!("fell back to primary despite a located pointer"),
    );
    assert_eq!(picked, Some("cursor"));
}

#[test]
fn monitor_choice_falls_back_when_the_pointer_is_on_no_display() {
    let picked = pick_monitor(
        Some((99_999.0, 99_999.0)),
        |_, _| None,
        || Some("current"),
        || panic!("skipped the window's own monitor"),
    );
    assert_eq!(picked, Some("current"));
}

#[test]
fn monitor_choice_skips_the_point_lookup_without_a_pointer() {
    let picked = pick_monitor(
        None,
        |_, _| panic!("looked up a monitor for a pointer that was never located"),
        || Some("current"),
        || panic!("skipped the window's own monitor"),
    );
    assert_eq!(picked, Some("current"));
}

#[test]
fn monitor_choice_ends_at_primary() {
    assert_eq!(
        pick_monitor(None, |_, _| None, || None, || Some("primary")),
        Some("primary"),
    );
    assert_eq!(
        pick_monitor::<&str>(None, |_, _| None, || None, || None),
        None,
    );
}

fn stable_runtime() -> Discovery {
    Discovery {
        url: "http://127.0.0.1:8800".to_string(),
        host: "127.0.0.1".to_string(),
        port: 8800,
        token: "stable-token".to_string(),
        source: "live".to_string(),
    }
}

#[test]
fn a_missed_or_unhealthy_runtime_spawns_with_the_reason_logged() {
    let (plan, reason) =
        resolve_automatic_plan("stable", None, Path::new("/repo"), None, |_| true);
    assert!(matches!(plan, SidecarPlan::Spawn(_)));
    assert_eq!(reason, "no_runtime_json");
    let (plan, reason) = resolve_automatic_plan(
        "stable",
        None,
        Path::new("/repo"),
        Some(stable_runtime()),
        |_| false,
    );
    assert!(matches!(plan, SidecarPlan::Spawn(_)));
    assert_eq!(reason, "runtime_unhealthy");
}

#[test]
fn plan_log_line_names_plan_reason_and_url_but_never_the_token() {
    let d = stable_runtime();
    let line = plan_log_line("attach", "launchd_runtime_appeared", Some(&d.url));
    assert_eq!(
        line,
        "herdeck: runtime plan=attach reason=launchd_runtime_appeared url=http://127.0.0.1:8800"
    );
    assert!(!line.contains(&d.token));
    assert_eq!(
        plan_log_line("spawn", "runtime_unhealthy", None),
        "herdeck: runtime plan=spawn reason=runtime_unhealthy"
    );
}

#[test]
fn an_attached_runtime_counts_as_lost_only_after_three_failures_and_30s() {
    let t0 = std::time::Instant::now();
    let mut loss = AttachLoss::default();
    // Three quick failures (a runtime restarting) are not a loss.
    assert!(!loss.record_failure(t0));
    assert!(!loss.record_failure(t0 + Duration::from_secs(5)));
    assert!(!loss.record_failure(t0 + Duration::from_secs(10)));
    // Nor is a long gap with too few failures.
    let mut sparse = AttachLoss::default();
    assert!(!sparse.record_failure(t0));
    assert!(!sparse.record_failure(t0 + Duration::from_secs(40)));
    // Both thresholds met -> lost.
    assert!(!loss.record_failure(t0 + Duration::from_secs(29)));
    assert!(loss.record_failure(t0 + ATTACH_LOST_AFTER));
    // Any success restarts the clock: no flapping faster than the window.
    loss.record_ok();
    let t1 = t0 + Duration::from_secs(60);
    for i in 0..10 {
        assert!(!loss.record_failure(t1 + Duration::from_secs(i)));
    }
    assert!(loss.record_failure(t1 + ATTACH_LOST_AFTER));
}

#[test]
fn a_self_spawned_shell_reattaches_only_to_a_different_healthy_runtime() {
    let own = discovery_on(51000);
    let launchd = stable_runtime();
    // Healthy launchd runtime appeared -> switch.
    assert_eq!(
        reattach_target(true, Some(&own), Some(launchd.clone()), |_| true),
        Some(launchd.clone())
    );
    // Not (yet) healthy, or no runtime.json -> stay on our sidecar.
    assert!(reattach_target(true, Some(&own), Some(launchd.clone()), |_| false).is_none());
    assert!(reattach_target(true, Some(&own), None, |_| true).is_none());
    // Already on it -> nothing to do (and no probe).
    assert!(reattach_target(true, Some(&launchd), Some(launchd.clone()), |_| panic!(
        "probed the runtime we are already on"
    ))
    .is_none());
    // Env override / attached / dev channel -> never repointed.
    assert!(reattach_target(false, Some(&own), Some(launchd), |_| true).is_none());
}

#[test]
fn dev_plan_spawns_instead_of_attaching_a_healthy_stable_runtime() {
    let plan = resolve_automatic_plan(
        build_channel::DEV_CHANNEL,
        None,
        Path::new("/repo"),
        Some(stable_runtime()),
        |_| true,
    );

    assert_eq!(plan.1, "attach_disabled_for_channel");
    match plan.0 {
        SidecarPlan::Spawn(spec) => {
            assert!(spec.program.ends_with("/.venv/bin/python"));
        }
        SidecarPlan::External(..) => panic!("dev build attached the stable runtime"),
    }
}

#[test]
fn stable_plan_attaches_with_the_runtime_json_flag_set() {
    let plan = resolve_automatic_plan(
        "stable",
        None,
        Path::new("/repo"),
        Some(stable_runtime()),
        |_| true,
    );

    assert_eq!(plan.1, "runtime_json_healthy");
    match plan.0 {
        // The bool is the whole re-discovery safety gate: only this path
        // may ever be repointed by a re-read runtime.json (review LOW — a
        // polarity regression here would reintroduce the hijack).
        SidecarPlan::External(_, true) => {}
        _ => panic!("stable build must attach from runtime.json with flag=true"),
    }
}

// Task 4: the tray's window-mode picker is gone, replaced by a single
// deck-visibility toggle and an always-on-top checkbox. One label per menu
// item now, not per mode.
#[test]
fn every_tray_label_exists_in_both_languages() {
    let en = tray_labels("en");
    let cs = tray_labels("cs");
    assert_eq!(en.len(), cs.len());
    assert!(en.iter().all(|l| !l.is_empty()));
    assert!(cs.iter().all(|l| !l.is_empty()));
    // The mode radio items are gone; nothing may name them any more.
    assert!(!cs.iter().any(|l| l.contains("Režim okna")));
    // show_app + toggle_deck(show) + toggle_deck(hide) + deck_aot +
    // autostart + reconnect + check_update + quit — `TrayMenuItems::
    // retitle` indexes every one of these, so the array length must
    // match exactly.
    assert_eq!(en.len(), 8);
    // A label accidentally left English in the cs array (or vice versa)
    // is exactly the defect this test exists to catch.
    for (i, (e, c)) in en.iter().zip(cs.iter()).enumerate() {
        assert_ne!(e, c, "tray_labels[{i}] is identical in en and cs");
    }
}

// The new "Check for updates" tray item (slot 6, just above "Quit" — see
// `build_tray`/`TrayMenuItems::retitle`), pinned by exact text rather than
// just "non-empty and differs from cs", which the generic parity test
// above already covers: a future edit that renames it in one language
// only, or that shuffles it to the wrong slot, must fail HERE.
#[test]
fn check_update_tray_label_exists_in_both_languages() {
    assert_eq!(tray_labels("en")[6], "Check for updates");
    assert_eq!(tray_labels("cs")[6], "Zkontrolovat aktualizace");
}

// The `toggle_deck` tray item's text depends on BOTH the language and the
// deck's current visibility. Pulled out of the (untestable-without-a-tray)
// retitle logic into a pure function so that interaction has a test at all.
#[test]
fn toggle_deck_label_reflects_visibility_in_both_languages() {
    assert_eq!(toggle_deck_label("en", false), "Show deck");
    assert_eq!(toggle_deck_label("en", true), "Hide deck");
    assert_eq!(toggle_deck_label("cs", false), "Zobrazit deck");
    assert_eq!(toggle_deck_label("cs", true), "Skrýt deck");
}

// The label follows the DECK's visibility and nothing else. Showing the app
// window (from the tray, or from re-onboarding) must leave "Show deck"
// alone; syncing on every window would make the item name the wrong gesture
// just as surely as never syncing it does.
#[test]
fn only_the_deck_window_refreshes_the_deck_tray_label() {
    assert_eq!(deck_label_refresh(DECK_WINDOW, true), Some(true));
    assert_eq!(deck_label_refresh(DECK_WINDOW, false), Some(false));
    assert_eq!(deck_label_refresh(APP_WINDOW, true), None);
    assert_eq!(deck_label_refresh("some-future-window", false), None);
}

// A config that cannot be READ is not a config that says false: applying
// false there would unfloat the deck, uncheck the tray box and rewrite the
// cached flag while config.toml still said true.
#[test]
fn an_unreadable_config_leaves_the_live_always_on_top_alone() {
    let dir = std::env::temp_dir().join("herdeck-aot-target");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();

    assert_eq!(deck_always_on_top_target(&dir.join("absent.toml")), None);
    // A directory is readable-but-not-a-file: still no value to apply.
    assert_eq!(deck_always_on_top_target(&dir), None);

    let empty = dir.join("empty.toml");
    std::fs::write(&empty, "").unwrap();
    assert_eq!(deck_always_on_top_target(&empty), Some(false));

    let on = dir.join("on.toml");
    std::fs::write(&on, "[desktop]\ndeck_always_on_top = true\n").unwrap();
    assert_eq!(deck_always_on_top_target(&on), Some(true));

    // And it resolves, not just parses: the legacy migration fallback
    // applies to a live reload exactly as it does at startup.
    let legacy = dir.join("legacy.toml");
    std::fs::write(&legacy, "[desktop]\nwindow_mode = \"always_on_top\"\n").unwrap();
    assert_eq!(deck_always_on_top_target(&legacy), Some(true));
}

// The deck's right-click context menu shares three of its six items with
// the tray: this pins down that they are, byte-for-byte, the SAME string —
// not just "close enough" translations typed twice.
#[test]
fn deck_context_menu_reuses_the_trays_hide_aot_and_open_labels() {
    for lang in ["en", "cs"] {
        let tray = tray_labels(lang);
        let ctx = deck_context_menu_texts(lang);
        assert_eq!(ctx[0], tray[2], "hide label diverged from the tray's ({lang})");
        assert_eq!(ctx[4], tray[3], "always-on-top label diverged from the tray's ({lang})");
        assert_eq!(ctx[5], tray[0], "open-app label diverged from the tray's ({lang})");
    }
}

// Mirrors `every_tray_label_exists_in_both_languages`: every context-menu
// text must exist in both languages and actually differ between them (a
// label left English in the cs array, or vice versa, is the bug this
// guards against).
#[test]
fn every_deck_context_menu_text_exists_in_both_languages() {
    let en = deck_context_menu_texts("en");
    let cs = deck_context_menu_texts("cs");
    assert!(en.iter().all(|l| !l.is_empty()));
    assert!(cs.iter().all(|l| !l.is_empty()));
    for (i, (e, c)) in en.iter().zip(cs.iter()).enumerate() {
        assert_ne!(e, c, "deck_context_menu_texts[{i}] is identical in en and cs");
    }
}

// Same shape of guarantee for the three zoom-only labels, on their own —
// `deck_context_menu_texts` already covers them too, but this pins the
// smaller building block down directly.
#[test]
fn every_zoom_label_exists_in_both_languages() {
    let en = deck_context_zoom_labels("en");
    let cs = deck_context_zoom_labels("cs");
    assert!(en.iter().all(|l| !l.is_empty()));
    assert!(cs.iter().all(|l| !l.is_empty()));
    // Element-wise, not just "the arrays as a whole differ somewhere" —
    // the latter would miss a single label left untranslated as long as
    // the other two still differed.
    for (i, (e, c)) in en.iter().zip(cs.iter()).enumerate() {
        assert_ne!(e, c, "deck_context_zoom_labels[{i}] is identical in en and cs");
    }
}

// The id→command mapping the `on_menu_event` match delegates to. Any id
// that is not one of the three zoom items must resolve to `None` rather
// than accidentally firing a zoom on an unrelated click.
#[test]
fn zoom_menu_ids_map_to_the_floating_scale_commands_they_name() {
    assert_eq!(zoom_command_for_menu_id("zoom_in"), Some("in"));
    assert_eq!(zoom_command_for_menu_id("zoom_out"), Some("out"));
    assert_eq!(zoom_command_for_menu_id("zoom_reset"), Some("reset"));
    assert_eq!(zoom_command_for_menu_id("show_app"), None);
    assert_eq!(zoom_command_for_menu_id("hide_deck"), None);
    assert_eq!(zoom_command_for_menu_id("deck_aot"), None);
}
