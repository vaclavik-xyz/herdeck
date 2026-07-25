fn main() {
    println!("cargo:rerun-if-env-changed=HERDECK_BUILD_CHANNEL");
    println!("cargo:rerun-if-env-changed=HERDECK_BUILD_SHA");
    tauri_build::build();
}
