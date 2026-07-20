//! Tauri commands exposed to the React frontend.
//!
//! The frontend invokes `rpc_call` with a method name + params; we forward to
//! the Python sidecar and return the response (or error) verbatim. Progress
//! notifications come back to the frontend via Tauri events named
//! `sidecar:progress` (see `sidecar::handle_message`).

use crate::AppState;
use serde_json::Value;
use tauri::State;

#[tauri::command]
pub async fn rpc_call(
    state: State<'_, AppState>,
    method: String,
    params: Option<Value>,
) -> Result<Value, String> {
    let params = params.unwrap_or(Value::Object(Default::default()));
    // Transport failures come back as a structured `TransportError`; serialize
    // it into the SAME `{message, data:{kind, headline, detail}}` envelope
    // Python errors use, so the frontend's single RpcError parser produces a
    // plain headline + a details toggle + a Restart/Retry action for every
    // path. (No more `"sidecar error:"` free-text prefix — the frontend used to
    // strip it with two ad-hoc regexes.)
    let response = state
        .sidecar
        .call(&method, params)
        .await
        .map_err(|e| e.into_envelope_json())?;

    // JSON-RPC: response has either `result` or `error`
    if let Some(error) = response.get("error") {
        return Err(
            serde_json::to_string(error).unwrap_or_else(|_| "<unrepresentable>".into())
        );
    }
    Ok(response
        .get("result")
        .cloned()
        .unwrap_or(Value::Null))
}

#[tauri::command]
pub fn sidecar_status(state: State<'_, AppState>) -> Value {
    serde_json::json!({
        "binary": state.sidecar.binary_path(),
    })
}

/// Drain the sidecar's buffered startup notifications (`ready` / `notify`).
///
/// Those fire at sidecar startup — typically before the webview has mounted
/// and registered its `sidecar:*` listeners, and Tauri events are not queued
/// for late subscribers. The shell buffers them; the frontend collects them
/// here once after mount. Each entry is `{"method": ..., "params": ...}`.
/// After the first drain, subsequent notifications emit live as events.
#[tauri::command]
pub async fn drain_startup_notifications(
    state: State<'_, AppState>,
) -> Result<Vec<Value>, String> {
    Ok(state.sidecar.drain_startup_notifications().await)
}
