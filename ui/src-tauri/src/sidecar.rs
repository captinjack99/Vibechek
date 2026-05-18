//! Python sidecar process manager.
//!
//! Spawns `vibechek rpc`, multiplexes JSON-RPC requests over its stdin, and
//! demultiplexes responses by `id` field on stdout. Progress notifications
//! (no `id`) are re-emitted as Tauri events named `sidecar:<method>` so the
//! frontend can subscribe to them.
//!
//! Sidecar binary resolution order:
//!   1. `VIBECHEK_SIDECAR` env var (absolute path) — useful in dev so you
//!      can point at `.venv/Scripts/vibechek.exe`.
//!   2. `vibechek-sidecar` next to the Tauri exe (set by `externalBin` in
//!      tauri.conf.json — Tauri appends a platform triple at bundle time).
//!   3. `vibechek` on PATH (development fallback).

use anyhow::{anyhow, Context, Result};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc,
};
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{ChildStdin, Command};
use tokio::sync::{oneshot, Mutex};

// Per-method RPC timeouts (audit #12). A single one-hour ceiling for every
// method is wrong in both directions: too long for a hung `system_info`
// (the GUI sits at a spinner for an hour) and too short for analyzing a
// 50,000-track library on a slow disk.
//
// `timeout_for(method)` returns:
//   - `Some(60s)`  for quick reads (config, system_info, status probes...)
//   - `Some(60m)`  for medium ops (scans, dedupe, installs)
//   - `None`       for `analyze_directory` — duration is unbounded; the
//                   sidecar-died heartbeat path (oneshot Cancellation) is
//                   what guards against hangs, NOT a wall-clock timeout.
//
// Anything not in the map gets the conservative one-hour default so a new
// RPC method we forgot to classify still behaves like the legacy ceiling.
const DEFAULT_TIMEOUT_SECS: u64 = 60 * 60; // 1h fallback for unclassified methods
const QUICK_TIMEOUT_SECS: u64 = 60;        // 60s — anything that should feel instant
const MEDIUM_TIMEOUT_SECS: u64 = 60 * 60;  // 1h — installs, dedupe, organize, etc.

/// Return the wall-clock timeout for a JSON-RPC method, or `None` for "no
/// timeout — rely on the sidecar-died drain to surface failures".
fn timeout_for(method: &str) -> Option<Duration> {
    // Quick reads: must return within seconds. These all touch local state
    // (or short-cached values) and a hang here means the sidecar is wedged.
    const QUICK: &[&str] = &[
        "ping",
        "version",
        "system_info",
        "preflight",
        "wsl_status",
        "engine_gpu_status",
        "library_state",
        "backup_history",
        "get_config",
        "sidecar_status",
        "scan_directory",
    ];
    // Medium: bounded ops that can legitimately take many minutes (installs,
    // scans, plans, organizations, model downloads). One hour is generous;
    // we don't want to kill an `install_vibechek_in_wsl` on a slow apt mirror.
    const MEDIUM: &[&str] = &[
        "scan_only",
        "find_duplicates",
        "plan_organization",
        "organize",
        "apply_ml_tags",
        "backup_tags",
        "restore_tags",
        "download_models",
        "install_wsl",
        "install_vibechek_in_wsl",
        "install_cuda_libs_in_wsl",
        "install_essentia_native",
    ];

    if method == "analyze_directory" {
        // Unbounded: a 50k-track library on a slow USB drive can take many
        // hours. Hanging detection comes from the sidecar-EOF drain path
        // (mark_dead_and_drain) and progress notifications, NOT from a
        // wall-clock timeout that would interrupt a successful long run.
        return None;
    }
    if QUICK.contains(&method) {
        return Some(Duration::from_secs(QUICK_TIMEOUT_SECS));
    }
    if MEDIUM.contains(&method) {
        return Some(Duration::from_secs(MEDIUM_TIMEOUT_SECS));
    }
    // Conservative fallback for any future RPC method we forget to classify.
    Some(Duration::from_secs(DEFAULT_TIMEOUT_SECS))
}

/// Handle the frontend holds (via AppState) to talk to the sidecar.
#[derive(Clone)]
pub struct SidecarHandle {
    inner: Arc<Inner>,
}

struct Inner {
    next_id: AtomicU64,
    pending: Mutex<HashMap<u64, oneshot::Sender<Value>>>,
    stdin: Mutex<ChildStdin>,
    binary_path: String,
    /// Set once the sidecar's stdout has EOF'd or the wait task observed exit.
    /// All subsequent `call()`s fail fast instead of hanging until the
    /// per-method timeout deadline (`timeout_for`).
    dead: AtomicBool,
}

impl Inner {
    fn is_dead(&self) -> bool {
        self.dead.load(Ordering::Acquire)
    }

    fn mark_dead(&self) {
        self.dead.store(true, Ordering::Release);
    }
}

impl SidecarHandle {
    pub fn binary_path(&self) -> &str {
        &self.inner.binary_path
    }

    /// Send a JSON-RPC request and await its response.
    pub async fn call(&self, method: &str, params: Value) -> Result<Value> {
        // Fast-fail if we already know the sidecar is gone, rather than
        // hanging on the 1-hour timeout.
        if self.inner.is_dead() {
            return Err(anyhow!(
                "sidecar is no longer running (binary: {}); restart the app",
                self.inner.binary_path
            ));
        }

        let id = self.inner.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();

        {
            let mut pending = self.inner.pending.lock().await;
            pending.insert(id, tx);
        }

        let request = json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method,
            "params": params,
        });
        let line = serde_json::to_string(&request)? + "\n";

        {
            let mut stdin = self.inner.stdin.lock().await;
            stdin
                .write_all(line.as_bytes())
                .await
                .context("write to sidecar stdin")?;
            stdin.flush().await.context("flush sidecar stdin")?;
        }

        // Per-method timeout (audit #12). `None` means "wait indefinitely" —
        // used for `analyze_directory`, where a long-running run is normal
        // and the only legitimate way to detect death is the sidecar-EOF
        // drain path that wakes our oneshot via `mark_dead_and_drain`.
        match timeout_for(method) {
            None => match rx.await {
                Ok(value) => Ok(value),
                Err(_canceled) => Err(anyhow!(
                    "sidecar died mid-request on method '{}' (binary: {})",
                    method,
                    self.inner.binary_path
                )),
            },
            Some(deadline) => match tokio::time::timeout(deadline, rx).await {
                Ok(Ok(value)) => Ok(value),
                Ok(Err(_canceled)) => {
                    // Receiver dropped — sender was either taken by the EOF
                    // drain (sidecar died) or never got the chance to fire.
                    Err(anyhow!(
                        "sidecar died mid-request on method '{}' (binary: {})",
                        method,
                        self.inner.binary_path
                    ))
                }
                Err(_elapsed) => {
                    // Drop the pending entry so we don't leak it
                    let mut pending = self.inner.pending.lock().await;
                    pending.remove(&id);
                    Err(anyhow!(
                        "sidecar call '{}' timed out after {}s",
                        method,
                        deadline.as_secs()
                    ))
                }
            },
        }
    }
}

/// Spawn the sidecar and start its background reader task.
///
/// Called from Tauri's `setup()`, which runs synchronously on the main thread
/// — there is no ambient Tokio runtime there. We bridge into Tauri's async
/// runtime explicitly via `block_on` for the spawn itself, and use
/// `tauri::async_runtime::spawn` (not bare `tokio::spawn`) for the background
/// reader tasks so they land on the runtime Tauri owns.
pub fn spawn(app: AppHandle) -> Result<SidecarHandle> {
    let binary = resolve_sidecar_binary().context("locate vibechek sidecar binary")?;
    eprintln!("Spawning sidecar: {} rpc", binary);

    tauri::async_runtime::block_on(async move { spawn_in_runtime(binary, app).await })
}

async fn spawn_in_runtime(binary: String, app: AppHandle) -> Result<SidecarHandle> {
    let mut child = Command::new(&binary)
        .arg("rpc")
        // Force UTF-8 mode in the sidecar Python (audit #8). Without this,
        // pathlib.Path on Windows uses the legacy code page (cp1252) for
        // os.fspath() — track files with Cyrillic / CJK / emoji names round-
        // trip as mojibake through JSON-RPC and break the WSL path translation.
        .env("PYTHONUTF8", "1")
        // Belt-and-suspenders: also force stdio encoding for any sub-process
        // the sidecar spawns (PyInstaller may not honor PYTHONUTF8 itself).
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .with_context(|| format!("spawning sidecar at {binary}"))?;

    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow!("sidecar stdin was not captured"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow!("sidecar stdout was not captured"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| anyhow!("sidecar stderr was not captured"))?;

    let inner = Arc::new(Inner {
        next_id: AtomicU64::new(1),
        pending: Mutex::new(HashMap::new()),
        stdin: Mutex::new(stdin),
        binary_path: binary,
        dead: AtomicBool::new(false),
    });

    // stderr reader: just log everything so users can diagnose Python errors
    tauri::async_runtime::spawn(async move {
        let mut reader = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = reader.next_line().await {
            eprintln!("[sidecar] {line}");
        }
    });

    // stdout reader: demux responses + re-emit notifications. When the stream
    // hits EOF (Ok(None)) the sidecar is dead — flip the dead flag and drain
    // all pending oneshots so in-flight calls fail immediately instead of
    // hanging until the 1-hour RPC timeout.
    {
        let inner = inner.clone();
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            let mut reader = BufReader::new(stdout).lines();
            loop {
                match reader.next_line().await {
                    Ok(Some(line)) => {
                        if let Err(e) = handle_message(&inner, &app, &line).await {
                            eprintln!("dispatch error on '{line}': {e}");
                        }
                    }
                    Ok(None) => {
                        eprintln!(
                            "sidecar stdout EOF — process is dead (binary: {})",
                            inner.binary_path
                        );
                        mark_dead_and_drain(&inner).await;
                        break;
                    }
                    Err(e) => {
                        eprintln!(
                            "sidecar stdout read error: {e} (binary: {})",
                            inner.binary_path
                        );
                        mark_dead_and_drain(&inner).await;
                        break;
                    }
                }
            }
        });
    }

    // Wait task: log when child exits (so users see it in dev). Also flip the
    // dead flag in case the child exited without closing stdout cleanly (rare,
    // but belt-and-suspenders).
    {
        let inner = inner.clone();
        tauri::async_runtime::spawn(async move {
            match child.wait().await {
                Ok(status) => eprintln!(
                    "sidecar exited with {status} (binary: {})",
                    inner.binary_path
                ),
                Err(e) => eprintln!(
                    "sidecar wait failed: {e} (binary: {})",
                    inner.binary_path
                ),
            }
            mark_dead_and_drain(&inner).await;
        });
    }

    Ok(SidecarHandle { inner })
}

/// Flip the dead flag and fail every in-flight request with a clear error.
/// Idempotent — safe to call from both the stdout EOF path and the child-wait
/// path; the second call's drain just finds an empty map.
async fn mark_dead_and_drain(inner: &Arc<Inner>) {
    inner.mark_dead();
    // Take ownership of the pending map under the lock, then drop the lock
    // before iterating so handlers calling back into call() don't deadlock.
    let drained: HashMap<u64, oneshot::Sender<Value>> = {
        let mut pending = inner.pending.lock().await;
        std::mem::take(&mut *pending)
    };
    let count = drained.len();
    for (id, tx) in drained {
        let err = json!({
            "jsonrpc": "2.0",
            "id": id,
            "error": {
                "code": -32000,
                "message": format!(
                    "sidecar died (binary: {}); in-flight request aborted",
                    inner.binary_path
                ),
            }
        });
        let _ = tx.send(err);
    }
    if count > 0 {
        eprintln!("sidecar drain: aborted {count} in-flight request(s)");
    }
}

async fn handle_message(inner: &Arc<Inner>, app: &AppHandle, line: &str) -> Result<()> {
    let line = line.trim();
    if line.is_empty() {
        return Ok(());
    }

    // Audit #15: native CUDA/TF/essentia code occasionally writes raw bytes
    // directly to fd 1, bypassing Python's `_StdoutWriter` lock. Those
    // appear as non-JSON lines (e.g. "2026-05-17 14:23:11.123456: I tensorflow/...")
    // and would otherwise abort dispatch with a parse error, losing whatever
    // response was supposed to follow. We log noise at debug level and skip
    // the line — the JSON-RPC stream itself is unaffected because the next
    // newline-delimited frame is still valid.
    //
    // A heuristic to filter the obvious-noise case (lines that don't even
    // start with `{`) keeps the log quiet during a noisy analyze run.
    let msg: Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(e) => {
            if line.starts_with('{') {
                // Looks like JSON but didn't parse — worth a louder log so
                // we notice if the sidecar's JSON writer ever emits malformed
                // frames.
                eprintln!("sidecar: skipping malformed JSON line ({e}): {line}");
            } else {
                // Clearly stray printf noise — keep the log quiet but record
                // it so we can grep for it during debugging.
                log_native_noise(line);
            }
            return Ok(());
        }
    };

    // Response: has `id` and either `result` or `error`
    if let Some(id_val) = msg.get("id") {
        if let Some(id) = id_val.as_u64() {
            let mut pending = inner.pending.lock().await;
            if let Some(tx) = pending.remove(&id) {
                let _ = tx.send(msg);
            } else {
                eprintln!("response for unknown id {id}");
            }
            return Ok(());
        }
    }

    // Notification: emit as Tauri event. Includes `progress` (long ops),
    // `ready` (startup), and `notify` (startup warnings — audit #18).
    if let Some(method) = msg.get("method").and_then(|v| v.as_str()) {
        let event_name = format!("sidecar:{method}");
        let params = msg.get("params").cloned().unwrap_or(Value::Null);
        app.emit(&event_name, params)
            .with_context(|| format!("emit {event_name}"))?;
    }

    Ok(())
}

/// Log a stray non-JSON line from the sidecar's stdout (native printf noise).
/// We keep these as eprintln at trace-ish level — too useful to drop entirely
/// (debugging "why didn't my analyze finish") but too noisy to enable by
/// default once we wire up a real logger. For now: always print to stderr
/// with a clear prefix so users can grep/filter.
fn log_native_noise(line: &str) {
    // Truncate long lines so a giant stack dump or binary blob doesn't fill
    // the user's terminal. 200 chars is enough to identify the source.
    let truncated = if line.len() > 200 {
        format!("{}…", &line[..200])
    } else {
        line.to_string()
    };
    eprintln!("[sidecar stdout noise, ignored] {truncated}");
}

fn resolve_sidecar_binary() -> Result<String> {
    if let Ok(env_path) = std::env::var("VIBECHEK_SIDECAR") {
        if !env_path.is_empty() {
            return Ok(env_path);
        }
    }

    // Tauri's externalBin places the binary next to our exe with a platform
    // triple suffix. We check both the suffixed and unsuffixed forms.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            for name in &["vibechek-sidecar", "vibechek-sidecar.exe"] {
                let candidate = dir.join(name);
                if candidate.exists() {
                    return Ok(candidate.to_string_lossy().into_owned());
                }
            }
        }
    }

    // Final fallback: hope it's on PATH
    Ok("vibechek".to_string())
}
