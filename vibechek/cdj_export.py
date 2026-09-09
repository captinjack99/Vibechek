"""FLAC -> CDJ export: transcode a FLAC library to AIFF and rewrite a Rekordbox
XML so the cues/beatgrids survive, letting DJs play FLAC collections on older
Pioneer CDJs (CDJ-2000nexus and friends) that cannot read FLAC.

End-to-end workflow
-------------------
1. In Rekordbox: ``File -> Export Collection in xml format`` to dump your
   collection (and its analysis: beatgrids + cues) to a ``rekordbox.xml``.
2. Run this tool::

       vibechek cdj-export rekordbox.xml --out ./cdj_export

   For every FLAC ``TRACK`` it transcodes the audio to a 16-bit AIFF under
   ``--out`` and writes a new ``<out>/rekordbox_cdj.xml`` whose FLAC tracks now
   point at those AIFFs. Non-FLAC tracks pass through untouched.
3. Back in Rekordbox: ``File -> Import Collection`` (or import the playlist
   tree from the new XML). Rekordbox reads the AIFF tracks *with their original
   beatgrids and cues already attached* — no re-analysis needed.
4. Export those tracks to a USB stick for the CDJ as usual. The CDJ plays AIFF
   natively, so the FLAC-only limitation is gone.

Why AIFF and not MP3 (this is the whole point)
----------------------------------------------
Rekordbox beatgrids (``TEMPO``) and cue points (``POSITION_MARK``) are stored as
**sample/second offsets into the decoded PCM stream**. AIFF (PCM) is a *bit-for-
bit identical* decode of the FLAC audio: same sample rate, same sample count,
sample 0 of the AIFF == sample 0 of the FLAC decode. So every grid anchor and
cue lands on the exact same audio it did before — we copy ``TEMPO`` and
``POSITION_MARK`` across verbatim with **zero offset math**.

MP3 cannot do this. The MP3 codec adds encoder/decoder priming + padding
(~1105 samples, ~26 ms at 44.1 kHz) and the frame structure shifts the audio,
so cue points and the beatgrid drift by tens of milliseconds — audible and
unacceptable for beatmatching. Lossy quality loss aside, that timing shift alone
rules MP3 out. AIFF keeps the grid sample-exact.

Transcoder gating
-----------------
The actual decode/encode is done by ``soundfile`` (libsndfile) when it is
importable -- it reads FLAC and writes AIFF while preserving the exact frame
count. ``soundfile`` is an *optional* dependency (``pip install vibechek[cdj]``);
if it is missing we fall back to an ``ffmpeg`` subprocess. If neither is
available we raise :class:`CdjExportError` explaining how to install one. The
import of ``soundfile`` is deliberately deferred to call time so importing this
module never requires the optional dep.

Safety
------
This operation is strictly additive: it only ever *writes into* ``out_dir`` and
*reads* the source FLACs and the input XML. Source audio files and the input XML
are never modified, so there is nothing to undo -- re-running is safe and a
botched run is thrown away by deleting ``out_dir``.

Note on pyrekordbox
-------------------
We parse and rewrite the Rekordbox XML with the stdlib ``xml.etree`` rather than
``pyrekordbox`` to keep dependencies light and the rewrite auditable: we only
touch ``Location``/``Kind``/``Size`` on FLAC tracks and pass everything else
(``TEMPO``, ``POSITION_MARK``, playlists) through unchanged.
"""

from __future__ import annotations

import logging
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from vibechek.utils import find_executable

log = logging.getLogger(__name__)

# Rekordbox's <TRACK Kind="..."> value for an AIFF file. This exact string is
# what Rekordbox itself writes for AIFFs, so importing the rewritten XML treats
# the track as a native AIFF.
AIFF_KIND = "AIFF File"

# Above this we down-sample to 44.1 kHz (see _target_samplerate). CDJ-2000nexus
# era hardware tops out at 48 kHz; high-res 88.2/96 kHz FLACs must be reduced.
MAX_CDJ_SAMPLERATE = 48000

# Progress callback: (done, total, current_filename) -> None. Matches the shape
# the rest of the package uses (see vibechek.utils.ProgressCallback) so the CLI
# progress bar and the future GUI can subscribe identically.
ProgressCallback = Callable[[int, int, str], None]


class CdjExportError(Exception):
    """Raised when CDJ export cannot proceed (no transcoder, bad XML, ...)."""


@dataclass
class TrackError:
    """One per-track failure, collected so a single bad file doesn't abort the
    whole export."""

    location: str
    message: str


@dataclass
class CdjExportResult:
    """Summary of an :func:`export_for_cdj` run.

    Attributes:
        flac_converted: FLAC tracks successfully transcoded to AIFF (0 in a dry
            run -- planning only).
        flac_planned: FLAC tracks that *would* be converted (set in dry runs;
            equals flac_converted on a real run with no errors).
        passthrough: non-FLAC tracks left untouched.
        skipped: FLAC tracks skipped because the source file was missing or its
            ``Location`` could not be parsed.
        errors: count of per-track failures (also see ``track_errors``).
        output_xml: path to the written ``rekordbox_cdj.xml`` (None on dry run).
        out_dir: the output directory.
        track_errors: per-track error detail.
        resampled: source locations whose audio came out at a LOWER sample rate
            than the source (hi-res, or an unprobeable source that got the
            CDJ-safe 44.1 kHz forced on it). This is an irreversible quality
            reduction the caller should tell the user about.
        renamed: destination filenames that had to be disambiguated (``_1``,
            ``_2``, …) because ``out_dir`` already held that name, or because two
            sources in this run share a stem. Nothing is ever overwritten.
    """

    flac_converted: int = 0
    flac_planned: int = 0
    passthrough: int = 0
    skipped: int = 0
    errors: int = 0
    output_xml: Path | None = None
    out_dir: Path | None = None
    track_errors: list[TrackError] = field(default_factory=list)
    resampled: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Rekordbox file:// URI <-> filesystem path
# ---------------------------------------------------------------------------
#
# Rekordbox stores track locations as percent-encoded ``file://localhost/...``
# URIs. The path component after ``localhost`` is an *absolute* path:
#   - Windows: ``file://localhost/C:/Users/dj/track.flac``  (note the drive
#     letter immediately after the slash, forward slashes throughout)
#   - macOS/Linux: ``file://localhost/Users/dj/track.flac``
# Characters like spaces, ``#`` and non-ASCII are percent-encoded.


def location_to_path(location: str) -> Path:
    """Convert a Rekordbox ``file://localhost/...`` URI to a concrete ``Path``.

    Handles Windows drive-letter URIs (``file://localhost/C:/...``) and POSIX
    URIs, and percent-decodes the path. Raises :class:`CdjExportError` on a URI
    we don't understand rather than silently producing a bad path.

    Returns a concrete ``Path`` so callers can ``.exists()`` / ``.stat()`` it
    directly. We build it from the forward-slash string (NOT via ``PureWindowsPath``
    — that converts to backslashes, which on Linux/macOS becomes a single mangled
    ``PosixPath`` segment; that was the original cross-platform bug). On Windows a
    drive URI yields a ``WindowsPath`` with ``.drive == "C:"``; on POSIX hosts the
    string (and so ``.name`` and matching) is preserved verbatim, and a Windows
    URI processed on a POSIX host simply won't resolve to a local file — the
    correct outcome, since its audio isn't present there anyway.
    """
    parsed = urllib.parse.urlsplit(location)
    if parsed.scheme != "file":
        raise CdjExportError(f"Not a file:// location: {location!r}")
    # urlsplit puts "localhost" in netloc and the rest in path. Some exports use
    # the bare ``file:///C:/...`` form (empty netloc) -- both are valid.
    raw = urllib.parse.unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        # A non-localhost host is a network location (NAS/UNC library):
        # ``file://NAS/music/x.flac`` -> ``\\NAS\music\x.flac``. Dropping the host
        # yielded ``/music/x.flac``, a path that exists nowhere. Same fix
        # ``tag_priors._location_to_path`` already carries for the import side.
        return Path(f"//{parsed.netloc}{raw}")
    # raw is like "/C:/Users/dj/x.flac" on Windows or "/Users/dj/x.flac" on POSIX.
    if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        # Windows drive path: strip the leading slash -> "C:/Users/dj/x.flac".
        # Keep forward slashes (do NOT route through PureWindowsPath).
        return Path(raw[1:])
    return Path(raw)


def path_to_location(path: Path) -> str:
    """Convert a filesystem Path to a Rekordbox ``file://localhost/...`` URI.

    Inverse of :func:`location_to_path`. Produces the exact percent-encoded,
    forward-slash form Rekordbox expects, including the drive letter on Windows,
    and the ``file://HOST/share/...`` form for a UNC (NAS) path.
    """
    abs_path = path.resolve()
    raw = str(abs_path)
    if raw.startswith(("\\\\", "//")):
        # UNC: the FIRST component is a host, not a folder. `lstrip("/")` on the
        # pathname2url output collapsed every leading slash, so \\NAS\music was
        # written as file://localhost/NAS/music -- i.e. C:\NAS\music, a path that
        # exists nowhere. Every track of a NAS export became a missing file in
        # Rekordbox, silently: the export reports 0 errors either way.
        host, _, rest = raw.replace("\\", "/").lstrip("/").partition("/")
        return f"file://{host}/{urllib.parse.quote(rest)}"
    # pathname2url gives "/C:/Users/dj/x.aiff" on Windows (with drive) and
    # "/Users/dj/x.aiff" on POSIX, percent-encoding unsafe characters.
    url_path = urllib.request.pathname2url(raw)
    # On Windows pathname2url yields "///C:/..." (it prepends slashes for the
    # would-be host); normalize to a single leading slash so we control the host.
    url_path = "/" + url_path.lstrip("/")
    return f"file://localhost{url_path}"


# ---------------------------------------------------------------------------
# Transcode
# ---------------------------------------------------------------------------


def _target_samplerate(src_rate: int) -> int:
    """CDJ-safe sample rate: keep <=48 kHz as-is, otherwise drop to 44.1 kHz.

    >48 kHz (88.2/96/192 kHz hi-res FLAC) is not playable on CDJ-2000nexus-era
    hardware, so we down-sample to 44.1 kHz. NOTE: a sample-rate *change* alters
    the sample count, so a track that gets down-sampled will have its cue/grid
    sample offsets rescaled -- see export_for_cdj where such tracks are flagged.
    """
    return src_rate if src_rate <= MAX_CDJ_SAMPLERATE else 44100


def _ffmpeg_path() -> str | None:
    """Absolute path to `ffmpeg`, or None.

    `find_executable` rather than `shutil.which`: on Windows the cwd is searched
    ahead of PATH, so an `ffmpeg.exe` sitting in a freshly unpacked sample-pack
    folder (the very kind of folder an export runs against) would be executed
    once per transcoded track. It also absolutizes, so the resolved program does
    not re-resolve against wherever the subprocess happens to start.
    """
    return find_executable("ffmpeg")


def _have_ffmpeg() -> bool:
    return _ffmpeg_path() is not None


def _soundfile_module():
    """Return the imported ``soundfile`` module, or None if unavailable.

    Deferred import so this module imports without the optional ``cdj`` extra.
    """
    try:
        import soundfile  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- ImportError or a libsndfile load error
        return None
    return soundfile


def transcode_to_aiff(src: Path, dst: Path) -> None:
    """Losslessly transcode a FLAC file to a 16-bit AIFF, sample-accurately.

    Sample accuracy is the core guarantee: the AIFF has the *exact same* frame
    count and sample rate as the FLAC decode (unless the source is >48 kHz, in
    which case it is down-sampled to 44.1 kHz -- see :func:`_target_samplerate`),
    so Rekordbox grid/cue sample offsets stay valid.

    Prefers ``soundfile`` (libsndfile); falls back to ``ffmpeg`` on PATH. Raises
    :class:`CdjExportError` if neither is available.

    Args:
        src: source ``.flac`` file.
        dst: destination ``.aiff`` file (parent dir is created).
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    sf = _soundfile_module()
    if sf is not None:
        _transcode_soundfile(sf, src, dst)
        return
    if _have_ffmpeg():
        _transcode_ffmpeg(src, dst)
        return
    raise CdjExportError(
        "No FLAC->AIFF transcoder available. Install the optional 'cdj' extra "
        "(`pip install vibechek[cdj]`, which pulls in soundfile), or put "
        "`ffmpeg` on your PATH."
    )


def _transcode_soundfile(sf, src: Path, dst: Path) -> None:
    """Decode FLAC -> int16 PCM -> AIFF via libsndfile, preserving frame count.

    Reading as ``dtype='int16'`` quantizes to 16-bit (CDJ-friendly, matches
    what Rekordbox would export) without changing the number of frames. Writing
    with ``format='AIFF', subtype='PCM_16'`` yields a standard AIFF the CDJ and
    Rekordbox both read. If the source rate is >48 kHz we resample to 44.1 kHz.
    """
    info = sf.info(str(src))
    target_rate = _target_samplerate(info.samplerate)
    data = sf.read(str(src), dtype="int16")[0] if target_rate == info.samplerate else None
    if data is not None:
        # No rate change: frame-for-frame identical, the sample-accurate path.
        sf.write(str(dst), data, info.samplerate, format="AIFF", subtype="PCM_16")
        return
    # >48 kHz source: read as float, resample to 44.1 kHz, then write. This path
    # intentionally changes the frame count; callers flag such tracks.
    float_data, src_rate = sf.read(str(src), dtype="float32")
    resampled = _resample_float(float_data, src_rate, target_rate)
    sf.write(str(dst), resampled, target_rate, format="AIFF", subtype="PCM_16")


def _resample_float(data, src_rate: int, dst_rate: int):
    """Linear-interpolation resample of a (frames,) or (frames, channels) array.

    Deliberately simple (no extra deps): used only for the rare >48 kHz hi-res
    case, where the goal is CDJ playability rather than mastering-grade SRC.
    """
    import numpy as np  # noqa: PLC0415 -- soundfile already pulls in numpy

    arr = np.asarray(data)
    n_src = arr.shape[0]
    n_dst = int(round(n_src * dst_rate / src_rate))
    src_idx = np.arange(n_src)
    dst_idx = np.linspace(0, n_src - 1, n_dst)
    if arr.ndim == 1:
        return np.interp(dst_idx, src_idx, arr).astype(arr.dtype)
    cols = [np.interp(dst_idx, src_idx, arr[:, c]) for c in range(arr.shape[1])]
    return np.stack(cols, axis=1).astype(arr.dtype)


def _declared_samplerate(track: ET.Element) -> int | None:
    """The TRACK's declared ``SampleRate``, or None when absent/unparseable.

    A probe-free second opinion on the source rate: Rekordbox writes it, and it
    is the only signal left when neither ffprobe nor mutagen can read the file.
    """
    raw = (track.get("SampleRate") or "").strip()
    try:
        rate = int(float(raw))
    except ValueError:
        return None
    return rate if rate > 0 else None


def _probe_samplerate(src: Path) -> int | None:
    """Best-effort source sample rate WITHOUT soundfile (the ffmpeg path's case).

    The ffmpeg fallback is only ever reached when ``soundfile`` is unavailable,
    so we must not rely on it here. Try ``ffprobe`` (ships alongside ffmpeg),
    then fall back to mutagen's ``FLAC.info.sample_rate``. Returns None if the
    rate genuinely can't be determined, letting the caller choose a CDJ-safe
    default rather than guessing.
    """
    # ffprobe is bundled with ffmpeg and reads the rate straight from the header.
    # Resolved the same cwd-discarding way as ffmpeg itself (see _ffmpeg_path);
    # absent, we simply fall through to the mutagen header read below.
    ffprobe = find_executable("ffprobe")
    if ffprobe:
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=sample_rate",
                    "-of",
                    "default=nw=1:nk=1",
                    str(src),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            out = (proc.stdout or "").strip()
            if proc.returncode == 0 and out.isdigit():
                return int(out)
        except OSError:
            pass
    # Fallback: read the FLAC header with mutagen (a core dependency).
    try:
        from mutagen.flac import FLAC  # noqa: PLC0415

        rate = FLAC(str(src)).info.sample_rate
        if rate:
            return int(rate)
    except Exception:  # noqa: BLE001 -- mutagen failure is non-fatal; fall through
        pass
    return None


def _transcode_ffmpeg(src: Path, dst: Path) -> None:
    """Fallback: ``ffmpeg -i src -c:a pcm_s16be dst``.

    BIG-endian, deliberately. ffmpeg's aiff muxer switches to the AIFF-C variant
    (``FORM….AIFC``, compression type ``sowt``) for any codec that isn't
    big-endian PCM, so ``pcm_s16le`` emitted a structurally different container
    from the primary soundfile path (``FORM….AIFF``) while the rewritten XML
    labels both ``Kind="AIFF File"``. ``pcm_s16be`` makes the two paths agree on
    the format this module exists to produce.

    ffmpeg keeps the source sample rate (and thus frame count) unless we ask
    otherwise; for >48 kHz sources we add ``-ar 44100`` to keep the CDJ happy.

    The source rate is probed via ffprobe/mutagen (NOT soundfile — it is
    guaranteed absent on this path). If the rate can't be determined we still
    force ``-ar 44100``: always CDJ-safe, and far safer than shipping an
    unplayable hi-res AIFF. That IS a real resample for an unprobeable 48 kHz
    source, not a no-op — ``export_for_cdj`` reports it via ``result.resampled``.
    """
    exe = _ffmpeg_path()
    if exe is None:
        # Reached directly (not via transcode_to_aiff's gate) or ffmpeg vanished
        # between the check and here. Say so instead of handing subprocess a
        # bare name that Windows would resolve against the current directory.
        raise CdjExportError(
            "`ffmpeg` is not on your PATH. Install it, or install the optional "
            "'cdj' extra (`pip install vibechek[cdj]`) for the soundfile path."
        )
    sr = _probe_samplerate(src)
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if sr is None or sr > MAX_CDJ_SAMPLERATE:
        cmd += ["-ar", "44100"]
    cmd += ["-c:a", "pcm_s16be", str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as e:
        raise CdjExportError(f"ffmpeg failed to start: {e}") from e
    if proc.returncode != 0:
        raise CdjExportError(
            f"ffmpeg transcode failed ({src.name}): {proc.stderr.strip() or proc.returncode}"
        )


def _copy_tags(src: Path, dst: Path) -> None:
    """Best-effort: copy tags from the FLAC to the new AIFF.

    Uses the tagger's format-complete reader/writer: ``read_all_tags`` on the
    FLAC yields a canonical dict (title/artist/album/genre/bpm/key + every other
    Vorbis comment), and ``write_all_tags`` writes those onto the AIFF's ID3
    chunk (incl. any GEOB/PRIV frames the dict carries). The authoritative
    cue/grid data still rides in the rewritten XML; this is just so the AIFF
    isn't tagless if imported standalone. Failures are non-fatal.
    """
    try:
        from vibechek.tagger import read_all_tags, write_all_tags  # noqa: PLC0415

        tags = read_all_tags(src)
        tags.pop("_unsupported", None)
        tags.pop("_error", None)
        # The snapshot always carries bookkeeping keys (`_snapshot_version`),
        # so "anything to copy?" must look at the real tag fields, not at
        # the dict's truthiness — a tagless FLAC would otherwise get an empty
        # ID3 chunk written into the AIFF for nothing.
        if any(not k.startswith("_") for k in tags):
            write_all_tags(dst, tags)
    except Exception as e:  # noqa: BLE001 -- tags are a nicety, never block export
        log.debug("Tag copy %s -> %s skipped: %s", src, dst, e)


# ---------------------------------------------------------------------------
# Rekordbox XML parse / rewrite
# ---------------------------------------------------------------------------


def parse_rekordbox_xml(path: Path) -> ET.ElementTree:
    """Parse a Rekordbox collection XML into an ElementTree.

    Raises :class:`CdjExportError` (rather than a raw ``ParseError``) on a file
    that isn't well-formed XML or isn't a Rekordbox export.
    """
    path = Path(path)
    how_to = (
        " In Rekordbox: File → Export Collection in xml format, then pick "
        "that file."
    )
    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        # Plain headline + how-to; the parser error / path are DEMOTED to the log.
        log.warning("Not valid XML (%s): %s", path, e)
        raise CdjExportError(
            "That file doesn't look like a Rekordbox collection export." + how_to
        ) from e
    root = tree.getroot()
    if root.tag != "DJ_PLAYLISTS":
        log.warning(
            "Not a Rekordbox XML (root is <%s>, expected <DJ_PLAYLISTS>): %s",
            root.tag, path,
        )
        raise CdjExportError(
            "That file doesn't look like a Rekordbox collection export." + how_to
        )
    return tree


def _is_flac_track(track: ET.Element) -> bool:
    """A TRACK is FLAC if Kind says so or the location ends in .flac.

    Rekordbox writes ``Kind="FLAC"`` for FLACs; we also sniff the extension as a
    belt-and-suspenders check for exports that label Kind differently.
    """
    kind = (track.get("Kind") or "").upper()
    if "FLAC" in kind:
        return True
    loc = (track.get("Location") or "").lower()
    return loc.endswith(".flac")


def _collection_tracks(root: ET.Element) -> list[ET.Element]:
    """The TRACK nodes that describe real audio files, never playlist references.

    A standard Rekordbox export wraps them in ``<COLLECTION>``; a non-standard
    one may omit that wrapper, and falling back to ``root.iter("TRACK")`` is what
    keeps those exports working (without it the discovery pass found zero tracks
    while the rewrite pass happily walked them — a green "Converted 0 FLAC"
    success over an XML still pointing at unplayable FLACs).

    But a bare ``iter()`` also walks the ``<PLAYLISTS>`` tree's
    ``<TRACK Key="1"/>`` *reference* nodes, which carry no Location and point
    back into the collection by TrackID. Counting those inflated `passthrough`
    with entries that are not files at all, so the fallback subtracts them
    explicitly (identity, not value — two reference nodes can look identical).
    """
    collection = root.find("COLLECTION")
    if collection is not None:
        return collection.findall("TRACK")
    playlist_refs = {ref for pl in root.iter("PLAYLISTS") for ref in pl.iter("TRACK")}
    return [t for t in root.iter("TRACK") if t not in playlist_refs]


def rewrite_for_cdj(
    tree: ET.ElementTree,
    src_to_dst: dict[str, Path],
    out_dir: Path,
) -> ET.ElementTree:
    """Return a deep-copied tree with FLAC tracks repointed at their AIFFs.

    For each FLAC ``TRACK`` whose source path is a key in ``src_to_dst`` we
    rewrite ``Location`` to the AIFF's ``file://localhost`` URI, set
    ``Kind="AIFF File"`` and refresh ``Size`` from the AIFF on disk. Non-FLAC
    tracks -- and any FLAC track not present in ``src_to_dst`` (e.g. it failed
    to transcode) -- are left exactly as they were.

    Crucially we DO NOT touch the ``TEMPO`` (beatgrid) or ``POSITION_MARK``
    (cue) child elements: those are sample/second offsets into the decoded PCM,
    and the AIFF is a sample-identical decode of the FLAC, so the offsets remain
    valid byte-for-byte. No cue/grid offset math is needed -- that equivalence
    is the entire reason we target AIFF rather than a lossy/padded format.

    For tracks that were down-sampled (>48 kHz hi-res reduced to 44.1 kHz) the
    on-disk AIFF rate no longer matches the TRACK's declared ``SampleRate``; we
    rewrite ``SampleRate`` to the AIFF's real rate and clear the now-stale
    ``BitRate`` (Rekordbox recomputes it for lossless on import) so the XML does
    not advertise a rate the file no longer has.

    The input ``tree`` is not mutated; a copy is returned.
    """
    out_dir = Path(out_dir)
    # Work on a copy so the caller's parsed tree (and the on-disk input XML)
    # are never modified -- the whole operation must be non-destructive.
    new_root = _deepcopy_element(tree.getroot())
    new_tree = ET.ElementTree(new_root)

    # src_to_dst keys come from location_to_path(...). Normalize once so lookups
    # are robust to case/separator differences in how locations were written.
    norm_map = {_norm_key(Path(k)): v for k, v in src_to_dst.items()}

    # Only rewrite COLLECTION/TRACK entries (they have Location + audio); the
    # PLAYLISTS tree's <TRACK Key="..."/> reference nodes carry no Location and
    # are left untouched — see `_collection_tracks`, shared with the discovery
    # pass so the two can never disagree about what a track is.
    for track in _collection_tracks(new_root):
        if not _is_flac_track(track):
            continue
        location = track.get("Location")
        if not location:
            continue
        try:
            src_path = location_to_path(location)
        except CdjExportError:
            continue
        dst = norm_map.get(_norm_key(src_path))
        if dst is None:
            # FLAC track we didn't (or couldn't) transcode -> leave untouched.
            continue
        track.set("Location", path_to_location(dst))
        track.set("Kind", AIFF_KIND)
        try:
            track.set("Size", str(Path(dst).stat().st_size))
        except OSError:
            # Size is informational for Rekordbox; if the AIFF isn't there
            # (dry-run planning path) keep whatever was there.
            pass
        # If the AIFF's real rate differs from the TRACK's declared SampleRate
        # (a down-sampled >48 kHz hi-res track), correct the metadata so the XML
        # doesn't advertise a rate the file no longer has. Clear the stale
        # BitRate too — Rekordbox recomputes it for lossless on import.
        real_rate = _aiff_samplerate(Path(dst))
        if real_rate is not None:
            declared = track.get("SampleRate")
            if declared is None or declared.strip() != str(real_rate):
                track.set("SampleRate", str(real_rate))
                if track.get("BitRate") is not None:
                    track.set("BitRate", "0")
    return new_tree


def _aiff_samplerate(path: Path) -> int | None:
    """Read an AIFF's sample rate, or None if it can't be determined / absent.

    Used to detect a down-sample so :func:`rewrite_for_cdj` can correct the
    XML's ``SampleRate``. Prefers ``soundfile`` when present, then falls back to
    mutagen's ``AIFF.info.sample_rate`` (soundfile is absent on the ffmpeg path).
    A missing file (e.g. dry-run planning) yields None, leaving metadata as-is.
    """
    sf = _soundfile_module()
    if sf is not None:
        try:
            return int(sf.info(str(path)).samplerate)
        except Exception:  # noqa: BLE001 -- fall through to mutagen
            pass
    try:
        from mutagen.aiff import AIFF  # noqa: PLC0415

        rate = AIFF(str(path)).info.sample_rate
        if rate:
            return int(rate)
    except Exception:  # noqa: BLE001 -- best-effort; None means "leave metadata"
        pass
    return None


def _norm_key(path: Path) -> str:
    """Case-insensitive, separator-insensitive key for path matching.

    Windows filesystems are case-insensitive and Rekordbox may emit either
    slash style; normalize so the rewrite reliably finds the track it just
    transcoded regardless of those cosmetic differences.
    """
    return str(path).replace("\\", "/").casefold()


def _deepcopy_element(elem: ET.Element) -> ET.Element:
    """Deep-copy an ElementTree element.

    ``copy.deepcopy`` works on Element, but recursing explicitly keeps us
    independent of pickling internals and preserves tail/text/attrib exactly --
    which matters because we promise TEMPO/POSITION_MARK pass through verbatim.
    """
    import copy  # noqa: PLC0415

    return copy.deepcopy(elem)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def export_for_cdj(
    rekordbox_xml: Path,
    out_dir: Path,
    *,
    on_progress: ProgressCallback | None = None,
    dry_run: bool = False,
) -> CdjExportResult:
    """Transcode every FLAC in a Rekordbox collection to AIFF and rewrite the XML.

    Output layout: AIFFs are written **flattened** into ``out_dir`` (one folder,
    no source tree mirroring) with the source stem and a ``.aiff`` suffix.
    Stem collisions (two different FLACs named ``intro.flac``) are disambiguated
    by appending ``_1``, ``_2``, ... so no file is silently overwritten. The new
    ``rekordbox_cdj.xml`` is written at ``out_dir/rekordbox_cdj.xml``.

    Safety: this only ever writes inside ``out_dir``; the source FLACs and the
    input XML are never modified.

    Args:
        rekordbox_xml: the collection XML exported from Rekordbox.
        out_dir: directory to write AIFFs + the new XML into (created if absent).
        on_progress: optional ``(done, total, name)`` callback per FLAC track.
        dry_run: if True, plan the work (counts + intended AIFF paths) but write
            no audio and no XML.

    Returns:
        :class:`CdjExportResult` with counts, the output XML path, and per-track
        errors.
    """
    rekordbox_xml = Path(rekordbox_xml)
    out_dir = Path(out_dir)
    tree = parse_rekordbox_xml(rekordbox_xml)
    root = tree.getroot()

    result = CdjExportResult(out_dir=out_dir)

    # First pass: classify tracks and assign collision-free AIFF destinations.
    # Only COLLECTION/TRACK entries carry a Location + audio; the PLAYLISTS tree
    # has its own <TRACK Key="..."/> *reference* nodes (no Location) which we
    # must NOT count or touch — they point back into the collection by TrackID.
    flac_tracks: list[tuple[ET.Element, Path, Path]] = []  # (track, src, dst)
    used_stems: set[str] = set()
    # Same source of truth `rewrite_for_cdj` uses: without the COLLECTION-less
    # fallback, a non-standard export that omits the <COLLECTION> wrapper found
    # zero tracks here while the rewrite pass happily walked them — a green
    # "Converted 0 FLAC" success over an XML still pointing at the unplayable
    # FLACs. `_collection_tracks` keeps the playlist reference nodes out of it.
    for track in _collection_tracks(root):
        location = track.get("Location")
        if not location:
            # A COLLECTION TRACK with no Location can't be exported.
            result.passthrough += 1
            continue
        if not _is_flac_track(track):
            result.passthrough += 1
            continue
        try:
            src = location_to_path(location)
        except CdjExportError:
            result.skipped += 1
            result.track_errors.append(TrackError(location, "unparseable Location URI"))
            continue
        if not src.exists():
            result.skipped += 1
            result.track_errors.append(TrackError(location, f"source file not found: {src}"))
            continue
        dst = _assign_dst(out_dir, src, used_stems)
        if dst.stem.casefold() != src.stem.casefold():
            result.renamed.append(dst.name)
        flac_tracks.append((track, src, dst))

    total = len(flac_tracks)
    src_to_dst: dict[str, Path] = {}

    for i, (track, src, dst) in enumerate(flac_tracks):
        if on_progress:
            on_progress(i, total, src.name)
        if dry_run:
            # Plan only: record the intended mapping so the XML preview is real,
            # but don't touch disk.
            src_to_dst[str(src)] = dst
            result.flac_planned += 1
            continue
        try:
            transcode_to_aiff(src, dst)
            _copy_tags(src, dst)
            src_to_dst[str(src)] = dst
            result.flac_converted += 1
            result.flac_planned += 1
            # Record an irreversible down-sample so the caller/CLI can warn the
            # user. Compare against the rate the AIFF actually came out at, and
            # fall back to the TRACK's declared SampleRate when the source can't
            # be probed: the old test re-called the same deterministic
            # `_probe_samplerate` and required it to exceed 48 kHz, which is
            # exactly the branch the forced `-ar 44100` fires on when the probe
            # returns None — structurally guaranteed to miss the tracks it was
            # written to report.
            aiff_rate = _aiff_samplerate(dst)
            if aiff_rate is not None:
                src_rate = _probe_samplerate(src) or _declared_samplerate(track)
                if src_rate is not None and src_rate > aiff_rate:
                    result.resampled.append(str(src))
        except CdjExportError as e:
            # A missing transcoder is fatal for the whole run -- re-raise so the
            # user gets one clear message instead of N identical per-track errors.
            if "No FLAC->AIFF transcoder" in str(e):
                raise
            result.errors += 1
            result.track_errors.append(TrackError(str(src), str(e)))
        except Exception as e:  # noqa: BLE001 -- one bad file shouldn't kill the run
            result.errors += 1
            result.track_errors.append(TrackError(str(src), str(e)))

    if on_progress and total:
        on_progress(total, total, "done")

    new_tree = rewrite_for_cdj(tree, src_to_dst, out_dir)
    output_xml = out_dir / "rekordbox_cdj.xml"
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_tree(new_tree, output_xml)
        result.output_xml = output_xml
    return result


def _assign_dst(out_dir: Path, src: Path, used_stems: set[str]) -> Path:
    """Pick a collision-free ``<out_dir>/<stem>.aiff`` for a source file.

    Collision-free against BOTH this run's assignments and whatever already sits
    in ``out_dir``. Seeding only from the in-memory set made the module's
    "strictly additive ... no file is silently overwritten" promise true only
    *within one run*: a DJ who keeps FLACs beside their own hand-edited AIFFs and
    exports into that folder had ``Track.flac`` clobber ``Track.aiff`` — both
    writers overwrite unconditionally (``sf.write``; ``ffmpeg -y``).
    """
    stem = src.stem
    candidate = stem
    n = 1
    while candidate.casefold() in used_stems or (out_dir / f"{candidate}.aiff").exists():
        candidate = f"{stem}_{n}"
        n += 1
    used_stems.add(candidate.casefold())
    return out_dir / f"{candidate}.aiff"


def _write_tree(tree: ET.ElementTree, path: Path) -> None:
    """Write the rewritten XML, declaring UTF-8 like Rekordbox does.

    We don't reuse atomic_write_json (that's JSON-only); ElementTree.write with
    a temp-then-replace keeps the write crash-safe and never leaves a half XML.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".partial")
    try:
        tree.write(tmp, encoding="utf-8", xml_declaration=True)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


__all__ = [
    "CdjExportError",
    "CdjExportResult",
    "TrackError",
    "transcode_to_aiff",
    "parse_rekordbox_xml",
    "rewrite_for_cdj",
    "export_for_cdj",
    "location_to_path",
    "path_to_location",
]
