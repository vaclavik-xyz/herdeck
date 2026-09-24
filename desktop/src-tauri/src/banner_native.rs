//! Native banners: posting (an `NSUserNotification` built with objc2 on
//! macOS, the notification plugin elsewhere), withdrawing an agent's banners,
//! and acting on a banner click (reveal / drill / answer / reply). The pure
//! half — parsing, the bounded banner book, intent mapping — is `banners`.

use std::sync::Mutex;

use tauri::Manager;

use crate::notify_pump::PendingNotification;
use crate::window_roles::reveal_deck;
#[cfg(target_os = "macos")]
use crate::notifications;
use crate::{banners, http, AppState, SIDECAR_TIMEOUT};

/// Remove this agent's banners from Notification Center (only the ones this
/// shell delivered and still remembers — the book is bounded).
pub(crate) fn withdraw_banners(agent: &banners::AgentRef) {
    let identifiers = BANNER_BOOK
        .lock()
        .map(|mut book| book.take_agent(agent))
        .unwrap_or_default();
    #[cfg(target_os = "macos")]
    banner_post::remove_delivered(&identifiers);
    #[cfg(not(target_os = "macos"))]
    let _ = identifiers; // the plugin's banners cannot be withdrawn
}

/// Attribute our banners to this app's bundle — the same choice the
/// notification plugin makes (Terminal in `tauri dev`, where the binary has no
/// registered bundle). First caller wins; the plugin shares this global.
#[cfg(target_os = "macos")]
pub(crate) fn ensure_notification_application(app: &tauri::AppHandle) {
    use std::sync::Once;
    static SET: Once = Once::new();
    let identifier = if tauri::is_dev() {
        "com.apple.Terminal".to_string()
    } else {
        app.config().identifier.clone()
    };
    SET.call_once(|| {
        let _ = mac_notification_sys::set_application(&identifier);
    });
}

/// Banners this shell delivered, by Notification Center identifier (bounded).
/// Read on the main thread when a banner is activated.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) static BANNER_BOOK: Mutex<banners::BannerBook> = Mutex::new(banners::BannerBook::new());

/// The answer context of a delivered banner, if this shell posted it.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) fn banner_context(identifier: &str) -> Option<banners::BannerContext> {
    BANNER_BOOK.lock().ok()?.get(identifier).cloned()
}

/// Post one banner, fire-and-forget, with its sound attached. On macOS it is
/// built directly as an `NSUserNotification` (objc2) so it carries OUR
/// identifier — the key the click handler and `BANNER_BOOK` use to know which
/// agent (and block episode) a banner belongs to — and, for an actionable
/// blocked alert, Approve/Deny buttons or an inline reply field.
/// `mac-notification-sys` still provides the bundle attribution
/// (`ensure_notification_application`) and the center delegate we proxy
/// (`banner_clicks`); its own `send` could do neither (a random UUID per
/// banner, and a blocking wait for any banner with buttons). Clicks are
/// observed by `banner_clicks`, never by a parked per-banner thread.
#[cfg(target_os = "macos")]
pub(crate) fn post_native_notification(
    app: &tauri::AppHandle,
    item: &PendingNotification,
) -> Result<(), String> {
    ensure_notification_application(app);
    // Before the post, so even the very first banner's click is seen.
    banner_clicks::ensure_installed(app);
    let sound = notifications::banner_sound_name(&item.sound, &notifications::sound_dirs()).unwrap_or_else(|err| {
        eprintln!("herdeck: {err}; posting id={} silently", item.id);
        None
    });
    let identifier = banners::banner_identifier(&item.generation, item.seq);
    if let Some(agent) = item.meta.agent.clone() {
        // Recorded first: a click racing the delivery must find its context.
        if let Ok(mut book) = BANNER_BOOK.lock() {
            book.record(
                identifier.clone(),
                banners::BannerContext {
                    agent,
                    episode: item.meta.episode.clone(),
                    sig: item.meta.sig.clone(),
                    actions: item.meta.actions.clone(),
                },
            );
        }
    }
    let image = item.icon.as_deref().and_then(notifications::banner_image_path);
    banner_post::deliver(&identifier, item, sound.as_deref(), image).inspect_err(|_| {
        if let Ok(mut book) = BANNER_BOOK.lock() {
            book.forget(&identifier);
        }
    })
}

/// Building and delivering one `NSUserNotification` (the legacy API
/// `mac-notification-sys` uses too — no permission prompt, works unsigned).
#[cfg(target_os = "macos")]
#[allow(deprecated)]
pub(crate) mod banner_post {
    use objc2::rc::{Allocated, Retained};
    use objc2::runtime::{AnyClass, AnyObject};
    use objc2::{msg_send, ClassType};
    use objc2_foundation::{
        ns_string, NSArray, NSNumber, NSString, NSUserNotification, NSUserNotificationAction,
        NSUserNotificationCenter,
    };

    use super::PendingNotification;

    fn image(path: &str) -> Option<Retained<AnyObject>> {
        let class = AnyClass::get(c"NSImage")?;
        // SAFETY: -[NSImage initWithContentsOfFile:] takes an NSString and
        // returns nil for an unreadable file.
        unsafe {
            let alloc: Allocated<AnyObject> = msg_send![class, alloc];
            msg_send![alloc, initWithContentsOfFile: &*NSString::from_str(path)]
        }
    }

    fn default_center() -> Option<Retained<NSUserNotificationCenter>> {
        // Nil when the process has no bundle identity (an unbundled dev binary
        // without the Terminal attribution).
        unsafe { msg_send![NSUserNotificationCenter::class(), defaultUserNotificationCenter] }
    }

    pub fn deliver(
        identifier: &str,
        item: &PendingNotification,
        sound: Option<&str>,
        image_path: Option<&str>,
    ) -> Result<(), String> {
        let center = default_center().ok_or("no notification center for this process")?;
        let banner = NSUserNotification::new();
        banner.setIdentifier(Some(&NSString::from_str(identifier)));
        banner.setTitle(Some(&NSString::from_str(&item.title)));
        banner.setInformativeText(Some(&NSString::from_str(&item.body)));
        if let Some(sound) = sound {
            banner.setSoundName(Some(&NSString::from_str(sound)));
        }
        let meta = &item.meta;
        if let Some((first, rest)) = meta.actions.split_first() {
            // Approve on the action button, Deny in its drop-down
            // (additionalActions, macOS 10.10+). `_showsButtons` (the private
            // key mac-notification-sys sets for its own drop-down) keeps the
            // buttons visible on a banner-style alert.
            banner.setHasActionButton(true);
            banner.setActionButtonTitle(&NSString::from_str(&first.label));
            let extra: Vec<Retained<NSUserNotificationAction>> = rest
                .iter()
                .map(|action| {
                    NSUserNotificationAction::actionWithIdentifier_title(
                        Some(&NSString::from_str(&action.id)),
                        Some(&NSString::from_str(&action.label)),
                    )
                })
                .collect();
            if !extra.is_empty() {
                banner.setAdditionalActions(Some(&NSArray::from_retained_slice(&extra)));
            }
            // SAFETY: KVC on a key NSUserNotification has on every macOS that
            // still ships the legacy API (mac-notification-sys relies on it).
            unsafe {
                let _: () = msg_send![&*banner, setValue: &*NSNumber::new_bool(true), forKey: ns_string!("_showsButtons")];
            }
        } else if let Some(placeholder) = meta.reply.as_deref() {
            banner.setHasReplyButton(true);
            banner.setResponsePlaceholder(Some(&NSString::from_str(placeholder)));
        } else {
            banner.setHasActionButton(false);
        }
        if let Some(image) = image_path.and_then(image) {
            // The project mark twice: `_identityImage` swaps the left-hand app
            // icon through a private key that newer macOS may ignore;
            // `contentImage` (public API) shows it on the right either way.
            // SAFETY: the same keys/selectors mac-notification-sys uses.
            unsafe {
                let _: () = msg_send![&*banner, setValue: &*image, forKey: ns_string!("_identityImage")];
                let _: () = msg_send![&*banner, setValue: &*NSNumber::new_bool(false), forKey: ns_string!("_identityImageHasBorder")];
                let _: () = msg_send![&*banner, setContentImage: &*image];
            }
        }
        center.deliverNotification(&banner);
        Ok(())
    }

    /// Remove every delivered banner whose identifier is in `identifiers`.
    /// `deliveredNotifications` is a synchronous XPC round trip: called only
    /// for a withdraw item, on the pump thread, never on a timer.
    pub fn remove_delivered(identifiers: &[String]) {
        if identifiers.is_empty() {
            return;
        }
        let Some(center) = default_center() else {
            return;
        };
        for banner in center.deliveredNotifications().iter() {
            let ours = banner
                .identifier()
                .is_some_and(|id| identifiers.iter().any(|want| *want == id.to_string()));
            if ours {
                center.removeDeliveredNotification(&banner);
            }
        }
    }
}

/// Carry out what a banner activation asked for (main thread; the HTTP calls
/// run on their own thread). A successful answer does NOT bring the deck
/// forward — answering from the banner is the point; a stale or failed one
/// opens that agent's drill so the user sees the prompt as it is now.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) fn handle_banner_intent(app: &tauri::AppHandle, intent: banners::BannerIntent) {
    use banners::BannerIntent;
    match intent {
        BannerIntent::Ignore => {}
        BannerIntent::Reveal => reveal_deck(app),
        BannerIntent::Drill(agent) => {
            reveal_deck(app);
            let app = app.clone();
            std::thread::spawn(move || open_agent_drill(&app, &agent));
        }
        BannerIntent::Answer { ref agent, .. } | BannerIntent::Reply { ref agent, .. } => {
            let agent = agent.clone();
            let Some(body) = banners::answer_body(&intent) else {
                return;
            };
            let app = app.clone();
            std::thread::spawn(move || {
                let discovery = app.state::<AppState>().discovery.lock().unwrap().clone();
                let result = match discovery {
                    Some(d) => http::post_agent_action(
                        &d.host,
                        d.port,
                        &d.token,
                        SIDECAR_TIMEOUT,
                        http::AGENT_ANSWER_PATH,
                        &body,
                    ),
                    None => Err("runtime not discovered".to_string()),
                };
                eprintln!(
                    "herdeck: banner answer agent={}:{} result={result:?}",
                    agent.server_id, agent.pane_id
                );
                if banners::answer_needs_drill(&result) {
                    reveal_deck(&app);
                    open_agent_drill(&app, &agent);
                }
            });
        }
    }
}

/// `POST /agents/drill` for one agent (blocking; call off the main thread).
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) fn open_agent_drill(app: &tauri::AppHandle, agent: &banners::AgentRef) {
    let Some(d) = app.state::<AppState>().discovery.lock().unwrap().clone() else {
        return;
    };
    let code = http::post_agent_action(
        &d.host,
        d.port,
        &d.token,
        SIDECAR_TIMEOUT,
        http::AGENT_DRILL_PATH,
        &banners::drill_body(agent),
    );
    if code != Ok(204) {
        eprintln!(
            "herdeck: banner drill agent={}:{} result={code:?}",
            agent.server_id, agent.pane_id
        );
    }
}

/// Whether to present a banner while Herdeck is frontmost: the wrapped
/// delegate's answer when it has one, else yes (AppKit's own default is no).
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) fn should_present_banner(inner_answer: Option<bool>) -> bool {
    inner_answer.unwrap_or(true)
}

/// Banner clicks → reveal the deck, with no per-banner thread and no polling.
///
/// `NSUserNotificationCenter` has exactly one delegate. `mac-notification-sys`
/// creates it (we still call its `setupDelegate`, and its handler removes an
/// activated banner from Notification Center); our banners no longer go
/// through the crate's `send`, so nothing waits on its `didDeliverNotification:`.
/// We put a forwarding proxy in front of it: every delegate message is passed
/// on unchanged, and `didActivateNotification:` also carries out the banner's
/// intent (reveal / drill / answer / reply, see `banners::banner_intent`).
/// That is one object for the whole process, event driven, on the main thread
/// where AppKit delivers these callbacks anyway.
///
/// It also answers `shouldPresentNotification:` (the crate does not), so a
/// banner — and the sound it carries — still shows while Herdeck is frontmost.
///
/// `install_at_startup` makes the crate create its delegate (normally done
/// lazily inside its first send) and wraps it before any banner is posted, so
/// the first banner already goes through the proxy. `ensure_installed` before
/// each post is a cheap guard in case anything replaced the delegate since.
#[cfg(target_os = "macos")]
#[allow(deprecated)] // NSUserNotification*: the legacy API mac-notification-sys posts through
pub(crate) mod banner_clicks {
    use std::cell::RefCell;
    use std::sync::OnceLock;

    use objc2::rc::Retained;
    use objc2::runtime::{NSObject, NSObjectProtocol, ProtocolObject};
    use objc2::{
        define_class, msg_send, sel, ClassType, DefinedClass, MainThreadMarker, MainThreadOnly,
    };
    use objc2_foundation::{
        NSUserNotification, NSUserNotificationCenter, NSUserNotificationCenterDelegate,
    };

    type Delegate = ProtocolObject<dyn NSUserNotificationCenterDelegate>;

    pub struct Ivars {
        /// The delegate we stand in front of (the crate's), if any.
        inner: Option<Retained<Delegate>>,
    }

    define_class!(
        #[unsafe(super(NSObject))]
        #[thread_kind = MainThreadOnly]
        #[name = "HerdeckBannerClickDelegate"]
        #[ivars = Ivars]
        pub struct ClickDelegate;

        unsafe impl NSObjectProtocol for ClickDelegate {}

        unsafe impl NSUserNotificationCenterDelegate for ClickDelegate {
            #[unsafe(method(userNotificationCenter:didDeliverNotification:))]
            fn did_deliver(&self, center: &NSUserNotificationCenter, notification: &NSUserNotification) {
                if let Some(inner) = self.forward_to(sel!(userNotificationCenter:didDeliverNotification:)) {
                    let _: () = unsafe {
                        msg_send![inner, userNotificationCenter: center, didDeliverNotification: notification]
                    };
                }
            }

            // NSUserNotificationCenter's default is to NOT present a banner
            // while the posting app is frontmost, and the crate's delegate does
            // not implement this. Now that the sound rides on the banner, an
            // unpresented banner would be a fully silent alert (including the
            // settings Test button, which is pressed with Herdeck frontmost).
            #[unsafe(method(userNotificationCenter:shouldPresentNotification:))]
            fn should_present(&self, center: &NSUserNotificationCenter, notification: &NSUserNotification) -> bool {
                let inner = self
                    .forward_to(sel!(userNotificationCenter:shouldPresentNotification:))
                    .map(|inner| -> bool {
                        unsafe {
                            msg_send![inner, userNotificationCenter: center, shouldPresentNotification: notification]
                        }
                    });
                super::should_present_banner(inner)
            }

            #[unsafe(method(userNotificationCenter:didActivateNotification:))]
            fn did_activate(&self, center: &NSUserNotificationCenter, notification: &NSUserNotification) {
                let identifier = notification.identifier().map(|id| id.to_string());
                let context = identifier.as_deref().and_then(super::banner_context);
                let additional = notification
                    .additionalActivationAction()
                    .and_then(|action| action.identifier())
                    .map(|id| id.to_string());
                let reply = notification.response().map(|text| text.string().to_string());
                let intent = super::banners::banner_intent(
                    notification.activationType().0,
                    context.as_ref(),
                    additional.as_deref(),
                    reply.as_deref(),
                );
                if let Some(app) = APP.get() {
                    super::handle_banner_intent(app, intent);
                }
                if let Some(inner) = self.forward_to(sel!(userNotificationCenter:didActivateNotification:)) {
                    let _: () = unsafe {
                        msg_send![inner, userNotificationCenter: center, didActivateNotification: notification]
                    };
                }
            }
        }

        impl ClickDelegate {
            // Private NSUserNotificationCenter callback the crate implements
            // (close button); forwarded so it keeps working.
            #[unsafe(method(userNotificationCenter:didDismissAlert:))]
            fn did_dismiss_alert(&self, center: &NSUserNotificationCenter, notification: &NSUserNotification) {
                if let Some(inner) = self.forward_to(sel!(userNotificationCenter:didDismissAlert:)) {
                    let _: () = unsafe {
                        msg_send![inner, userNotificationCenter: center, didDismissAlert: notification]
                    };
                }
            }
        }
    );

    impl ClickDelegate {
        fn forward_to(&self, selector: objc2::runtime::Sel) -> Option<&Delegate> {
            self.ivars()
                .inner
                .as_deref()
                .filter(|inner| inner.respondsToSelector(selector))
        }
    }

    static APP: OnceLock<tauri::AppHandle> = OnceLock::new();

    thread_local! {
        /// The installed proxy. The center holds its delegate unretained, so
        /// this keeps it alive; a replaced proxy is dropped once the center no
        /// longer points at it.
        static PROXY: RefCell<Option<Retained<ClickDelegate>>> = const { RefCell::new(None) };
    }

    extern "C" {
        /// mac-notification-sys's own ObjC entry point (notify.m, in the
        /// `libnotify.a` the crate links): creates its delegate singleton and
        /// sets it on the center, once (`dispatch_once`). The crate only calls
        /// it inside its first send, which would put a banner through the
        /// crate's delegate before our proxy could wrap it.
        fn setupDelegate();
    }

    /// Install the proxy before anything is posted, so the very first banner
    /// already gets `shouldPresentNotification:` (presented while Herdeck is
    /// frontmost) and its click is seen. Order matters: attribute the bundle
    /// first (an unbundled `tauri dev` binary has no notification center
    /// otherwise), then let the crate create its delegate, then wrap it; the
    /// crate's later `setupDelegate` calls are then no-ops. Main thread.
    pub fn install_at_startup(app: &tauri::AppHandle) {
        super::ensure_notification_application(app);
        // SAFETY: a plain C function with no arguments; idempotent.
        unsafe { setupDelegate() };
        ensure_installed(app);
    }

    /// Put the proxy in front of the center's current delegate unless it is
    /// already there. Main thread only.
    fn install(mtm: MainThreadMarker) {
        // Nil when the process has no bundle identity; nothing to wrap then.
        let center: Option<Retained<NSUserNotificationCenter>> = unsafe {
            msg_send![NSUserNotificationCenter::class(), defaultUserNotificationCenter]
        };
        let Some(center) = center else {
            return;
        };
        // SAFETY: the current delegate is either ours (kept alive by PROXY) or
        // the crate's process-lifetime singleton.
        let current = unsafe { center.delegate() };
        if current
            .as_deref()
            .is_some_and(|d| d.isKindOfClass(ClickDelegate::class()))
        {
            return;
        }
        let proxy = ClickDelegate::alloc(mtm).set_ivars(Ivars { inner: current });
        let proxy: Retained<ClickDelegate> = unsafe { msg_send![super(proxy), init] };
        // SAFETY: PROXY keeps the delegate alive for as long as it is set.
        unsafe { center.setDelegate(Some(ProtocolObject::from_ref(&*proxy))) };
        PROXY.with(|slot| *slot.borrow_mut() = Some(proxy));
    }

    /// Install (or re-install) the proxy. Cheap and idempotent: one property
    /// read on the main thread per call.
    pub fn ensure_installed(app: &tauri::AppHandle) {
        let _ = APP.set(app.clone());
        if let Some(mtm) = MainThreadMarker::new() {
            install(mtm);
            return;
        }
        let _ = app.run_on_main_thread(|| {
            if let Some(mtm) = MainThreadMarker::new() {
                install(mtm);
            }
        });
    }
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn post_native_notification(
    app: &tauri::AppHandle,
    item: &PendingNotification,
) -> Result<(), String> {
    use tauri_plugin_notification::NotificationExt;
    app.notification()
        .builder()
        .title(&item.title)
        .body(&item.body)
        .show()
        .map_err(|err| err.to_string())
}
