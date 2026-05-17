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
    let response = state
        .sidecar
        .call(&method, params)
        .await
        .map_err(|e| format!("sidecar error: {e}"))?;

    // JSON-RPC: response has either `result` or `error`
    if let Some(error) = response.get("error") {
        return Err(format!(
            "{}",
            serde_json::to_string(error).unwrap_or_else(|_| "<unrepresentable>".into())
        ));
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
