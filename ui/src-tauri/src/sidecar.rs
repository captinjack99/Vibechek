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
    atomic::{AtomicU64, Ordering},
    Arc,
};
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{ChildStdin, Command};
use tokio::sync::{oneshot, Mutex};

const RPC_TIMEOUT_SECS: u64 = 60 * 60; // 1 hour — analyses on big libraries

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
}

impl SidecarHandle {
    pub fn binary_path(&self) -> &str {
        &self.inner.binary_path
    }

    /// Send a JSON-RPC request and await its response.
    pub async fn call(&self, method: &str, params: Value) -> Result<Value> {
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

        match tokio::time::timeout(Duration::from_secs(RPC_TIMEOUT_SECS), rx).await {
            Ok(Ok(value)) => Ok(value),
            Ok(Err(_canceled)) => {
                // Receiver dropped — sidecar must have died
                Err(anyhow!("sidecar dropped the response channel"))
            }
            Err(_elapsed) => {
                // Drop the pending entry so we don't leak it
                let mut pending = self.inner.pending.lock().await;
                pending.remove(&id);
                Err(anyhow!("sidecar call '{}' timed out", method))
            }
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
    });

    // stderr reader: just log everything so users can diagnose Python errors
    tauri::async_runtime::spawn(async move {
        let mut reader = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = reader.next_line().await {
            eprintln!("[sidecar] {line}");
        }
    });

    // stdout reader: demux responses + re-emit notifications
    {
        let inner = inner.clone();
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            let mut reader = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                if let Err(e) = handle_message(&inner, &app, &line).await {
                    eprintln!("dispatch error on '{line}': {e}");
                }
            }
            eprintln!("sidecar stdout closed; further calls will hang until timeout");
        });
    }

    // Wait task: log when child exits (so users see it in dev)
    tauri::async_runtime::spawn(async move {
        match child.wait().await {
            Ok(status) => eprintln!("sidecar exited with {status}"),
            Err(e) => eprintln!("sidecar wait failed: {e}"),
        }
    });

    Ok(SidecarHandle { inner })
}

async fn handle_message(inner: &Arc<Inner>, app: &AppHandle, line: &str) -> Result<()> {
    let line = line.trim();
    if line.is_empty() {
        return Ok(());
    }
    let msg: Value = serde_json::from_str(line)
        .with_context(|| format!("parse sidecar line: {line}"))?;

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

    // Notification: emit as Tauri event
    if let Some(method) = msg.get("method").and_then(|v| v.as_str()) {
        let event_name = format!("sidecar:{method}");
        let params = msg.get("params").cloned().unwrap_or(Value::Null);
        app.emit(&event_name, params)
            .with_context(|| format!("emit {event_name}"))?;
    }

    Ok(())
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
