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

// Per-method RPC timeouts. A single one-hour ceiling for every
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
    ];
    // Medium: bounded ops that can legitimately take many minutes (installs,
    // scans, plans, organizations, model downloads). One hour is generous;
    // we don't want to kill an `install_vibechek_in_wsl` on a slow apt mirror.
    const MEDIUM: &[&str] = &[
        // scan_directory enumerates + stat()s every audio file under a root.
        // On a 50k-track library on a slow USB/SMB mount that easily exceeds
        // the 60s QUICK budget — a timeout there drops the pending entry and
        // discards the result even when the scan eventually finishes. It's a
        // bounded op, so it belongs here, not in QUICK.
        "scan_directory",
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

// ---------------------------------------------------------------------------
// Structured transport errors
//
// Rust-side transport failures (dead sidecar, mid-request death, per-method
// timeout) used to reach the frontend as free text — ErrorToast couldn't split
// them into a plain headline + a technical detail toggle, and the raw binary
// path / method name / timeout had nowhere to demote to. We now serialize them
// into the SAME envelope shape the Python errors already use:
//
//     { "code", "message", "data": { "kind", "headline", "detail" } }
//
// so the one shared frontend parser (RpcError) produces a plain headline and a
// details toggle for EVERY path. `kind` drives the recovery affordance the UI
// offers:
//   - "engine_dead" → ErrorToast shows "Restart Vibechek"
//   - "retryable"   → ErrorToast shows "Try again" (re-issues the call)
//   - "fatal"       → no recovery action (should be rare for transport)
// ---------------------------------------------------------------------------

/// Error class the frontend branches on to pick a recovery action.
#[derive(Debug, Clone, Copy)]
pub enum TransportErrorKind {
    /// The sidecar process is gone (already-dead flag, mid-request death, or
    /// the stdout-EOF drain). Recovery = restart the app.
    EngineDead,
    /// A per-method wall-clock timeout. The sidecar may still be alive, so the
    /// safe recovery is to re-issue the call, not to restart.
    Retryable,
    /// An internal transport fault we can't classify (should be rare).
    Fatal,
}

impl TransportErrorKind {
    fn as_str(self) -> &'static str {
        match self {
            TransportErrorKind::EngineDead => "engine_dead",
            TransportErrorKind::Retryable => "retryable",
            TransportErrorKind::Fatal => "fatal",
        }
    }
}

/// A transport-level failure carrying a plain, user-facing `headline` and a
/// technical `detail` (the raw error text, incl. the binary path / method name)
/// the frontend can demote to a details toggle.
#[derive(Debug)]
pub struct TransportError {
    kind: TransportErrorKind,
    headline: String,
    detail: String,
}

impl TransportError {
    /// The sidecar is gone. One plain headline for every death path; the
    /// specific reason (mid-request, already-dead, stdin write failure) lands
    /// in `detail`.
    fn engine_dead(detail: impl Into<String>) -> Self {
        Self {
            kind: TransportErrorKind::EngineDead,
            headline: "The analysis service stopped unexpectedly.".into(),
            detail: detail.into(),
        }
    }

    /// A per-method wall-clock timeout. Headline names the operation in plain
    /// terms ("The library scan is taking longer than expected.").
    fn timeout(method: &str, secs: u64) -> Self {
        Self {
            kind: TransportErrorKind::Retryable,
            headline: format!(
                "{} is taking longer than expected.",
                plain_operation_name(method)
            ),
            detail: format!("sidecar call '{method}' timed out after {secs}s"),
        }
    }

    /// An internal fault we couldn't classify (e.g. request encoding failed).
    fn fatal(detail: impl Into<String>) -> Self {
        Self {
            kind: TransportErrorKind::Fatal,
            headline: "Vibechek hit an unexpected internal error.".into(),
            detail: detail.into(),
        }
    }

    /// Serialize into the `{code, message, data:{kind, headline, detail}}`
    /// envelope the frontend's RpcError parser understands. `message` mirrors
    /// the plain headline so any consumer that reads only `.message` still gets
    /// user-facing text — never the raw error.
    pub fn into_envelope_json(self) -> String {
        let value = json!({
            "code": -32001,
            "message": self.headline,
            "data": {
                "kind": self.kind.as_str(),
                "headline": self.headline,
                "detail": self.detail,
            }
        });
        serde_json::to_string(&value).unwrap_or_else(|_| {
            // Every field is a plain string, so this cannot realistically fail;
            // never panic on the error path — fall back to a minimal envelope.
            "{\"code\":-32001,\"message\":\"The analysis service stopped unexpectedly.\"}"
                .to_string()
        })
    }
}

/// Map a raw JSON-RPC method name to a plain, user-facing operation name for
/// error headlines. Mirrors the taxonomy `timeout_for` groups methods by;
/// anything unmapped falls back to a neutral "The operation".
fn plain_operation_name(method: &str) -> &'static str {
    match method {
        "analyze_directory" => "Analysis",
        "scan_directory" | "scan_only" => "The library scan",
        "find_duplicates" | "handle_duplicates" => "The duplicate scan",
        "plan_organization" | "organize" => "The organize",
        "apply_ml_tags" => "Applying tags",
        "backup_tags" => "The tag backup",
        "restore_tags" | "restore_tags_with_remap" => "The tag restore",
        "download_models" => "The model download",
        "install_wsl" | "install_vibechek_in_wsl" | "upgrade_vibechek_in_wsl"
        | "install_cuda_libs_in_wsl" | "install_essentia_native" | "setup_onnx_engine"
        | "setup_clap_engine" | "setup_genre_resolver" => "Setup",
        "revert_journal" => "The undo",
        _ => "The operation",
    }
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
    /// One-shot trigger telling the wait task to kill a still-running child.
    /// Fired by `mark_dead_and_drain`: once we've declared the sidecar dead
    /// and told the user "in-flight request aborted", the process must not
    /// keep running (and possibly keep mutating the user's files).
    kill_tx: std::sync::Mutex<Option<oneshot::Sender<()>>>,
    /// `ready`/`notify` fire at sidecar startup, typically BEFORE the webview
    /// has mounted and registered its event listeners — and Tauri events are
    /// not queued for late subscribers, so a fast sidecar start silently
    /// dropped the install-path hang warning. Buffer them here until the
    /// frontend's one-time drain (`drain_startup_notifications`); after the
    /// drain, notifications emit live (the frontend is listening by then).
    startup_events: Mutex<Vec<Value>>,
    startup_drained: AtomicBool,
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
    ///
    /// The `Err` variant is a structured [`TransportError`] carrying a plain
    /// headline, a technical detail, and a kind. `commands::rpc_call` serializes
    /// it into the same envelope shape Python errors use, so ErrorToast can
    /// render a plain headline, a details toggle, and a Restart/Retry action.
    pub async fn call(&self, method: &str, params: Value) -> Result<Value, TransportError> {
        // Fast-fail if we already know the sidecar is gone, rather than
        // hanging on the 1-hour timeout.
        if self.inner.is_dead() {
            return Err(TransportError::engine_dead(format!(
                "sidecar is no longer running (binary: {}); restart the app",
                self.inner.binary_path
            )));
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
        let line = match serde_json::to_string(&request) {
            Ok(s) => s + "\n",
            Err(e) => {
                return Err(TransportError::fatal(format!(
                    "failed to encode request for '{method}': {e}"
                )));
            }
        };

        {
            let mut stdin = self.inner.stdin.lock().await;
            // A stdin write/flush failure means the sidecar's pipe is gone —
            // treat it as a death, not a generic error, so the UI offers
            // Restart rather than a bare stack fragment.
            if let Err(e) = stdin.write_all(line.as_bytes()).await {
                return Err(TransportError::engine_dead(format!(
                    "write to sidecar stdin failed for '{method}' (binary: {}): {e}",
                    self.inner.binary_path
                )));
            }
            if let Err(e) = stdin.flush().await {
                return Err(TransportError::engine_dead(format!(
                    "flush sidecar stdin failed for '{method}' (binary: {}): {e}",
                    self.inner.binary_path
                )));
            }
        }

        // Per-method timeout. `None` means "wait indefinitely" —
        // used for `analyze_directory`, where a long-running run is normal
        // and the only legitimate way to detect death is the sidecar-EOF
        // drain path that wakes our oneshot via `mark_dead_and_drain`.
        match timeout_for(method) {
            None => match rx.await {
                Ok(value) => Ok(value),
                Err(_canceled) => Err(TransportError::engine_dead(format!(
                    "sidecar died mid-request on method '{}' (binary: {})",
                    method, self.inner.binary_path
                ))),
            },
            Some(deadline) => match tokio::time::timeout(deadline, rx).await {
                Ok(Ok(value)) => Ok(value),
                Ok(Err(_canceled)) => {
                    // Receiver dropped — sender was either taken by the EOF
                    // drain (sidecar died) or never got the chance to fire.
                    Err(TransportError::engine_dead(format!(
                        "sidecar died mid-request on method '{}' (binary: {})",
                        method, self.inner.binary_path
                    )))
                }
                Err(_elapsed) => {
                    // Drop the pending entry so we don't leak it
                    let mut pending = self.inner.pending.lock().await;
                    pending.remove(&id);
                    Err(TransportError::timeout(method, deadline.as_secs()))
                }
            },
        }
    }

    /// Return (and clear) the buffered startup notifications, flipping the
    /// buffer into live-emit mode. Called once by the frontend after mount.
    /// The flag flips under the buffer lock so an event arriving concurrently
    /// is either included in the drain or emitted live — never lost.
    pub async fn drain_startup_notifications(&self) -> Vec<Value> {
        let mut buf = self.inner.startup_events.lock().await;
        self.inner.startup_drained.store(true, Ordering::Release);
        std::mem::take(&mut *buf)
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
        // Force UTF-8 mode in the sidecar Python. Without this,
        // pathlib.Path on Windows uses the legacy code page (cp1252) for
        // os.fspath() — track files with Cyrillic / CJK / emoji names round-
        // trip as mojibake through JSON-RPC and break the WSL path translation.
        .env("PYTHONUTF8", "1")
        // Belt-and-suspenders: also force stdio encoding for any sub-process
        // the sidecar spawns (PyInstaller may not honor PYTHONUTF8 itself).
        .env("PYTHONIOENCODING", "utf-8")
        // Backstop: if the wait task is ever dropped with the child still
        // running (tokio runtime shutdown at app exit), kill the child rather
        // than leak a live Python process that keeps running ML jobs — and
        // possibly writing tags — with nobody draining its pipes.
        .kill_on_drop(true)
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

    let (kill_tx, kill_rx) = oneshot::channel::<()>();
    let inner = Arc::new(Inner {
        next_id: AtomicU64::new(1),
        pending: Mutex::new(HashMap::new()),
        stdin: Mutex::new(stdin),
        binary_path: binary,
        dead: AtomicBool::new(false),
        kill_tx: std::sync::Mutex::new(Some(kill_tx)),
        startup_events: Mutex::new(Vec::new()),
        startup_drained: AtomicBool::new(false),
    });

    // stderr reader: log everything so users can diagnose Python errors.
    // Read RAW bytes and lossy-decode instead of `lines()`: tokio's `lines()`
    // returns Err(InvalidData) on a non-UTF-8 line — and stderr is exactly
    // where native TF/CUDA/essentia code printf's raw bytes — after which the
    // old `while let Ok(Some(..))` loop exited and NOTHING drained the pipe.
    // The OS pipe buffer then filled and the sidecar's next stderr write
    // blocked forever (a silent wedge). Lossy decoding can never error the
    // loop out; we keep consuming stderr for as long as the child lives.
    tauri::async_runtime::spawn(async move {
        let mut reader = BufReader::new(stderr);
        let mut buf: Vec<u8> = Vec::with_capacity(1024);
        loop {
            buf.clear();
            match reader.read_until(b'\n', &mut buf).await {
                Ok(0) => break, // EOF — child closed stderr
                Ok(_) => {
                    let line = String::from_utf8_lossy(&buf);
                    eprintln!("[sidecar] {}", line.trim_end_matches(['\r', '\n']));
                }
                Err(e) => {
                    // A genuine I/O error (not bad UTF-8 — lossy decoding
                    // can't fail): the pipe itself is gone, nothing left to
                    // drain.
                    eprintln!("[sidecar] stderr read error: {e}");
                    break;
                }
            }
        }
    });

    // stdout reader: demux responses + re-emit notifications. When the stream
    // hits EOF the sidecar is dead — flip the dead flag and drain all pending
    // oneshots so in-flight calls fail immediately instead of hanging until
    // the 1-hour RPC timeout. Raw bytes + lossy decode for the same reason as
    // stderr above: native code occasionally writes raw (possibly non-UTF-8)
    // bytes to fd 1, and a `lines()` Err aborted this loop — killing response
    // demuxing for the rest of the app's life while the sidecar lived on. A
    // lossy-decoded noise line simply fails JSON parsing and is skipped; real
    // JSON-RPC frames from the sidecar are always valid UTF-8.
    {
        let inner = inner.clone();
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            let mut reader = BufReader::new(stdout);
            let mut buf: Vec<u8> = Vec::with_capacity(64 * 1024);
            loop {
                buf.clear();
                match reader.read_until(b'\n', &mut buf).await {
                    Ok(0) => {
                        eprintln!(
                            "sidecar stdout EOF — process is dead (binary: {})",
                            inner.binary_path
                        );
                        mark_dead_and_drain(&inner).await;
                        break;
                    }
                    Ok(_) => {
                        let line = String::from_utf8_lossy(&buf).into_owned();
                        if let Err(e) = handle_message(&inner, &app, &line).await {
                            eprintln!("dispatch error on '{line}': {e}");
                        }
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
    // but belt-and-suspenders). The `select!` doubles as the kill path: when
    // `mark_dead_and_drain` fires `kill_tx` (stdout error/EOF with the process
    // still alive), we terminate the child — a sidecar we've told the user is
    // dead ("in-flight request aborted; restart the app") must not keep
    // running its long op and mutating files, and with its stdout no longer
    // drained it would eventually wedge on a full pipe anyway.
    {
        let inner = inner.clone();
        tauri::async_runtime::spawn(async move {
            tokio::select! {
                status = child.wait() => match status {
                    Ok(status) => eprintln!(
                        "sidecar exited with {status} (binary: {})",
                        inner.binary_path
                    ),
                    Err(e) => eprintln!(
                        "sidecar wait failed: {e} (binary: {})",
                        inner.binary_path
                    ),
                },
                _ = kill_rx => {
                    eprintln!(
                        "killing sidecar that was declared dead (binary: {})",
                        inner.binary_path
                    );
                    let _ = child.start_kill();
                    let _ = child.wait().await;
                }
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
    // Ask the wait task to kill the child if it's still running (no-op when
    // the child already exited — the select's wait branch consumed the
    // receiver and this send just fails). take() makes it one-shot.
    if let Some(tx) = inner.kill_tx.lock().expect("kill_tx lock poisoned").take() {
        let _ = tx.send(());
    }
    // Take ownership of the pending map under the lock, then drop the lock
    // before iterating so handlers calling back into call() don't deadlock.
    let drained: HashMap<u64, oneshot::Sender<Value>> = {
        let mut pending = inner.pending.lock().await;
        std::mem::take(&mut *pending)
    };
    let count = drained.len();
    for (id, tx) in drained {
        // Carry the same structured envelope shape Python errors use so the
        // frontend renders a plain headline + a details toggle + a "Restart
        // Vibechek" action (kind=engine_dead). This is the path a mid-analyze
        // death takes — the pending analyze oneshot is drained here, not via
        // the `call()` timeout branch.
        let detail = format!(
            "sidecar died (binary: {}); in-flight request aborted",
            inner.binary_path
        );
        let headline = "The analysis service stopped unexpectedly and this action didn't finish.";
        let err = json!({
            "jsonrpc": "2.0",
            "id": id,
            "error": {
                "code": -32000,
                "message": headline,
                "data": {
                    "kind": "engine_dead",
                    "headline": headline,
                    "detail": detail,
                }
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

    // native CUDA/TF/essentia code occasionally writes raw bytes
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

    // Response: has `id` and either `result` or `error`.
    //
    // We always send numeric (`u64`) ids, but accept a stringified id
    // defensively: a JSON-RPC peer (or a future sidecar build) is free to echo
    // the id back as a string like `"5"`, and JS round-trips through Number can
    // also stringify. Without this, `as_u64()` returns None, we fall through to
    // the notification branch, the response is silently dropped, and the caller
    // hangs until its per-method timeout. Coerce a numeric-string id back to
    // u64 so it matches the pending map.
    if let Some(id_val) = msg.get("id") {
        let id = id_val
            .as_u64()
            .or_else(|| id_val.as_str().and_then(|s| s.trim().parse::<u64>().ok()));
        if let Some(id) = id {
            let mut pending = inner.pending.lock().await;
            if let Some(tx) = pending.remove(&id) {
                let _ = tx.send(msg);
            } else {
                // Expected when a slow op finished just after its wall-clock
                // timeout dropped the pending entry (the result is discarded —
                // the caller already got a timeout error). Not an error.
                eprintln!("[sidecar] late/duplicate response for id {id} (likely post-timeout) — ignoring");
            }
            return Ok(());
        }
        // A non-null id we couldn't coerce to u64 (e.g. a non-numeric string).
        // We never issue such ids, so this is a protocol oddity worth logging
        // rather than silently treating as a notification.
        if !id_val.is_null() {
            eprintln!("[sidecar] response with non-numeric id {id_val} — cannot match a pending request; ignoring");
            return Ok(());
        }
    }

    // Notification: emit as Tauri event. Includes `progress` (long ops),
    // `ready` (startup), and `notify` (startup warnings).
    if let Some(method) = msg.get("method").and_then(|v| v.as_str()) {
        let params = msg.get("params").cloned().unwrap_or(Value::Null);

        // Startup notifications (`ready`/`notify`) fire before the webview has
        // registered its listeners; Tauri does not queue events for late
        // subscribers, so emitting them live would drop them on a fast sidecar
        // start. Buffer until the frontend's one-time drain. The flag is
        // re-checked under the buffer lock so a drain racing this push either
        // includes the event or leaves us to emit it live — never both/neither.
        if (method == "ready" || method == "notify")
            && !inner.startup_drained.load(Ordering::Acquire)
        {
            let mut buf = inner.startup_events.lock().await;
            if !inner.startup_drained.load(Ordering::Acquire) {
                if buf.len() < 64 {
                    buf.push(json!({ "method": method, "params": params }));
                }
                return Ok(());
            }
        }

        let event_name = format!("sidecar:{method}");
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
    // Truncate by CHARS, not bytes: `&line[..200]` panics when byte 200 is
    // not a UTF-8 char boundary — and native noise is exactly where non-ASCII
    // (Cyrillic/CJK/accented track paths) shows up. That panic unwound the
    // stdout reader task, silently killing response demuxing for the rest of
    // the app's life.
    let mut truncated: String = line.chars().take(200).collect();
    if truncated.len() < line.len() {
        truncated.push('…');
    }
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

    // Development fallback: a bare `vibechek` on PATH. Only use it if it
    // actually resolves — spawning a name that isn't there would surface as an
    // opaque OS "program not found" mid-startup. When nothing is found we fail
    // here with a clear message so the setup handler can show the install
    // dialog (K1) instead of spawning a nonexistent command.
    if let Some(found) = find_on_path("vibechek") {
        return Ok(found);
    }

    Err(anyhow!(
        "could not locate the vibechek analysis service. Looked at: VIBECHEK_SIDECAR, \
         a bundled `vibechek-sidecar` next to the app executable, and `vibechek` on PATH."
    ))
}

/// Resolve an executable `name` against the `PATH` env var, honoring the
/// platform's executable extensions (`.exe`/`.bat`/`.cmd` on Windows via
/// `PATHEXT`, bare name elsewhere). Returns the first match's full path.
fn find_on_path(name: &str) -> Option<String> {
    let path = std::env::var_os("PATH")?;
    // On Windows, a bare `vibechek` on the command line resolves via PATHEXT;
    // replicate that here so we don't reject a real install just because the
    // literal `vibechek` (no extension) isn't a file.
    #[cfg(windows)]
    let exts: Vec<String> = std::env::var("PATHEXT")
        .unwrap_or_else(|_| ".EXE;.BAT;.CMD".to_string())
        .split(';')
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect();
    #[cfg(not(windows))]
    let exts: Vec<String> = Vec::new();

    for dir in std::env::split_paths(&path) {
        // Bare name first (POSIX, or a Windows file that already has an ext).
        let bare = dir.join(name);
        if bare.is_file() {
            return Some(bare.to_string_lossy().into_owned());
        }
        for ext in &exts {
            let candidate = dir.join(format!("{name}{ext}"));
            if candidate.is_file() {
                return Some(candidate.to_string_lossy().into_owned());
            }
        }
    }
    None
}
