//! Vibechek desktop shell.
//!
//! Thin Tauri wrapper that spawns the Python sidecar (`vibechek rpc`),
//! forwards JSON-RPC calls from the React frontend, and re-broadcasts
//! progress notifications as Tauri events the frontend can subscribe to.

mod commands;
mod sidecar;

use sidecar::SidecarHandle;
use tauri::Manager;

/// Application state, accessible from every Tauri command handler.
pub struct AppState {
    pub sidecar: SidecarHandle,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init());

    // Auto-updater + relaunch. Both plugins are desktop-only (the updater pulls
    // the signed `latest.json` release artifact; process::relaunch restarts the
    // app after install). Mobile builds skip them.
    #[cfg(desktop)]
    {
        builder = builder
            .plugin(tauri_plugin_updater::Builder::new().build())
            .plugin(tauri_plugin_process::init());
    }

    builder
        .setup(|app| {
            // Spawn the Python sidecar at startup. If it dies later we don't
            // try to restart automatically — surface the failure to the user
            // via the rpc_call error path instead.
            let handle = sidecar::spawn(app.handle().clone())?;
            app.manage(AppState { sidecar: handle });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::rpc_call,
            commands::sidecar_status,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
