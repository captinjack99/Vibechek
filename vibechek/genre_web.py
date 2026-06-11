"""Online web-synthesis genre resolver (the ~60% "catalog lookup" classifier).

For a track's artist+title, a local LLM reads keyless web-search snippets and
synthesizes the musically-specific Beatport-style subgenre, distrusting
commercial chart buckets ("Dance/Pop") and verifying the result actually matches
the track (evidence grounding). Output feeds `genres.reconcile_genre`'s web tier.

Design for productionisation:
  * The reconcile/canon logic is pure-python (testable). Only the LLM HTTP call
    and `ddgs` are lazy + mockable.
  * `resolve()` is TOTALLY non-throwing: any failure (no Ollama, no network,
    throttle, bad JSON) returns an empty genre so analysis falls back to the
    audio read. It must never take down an analyze run.
  * LLM backend is pluggable; "ollama" (local, private, the shipped backend) is
    implemented. A BYO cloud-key backend can slot into `_llm_chat`.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
DEFAULT_MODEL = "qwen2.5:7b"

# Controlled genre vocabulary the LLM must choose from (matches the CLAP
# reference + genres.py taxonomy). Kept in sync with build_reference.py classes.
VOCAB: tuple[str, ...] = (
    "Tech House", "Bass House", "Deep House", "Progressive House",
    "Melodic House & Techno", "Funky House", "Afro House", "Future House",
    "Electro House", "Organic House", "House", "Techno", "Minimal",
    "Trance", "Psytrance", "Drum & Bass", "Dubstep", "Melodic Dubstep",
    "Midtempo Bass", "Nu-Disco", "Disco", "Indie Dance", "Future Rave",
    "Big Room", "Electronica", "Electro", "Breaks", "Hardstyle", "Hardcore",
    "Pop", "Hip Hop", "Trap",
)

_SYSTEM = (
    "You are a professional DJ music librarian who classifies electronic dance "
    "tracks into precise Beatport-style subgenres.\n"
    "RULES:\n"
    f"1. Output exactly ONE genre from this controlled list: {', '.join(VOCAB)}.\n"
    "2. Commercial chart buckets — 'Dance/Pop', 'Pop', 'Dance', 'Dance/Electronic', "
    "'EDM', 'Electronic' — are SALES categories, not musical genres. Distrust them and "
    "pick the specific musical subgenre the track actually is.\n"
    "3. The file's existing tag is a useful hint but may be wrong or too generic.\n"
    "4. An audio classifier's guess is given; it usually gets the family right but the "
    "exact subgenre wrong — treat it as weak evidence.\n"
    "5. GROUNDING: set \"source_matched\" true ONLY IF the web results contain an "
    "EXPLICIT genre for THIS EXACT track (a result whose title matches BOTH this artist "
    "AND title, from a DJ/catalog source: Beatport, Traxsource, Beatsource, "
    "1001tracklists, RateYourMusic, Junodownload, Discogs). Otherwise false; never invent "
    "a genre from the title. With no web results, source_matched is always false.\n"
    'Respond as JSON: {"genre": "<one from the list>", "confidence": <0.0-1.0>, '
    '"source_matched": <true|false>, "evidence": "<exact source+genre used, or empty>"}.'
)


def _canon(g: str | None) -> str:
    """Map an LLM/web genre string onto the known taxonomy (best-effort).

    Exact (case/separator-insensitive) vocab match first, then the LONGEST
    vocab entry contained in the answer. Never the reverse containment — that
    direction mapped generic answers onto the first *specific* subgenre that
    happened to contain them ("house" → "Tech House", "disco" → "Nu-Disco"),
    and the wrong subgenre then flowed into reconciliation as the web read.
    """
    from vibechek.genres import DJ_GENRE_MAP  # noqa: PLC0415

    t = (g or "").strip()
    if not t:
        return ""
    norm = re.sub(r"[-_/]+", " ", t.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    for v in VOCAB:
        if norm == v.lower():
            return DJ_GENRE_MAP.get(v, v)
    best = ""
    for v in VOCAB:
        if v.lower() in norm and len(v) > len(best):
            best = v
    if best:
        return DJ_GENRE_MAP.get(best, best)
    return DJ_GENRE_MAP.get(t, t)


def ensure_backend(backend: str = "ollama", timeout: float = 3.0) -> bool:
    """True when the LLM backend is reachable; best-effort starts the managed
    local Ollama when it isn't. NEVER raises.

    The resolver setup nohup-starts `ollama serve` exactly once — it dies with
    the WSL VM (reboot, `wsl --shutdown`), which used to leave the web lookup
    silently dead: every track still paid a web search, then the LLM call
    failed and fell back to audio with no signal. Callers should skip the web
    tier entirely (loudly) when this returns False.
    """
    if backend != "ollama":
        return False
    import urllib.request  # noqa: PLC0415

    def _up() -> bool:
        try:
            with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=timeout):  # noqa: S310
                return True
        except Exception:  # noqa: BLE001
            return False

    if _up():
        return True
    exe = Path.home() / "ollama" / "bin" / "ollama"   # the no-sudo managed install
    if not exe.is_file():
        return False
    try:
        import os  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        log_path = Path.home() / ".vibechek" / "ollama.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as lf:
            subprocess.Popen(  # noqa: S603
                [str(exe), "serve"],
                env={**os.environ, "OLLAMA_HOST": "127.0.0.1:11434"},
                stdout=lf, stderr=lf,
                start_new_session=True,   # detach: survives this process
            )
        for _ in range(20):
            time.sleep(0.5)
            if _up():
                log.info("Restarted the managed Ollama server for genre lookup")
                return True
    except Exception as e:  # noqa: BLE001
        log.debug("Could not start the managed Ollama server: %s", e)
    return False


def _ollama_chat(system: str, user: str, model: str, timeout: float = 60.0) -> dict:
    """Call a local Ollama server, force JSON, return the parsed object."""
    import urllib.request  # noqa: PLC0415

    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False, "format": "json",
        "options": {"temperature": 0, "num_predict": 200},
    }
    req = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (localhost)
        resp = json.loads(r.read())
    content = resp["message"]["content"]
    try:
        return json.loads(content)
    except Exception:
        return {"genre": content.strip()[:40], "confidence": 0.3, "source_matched": False}


def _llm_chat(system: str, user: str, backend: str, model: str, timeout: float) -> dict:
    if backend == "ollama":
        return _ollama_chat(system, user, model, timeout)
    raise RuntimeError(f"unsupported genre LLM backend: {backend!r}")


def _ddgs_snippets(query: str, n: int = 6) -> str:
    """Keyless web search → newline-joined 'title: body' snippets (lazy ddgs).

    One retry with a short sleep: sequential per-track queries over a large
    library WILL hit transient rate limits, and without the retry every track
    after the first throttle silently degrades to an ungrounded LLM guess.
    """
    try:
        from ddgs import DDGS  # noqa: PLC0415
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore  # noqa: PLC0415
    res = None
    last: Exception | None = None
    for attempt in range(2):
        try:
            with DDGS() as d:
                res = d.text(query, max_results=n)
            break
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt == 0:
                time.sleep(2.0)
    if res is None:
        raise last if last else RuntimeError("ddgs returned nothing")
    lines = []
    for r in res:
        t = (r.get("title") or "")[:90]
        b = (r.get("body") or "")[:220].replace("\n", " ")
        lines.append(f"- {t}: {b}")
    return "\n".join(lines) if lines else "(no results)"


def _evidence_supports(genre: str, evidence: str) -> bool:
    """Does the cited evidence text actually NAME the genre? Filters the model's
    hallucinated `source_matched` claims (evidence with no genre stated)."""
    g, ev = genre.lower(), (evidence or "").lower()
    if not ev:
        return False
    if g in ev:
        return True
    words = [w for w in re.split(r"[ &]+", g) if len(w) > 3]
    return bool(words) and all(w in ev for w in words)


def resolve(
    artist: str,
    title: str,
    tag: str = "",
    audio_genre: str = "",
    *,
    backend: str = "ollama",
    model: str = DEFAULT_MODEL,
    use_web: bool = True,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Resolve a grounded genre for one track. NEVER raises — returns
    {genre, confidence, source_matched, used_web}; genre="" means "no read,
    fall back to audio"."""
    empty = {"genre": "", "confidence": 0.0, "source_matched": False, "used_web": False}
    if not (artist and title):
        return empty
    base = (f"Artist: {artist}\nTitle: {title}\n"
            f"File tag: {tag or '(none)'}\nAudio-classifier guess: {audio_genre or '(none)'}\n"
            "Classify this track.")
    try:
        snippets = ""
        used_web = False
        if use_web:
            try:
                snippets = _ddgs_snippets(f"{artist} {title} genre beatport")
                used_web = not snippets.startswith("(")
            except Exception as e:  # noqa: BLE001 — web is best-effort
                log.debug("ddgs lookup failed (%s - %s): %s", artist, title, e)
        user = base
        if used_web:
            user += ("\n\nWeb search results (ignore non-matching songs):\n" + snippets
                     + "\n\nClassify using only results that match this exact track.")
        obj = _llm_chat(_SYSTEM, user, backend, model, timeout)
    except Exception as e:  # noqa: BLE001 — never break analysis
        log.debug("web genre resolve failed (%s - %s): %s", artist, title, e)
        return empty

    genre = _canon(obj.get("genre"))
    if not genre:
        return {**empty, "used_web": used_web}
    # Grounding requires ACTUAL web results: with no snippets a model can still
    # claim source_matched + fabricate plausible "evidence" text, which would
    # pass the text gate and earn a tag-override authority it never had.
    src = (
        used_web
        and bool(obj.get("source_matched"))
        and _evidence_supports(genre, str(obj.get("evidence", "")))
    )
    try:
        conf = float(obj.get("confidence", 0.5) or 0.5)
    except (TypeError, ValueError):
        conf = 0.5
    return {"genre": genre, "confidence": conf, "source_matched": src, "used_web": used_web}


__all__ = ["resolve", "ensure_backend", "VOCAB", "DEFAULT_MODEL"]
