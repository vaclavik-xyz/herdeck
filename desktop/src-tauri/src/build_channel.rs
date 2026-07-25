//! Compile-time identity for stable and disposable desktop builds.

use std::path::{Path, PathBuf};

pub const DEV_CHANNEL: &str = "dev";
pub const DEV_CONFIG_DIR: &str = "herdeck-dev";
pub const DEV_KEYRING_SERVICE: &str = "herdeck-dev";

pub fn current() -> &'static str {
    option_env!("HERDECK_BUILD_CHANNEL").unwrap_or("stable")
}

pub fn is_dev() -> bool {
    current() == DEV_CHANNEL
}

pub fn updates_enabled() -> bool {
    updates_enabled_for(current())
}

fn updates_enabled_for(channel: &str) -> bool {
    channel != DEV_CHANNEL
}

pub fn shared_runtime_attach_enabled() -> bool {
    shared_runtime_attach_enabled_for(current())
}

pub(crate) fn shared_runtime_attach_enabled_for(channel: &str) -> bool {
    channel != DEV_CHANNEL
}

pub fn config_override(explicit: Option<&str>, home: &Path) -> Option<PathBuf> {
    config_override_for(current(), explicit, home)
}

pub fn config_override_for(channel: &str, explicit: Option<&str>, home: &Path) -> Option<PathBuf> {
    if let Some(path) = explicit.filter(|path| !path.is_empty()) {
        return Some(PathBuf::from(path));
    }
    (channel == DEV_CHANNEL).then(|| {
        home.join(".config")
            .join(DEV_CONFIG_DIR)
            .join("config.toml")
    })
}

pub fn keyring_service_override() -> Option<&'static str> {
    is_dev().then_some(DEV_KEYRING_SERVICE)
}

pub fn display_name() -> String {
    display_name_for(current(), option_env!("HERDECK_BUILD_SHA"))
}

fn display_name_for(channel: &str, sha: Option<&str>) -> String {
    if channel != DEV_CHANNEL {
        return "herdeck".to_string();
    }
    match sha.filter(|value| !value.is_empty()) {
        Some(value) => format!(
            "Herdeck Dev · {}",
            value.chars().take(7).collect::<String>()
        ),
        None => "Herdeck Dev".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dev_config_is_isolated_unless_explicitly_overridden() {
        let home = Path::new("/Users/tester");
        assert_eq!(
            config_override_for(DEV_CHANNEL, None, home),
            Some(PathBuf::from(
                "/Users/tester/.config/herdeck-dev/config.toml"
            ))
        );
        assert_eq!(
            config_override_for(DEV_CHANNEL, Some("/tmp/custom.toml"), home),
            Some(PathBuf::from("/tmp/custom.toml"))
        );
        assert_eq!(config_override_for("stable", None, home), None);
    }

    #[test]
    fn dev_title_contains_the_source_revision() {
        assert_eq!(
            display_name_for(DEV_CHANNEL, Some("abcdef123456")),
            "Herdeck Dev · abcdef1"
        );
        assert_eq!(display_name_for(DEV_CHANNEL, None), "Herdeck Dev");
        assert_eq!(display_name_for("stable", Some("abcdef1")), "herdeck");
    }

    #[test]
    fn dev_channel_never_uses_the_stable_updater() {
        assert!(!updates_enabled_for(DEV_CHANNEL));
        assert!(updates_enabled_for("stable"));
    }

    #[test]
    fn dev_channel_never_attaches_the_stable_runtime() {
        assert!(!shared_runtime_attach_enabled_for(DEV_CHANNEL));
        assert!(shared_runtime_attach_enabled_for("stable"));
    }
}
