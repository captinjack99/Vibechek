"""Import DJ-tool metadata as tag-tier priors (trust-UX #3).

Rekordbox keeps the genre/key/comment edits a DJ makes in its OWN database —
they are never written back to the audio files, so Vibechek's tag reader can't
see exactly the hand-curation "augment, don't overwrite" exists to preserve.
The Rekordbox collection XML export carries them per track; this module parses
it and merges the values into each record's ``existing_tags`` so the SAME
reconciliation the file tags get (``genres.reconcile_genre`` at the "tag"
tier, key surfacing via ``_reconcile_record_key``) applies to them.

What gets imported, and why only that (measured on the 72-track gold corpus,
see internal/bughunt/score_tag_priors.py):

- **Genre** — the one field where curated tags beat the audio model (the
  shipping ``prefer_tag`` policy). The Rekordbox value REPLACES the file tag at
  the tag tier when they differ (the DJ edited it in Rekordbox on purpose);
  ``existing_tags["genre_origin"] = "rekordbox"`` records where it came from.
- **Key (Tonality)** — fills the tag key only when the file has none. Never
  preferred over audio (tag keys measured 49% exact vs audio 63%; wrong 10:1
  on disagreement) — it feeds the read-only ml_key_tag/ml_key_conflict
  surfacing instead.
- **Comments** — parsed for Mixed In Key's "Energy N" (1-10) into
  ``energy_mik``; the raw comment itself is not stored.

Priors persist in a sidecar next to the library's analysis JSON
(``<analysis>.priors.json``) and are re-merged on every future analyze (the
RPC layer loads the sidecar and hands it to ``analyze_directory``), so a
re-scan doesn't lose the import. The CLI ``analyze`` path doesn't consult
sidecars — priors are a GUI-library concept.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from vibechek.io import atomic_write_json

log = logging.getLogger(__name__)

PRIORS_SUFFIX = ".priors.json"


def norm_path(p: Any) -> str:
    """One normalization for matching XML locations to analyzed paths."""
    return os.path.normcase(os.path.normpath(str(p)))


def priors_path_for(analysis_path: Path | str) -> Path:
    p = Path(analysis_path)
    return p.with_name(p.stem + PRIORS_SUFFIX)


def load_priors(analysis_path: Path | str) -> dict[str, dict[str, Any]]:
    """The persisted priors map for a library (empty when absent/corrupt).

    Corrupt is WARN-and-empty rather than raise: a broken sidecar must never
    make the library unanalyzable — the user can simply re-import.
    """
    path = priors_path_for(analysis_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("priors sidecar is not an object")
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError, json.JSONDecodeError) as e:
        log.warning("Ignoring unreadable priors sidecar %s: %s", path, e)
        return {}


def save_priors(analysis_path: Path | str, priors: dict[str, dict[str, Any]]) -> Path:
    path = priors_path_for(analysis_path)
    atomic_write_json(path, priors, indent=2, ensure_ascii=False)
    return path


_DRIVE_PREFIX_RE = re.compile(r"^/[A-Za-z]:")


def _location_to_path(location: str) -> str | None:
    """Rekordbox TRACK Location (``file://localhost/D:/My%20Music/x.flac``)
    → a local filesystem path. Returns None for empty/unusable values."""
    if not location:
        return None
    if location.startswith("file:"):
        parsed = urlparse(location)
        path = unquote(parsed.path)
        if _DRIVE_PREFIX_RE.match(path):
            path = path[1:]  # "/D:/Music/…" → "D:/Music/…"
        return path or None
    return unquote(location)


def parse_rekordbox_collection(xml_path: Path | str) -> dict[str, dict[str, Any]]:
    """Parse a Rekordbox collection XML into ``{norm_path: prior}``.

    A prior carries only the non-empty fields we import: ``genre``, ``key``
    (from Tonality), ``energy_mik`` (parsed from Comments). Tracks with no
    usable field are dropped. Raises ``CdjExportError`` on a non-Rekordbox
    file (same validation as the CDJ export path).
    """
    from vibechek.analyzer import _parse_mik_energy  # noqa: PLC0415 — deferred, no import cycle
    from vibechek.cdj_export import parse_rekordbox_xml  # noqa: PLC0415

    tree = parse_rekordbox_xml(Path(xml_path))
    root = tree.getroot()
    collection = root.find("COLLECTION")
    tracks = collection.iter("TRACK") if collection is not None else root.iter("TRACK")

    out: dict[str, dict[str, Any]] = {}
    for tr in tracks:
        loc = _location_to_path(tr.get("Location") or "")
        if not loc:
            continue  # playlist entries reference by Key=, no Location
        prior: dict[str, Any] = {}
        genre = (tr.get("Genre") or "").strip()
        if genre:
            prior["genre"] = genre
        key = (tr.get("Tonality") or "").strip()
        if key:
            prior["key"] = key
        mik = _parse_mik_energy(tr.get("Comments"))
        if mik is not None:
            prior["energy_mik"] = mik
        if prior:
            out[norm_path(loc)] = prior
    return out


def merge_prior_into_record(record: dict[str, Any], prior: dict[str, Any]) -> bool:
    """Merge one prior into a track record's existing_tags, in place.

    Genre replaces the tag-tier value when it differs (a deliberate Rekordbox
    edit supersedes the file tag; provenance recorded in ``genre_origin``).
    Key and energy_mik only FILL — the file's own tag wins when present.
    Returns True when anything changed (drives idempotent re-imports).
    """
    et = record.setdefault("existing_tags", {})
    changed = False

    genre = (prior.get("genre") or "").strip()
    if genre and (et.get("genre") or "").strip().casefold() != genre.casefold():
        et["genre"] = genre
        et["genre_origin"] = "rekordbox"
        changed = True

    key = (prior.get("key") or "").strip()
    if key and not et.get("key"):
        et["key"] = key
        changed = True

    mik = prior.get("energy_mik")
    if mik is not None and et.get("energy_mik") is None:
        et["energy_mik"] = mik
        changed = True

    return changed


def merge_priors_into_results(
    results: list[dict[str, Any]], priors: dict[str, dict[str, Any]]
) -> int:
    """Analyze-time hook: merge the sidecar priors into fresh records before
    the final reconcile pass (analyze_directory calls this; reconciliation
    then treats the merged values exactly like file tags). Returns the number
    of records a prior applied to."""
    n = 0
    for r in results:
        prior = priors.get(norm_path(r.get("path") or ""))
        if prior and merge_prior_into_record(r, prior):
            n += 1
    return n


def apply_priors_to_report(
    report: dict[str, Any],
    priors: dict[str, dict[str, Any]],
    policy: str,
    override_conf: float,
) -> tuple[list[dict[str, Any]], int]:
    """Import-time path: merge priors into a SAVED analysis and re-reconcile
    the affected records (genre + key surfacing), in place.

    Returns ``(updated_records, matched_count)`` — matched counts every record
    a prior exists for, updated only those that actually changed.
    """
    from vibechek.analyzer import (  # noqa: PLC0415 — deferred, no import cycle
        _reconcile_record_genre,
        _reconcile_record_key,
    )

    updated: list[dict[str, Any]] = []
    matched = 0
    for t in report.get("tracks") or []:
        prior = priors.get(norm_path(t.get("path") or ""))
        if not prior:
            continue
        matched += 1
        if merge_prior_into_record(t, prior):
            _reconcile_record_genre(t, policy, override_conf)
            _reconcile_record_key(t)
            updated.append(t)
    return updated, matched
