//! Keyboard focus for the mini dock's answer flow (`[hotkeys].next_blocked`
//! while `[desktop].floating = "dock"`).
//!
//! The hotkey routes to the dock instead of the deck's `POST /triage`: the
//! shell remembers which app was frontmost, shows and focuses the dock window
//! (so its WebView receives the answer keys) and tells the dock to open the
//! longest-blocked agent's card. When the user is done there (an answer, Esc,
//! Open in herdr) the dock calls `dock_release_focus`, which hands the focus
//! back to the remembered app.
//!
//! macOS: the frontmost app is read from `NSWorkspace.frontmostApplication`
//! and re-activated through `NSRunningApplication` (objc2, already a direct
//! dependency for the native banners). Only its pid is kept, so nothing
//! Objective-C crosses threads. Hiding our app instead (`NSApp hide:`) would
//! also hand focus back, but it hides the always-on-top dock with it.
//! Elsewhere the focus calls are no-ops: the window manager decides.

use crate::deck_prefs::Floating;
use crate::sync_util::LockExt;
use std::sync::Mutex;

/// Rust -> dock window: open the longest-blocked agent's card (or say that
/// nobody is waiting).
pub const DOCK_NEXT_BLOCKED_EVENT: &str = "dock-next-blocked";

/// Where the next-blocked hotkey goes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NextBlockedRoute {
    /// The dock: focus the floating window and let it open the card.
    Dock,
    /// The deck mirror: `POST /triage` opens the drill (the pre-dock behaviour).
    Triage,
}

pub fn next_blocked_route(floating: Floating) -> NextBlockedRoute {
    match floating {
        Floating::Dock => NextBlockedRoute::Dock,
        Floating::Deck => NextBlockedRoute::Triage,
    }
}

/// The app to give focus back to after the hotkey, given the one remembered
/// so far and the one frontmost now. A press while our own app is frontmost
/// (a second press, or the dock already focused) keeps the earlier one —
/// re-activating ourselves would hand nothing back.
pub fn remember_frontmost(previous: Option<i32>, frontmost: Option<i32>, own: i32) -> Option<i32> {
    match frontmost {
        Some(pid) if pid != own => Some(pid),
        _ => previous,
    }
}

/// Which app `dock_release_focus` re-activates: the remembered one, but only
/// while our app is still active. When it is not, the user (or herdr opening
/// its terminal) already moved the focus elsewhere and must not be overridden.
pub fn release_target(remembered: Option<i32>, own_app_active: bool, own: i32) -> Option<i32> {
    if !own_app_active {
        return None;
    }
    remembered.filter(|pid| *pid != own)
}

/// The pid of the app that was frontmost before the hotkey, until it is handed
/// focus back or the dock window loses focus on its own.
static RETURN_APP: Mutex<Option<i32>> = Mutex::new(None);

fn own_pid() -> i32 {
    std::process::id() as i32
}

/// Remember the frontmost app; call BEFORE focusing the dock window.
pub(crate) fn remember_frontmost_app() {
    let front = native::frontmost_pid();
    let mut slot = RETURN_APP.lock_or_recover();
    *slot = remember_frontmost(*slot, front, own_pid());
}

/// The dock window lost focus without `dock_release_focus` (the user clicked
/// another app or window): a later release must not yank them back.
pub(crate) fn forget_return_app() {
    *RETURN_APP.lock_or_recover() = None;
}

/// Give focus back to the app that was frontmost before the next-blocked
/// hotkey, if our app still holds it; forget it either way. A no-op when
/// nothing is remembered (the dock was used with the mouse).
#[tauri::command]
pub(crate) fn dock_release_focus() {
    let remembered = RETURN_APP.lock_or_recover().take();
    if remembered.is_none() {
        return;
    }
    if let Some(pid) = release_target(remembered, native::own_app_active(), own_pid()) {
        native::activate(pid);
    }
}

#[cfg(target_os = "macos")]
mod native {
    use objc2::rc::{autoreleasepool, Retained};
    use objc2::runtime::{AnyClass, AnyObject};
    use objc2::msg_send;

    /// `NSApplicationActivateIgnoringOtherApps`: ignored from macOS 14 on, where
    /// an app that is active (we are, while the dock holds focus) may yield.
    const ACTIVATE_IGNORING_OTHER_APPS: usize = 1 << 1;

    pub(super) fn frontmost_pid() -> Option<i32> {
        autoreleasepool(|_| {
            let cls = AnyClass::get(c"NSWorkspace")?;
            let ws: Option<Retained<AnyObject>> = unsafe { msg_send![cls, sharedWorkspace] };
            let ws = ws?;
            let app: Option<Retained<AnyObject>> = unsafe { msg_send![&*ws, frontmostApplication] };
            let app = app?;
            let pid: i32 = unsafe { msg_send![&*app, processIdentifier] };
            (pid > 0).then_some(pid)
        })
    }

    pub(super) fn own_app_active() -> bool {
        autoreleasepool(|_| {
            let Some(cls) = AnyClass::get(c"NSRunningApplication") else {
                return false;
            };
            let me: Option<Retained<AnyObject>> = unsafe { msg_send![cls, currentApplication] };
            match me {
                Some(me) => unsafe { msg_send![&*me, isActive] },
                None => false,
            }
        })
    }

    pub(super) fn activate(pid: i32) {
        autoreleasepool(|_| {
            let Some(cls) = AnyClass::get(c"NSRunningApplication") else {
                return;
            };
            let app: Option<Retained<AnyObject>> =
                unsafe { msg_send![cls, runningApplicationWithProcessIdentifier: pid] };
            if let Some(app) = app {
                let ok: bool = unsafe { msg_send![&*app, activateWithOptions: ACTIVATE_IGNORING_OTHER_APPS] };
                if !ok {
                    eprintln!("dock: could not give focus back to pid {pid}");
                }
            }
        })
    }
}

#[cfg(not(target_os = "macos"))]
mod native {
    pub(super) fn frontmost_pid() -> Option<i32> {
        None
    }
    pub(super) fn own_app_active() -> bool {
        false
    }
    pub(super) fn activate(_pid: i32) {}
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_hotkey_goes_to_the_dock_only_when_the_dock_floats() {
        assert_eq!(next_blocked_route(Floating::Dock), NextBlockedRoute::Dock);
        assert_eq!(next_blocked_route(Floating::Deck), NextBlockedRoute::Triage);
    }

    #[test]
    fn another_frontmost_app_is_remembered() {
        assert_eq!(remember_frontmost(None, Some(42), 7), Some(42));
        assert_eq!(remember_frontmost(Some(41), Some(42), 7), Some(42));
    }

    #[test]
    fn our_own_app_frontmost_keeps_the_earlier_one() {
        assert_eq!(remember_frontmost(Some(42), Some(7), 7), Some(42));
        assert_eq!(remember_frontmost(None, Some(7), 7), None);
        assert_eq!(remember_frontmost(Some(42), None, 7), Some(42));
    }

    #[test]
    fn release_hands_focus_back_only_while_we_hold_it() {
        assert_eq!(release_target(Some(42), true, 7), Some(42));
        assert_eq!(release_target(Some(42), false, 7), None);
        assert_eq!(release_target(None, true, 7), None);
        assert_eq!(release_target(Some(7), true, 7), None);
    }
}
