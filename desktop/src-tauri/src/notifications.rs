//! Notification glue: the settings commands (sound list, test banner,
//! permission), banner sound/image resolution, the shell identity carried by
//! banner claims, and the `AppHandle` adapter that runs `notify_pump`.

use std::env;
use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;

use tauri::Manager;

use crate::banner_native::{post_native_notification, withdraw_banners};
use crate::notify_pump::{self, NotifyCursor, PendingNotification};
use crate::proxy::run_blocking;
use crate::runtime_plan::{note_runtime_ok, rediscover_runtime};
use crate::sidecar::Discovery;
use crate::sync_util::LockExt;
use crate::tray::{TrayHandles, TrayMenuItems};
use crate::{
    banners, http, AppState, SIDECAR_TIMEOUT,
};

/// Per-process shell identity for the banner claim. A fresh shell process
/// carries a new generation, so the runtime can reject a stale shell's
/// fallback request without resetting its acknowledged feed.
pub(crate) fn shell_gen() -> String {
    use std::sync::OnceLock;
    static GEN: OnceLock<String> = OnceLock::new();
    GEN.get_or_init(|| {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        format!("{}-{:x}", std::process::id(), nanos)
    })
    .clone()
}

/// The sound played for `sound = true` (and for a test banner with no sound
/// picked), matching the runtime's osascript fallback.
pub(crate) const DEFAULT_NOTIFICATION_SOUND: &str = "Glass";

/// File extensions a named sound can have. Only used to list and check names:
/// the banner itself carries the bare name (`NSUserNotification.soundName`, and
/// the runtime's osascript `sound name`), which macOS resolves by `NSSound(named:)`.
pub(crate) const SOUND_EXTENSIONS: [&str; 6] = ["aiff", "aif", "caf", "wav", "m4a", "mp3"];

/// Where named sounds live, in `NSSound(named:)`'s own search order: the
/// user's, then the machine's, then the system's. Empty off macOS — there the
/// settings UI falls back to a free-text sound field.
pub(crate) fn sound_dirs() -> Vec<PathBuf> {
    if !cfg!(target_os = "macos") {
        return Vec::new();
    }
    let mut dirs = Vec::new();
    if let Ok(home) = env::var("HOME") {
        if !home.is_empty() {
            dirs.push(PathBuf::from(home).join("Library/Sounds"));
        }
    }
    dirs.push(PathBuf::from("/Library/Sounds"));
    dirs.push(PathBuf::from("/System/Library/Sounds"));
    dirs
}

/// A sound name safe to put on a banner / hand to the runtime: letters, digits, space,
/// `_` and `-` — no path separators, no dots, nothing to escape.
pub(crate) fn valid_sound_name(name: &str) -> bool {
    !name.is_empty()
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, ' ' | '_' | '-'))
}

pub(crate) fn sound_stem(path: &Path) -> Option<String> {
    let ext = path.extension()?.to_str()?.to_ascii_lowercase();
    if !SOUND_EXTENSIONS.contains(&ext.as_str()) {
        return None;
    }
    let stem = path.file_stem()?.to_str()?;
    valid_sound_name(stem).then(|| stem.to_string())
}

/// Every playable sound name found in `dirs`, sorted and de-duplicated. Only
/// names that pass `valid_sound_name` are offered, so the settings picker can
/// never list a sound that playback would then refuse.
pub(crate) fn list_sound_names(dirs: &[PathBuf]) -> Vec<String> {
    let mut names: Vec<String> = dirs
        .iter()
        .filter_map(|dir| std::fs::read_dir(dir).ok())
        .flatten()
        .filter_map(|entry| entry.ok())
        .filter(|entry| entry.path().is_file())
        .filter_map(|entry| sound_stem(&entry.path()))
        .collect();
    names.sort_by_key(|n| n.to_lowercase());
    names.dedup();
    names
}

/// The file a sound name plays, searching `dirs` in order.
pub(crate) fn resolve_sound_path(name: &str, dirs: &[PathBuf]) -> Option<PathBuf> {
    if !valid_sound_name(name) {
        return None;
    }
    dirs.iter().find_map(|dir| {
        SOUND_EXTENSIONS
            .iter()
            .map(|ext| dir.join(format!("{name}.{ext}")))
            .find(|path| path.is_file())
    })
}

/// The sound a feed item (or test request) asks for: a name, `true` for the
/// default, anything else for silence.
pub(crate) fn requested_sound_name(sound: &serde_json::Value) -> Option<&str> {
    match sound {
        serde_json::Value::String(name) if !name.is_empty() => Some(name.as_str()),
        serde_json::Value::Bool(true) => Some(DEFAULT_NOTIFICATION_SOUND),
        _ => None,
    }
}

/// The sound to attach to a banner: `Ok(None)` = silent (`false`, `""`,
/// `sound = false`), `Ok(Some(name))` = a name found in `dirs`, `Err` = a bad or
/// missing name. The sound rides on the notification itself (never a separate
/// player), so Focus / Do Not Disturb silences the sound together with the
/// banner instead of leaving a sound with no banner.
pub(crate) fn banner_sound_name(
    sound: &serde_json::Value,
    dirs: &[PathBuf],
) -> Result<Option<String>, String> {
    let Some(name) = requested_sound_name(sound) else {
        return Ok(None);
    };
    if !valid_sound_name(name) {
        return Err(format!("invalid notification sound name: {name:?}"));
    }
    if resolve_sound_path(name, dirs).is_none() {
        return Err(format!("notification sound not found: {name}"));
    }
    Ok(Some(name.to_string()))
}

/// The banner image path from a feed item, if it is one the runtime's
/// notify_icons wrote: an absolute path to `[a-z0-9-]+.png` directly inside a
/// `notification-icons` directory. Anything else is ignored (no image), so a
/// feed item can never make the shell read an arbitrary file.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) fn banner_image_path(raw: &str) -> Option<&str> {
    let path = Path::new(raw);
    let name = path.file_name()?.to_str()?;
    let stem = name.strip_suffix(".png")?;
    let parent_ok = path
        .parent()
        .and_then(|p| p.file_name())
        .is_some_and(|dir| dir == "notification-icons");
    let stem_ok = !stem.is_empty()
        && stem
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-');
    (path.is_absolute() && parent_ok && stem_ok).then_some(raw)
}

/// Localized title/body of the banner the settings "Test notification" button
/// shows. Native text, so it lives beside `tray_labels` rather than in a
/// WebView catalog.
pub(crate) fn test_notification_texts(lang: &str) -> (&'static str, &'static str) {
    match lang {
        "cs" => (
            "Herdeck – zkušební oznámení",
            "Takhle vypadá upozornění, když agent potřebuje vaši pozornost.",
        ),
        _ => (
            "Herdeck test notification",
            "This is how an alert looks when an agent needs your attention.",
        ),
    }
}

/// Named sounds the notification settings can offer (C4): sorted unique stems
/// from the macOS sound folders. Empty elsewhere — the UI then shows a free-text
/// field instead of a picker.
#[tauri::command]
pub(crate) fn notification_sounds() -> Vec<String> {
    list_sound_names(&sound_dirs())
}

/// Show a localized test banner through the same native path real alerts take,
/// carrying `sound` (a name; `None` = the default sound, `""` = silent). `Err`
/// carries a message the settings UI shows as is. The native post may block on
/// Notification Center (and, off macOS, on the notification plugin), so it runs
/// on the blocking pool — never on the main thread or an async worker.
#[tauri::command]
pub(crate) async fn test_notification(
    app: tauri::AppHandle,
    tray: tauri::State<'_, TrayHandles>,
    sound: Option<String>,
) -> Result<(), String> {
    let lang = tray
        .0
        .lock()
        .unwrap()
        .as_ref()
        .map(TrayMenuItems::current_lang)
        .unwrap_or_else(|| "en".to_string());
    let (title, body) = test_notification_texts(&lang);
    let sound = match sound {
        Some(name) => serde_json::Value::String(name),
        None => serde_json::Value::Bool(true),
    };
    // A bad or missing sound still shows the (silent) banner, then reports
    // why it was silent. Off macOS banners carry no sound at all.
    let sound_problem = if cfg!(target_os = "macos") {
        banner_sound_name(&sound, &sound_dirs()).err()
    } else {
        None
    };
    // A fresh identifier per press: a banner with the same identifier as one
    // still in Notification Center replaces it silently (no banner, no sound).
    static TEST_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
    let item = PendingNotification {
        id: "test".to_string(),
        generation: format!("test-{}", shell_gen()),
        seq: TEST_SEQ.fetch_add(1, Ordering::Relaxed),
        title: title.to_string(),
        body: body.to_string(),
        sound,
        icon: None,
        created_at_ms: None,
        meta: banners::BannerMeta::default(),
        kind: None,
    };
    run_blocking(move || {
        post_native_notification(&app, &item)?;
        sound_problem.map_or(Ok(()), Err)
    })
    .await
}

/// Whether native notifications are permitted (C4). Always `None` ("unknown"):
/// banners go through the legacy `NSUserNotificationCenter` on macOS, which has
/// no permission query, and the notification plugin reports a hard-coded
/// "granted" on every desktop OS — passing that on would claim a certainty the
/// shell does not have.
#[tauri::command]
pub(crate) fn notification_permission() -> Option<bool> {
    None
}

/// Keep the time-sensitive long-poll pump out of App Nap while this process is
/// responsible for native banners. The allowing-idle-system-sleep option keeps
/// the Mac itself free to sleep; it only prevents macOS from throttling this
/// background app and letting the runtime's osascript fallback win the race.
#[cfg(target_os = "macos")]
pub(crate) fn prevent_notification_pump_app_nap() {
    use objc2_foundation::{ns_string, NSActivityOptions, NSProcessInfo};

    let activity = NSProcessInfo::processInfo().beginActivityWithOptions_reason(
        NSActivityOptions::UserInitiatedAllowingIdleSystemSleep,
        ns_string!("Deliver Herdeck agent notifications"),
    );
    // Banner duty lasts for the process lifetime. Leaking the opaque activity
    // token intentionally mirrors ending it only during process teardown.
    std::mem::forget(activity);
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn prevent_notification_pump_app_nap() {}

/// The shell side of the notify pump: `AppState` (permission, discovery,
/// re-discovery), the loopback HTTP calls, and the native poster.
pub(crate) struct AppNotify(tauri::AppHandle);

impl notify_pump::NotifyHost for AppNotify {
    fn permission(&self) -> bool {
        self.0.state::<AppState>().notify_permission.load(Ordering::Relaxed)
    }

    fn discovery(&self) -> Option<Discovery> {
        self.0.state::<AppState>().discovery.lock_or_recover().clone()
    }

    fn rediscover(&self) -> Option<Discovery> {
        rediscover_runtime(&self.0)
    }

    fn note_runtime_ok(&self) {
        note_runtime_ok(&self.0.state::<AppState>());
    }
}

impl notify_pump::NotifyTransport for AppNotify {
    fn poll(&self, d: &Discovery, cursor: &NotifyCursor) -> Result<(u16, String), String> {
        http::fetch_notifications_status(
            &d.host,
            d.port,
            &d.token,
            notify_pump::NOTIFY_POLL_TIMEOUT,
            cursor.generation.as_deref(),
            cursor.seq,
            &shell_gen(),
        )
    }

    fn ack(&self, d: &Discovery, generation: &str, seq: u64) -> Result<u16, String> {
        http::ack_notification(&d.host, d.port, &d.token, SIDECAR_TIMEOUT, generation, seq)
    }

    fn fallback(
        &self,
        d: &Discovery,
        generation: &str,
        seq: u64,
        error: &str,
    ) -> Result<u16, String> {
        http::fallback_notification(
            &d.host,
            d.port,
            &d.token,
            SIDECAR_TIMEOUT,
            generation,
            seq,
            &shell_gen(),
            error,
        )
    }
}

impl notify_pump::BannerPoster for AppNotify {
    fn post(&self, item: &PendingNotification) -> Result<(), String> {
        post_native_notification(&self.0, item)
    }

    fn withdraw(&self, agent: &banners::AgentRef) {
        withdraw_banners(agent);
    }
}

/// Generation-aware long-poll notification pump (see `notify_pump`). The
/// blocking request itself keeps banner duty claimed and wakes immediately
/// when the runtime queues an event. Only a successfully shown banner is
/// acknowledged.
pub(crate) fn start_notify_pump(app: tauri::AppHandle) {
    let cursor = app.state::<AppState>().notify_cursor.clone();
    std::thread::spawn(move || {
        notify_pump::NotifyPump {
            host: AppNotify(app.clone()),
            transport: AppNotify(app.clone()),
            poster: AppNotify(app),
            clock: notify_pump::SystemClock,
            cursor,
        }
        .run()
    });
}
