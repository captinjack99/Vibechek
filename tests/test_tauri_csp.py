"""Guard the Tauri Content-Security-Policy so audio preview can't regress.

Track preview uses WaveSurfer's WebAudio backend, which fetches the track over
the `asset:` protocol and THEN fetches a `blob:` URL to feed the decoder. The
CSP `connect-src` directive must allow BOTH schemes; if `blob:` is missing the
decode fetch is silently CSP-blocked and WaveSurfer fires neither `ready` nor
`error`, so the player sticks on "Loading…" for 15s and then times out.

Regression: the beta.10 CSP fix added `asset:`/`asset.localhost` to connect-src
(fixing the asset fetch) but omitted `blob:`, leaving every preview to hang.
"""
from __future__ import annotations

import json
from pathlib import Path

CONF = Path(__file__).resolve().parents[1] / "ui" / "src-tauri" / "tauri.conf.json"


def _find_csp(obj) -> str | None:
    """Locate the (single) `csp` string anywhere in the config tree."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "csp" and isinstance(v, str):
                return v
            found = _find_csp(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_csp(item)
            if found:
                return found
    return None


def _connect_src() -> str:
    csp = _find_csp(json.loads(CONF.read_text(encoding="utf-8")))
    assert csp, "no csp string found in tauri.conf.json"
    for directive in csp.split(";"):
        directive = directive.strip()
        if directive.startswith("connect-src"):
            return directive
    return ""


def test_connect_src_allows_asset_scheme() -> None:
    cs = _connect_src()
    assert cs, "no connect-src directive in the CSP"
    assert "asset:" in cs, "asset: scheme missing — the audio asset fetch is blocked"


def test_connect_src_allows_blob_for_waveform_decode() -> None:
    cs = _connect_src()
    assert "blob:" in cs, (
        "blob: missing from connect-src — WaveSurfer's WebAudio decode fetch is "
        "CSP-blocked, so track preview hangs 15s then times out"
    )
