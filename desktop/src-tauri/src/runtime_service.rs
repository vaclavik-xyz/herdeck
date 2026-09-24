//! Keep a `herdeck-service install runtime --from-app` unit in step with the app.
//!
//! Such a launchd unit runs the frozen runtime bundled inside THIS .app
//! (`Contents/Resources/herdeck-deckapp/herdeck-deckapp`). The updater replaces
//! the bundle on disk, but the running runtime keeps executing the old binary
//! until something restarts it — so after an install, and before the app itself
//! restarts, kickstart that unit. Units that run from anywhere else (a source
//! checkout's `python -m herdeck.runtime`, another bundle) are never touched.
//! The label and binary path mirror `src/herdeck/service.py`.

use std::path::{Path, PathBuf};

/// launchd label written by `herdeck-service install runtime`.
pub const RUNTIME_LABEL: &str = "dev.herdeck.runtime";
/// The frozen runtime's path inside the .app (service.py `APP_RUNTIME_BINARY`).
const BUNDLED_RUNTIME: &str = "Contents/Resources/herdeck-deckapp/herdeck-deckapp";

/// The `.app` bundle an executable at `…/X.app/Contents/MacOS/bin` lives in.
pub fn bundle_of_exe(exe: &Path) -> Option<PathBuf> {
    let macos = exe.parent()?;
    if macos.file_name()? != "MacOS" {
        return None;
    }
    let contents = macos.parent()?;
    if contents.file_name()? != "Contents" {
        return None;
    }
    let app = contents.parent()?;
    if app.extension()? != "app" {
        return None;
    }
    Some(app.to_path_buf())
}

/// Python's `plistlib` escapes exactly these in `<string>` values.
fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// The first `ProgramArguments` entry of an XML plist, still XML-escaped.
fn program_of_plist(plist_xml: &str) -> Option<&str> {
    let start = plist_xml.find("<key>ProgramArguments</key>")?;
    let rest = &plist_xml[start..];
    let array = &rest[..rest.find("</array>")?];
    let open = array.find("<string>")? + "<string>".len();
    let close = open + array[open..].find("</string>")?;
    Some(&array[open..close])
}

/// True when the unit's program is the frozen runtime inside one of `bundles`.
pub fn unit_runs_from_bundle(plist_xml: &str, bundles: &[PathBuf]) -> bool {
    let Some(program) = program_of_plist(plist_xml) else {
        return false;
    };
    bundles.iter().any(|bundle| {
        let binary = bundle.join(BUNDLED_RUNTIME);
        xml_escape(&binary.to_string_lossy()) == program
    })
}

/// Who owns the installed runtime unit, from the app's point of view.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnitOwner {
    /// No unit installed.
    None,
    /// Runs the frozen runtime bundled in THIS app.
    ThisApp,
    /// Runs the frozen runtime of another app bundle.
    OtherApp,
    /// Runs anything else (a source checkout, a venv, a hand-written unit).
    Checkout,
}

/// Classify an installed unit (`None` when there is no plist text).
pub fn unit_owner(plist_xml: Option<&str>, bundles: &[PathBuf]) -> UnitOwner {
    let Some(xml) = plist_xml else {
        return UnitOwner::None;
    };
    if unit_runs_from_bundle(xml, bundles) {
        return UnitOwner::ThisApp;
    }
    match program_of_plist(xml) {
        Some(program) if program.ends_with(&format!("/{BUNDLED_RUNTIME}")) => UnitOwner::OtherApp,
        _ => UnitOwner::Checkout,
    }
}

/// The installed runtime unit's owner on this Mac (`None` elsewhere: only a
/// launchd unit can run an app bundle's runtime).
pub fn installed_unit_owner() -> UnitOwner {
    #[cfg(target_os = "macos")]
    {
        let home = std::env::var("HOME").unwrap_or_default();
        let plist = Path::new(&home)
            .join("Library/LaunchAgents")
            .join(format!("{RUNTIME_LABEL}.plist"));
        let xml = std::fs::read_to_string(plist).ok();
        unit_owner(xml.as_deref(), &own_bundles())
    }
    #[cfg(not(target_os = "macos"))]
    {
        UnitOwner::None
    }
}

/// This app's bundle path, as launched and canonicalised (the service resolves
/// `--from-app`, so either spelling may be the one in the plist).
#[cfg(target_os = "macos")]
fn own_bundles() -> Vec<PathBuf> {
    let Ok(exe) = std::env::current_exe() else {
        return Vec::new();
    };
    let mut bundles: Vec<PathBuf> = bundle_of_exe(&exe).into_iter().collect();
    if let Some(canonical) = std::fs::canonicalize(&exe)
        .ok()
        .and_then(|exe| bundle_of_exe(&exe))
    {
        if !bundles.contains(&canonical) {
            bundles.push(canonical);
        }
    }
    bundles
}

/// After a successful updater install: restart the runtime unit if (and only
/// if) it runs from this bundle, then wait briefly for the new runtime to
/// publish runtime.json so the restarting app attaches to it instead of
/// spawning its own sidecar. Blocking; call it off the async executor.
#[cfg(target_os = "macos")]
pub fn restart_bundled_runtime_after_update() {
    use std::time::{Duration, Instant, SystemTime};

    let home = std::env::var("HOME").unwrap_or_default();
    let plist = Path::new(&home)
        .join("Library/LaunchAgents")
        .join(format!("{RUNTIME_LABEL}.plist"));
    let Ok(xml) = std::fs::read_to_string(&plist) else {
        return; // no herdeck-service runtime unit installed
    };
    if !unit_runs_from_bundle(&xml, &own_bundles()) {
        eprintln!(
            "herdeck: update: runtime unit {RUNTIME_LABEL} does not run from this app bundle; not restarting it"
        );
        return;
    }
    // SAFETY: getuid has no preconditions and cannot fail.
    let uid = unsafe { libc::getuid() };
    let target = format!("gui/{uid}/{RUNTIME_LABEL}");
    let started = SystemTime::now();
    match std::process::Command::new("/bin/launchctl")
        .args(["kickstart", "-k", &target])
        .status()
    {
        Ok(status) if status.success() => {
            eprintln!("herdeck: update: restarted bundled runtime ({target}) to match the new app");
        }
        Ok(status) => {
            eprintln!("herdeck: update: launchctl kickstart -k {target} failed: {status}");
            return;
        }
        Err(err) => {
            eprintln!("herdeck: update: could not run launchctl kickstart for {target}: {err}");
            return;
        }
    }
    let discovery = crate::sidecar::runtime_file_path();
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        let fresh = std::fs::metadata(&discovery)
            .and_then(|meta| meta.modified())
            .map(|modified| modified >= started)
            .unwrap_or(false);
        if fresh {
            return;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    eprintln!("herdeck: update: restarted runtime has not published {} yet", discovery.display());
}

#[cfg(not(target_os = "macos"))]
pub fn restart_bundled_runtime_after_update() {}

#[cfg(test)]
mod tests {
    use super::*;

    fn plist(program: &str) -> String {
        format!(
            "<?xml version=\"1.0\"?>\n<plist version=\"1.0\">\n<dict>\n\t<key>Label</key>\n\
             \t<string>dev.herdeck.runtime</string>\n\t<key>ProgramArguments</key>\n\t<array>\n\
             \t\t<string>{program}</string>\n\t\t<string>-m</string>\n\t</array>\n</dict>\n</plist>\n"
        )
    }

    #[test]
    fn bundle_of_exe_finds_the_app() {
        let exe = Path::new("/Applications/herdeck.app/Contents/MacOS/herdeck-desktop");
        assert_eq!(bundle_of_exe(exe), Some(PathBuf::from("/Applications/herdeck.app")));
        assert_eq!(bundle_of_exe(Path::new("/usr/local/bin/herdeck-desktop")), None);
        assert_eq!(bundle_of_exe(Path::new("/x/Contents/MacOS/bin")), None);
    }

    #[test]
    fn matches_unit_running_this_bundles_runtime() {
        let xml = plist("/Applications/herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp");
        assert!(unit_runs_from_bundle(&xml, &[PathBuf::from("/Applications/herdeck.app")]));
    }

    #[test]
    fn never_matches_a_source_checkout_unit() {
        let xml = plist("/Users/me/herdeck/venv/bin/python");
        assert!(!unit_runs_from_bundle(&xml, &[PathBuf::from("/Applications/herdeck.app")]));
    }

    #[test]
    fn never_matches_another_bundle() {
        let xml = plist("/Users/me/Downloads/herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp");
        assert!(!unit_runs_from_bundle(&xml, &[PathBuf::from("/Applications/herdeck.app")]));
    }

    #[test]
    fn compares_against_the_xml_escaped_path() {
        let xml = plist("/Apps/R&amp;D/herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp");
        assert!(unit_runs_from_bundle(&xml, &[PathBuf::from("/Apps/R&D/herdeck.app")]));
    }

    #[test]
    fn classifies_the_unit_owner() {
        let ours = [PathBuf::from("/Applications/herdeck.app")];
        assert_eq!(unit_owner(None, &ours), UnitOwner::None);
        let this = plist("/Applications/herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp");
        assert_eq!(unit_owner(Some(&this), &ours), UnitOwner::ThisApp);
        let other = plist("/Users/me/Downloads/herdeck.app/Contents/Resources/herdeck-deckapp/herdeck-deckapp");
        assert_eq!(unit_owner(Some(&other), &ours), UnitOwner::OtherApp);
        let checkout = plist("/Users/me/herdeck/.venv/bin/python");
        assert_eq!(unit_owner(Some(&checkout), &ours), UnitOwner::Checkout);
        assert_eq!(unit_owner(Some("<dict></dict>"), &ours), UnitOwner::Checkout);
    }

    #[test]
    fn only_the_program_counts_not_other_strings() {
        let xml = format!(
            "<dict><key>Label</key><string>/Applications/herdeck.app/{BUNDLED_RUNTIME}</string>\
             <key>ProgramArguments</key><array><string>/usr/bin/python3</string></array></dict>"
        );
        assert!(!unit_runs_from_bundle(&xml, &[PathBuf::from("/Applications/herdeck.app")]));
        assert!(!unit_runs_from_bundle("<dict></dict>", &[PathBuf::from("/Applications/herdeck.app")]));
    }
}
