//! Vibechek desktop shell.
//!
//! Thin Tauri wrapper that spawns the Python sidecar (`vibechek rpc`),
//! forwards JSON-RPC calls from the React frontend, and re-broadcasts
//! progress notifications as Tauri events the frontend can subscribe to.

// Declared first: `shell_log!` is textually in scope for the modules below.
pub mod shell_log;

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
    // Open the shell's own log first: a windowed release build has no console,
    // so without this every diagnostic below is written to a NULL stderr handle
    // and silently discarded.
    shell_log::init();
    // Panics land in the same file, for the same reason.
    shell_log::install_panic_hook();

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
                    shell_log::write(format_args!("sidecar spawn failed: {e:#}"));
                    // A native dialog has no details toggle, so append the raw
                    // error compactly in the body — a bug report still needs it.
                    // Point at the shell log too: the dialog is dismissed and
                    // gone, the file is what the user can attach.
                    let body = match shell_log::log_path() {
                        Some(path) => format!(
                            "Vibechek couldn't start its analysis service. \
                             Reinstalling Vibechek usually fixes this.\n\n\
                             Technical details:\n{e:#}\n\nLog: {}",
                            path.display()
                        ),
                        None => format!(
                            "Vibechek couldn't start its analysis service. \
                             Reinstalling Vibechek usually fixes this.\n\n\
                             Technical details:\n{e:#}"
                        ),
                    };
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

#[cfg(test)]
mod tests {
    use regex::Regex;

    /// The shipped config, compiled in so the guard can never drift from what
    /// the bundler reads.
    const CONF: &str = include_str!("../tauri.conf.json");

    /// Build the scope regex exactly the way tauri-plugin-shell builds it:
    /// `open_scope()` wraps the configured string as `^{validator}$`.
    ///
    /// Note the shape of the failure this guards. With no `plugins.shell.open`
    /// key at all, the plugin installs its OWN default
    /// (`^((mailto:\w+)|(tel:\w+)|(https?://\w+)).+`), which no filesystem path
    /// can ever match — "Open install folder" and "Show in folder" were
    /// rejected on every platform, and the frontend's `.catch` swallowed it, so
    /// the buttons did nothing at all. `"open": true` is NOT a fix either: the
    /// plugin maps `Flag(true)` to that same URL-only default. Only a
    /// validation *string* replaces it.
    fn open_scope() -> Regex {
        let conf: serde_json::Value =
            serde_json::from_str(CONF).expect("tauri.conf.json is valid JSON");
        let raw = conf["plugins"]["shell"]["open"]
            .as_str()
            .expect("plugins.shell.open must be a validation regex string");
        Regex::new(&format!("^{raw}$")).expect("scope regex compiles")
    }

    #[test]
    fn open_scope_accepts_the_folders_the_frontend_reveals() {
        let scope = open_scope();
        // App.tsx ("Open install folder") and GlobalAudioPlayer.tsx ("Show in
        // folder") both pass a containing directory, verbatim, as the OS
        // reported it.
        for path in [
            r"C:\Users\Jack\My Drive\Vibechek",
            "C:/Users/Jack/Music",
            r"D:\Музыка\Техно",
            r"\\nas\music\techno",
            "/Users/jack/Music",
            "/home/jack/.local/share/Vibechek",
        ] {
            assert!(scope.is_match(path), "shell scope rejects {path}");
        }
    }

    #[test]
    fn open_scope_still_accepts_the_urls() {
        let scope = open_scope();
        for url in [
            "https://github.com/captinjack99/Vibechek/releases",
            "http://localhost:5173/",
            "mailto:jcmamiye@gmail.com",
        ] {
            assert!(scope.is_match(url), "shell scope rejects {url}");
        }
    }

    #[test]
    fn open_scope_rejects_relative_strings() {
        let scope = open_scope();
        // A bare name would be handed to the OS opener and resolved against the
        // app's cwd; absolute-only keeps that off the table.
        for bad in ["calc.exe", "../../etc/passwd", "-i", "", "not a path"] {
            assert!(!scope.is_match(bad), "shell scope accepts {bad:?}");
        }
    }
}
