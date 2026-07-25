"""Online genre lookup — the DETERMINISTIC web tier.

For a track's artist+title, this reads the *structured genre field* off catalog
pages (Beatport first) and returns it only when the page provably describes THIS
track. No model is involved in the answer: a search, a few polite page fetches, a
bounded regex over the labelled field, an identity gate, and a chart-bucket
refusal. Output feeds `genres.reconcile_genre`'s web tier.

Why deterministic (measured 2026-07-24, 86-track adjudicated corpus — see
internal/MODEL_PORTFOLIO_2026-07-21.md addenda and internal/bughunt/v2_score_*.log):

  * shipping prefer_tag baseline                      52.3% exact / 70.9% family
  * v1 (this module's old LLM snippet synthesis)      59.3 / 72.7
  * full LLM design (voting + full pages + verifier)  ~71-73 / ~84-87 at 71 s/track
  * THIS tier — regex the tier-A genre field, verify
    identity on-page, refuse chart buckets, no LLM    73.3 / 87.2

Verification is the entire gain; the votes, the page prompts and the model itself
added ~nothing on top of it, at roughly 5x the cost. So the LLM is no longer in
`resolve()`. Its helpers (`ensure_backend`, `_ollama_chat`, `_llm_chat`, `_SYSTEM`)
stay in the module body for now — a deliberate later cleanup, not a live path.

Design notes for the shipping path:
  * Everything except `ddgs` (search) and `bs4` (HTML → text) is stdlib, and both
    are imported lazily so this module stays import-clean.
  * `resolve()` is TOTALLY non-throwing: any failure (no ddgs, no network,
    throttle, 403, malformed page) returns an empty genre so analysis falls back
    to tags + the audio read. It must never take down an analyze run.
  * Fetch policy: only URLs the search returned (no crawling), robots.txt
    honoured, one request per host per 1.5 s, 10 s timeout, capped body size.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retained LLM plumbing — NOT called by resolve(). See the module docstring.
# ---------------------------------------------------------------------------
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
DEFAULT_MODEL = "qwen2.5:7b"

# Controlled genre vocabulary a web read must land in (matches the CLAP
# reference + genres.py taxonomy). A parsed field that canons outside this list
# is discarded rather than passed through.
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
    """Map a web genre string onto the known taxonomy (best-effort).

    Exact (case/separator-insensitive) vocab match first, then the LONGEST
    vocab entry contained in the answer. Never the reverse containment — that
    direction mapped generic answers onto the first *specific* subgenre that
    happened to contain them ("house" → "Tech House", "disco" → "Nu-Disco"),
    and the wrong subgenre then flowed into reconciliation as the web read.

    Both sides of the exact rung are separator-normalized, so a catalog field
    that reads "Nu Disco" matches the vocab entry "Nu-Disco" instead of falling
    through to the containment rung and losing specificity to plain "Disco".
    """
    from vibechek.genres import DJ_GENRE_MAP  # noqa: PLC0415

    t = (g or "").strip()
    if not t:
        return ""
    norm = re.sub(r"[-_/]+", " ", t.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    for v in VOCAB:
        vn = re.sub(r"\s+", " ", re.sub(r"[-_/]+", " ", v.lower())).strip()
        if norm in (v.lower(), vn):
            return DJ_GENRE_MAP.get(v, v)
    best = ""
    for v in VOCAB:
        if v.lower() in norm and len(v) > len(best):
            best = v
    if best:
        return DJ_GENRE_MAP.get(best, best)
    return DJ_GENRE_MAP.get(t, t)


def ensure_backend(backend: str = "ollama", timeout: float = 3.0) -> bool:
    """DEPRECATED — the online genre lookup no longer uses a local LLM.

    Kept so any out-of-tree caller keeps working; `resolve()` never calls it and
    readiness is now `resolver_ready()`. True when the LLM backend is reachable;
    best-effort starts the managed local Ollama when it isn't. NEVER raises.
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
                log.info("Restarted the managed Ollama server")
                return True
    except Exception as e:  # noqa: BLE001
        log.debug("Could not start the managed Ollama server: %s", e)
    return False


def _ollama_chat(system: str, user: str, model: str, timeout: float = 60.0) -> dict:
    """DEPRECATED (see module docstring). Call a local Ollama server, force JSON."""
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
    """DEPRECATED (see module docstring)."""
    if backend == "ollama":
        return _ollama_chat(system, user, model, timeout)
    raise RuntimeError(f"unsupported genre LLM backend: {backend!r}")


def _evidence_supports(genre: str, evidence: str) -> bool:
    """DEPRECATED (see module docstring). Does the cited evidence NAME the genre?"""
    g, ev = genre.lower(), (evidence or "").lower()
    if not ev:
        return False
    if g in ev:
        return True
    words = [w for w in re.split(r"[ &]+", g) if len(w) > 3]
    return bool(words) and all(w in ev for w in words)


# ---------------------------------------------------------------------------
# Web-fetch policy
# ---------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
      "Gecko) Chrome/126.0 Safari/537.36")
FETCH_TIMEOUT = 10.0
MAX_FETCH_ATTEMPTS = 6        # try up to this many result URLs...
MAX_PAGES_PER_QUERY = 3       # ...and keep the first N that actually returned text
MAX_HTML_BYTES = 1_500_000
DOMAIN_MIN_INTERVAL = 1.5     # seconds between hits on one host
SEARCH_SLEEP = 2.0            # between ddgs calls
FETCH_THREADS = 4

# rateyourmusic's robots.txt prohibits ANY automated access to the service — that
# prohibition is about the service, not the transport, so it is never fetched and
# never used as evidence on any route (snippets and archives included).
HARD_BLOCK = frozenset({"rateyourmusic.com"})

# Trust tiers. A = may override a specific tag; B = may fill a generic/missing
# tag only; C = never evidence.
TIER_A = frozenset({"beatport.com", "traxsource.com"})
TIER_B = frozenset({"discogs.com", "junodownload.com", "beatsource.com"})

# Retailer SALES buckets, refused as evidence on every route. "electronic"
# matters specifically because Discogs files everything under Genre: Electronic
# and the canon maps the substring "electro" → "Electro", which would fabricate a
# real subgenre out of a bucket label. Beatport never says bare "Electronic" (it
# says "Electronica"), so this does not touch the tier-A numbers.
CHART_BUCKET = re.compile(r"\bdance pop\b|\bdance electronic\b|\bedm\b|\belectronic dance\b")
BUCKET_EXACT = frozenset({"electronic", "dance", "edm", "pop", "electronica dance",
                          "dance electronic", "electronic dance music", "music", "other"})

# Beatport/Traxsource-style detail block: "Label : X Genre : Tech House BPM: 126"
FIELD_A = re.compile(r"Genre\s*:\s*(.{2,60}?)\s+(?:BPM\b|Length\s*:|Released\s*:|Key\s*:)", re.I)
# Generic labelled field for everything else. Non-greedy WITH a stop-word
# lookahead: a Discogs page reads "Genre: Electronic Style: House, Tech House",
# and a greedy capture swallowed "Electronic Style" — which is not in
# BUCKET_EXACT, so the bucket refusal missed it and the canon turned the
# substring "electro" into a real subgenre. That artifact was the source of every
# "Discogs says Electro" record in the first tier-B measurement. Bound the
# capture, then refuse bucket words per segment.
FIELD_GEN = re.compile(
    r"\b(?:Genres?|Styles?)\s*[:–\-]\s*([A-Za-z0-9 ,&/'’\-]{2,60}?)"
    r"(?=\s*(?:Styles?\b|Genres?\b|Released\b|Country\b|Label\b|Format\b|Year\b|"
    r"Tracklist\b|Credits\b|Notes\b|BPM\b|Key\b|Length\b|[.|]|$))", re.I)

# The two search phrasings the measurement used, in order. The second only runs
# when the first left the track unresolved.
QUERIES: tuple[Any, ...] = (
    lambda a, t: f"{a} {t} genre beatport",
    lambda a, t: f'{a} "{t}" style',
)


# ---------------------------------------------------------------------------
# Normalization — shared by the identity gate and the quote check
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    """Case/punct/accent-insensitive, whitespace-collapsed."""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_FEAT = re.compile(r"\s*[\(\[]?\s*(feat\.?|ft\.?|featuring|with)\s+[^)\]]*[\)\]]?\s*", re.I)
_MIXQ = re.compile(r"\s*[\(\[]\s*(extended|original|radio|club|instrumental|vip|dub)\s*"
                   r"(mix|edit|version|cut)?\s*[\)\]]", re.I)


def _title_core(title: str) -> str:
    """The title with feat-credits and mix qualifiers stripped, normalized."""
    t = _FEAT.sub(" ", str(title))
    t = _MIXQ.sub(" ", t)
    t = re.sub(r"\s*-\s*(extended|original|radio|club)\s+(mix|edit|version)\s*$", " ", t,
               flags=re.I)
    return _norm(t)


def _artist_names(artist: str) -> list[str]:
    """Each collaborating artist, feat-credits removed, normalized."""
    parts = re.split(r"\s*(?:,|&|\bx\b|\bvs\.?\b|\bwith\b|/|\+)\s*",
                     _FEAT.sub(" ", str(artist)))
    return [_norm(p) for p in parts if len(_norm(p)) >= 3]


def _host_of(url: str) -> str:
    import urllib.parse  # noqa: PLC0415

    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _in_domain_set(host: str, domains: frozenset[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _domain_tier(url: str) -> str:
    """'A' (may override), 'B' (may fill), 'X' (never fetched), 'C' (never evidence)."""
    h = _host_of(url)
    if _in_domain_set(h, HARD_BLOCK):
        return "X"
    if _in_domain_set(h, TIER_A):
        return "A"
    if _in_domain_set(h, TIER_B):
        return "B"
    return "C"


def _iri_to_uri(url: str) -> str:
    """ddgs hands back IRIs with raw non-ASCII; urllib needs percent-encoded ASCII."""
    import urllib.parse  # noqa: PLC0415

    try:
        sp = urllib.parse.urlsplit(url)
        netloc = sp.netloc.encode("idna").decode("ascii") if any(
            ord(c) > 127 for c in sp.netloc) else sp.netloc
        return urllib.parse.urlunsplit((
            sp.scheme, netloc,
            urllib.parse.quote(sp.path, safe="/%:@!$&'()*+,;=~"),
            urllib.parse.quote(sp.query, safe="=&%:@!$'()*+,;/?~"),
            ""))
    except Exception:  # noqa: BLE001
        return url


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def _ddgs_results(query: str, n: int = 6) -> list[dict[str, str]]:
    """Keyless web search → [{title, body, url}] (lazy ddgs import).

    One retry with a short sleep: sequential per-track queries over a large
    library WILL hit transient rate limits, and without the retry every track
    after the first throttle silently degrades to no web read at all.
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
    return [{"title": (r.get("title") or "")[:200],
             "body": (r.get("body") or "")[:400].replace("\n", " "),
             "url": (r.get("href") or r.get("url") or "")}
            for r in res]


def _ddgs_snippets(query: str, n: int = 6) -> str:
    """DEPRECATED (see module docstring) — snippet block for the retired LLM prompt."""
    rows = _ddgs_results(query, n)
    lines = [f"- {r['title'][:90]}: {r['body'][:220]}" for r in rows]
    return "\n".join(lines) if lines else "(no results)"


# ---------------------------------------------------------------------------
# Polite fetching — result URLs only, robots honoured, per-host rate limit
# ---------------------------------------------------------------------------
_last_hit: dict[str, float] = {}
_host_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()
_robots_cache: dict[str, list[tuple[str, str]]] = {}
_robots_lock = threading.Lock()


def _host_lock(h: str) -> threading.Lock:
    with _locks_lock:
        return _host_locks.setdefault(h, threading.Lock())


def _robots_rules(host: str) -> list[tuple[str, str]]:
    """Rules for `User-agent: *` → [(allow|disallow, path)]. An unreachable
    robots.txt means 'unknown', which we treat as allow (the site's own 403 then
    decides). Cached per process."""
    with _robots_lock:
        if host in _robots_cache:
            return _robots_cache[host]
    import urllib.request  # noqa: PLC0415

    try:
        req = urllib.request.Request(f"https://{host}/robots.txt",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:  # noqa: S310
            txt = r.read(200_000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        txt = f"# unreachable: {type(e).__name__}"
    rules: list[tuple[str, str]] = []
    in_star = saw_star = False
    for raw_line in txt.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, val = line.partition(":")
        field, val = field.strip().lower(), val.strip()
        if field == "user-agent":
            if saw_star and in_star and val != "*":
                in_star = False
            elif val == "*":
                in_star = saw_star = True
            else:
                in_star = False
        elif in_star and field in ("allow", "disallow") and val:
            rules.append((field, val))
    with _robots_lock:
        _robots_cache[host] = rules
    return rules


def _robots_allows(url: str) -> bool:
    import urllib.parse  # noqa: PLC0415

    host = _host_of(url)
    if not host:
        return False
    path = urllib.parse.urlsplit(url).path or "/"
    best, verdict = -1, True
    for kind, pat in _robots_rules(host):
        pfx = pat.split("*", 1)[0]
        if path.startswith(pfx) and len(pfx) > best:
            best, verdict = len(pfx), (kind == "allow")
    return verdict


def _extract_text(html: str) -> str:
    """Readable text of one page: <title>, the description/keywords meta tags,
    any ld+json blocks, then the body text with scripts/styles removed."""
    from bs4 import BeautifulSoup  # noqa: PLC0415

    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []
    if soup.title and soup.title.string:
        parts.append(soup.title.string)
    for m in soup.find_all("meta"):
        if (m.get("name") or m.get("property") or "").lower() in (
                "description", "og:description", "keywords"):
            parts.append(m.get("content") or "")
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        parts.append((s.string or "")[:3000])
    for t in soup(["script", "style", "noscript", "svg", "template"]):
        t.decompose()
    parts.append(soup.get_text(" "))
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()[:200_000]


def _fetch_page(url: str) -> dict[str, Any]:
    """Fetch ONE search-result URL. No crawling: links out of the page are never
    followed. NEVER raises — failures come back as an empty `text`."""
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    out: dict[str, Any] = {"url": url, "host": _host_of(url), "tier": _domain_tier(url),
                           "final_url": url, "status": 0, "err": "", "text": ""}
    if out["tier"] == "X":
        out["err"] = "robots.txt prohibits automated access"
        return out
    if not url.lower().startswith(("http://", "https://")):
        out["err"] = "non-http url"
        return out
    try:
        if not _robots_allows(url):
            out["err"] = "robots.txt disallow"
            return out
        h = out["host"]
        with _host_lock(h):
            gap = DOMAIN_MIN_INTERVAL - (time.time() - _last_hit.get(h, 0.0))
            if gap > 0:
                time.sleep(gap)
            try:
                req = urllib.request.Request(_iri_to_uri(url), headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "identity",
                })
                with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:  # noqa: S310
                    raw = r.read(MAX_HTML_BYTES)
                    enc = r.headers.get_content_charset() or "utf-8"
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    out["status"] = r.status
                    out["final_url"] = r.geturl()
                if "html" not in ctype and "xml" not in ctype and "json" not in ctype:
                    out["err"] = f"non-html content-type: {ctype[:40]}"
                else:
                    out["text"] = _extract_text(raw.decode(enc, "replace"))
            except urllib.error.HTTPError as e:
                out["status"], out["err"] = e.code, f"HTTP {e.code}"
            except Exception as e:  # noqa: BLE001
                out["err"] = f"{type(e).__name__}: {e}"[:160]
            finally:
                _last_hit[h] = time.time()
    except Exception as e:  # noqa: BLE001 — robots probe / lock bookkeeping
        out["err"] = f"{type(e).__name__}: {e}"[:160]
    # A redirect can land on a different domain than the result URL promised.
    out["tier"] = _domain_tier(out.get("final_url") or url)
    return out


# ---------------------------------------------------------------------------
# The deterministic read: identity gate → field parse → bucket refusal
# ---------------------------------------------------------------------------
def _identity_grade(ntext: str, artist: str, title: str) -> str:
    """'full' = artist AND title present on the page, '' = not this track.

    'title-only' (title present but no artist name fits) is a WEAKER grade and is
    never silently promoted — only `_identity_ok` (full) licenses evidence.
    """
    tc = _title_core(title)
    if tc and tc not in ntext:
        return ""
    names = _artist_names(artist)
    if not names or any(a in ntext for a in names):
        return "full"
    return "title-only"


def _identity_ok(ntext: str, artist: str, title: str) -> bool:
    return _identity_grade(ntext, artist, title) == "full"


def _bucket_refused(raw: str) -> bool:
    """True when the string is a retailer sales bucket, not a musical genre."""
    n = _norm(raw)
    return bool(CHART_BUCKET.search(n)) or n in BUCKET_EXACT


def _parse_field(text: str, tier: str) -> tuple[str, str, str] | None:
    """Deterministic labelled-genre extraction.

    Returns (canon_genre, raw_field, quote) or None. `quote` is the whole matched
    span, kept so the caller can re-verify it as a substring of the fetched page
    text — the same substring discipline the retired LLM quotes were held to.
    """
    for pat in ((FIELD_A, FIELD_GEN) if tier == "A" else (FIELD_GEN, FIELD_A)):
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1).strip(" |,-")
        if _bucket_refused(raw):
            return None
        # Canon the SEGMENTS, not the whole field. The canon is substring-based,
        # so canon("Electronic") → "Electro": a top-level retailer bucket becomes
        # a real subgenre. Refusing bucket words per segment is what stops that.
        segs = [s.strip() for s in re.split(r"[,/|]|\s-\s", raw) if s.strip()] or [raw]
        for seg in segs:
            if _bucket_refused(seg):
                continue
            g = _canon(seg)
            if g in VOCAB:
                return g, raw, m.group(0).strip()
    return None


def _quote_verified(quote: str, text: str) -> bool:
    """The quote must be a real, normalized span of the page we actually fetched."""
    nq = _norm(quote)
    return bool(nq) and nq in _norm(text)


def _consensus(hits: list[dict[str, Any]]) -> str:
    """The most frequent genre among `hits`; "" on a tie."""
    if not hits:
        return ""
    import collections  # noqa: PLC0415

    c = collections.Counter(h["genre"] for h in hits).most_common()
    return c[0][0] if (len(c) == 1 or c[0][1] > c[1][1]) else ""


def _two_domain(hits: list[dict[str, Any]]) -> str:
    """A genre two DISTINCT evidence domains agree on; "" if none (or several)."""
    import collections  # noqa: PLC0415

    by: dict[str, set] = collections.defaultdict(set)
    for h in hits:
        if h["tier"] in ("A", "B"):
            by[h["genre"]].add(h["host"])
    win = [g for g, d in by.items() if len(d) >= 2]
    return win[0] if len(win) == 1 else ""


def resolver_ready() -> bool:
    """True when the online genre lookup has everything it needs to run.

    Replaces the old `ensure_backend` probe: the tier needs `ddgs` (search) and
    `bs4` (HTML → text), not a local LLM. Uses `find_spec` so probing never
    imports the packages. Callers should skip the tier entirely (loudly) when
    this is False rather than pay a web search per track for nothing.
    """
    import importlib.util  # noqa: PLC0415

    try:
        search = any(importlib.util.find_spec(m) is not None
                     for m in ("ddgs", "duckduckgo_search"))
        parser = importlib.util.find_spec("bs4") is not None
    except (ImportError, ValueError):  # namespace/partial installs
        return False
    return bool(search and parser)


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
    """Resolve a verified genre for one track from catalog pages. NEVER raises.

    Returns {genre, confidence, source_matched, used_web, url, quote, tier}.
    `genre=""` means "no read — fall back to tags + audio".

    `source_matched` is the grounding flag `reconcile_genre` reads as
    `web_grounded`, and it is now strictly stronger than it was: it means the
    genre came out of a catalog page's structured genre field, the quote naming
    it is a verbatim span of the page bytes we fetched, and that page names both
    this artist and this title. A tier-B-only read is returned with
    `source_matched=False` so it can fill a generic/missing tag but can never
    override a specific one.

    `backend`, `model` and `timeout` are accepted for call-site compatibility and
    ignored — no model is involved. `use_web=False` disables the tier entirely
    (there is no offline read to fall back on).
    """
    empty: dict[str, Any] = {"genre": "", "confidence": 0.0, "source_matched": False,
                             "used_web": False, "url": "", "quote": "", "tier": ""}
    if not (artist and title) or not use_web:
        return empty

    hits: list[dict[str, Any]] = []
    used_web = False
    try:
        seen_urls: set[str] = set()
        for qi, qf in enumerate(QUERIES):
            if qi:
                time.sleep(SEARCH_SLEEP)   # politeness between searches
            try:
                results = _ddgs_results(qf(artist, title))
            except Exception as e:  # noqa: BLE001 — search is best-effort
                log.debug("web search failed (%s - %s): %s", artist, title, e)
                results = []
            if not results:
                continue
            used_web = True
            # Authoritative domains first: the page cap would otherwise be spent
            # on youtube/spotify shells that never state a genre.
            ranked = sorted(
                (("A", "B", "C", "X").index(_domain_tier(r["url"])), ri, r["url"])
                for ri, r in enumerate(results) if r["url"] and r["url"] not in seen_urls
            )
            cands = [u for _, _, u in ranked][:MAX_FETCH_ATTEMPTS]
            seen_urls.update(cands)
            if not cands:
                continue
            with ThreadPoolExecutor(max_workers=FETCH_THREADS) as ex:
                metas = list(ex.map(_fetch_page, cands))
            kept = 0
            for p in metas:
                if not p["text"] or kept >= MAX_PAGES_PER_QUERY:
                    continue
                kept += 1
                if not _identity_ok(_norm(p["text"]), artist, title):
                    continue
                got = _parse_field(p["text"], p["tier"])
                if not got or not _quote_verified(got[2], p["text"]):
                    continue
                hits.append({"genre": got[0], "raw": got[1], "quote": got[2],
                             "tier": p["tier"], "host": p["host"],
                             "url": p.get("final_url") or p["url"]})
            # Short-circuit: once a tier-A page (or two agreeing domains) has
            # answered, the second search phrasing buys nothing.
            if _consensus([h for h in hits if h["tier"] == "A"]) or _two_domain(hits):
                break
    except Exception as e:  # noqa: BLE001 — the web tier must never break analysis
        log.debug("online genre lookup failed (%s - %s): %s", artist, title, e)
        return empty

    def _pick(hs: list[dict[str, Any]], genre: str) -> dict[str, Any]:
        h = next(h for h in hs if h["genre"] == genre)
        return {"url": h["url"], "quote": h["quote"], "tier": h["tier"]}

    tier_a = [h for h in hits if h["tier"] == "A"]
    ga = _consensus(tier_a)
    if ga:
        return {"genre": ga, "confidence": 0.95, "source_matched": True,
                "used_web": True, **_pick(tier_a, ga)}
    g2 = _two_domain(hits)
    if g2:
        return {"genre": g2, "confidence": 0.9, "source_matched": True,
                "used_web": True, **_pick(hits, g2)}
    tier_b = [h for h in hits if h["tier"] == "B"]
    gb = _consensus(tier_b)
    if gb:
        # One tier-B catalog alone fills a generic/missing tag but is not allowed
        # to override a specific one — hence source_matched=False.
        return {"genre": gb, "confidence": 0.6, "source_matched": False,
                "used_web": True, **_pick(tier_b, gb)}
    return {**empty, "used_web": used_web}


__all__ = ["resolve", "resolver_ready", "ensure_backend", "VOCAB", "DEFAULT_MODEL"]
