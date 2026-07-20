//! Vibechek desktop shell.
//!
//! Thin Tauri wrapper that spawns the Python sidecar (`vibechek rpc`),
//! forwards JSON-RPC calls from the React frontend, and re-broadcasts
//! progress notifications as Tauri events the frontend can subscribe to.

mod commands;
mod sidecar;

use sidecar::SidecarHandle;
use tauri::Manager;
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};

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
            // Spawn the Python sidecar at startup. If it dies LATER we surface
            // the failure via the rpc_call error path (the structured envelope).
            //
            // But a spawn failure HERE means the app has no analysis service at
            // all. Propagating the error (`?`) would bubble to `.run().expect()`
            // and PANIC — in a windowed release build that means no window, no
            // console, nothing: a broken install is indistinguishable from
            // "didn't launch." Catch it and show a native dialog instead, then
            // exit cleanly.
            match sidecar::spawn(app.handle().clone()) {
                Ok(handle) => {
                    app.manage(AppState { sidecar: handle });
                }
                Err(e) => {
                    // Hide the (blank) main window so the user sees only the
                    // error dialog, not a broken empty shell behind it.
                    for (_, w) in app.webview_windows() {
                        let _ = w.hide();
                    }
                    // A native dialog has no details toggle, so append the raw
                    // error compactly in the body — a bug report still needs it.
                    let body = format!(
                        "Vibechek couldn't start its analysis service. \
                         Reinstalling Vibechek usually fixes this.\n\n\
                         Technical details:\n{e:#}"
                    );
                    // `blocking_show` dispatches the dialog onto the main thread
                    // and then blocks on the reply — calling it on the main
                    // thread before the event loop starts pumping would deadlock.
                    // Show it from a worker thread and return Ok so the loop
                    // starts and can service the dialog; the thread exits the
                    // process once the user dismisses it.
                    let handle = app.handle().clone();
                    std::thread::spawn(move || {
                        handle
                            .dialog()
                            .message(body)
                            .title("Vibechek couldn't start")
                            .kind(MessageDialogKind::Error)
                            .blocking_show();
                        std::process::exit(1);
                    });
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::rpc_call,
            commands::sidecar_status,
            commands::drain_startup_notifications,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
