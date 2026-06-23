// Prevents an extra console window on Windows in release builds. No-op elsewhere.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    herdeck_desktop_lib::run();
}
