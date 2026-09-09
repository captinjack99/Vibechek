"""Tests for vibechek.cdj_export — FLAC→CDJ (AIFF + rewritten Rekordbox XML).

Two layers:
  - XML rewrite + URI handling: fully exercisable with stdlib + a synthetic
    Rekordbox XML built in-test (no audio, no optional deps).
  - Transcode + orchestration: needs ``soundfile`` to synthesize a tiny FLAC
    and verify sample-accuracy; skipped with a clear reason when it's absent.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from vibechek.cdj_export import (
    AIFF_KIND,
    CdjExportError,
    export_for_cdj,
    location_to_path,
    parse_rekordbox_xml,
    path_to_location,
    rewrite_for_cdj,
    transcode_to_aiff,
)

try:
    import soundfile as _sf  # noqa: F401

    HAVE_SOUNDFILE = True
except Exception:  # noqa: BLE001
    HAVE_SOUNDFILE = False

needs_soundfile = pytest.mark.skipif(
    not HAVE_SOUNDFILE,
    reason="soundfile not installed — install the 'cdj' extra to run transcode tests",
)


# ---------------------------------------------------------------------------
# Synthetic Rekordbox XML fixture
# ---------------------------------------------------------------------------


def _build_rekordbox_xml(flac_location: str, mp3_location: str) -> str:
    """A minimal but realistic Rekordbox collection XML.

    One FLAC track (with a TEMPO beatgrid + two POSITION_MARK cues) and one MP3
    track. Mirrors the real export shape: <DJ_PLAYLISTS><COLLECTION><TRACK ...>.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
  <PRODUCT Name="rekordbox" Version="6.7.7" Company="AlphaTheta"/>
  <COLLECTION Entries="2">
    <TRACK TrackID="1" Name="Deep One" Kind="FLAC" Size="40000000"
           TotalTime="312" SampleRate="44100" BitRate="1411"
           Location="{flac_location}">
      <TEMPO Inizio="0.025" Bpm="124.00" Metro="4/4" Battito="1"/>
      <POSITION_MARK Name="" Type="0" Start="0.025" Num="-1"/>
      <POSITION_MARK Name="Drop" Type="0" Start="64.025" Num="0"/>
    </TRACK>
    <TRACK TrackID="2" Name="MP3 Track" Kind="MP3 File" Size="8000000"
           TotalTime="200" SampleRate="44100" BitRate="320"
           Location="{mp3_location}">
      <TEMPO Inizio="0.011" Bpm="128.00" Metro="4/4" Battito="1"/>
      <POSITION_MARK Name="" Type="0" Start="0.011" Num="-1"/>
    </TRACK>
  </COLLECTION>
  <PLAYLISTS>
    <NODE Type="0" Name="ROOT" Count="1">
      <NODE Name="My Set" Type="1" KeyType="0" Entries="2">
        <TRACK Key="1"/>
        <TRACK Key="2"/>
      </NODE>
    </NODE>
  </PLAYLISTS>
</DJ_PLAYLISTS>
"""


def _track_by_id(root: ET.Element, track_id: str) -> ET.Element:
    for t in root.iter("TRACK"):
        if t.get("TrackID") == track_id:
            return t
    raise AssertionError(f"TRACK {track_id} not found")


# ---------------------------------------------------------------------------
# URI handling
# ---------------------------------------------------------------------------


def test_windows_location_roundtrip_and_parse() -> None:
    from pathlib import PureWindowsPath

    loc = "file://localhost/C:/Users/dj/My%20Music/track.flac"
    p = location_to_path(loc)
    # Drive-letter path recovered, percent-decoded. `location_to_path` returns a
    # concrete (host-flavour) Path, so on a POSIX CI runner `p.drive` is "" —
    # assert the drive via PureWindowsPath(str(p)) so the check is OS-agnostic.
    assert PureWindowsPath(str(p)).drive.upper() == "C:"
    assert p.name == "track.flac"
    assert "My Music" in str(p)


def test_posix_location_parse() -> None:
    loc = "file://localhost/Users/dj/Music/track.flac"
    p = location_to_path(loc)
    assert str(p).replace("\\", "/") == "/Users/dj/Music/track.flac"


def test_network_location_keeps_the_unc_host_on_the_write_side() -> None:
    """REGRESSION: `pathname2url` folds a UNC host into the path, and collapsing
    every leading slash threw it away — ``\\\\NAS\\dj`` was written as
    ``file://localhost/NAS/dj``, i.e. ``C:\\NAS\\dj``. Exporting to a NAS produced
    a Rekordbox collection where every converted track is a missing file, with
    ``errors == 0`` and a green "Wrote …" line. The import side already fixes
    this class of bug (see tag_priors._location_to_path)."""
    from pathlib import PureWindowsPath

    if sys.platform == "win32":
        # The real end-to-end shape, guarded by platform: `path_to_location`
        # starts with `path.resolve()`, and posixpath.realpath collapses the
        # doubled root ("//NAS/..." -> "/NAS/..."), so on Linux/macOS this input
        # can never reach the UNC branch. tests/test_tag_priors.py guards the
        # identical assertion on the import side the same way.
        loc = path_to_location(Path("//NAS/dj/cdj export/X.aiff"))
        assert loc == "file://NAS/dj/cdj%20export/X.aiff"
        back = PureWindowsPath(str(location_to_path(loc)))
        assert str(back) == r"\\NAS\dj\cdj export\X.aiff"

    # Platform-independent cover of the same branch: hand the function exactly
    # what `Path.resolve()` returns on Windows for a UNC input, so the ubuntu
    # and macOS CI legs exercise the write side instead of skipping it.
    class _ResolvesToUnc:
        def resolve(self) -> PureWindowsPath:
            return PureWindowsPath(r"\\NAS\dj\cdj export\X.aiff")

    loc = path_to_location(_ResolvesToUnc())  # type: ignore[arg-type]
    assert loc == "file://NAS/dj/cdj%20export/X.aiff"
    assert str(PureWindowsPath(str(location_to_path(loc)))) == (
        r"\\NAS\dj\cdj export\X.aiff"
    )


def test_network_location_parses_from_either_host_form() -> None:
    """Rekordbox writes ``file://localhost//NAS/...``; we now write
    ``file://NAS/...``. Both must read back as the same UNC path."""
    from pathlib import PureWindowsPath

    for loc in ("file://localhost//NAS/music/x.flac", "file://NAS/music/x.flac"):
        got = PureWindowsPath(str(location_to_path(loc)))
        assert str(got) == r"\\NAS\music\x.flac", loc


def test_path_to_location_is_file_localhost_uri(tmp_path: Path) -> None:
    f = tmp_path / "a b#c.aiff"
    f.write_bytes(b"x")
    loc = path_to_location(f)
    assert loc.startswith("file://localhost/")
    # Special chars are percent-encoded …
    assert " " not in loc
    assert "#" not in loc
    # … and it round-trips back to the same file.
    assert location_to_path(loc).resolve() == f.resolve()


def test_location_to_path_rejects_non_file_uri() -> None:
    with pytest.raises(CdjExportError):
        location_to_path("http://example.com/track.flac")


# ---------------------------------------------------------------------------
# XML rewrite
# ---------------------------------------------------------------------------


def test_rewrite_repoints_flac_and_preserves_cues_grid(tmp_path: Path) -> None:
    src_flac = tmp_path / "song.flac"
    src_flac.write_bytes(b"not really flac, only the rewrite is under test")
    aiff = tmp_path / "out" / "song.aiff"
    aiff.parent.mkdir()
    aiff.write_bytes(b"0123456789")  # 10 bytes — Size should reflect this

    mp3_loc = path_to_location(tmp_path / "beat.mp3")
    xml = _build_rekordbox_xml(path_to_location(src_flac), mp3_loc)
    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(xml, encoding="utf-8")

    tree = parse_rekordbox_xml(xml_path)

    # Capture the FLAC track's TEMPO/POSITION_MARK as serialized bytes BEFORE
    # the rewrite so we can assert byte-for-byte preservation after.
    in_flac = _track_by_id(tree.getroot(), "1")
    grid_before = [ET.tostring(e) for e in in_flac.findall("TEMPO")]
    cues_before = [ET.tostring(e) for e in in_flac.findall("POSITION_MARK")]

    new_tree = rewrite_for_cdj(tree, {str(src_flac): aiff}, tmp_path / "out")
    new_root = new_tree.getroot()

    out_flac = _track_by_id(new_root, "1")
    # Location now points at the AIFF, Kind updated, Size refreshed.
    assert out_flac.get("Kind") == AIFF_KIND
    assert location_to_path(out_flac.get("Location")).resolve() == aiff.resolve()
    assert out_flac.get("Size") == "10"

    # TEMPO (beatgrid) + POSITION_MARK (cues) preserved byte-for-byte — this is
    # the core guarantee: AIFF is a sample-identical decode so no offset math.
    grid_after = [ET.tostring(e) for e in out_flac.findall("TEMPO")]
    cues_after = [ET.tostring(e) for e in out_flac.findall("POSITION_MARK")]
    assert grid_after == grid_before
    assert cues_after == cues_before

    # Non-FLAC (MP3) track is completely untouched.
    out_mp3 = _track_by_id(new_root, "2")
    in_mp3 = _track_by_id(tree.getroot(), "2")
    assert ET.tostring(out_mp3) == ET.tostring(in_mp3)


def test_rewrite_does_not_mutate_input_tree(tmp_path: Path) -> None:
    src_flac = tmp_path / "song.flac"
    src_flac.write_bytes(b"x")
    aiff = tmp_path / "song.aiff"
    aiff.write_bytes(b"y")
    xml = _build_rekordbox_xml(path_to_location(src_flac), path_to_location(tmp_path / "b.mp3"))
    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(xml, encoding="utf-8")

    tree = parse_rekordbox_xml(xml_path)
    original_flac_loc = _track_by_id(tree.getroot(), "1").get("Location")
    rewrite_for_cdj(tree, {str(src_flac): aiff}, tmp_path)
    # The input tree's FLAC Location is unchanged (rewrite works on a copy).
    assert _track_by_id(tree.getroot(), "1").get("Location") == original_flac_loc


def test_parse_rejects_non_rekordbox_xml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.xml"
    bad.write_text("<root><child/></root>", encoding="utf-8")
    with pytest.raises(CdjExportError):
        parse_rekordbox_xml(bad)


def test_windows_drive_location_handled_in_rewrite(tmp_path: Path) -> None:
    """A real Windows ``file://localhost/C:/...`` URI is matched + rewritten."""
    src_flac = tmp_path / "win.flac"
    src_flac.write_bytes(b"x")
    aiff = tmp_path / "win.aiff"
    aiff.write_bytes(b"yy")
    # Force the canonical Windows-style URI regardless of the test host.
    win_loc = "file://localhost/C:/Music/win.flac"
    xml = _build_rekordbox_xml(win_loc, path_to_location(tmp_path / "b.mp3"))
    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(xml, encoding="utf-8")
    tree = parse_rekordbox_xml(xml_path)

    # The src_to_dst key matches what location_to_path produces for win_loc.
    src_key = str(location_to_path(win_loc))
    new_tree = rewrite_for_cdj(tree, {src_key: aiff}, tmp_path)
    assert _track_by_id(new_tree.getroot(), "1").get("Kind") == AIFF_KIND


# ---------------------------------------------------------------------------
# Transcode — sample accuracy is the core guarantee
# ---------------------------------------------------------------------------


@needs_soundfile
def test_transcode_to_aiff_sample_accurate(tmp_path: Path) -> None:
    import numpy as np
    import soundfile as sf

    sr = 44100
    n = 13337  # deliberately non-round frame count
    sine = (0.5 * np.sin(2 * np.pi * 440 * np.arange(n) / sr)).astype("float32")
    flac = tmp_path / "tone.flac"
    sf.write(str(flac), sine, sr, format="FLAC")

    aiff = tmp_path / "out" / "tone.aiff"
    transcode_to_aiff(flac, aiff)

    assert aiff.exists()
    info = sf.info(str(aiff))
    # EXACT frame count and sample rate preserved → grid/cue offsets stay valid.
    assert info.frames == n
    assert info.samplerate == sr
    assert info.format == "AIFF"
    assert info.subtype == "PCM_16"


@needs_soundfile
def test_export_for_cdj_orchestration(tmp_path: Path) -> None:
    import numpy as np
    import soundfile as sf

    lib = tmp_path / "lib"
    lib.mkdir()
    flac = lib / "deep.flac"
    sf.write(str(flac), np.zeros(8000, dtype="float32"), 44100, format="FLAC")
    mp3 = lib / "beat.mp3"
    mp3.write_bytes(b"fake mp3, only passthrough behaviour is under test")

    xml = _build_rekordbox_xml(path_to_location(flac), path_to_location(mp3))
    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(xml, encoding="utf-8")
    flac_bytes_before = flac.read_bytes()
    xml_bytes_before = xml_path.read_bytes()

    out_dir = tmp_path / "export"
    result = export_for_cdj(xml_path, out_dir)

    assert result.flac_converted == 1
    assert result.passthrough == 1
    assert result.errors == 0
    assert result.skipped == 0
    assert result.output_xml == out_dir / "rekordbox_cdj.xml"
    assert result.output_xml.exists()
    # An AIFF was produced.
    assert list(out_dir.glob("*.aiff"))
    # Sources are untouched.
    assert flac.read_bytes() == flac_bytes_before
    assert xml_path.read_bytes() == xml_bytes_before

    # Rewritten XML points the FLAC track at an AIFF inside out_dir.
    tree = parse_rekordbox_xml(result.output_xml)
    flac_track = _track_by_id(tree.getroot(), "1")
    assert flac_track.get("Kind") == AIFF_KIND
    assert location_to_path(flac_track.get("Location")).parent.resolve() == out_dir.resolve()


@needs_soundfile
def test_export_for_cdj_dry_run_writes_no_audio(tmp_path: Path) -> None:
    import numpy as np
    import soundfile as sf

    lib = tmp_path / "lib"
    lib.mkdir()
    flac = lib / "deep.flac"
    sf.write(str(flac), np.zeros(4000, dtype="float32"), 44100, format="FLAC")
    xml = _build_rekordbox_xml(path_to_location(flac), path_to_location(lib / "x.mp3"))
    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(xml, encoding="utf-8")

    out_dir = tmp_path / "export"
    result = export_for_cdj(xml_path, out_dir, dry_run=True)

    assert result.flac_planned == 1
    assert result.flac_converted == 0  # nothing actually transcoded
    assert result.output_xml is None
    # No audio, no XML, ideally not even the directory.
    assert not list(tmp_path.rglob("*.aiff"))
    assert not (out_dir / "rekordbox_cdj.xml").exists()


# ---------------------------------------------------------------------------
# Transcoder gating
# ---------------------------------------------------------------------------


def test_transcode_errors_when_no_transcoder(tmp_path: Path, monkeypatch) -> None:
    """With neither soundfile nor ffmpeg, transcode raises a clear error."""
    import vibechek.cdj_export as cdj

    monkeypatch.setattr(cdj, "_soundfile_module", lambda: None)
    monkeypatch.setattr(cdj, "_have_ffmpeg", lambda: False)

    src = tmp_path / "a.flac"
    src.write_bytes(b"x")
    with pytest.raises(CdjExportError) as exc:
        transcode_to_aiff(src, tmp_path / "a.aiff")
    msg = str(exc.value).lower()
    assert "soundfile" in msg and "ffmpeg" in msg


@needs_soundfile
def test_export_never_overwrites_a_file_already_in_out_dir(tmp_path: Path) -> None:
    """REGRESSION: the module header promises "strictly additive … no file is
    silently overwritten", but the collision loop was seeded only from an
    in-memory set that starts empty each run, and both writers clobber
    unconditionally (``sf.write``; ``ffmpeg -y``). A DJ who keeps FLACs beside
    their own hand-edited AIFFs lost the AIFF."""
    import numpy as np
    import soundfile as sf

    lib = tmp_path / "lib"
    lib.mkdir()
    flac = lib / "Track.flac"
    sf.write(str(flac), np.zeros(4000, dtype="float32"), 44100, format="FLAC")

    out_dir = tmp_path / "Set"
    out_dir.mkdir()
    precious = out_dir / "Track.aiff"
    precious.write_bytes(b"MY PRECIOUS HAND-EDITED AIFF")

    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(
        _build_rekordbox_xml(path_to_location(flac), path_to_location(lib / "x.mp3")),
        encoding="utf-8")

    result = export_for_cdj(xml_path, out_dir)

    assert result.flac_converted == 1 and result.errors == 0
    assert precious.read_bytes() == b"MY PRECIOUS HAND-EDITED AIFF"
    assert (out_dir / "Track_1.aiff").exists()
    assert result.renamed == ["Track_1.aiff"]      # and the rename is reported


@needs_soundfile
def test_export_handles_a_collection_less_export(tmp_path: Path) -> None:
    """`rewrite_for_cdj` documents and implements an iter() fallback for exports
    that omit the <COLLECTION> wrapper; the discovery pass did not, so such a
    file produced a green "Converted 0 FLAC" success over an XML still pointing
    at the unplayable FLACs. Pick one contract — this is it."""
    import numpy as np
    import soundfile as sf

    lib = tmp_path / "lib"
    lib.mkdir()
    flac = lib / "a.flac"
    sf.write(str(flac), np.zeros(4000, dtype="float32"), 44100, format="FLAC")

    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<DJ_PLAYLISTS Version="1.0.0">\n'
        '  <PRODUCT Name="rekordbox" Version="6.7.7" Company="AlphaTheta"/>\n'
        f'  <TRACK TrackID="1" Name="a" Kind="FLAC" TotalTime="100" '
        f'SampleRate="44100" Location="{path_to_location(flac)}"/>\n'
        "</DJ_PLAYLISTS>\n",
        encoding="utf-8")

    result = export_for_cdj(xml_path, tmp_path / "out")

    assert result.flac_converted == 1 and result.errors == 0
    written = ET.parse(result.output_xml).getroot()
    track = _track_by_id(written, "1")
    assert track.get("Kind") == AIFF_KIND
    assert track.get("Location").endswith("a.aiff")


def test_export_skips_missing_source(tmp_path: Path) -> None:
    """A FLAC TRACK whose file doesn't exist is counted as skipped, not crashed."""
    missing = tmp_path / "gone.flac"  # never created
    xml = _build_rekordbox_xml(path_to_location(missing), path_to_location(tmp_path / "b.mp3"))
    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(xml, encoding="utf-8")

    result = export_for_cdj(xml_path, tmp_path / "out")
    assert result.skipped == 1
    assert result.flac_converted == 0
    assert result.passthrough == 1
    assert result.track_errors and "not found" in result.track_errors[0].message


# ---------------------------------------------------------------------------
# >48 kHz hi-res down-sample (CDJ-2000nexus tops out at 48 kHz)
# ---------------------------------------------------------------------------


@needs_soundfile
def test_ffmpeg_fallback_downsamples_above_48k(tmp_path: Path, monkeypatch) -> None:
    """Regression: the ffmpeg fallback MUST down-sample a >48 kHz FLAC.

    The ffmpeg path is only reached when soundfile is unavailable, so it cannot
    rely on soundfile to discover the source rate. We force that path by
    monkeypatching _soundfile_module to None (ffmpeg must be on PATH for this
    test). Before the fix the AIFF stayed at 96 kHz — unplayable on the CDJs the
    whole feature targets.
    """
    import shutil

    import numpy as np
    import soundfile as sf

    import vibechek.cdj_export as cdj

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH — required to exercise the fallback path")

    sr = 96000
    sine = (0.5 * np.sin(2 * np.pi * 440 * np.arange(sr) / sr)).astype("float32")
    flac = tmp_path / "hires.flac"
    sf.write(str(flac), sine, sr, format="FLAC")

    # Force the ffmpeg fallback exactly as a soundfile-less install would hit it.
    monkeypatch.setattr(cdj, "_soundfile_module", lambda: None)

    aiff = tmp_path / "out" / "hires.aiff"
    transcode_to_aiff(flac, aiff)

    assert aiff.exists()
    # Read the produced rate WITHOUT soundfile-dependence in the assert by
    # re-enabling soundfile only for inspection (the transcode already ran).
    out_rate = sf.info(str(aiff)).samplerate
    assert out_rate <= 48000, f"ffmpeg fallback left a >48 kHz AIFF ({out_rate} Hz)"
    assert out_rate == 44100


@needs_soundfile
def test_ffmpeg_fallback_keeps_44k_unchanged(tmp_path: Path, monkeypatch) -> None:
    """A <=48 kHz source must NOT be resampled by the ffmpeg fallback."""
    import shutil

    import numpy as np
    import soundfile as sf

    import vibechek.cdj_export as cdj

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH — required to exercise the fallback path")

    sr = 44100
    n = 12345
    sine = (0.5 * np.sin(2 * np.pi * 440 * np.arange(n) / sr)).astype("float32")
    flac = tmp_path / "cd.flac"
    sf.write(str(flac), sine, sr, format="FLAC")

    monkeypatch.setattr(cdj, "_soundfile_module", lambda: None)

    aiff = tmp_path / "out" / "cd.aiff"
    transcode_to_aiff(flac, aiff)

    info = sf.info(str(aiff))
    assert info.samplerate == 44100
    # No rate change -> ffmpeg preserves the exact frame count (grid stays valid).
    assert info.frames == n


@needs_soundfile
def test_ffmpeg_fallback_writes_a_canonical_big_endian_aiff(
    tmp_path: Path, monkeypatch,
) -> None:
    """REGRESSION: ffmpeg's aiff muxer switches to the AIFF-C variant
    (``FORM….AIFC`` / ``sowt``) for any codec that isn't big-endian PCM, so the
    ``pcm_s16le`` fallback emitted a structurally different container from the
    soundfile path while the XML labels both ``Kind="AIFF File"``."""
    import shutil

    import numpy as np
    import soundfile as sf

    import vibechek.cdj_export as cdj

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH — required to exercise the fallback path")

    sr = 44100
    sine = (0.5 * np.sin(2 * np.pi * 440 * np.arange(4410) / sr)).astype("float32")
    flac = tmp_path / "cd.flac"
    sf.write(str(flac), sine, sr, format="FLAC")

    reference = tmp_path / "ref.aiff"
    transcode_to_aiff(flac, reference)              # the soundfile path
    monkeypatch.setattr(cdj, "_soundfile_module", lambda: None)
    fallback = tmp_path / "out" / "cd.aiff"
    transcode_to_aiff(flac, fallback)               # the ffmpeg path

    assert reference.read_bytes()[8:12] == b"AIFF"
    assert fallback.read_bytes()[8:12] == b"AIFF"


def test_ffmpeg_cmd_forces_44k_when_rate_unknown(tmp_path: Path, monkeypatch) -> None:
    """If the source rate can't be probed, ffmpeg is invoked with -ar 44100.

    Forcing 44.1 kHz when the rate is unknown is always CDJ-safe and prevents
    shipping an unplayable hi-res AIFF. It is NOT a no-op for a 48 kHz source —
    that really is a resample, which `export_for_cdj` records in
    `result.resampled` from the TRACK's declared rate.
    Exercises the command construction without needing a real audio file.
    """
    import vibechek.cdj_export as cdj

    captured: dict[str, list[str]] = {}

    class _FakeProc:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    # Rate cannot be determined -> the fix must still add -ar 44100.
    monkeypatch.setattr(cdj, "_probe_samplerate", lambda src: None)
    # Pin the resolved binary so this stays a pure command-construction test on
    # a machine without ffmpeg installed.
    monkeypatch.setattr(cdj, "_ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(cdj.subprocess, "run", _fake_run)

    cdj._transcode_ffmpeg(tmp_path / "x.flac", tmp_path / "x.aiff")

    cmd = captured["cmd"]
    assert "-ar" in cmd
    assert cmd[cmd.index("-ar") + 1] == "44100"


# ---------------------------------------------------------------------------
# Down-sampled tracks: SampleRate/BitRate must be corrected in the XML
# ---------------------------------------------------------------------------


def _build_hires_rekordbox_xml(flac_location: str) -> str:
    """A Rekordbox collection XML with a single 96 kHz FLAC TRACK."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
  <PRODUCT Name="rekordbox" Version="6.7.7" Company="AlphaTheta"/>
  <COLLECTION Entries="1">
    <TRACK TrackID="1" Name="HiRes" Kind="FLAC" Size="80000000"
           TotalTime="312" SampleRate="96000" BitRate="4608"
           Location="{flac_location}">
      <TEMPO Inizio="0.025" Bpm="124.00" Metro="4/4" Battito="1"/>
      <POSITION_MARK Name="" Type="0" Start="0.025" Num="-1"/>
    </TRACK>
  </COLLECTION>
  <PLAYLISTS>
    <NODE Type="0" Name="ROOT" Count="0"/>
  </PLAYLISTS>
</DJ_PLAYLISTS>
"""


@needs_soundfile
def test_rewrite_corrects_stale_samplerate_for_downsampled_track(tmp_path: Path) -> None:
    """Regression: a down-sampled track's XML SampleRate/BitRate must be updated.

    Before the fix the TRACK kept SampleRate="96000" even though the on-disk AIFF
    is 44.1 kHz, so Rekordbox advertised a rate the file no longer had.
    """
    import numpy as np
    import soundfile as sf

    src_flac = tmp_path / "hires.flac"
    src_flac.write_bytes(b"flac placeholder - rewrite only reads the AIFF rate")

    # A real 44.1 kHz AIFF stands in for the down-sampled output.
    aiff = tmp_path / "out" / "hires.aiff"
    aiff.parent.mkdir()
    sf.write(str(aiff), np.zeros(4410, dtype="int16"), 44100, format="AIFF", subtype="PCM_16")

    xml = _build_hires_rekordbox_xml(path_to_location(src_flac))
    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(xml, encoding="utf-8")
    tree = parse_rekordbox_xml(xml_path)

    new_tree = rewrite_for_cdj(tree, {str(src_flac): aiff}, tmp_path / "out")
    out_flac = _track_by_id(new_tree.getroot(), "1")

    assert out_flac.get("Kind") == AIFF_KIND
    # SampleRate corrected to the AIFF's real rate; stale BitRate cleared.
    assert out_flac.get("SampleRate") == "44100"
    assert out_flac.get("BitRate") == "0"


@needs_soundfile
def test_rewrite_leaves_samplerate_when_not_resampled(tmp_path: Path) -> None:
    """A non-resampled (<=48 kHz) track keeps its declared SampleRate/BitRate."""
    import numpy as np
    import soundfile as sf

    src_flac = tmp_path / "song.flac"
    src_flac.write_bytes(b"x")
    aiff = tmp_path / "out" / "song.aiff"
    aiff.parent.mkdir()
    # AIFF rate matches the TRACK's declared 44100 -> nothing to correct.
    sf.write(str(aiff), np.zeros(4410, dtype="int16"), 44100, format="AIFF", subtype="PCM_16")

    xml = _build_rekordbox_xml(path_to_location(src_flac), path_to_location(tmp_path / "b.mp3"))
    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(xml, encoding="utf-8")
    tree = parse_rekordbox_xml(xml_path)

    new_tree = rewrite_for_cdj(tree, {str(src_flac): aiff}, tmp_path / "out")
    out_flac = _track_by_id(new_tree.getroot(), "1")
    assert out_flac.get("SampleRate") == "44100"
    assert out_flac.get("BitRate") == "1411"  # untouched


@needs_soundfile
def test_export_records_resampled_hires_track(tmp_path: Path) -> None:
    """End-to-end: a 96 kHz FLAC is reported in result.resampled and its XML rate fixed."""
    import numpy as np
    import soundfile as sf

    lib = tmp_path / "lib"
    lib.mkdir()
    flac = lib / "hires.flac"
    sr = 96000
    sf.write(str(flac), np.zeros(sr // 2, dtype="float32"), sr, format="FLAC")

    xml = _build_hires_rekordbox_xml(path_to_location(flac))
    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(xml, encoding="utf-8")

    out_dir = tmp_path / "export"
    result = export_for_cdj(xml_path, out_dir)

    assert result.flac_converted == 1
    assert result.errors == 0
    # The hi-res source is flagged as down-sampled (irreversible quality drop).
    assert result.resampled == [str(flac)]

    # The on-disk AIFF really is <=48 kHz ...
    aiff = next(out_dir.glob("*.aiff"))
    assert sf.info(str(aiff)).samplerate <= 48000
    # ... and the rewritten XML no longer advertises 96000.
    tree = parse_rekordbox_xml(result.output_xml)
    out_flac = _track_by_id(tree.getroot(), "1")
    assert out_flac.get("SampleRate") == "44100"


@needs_soundfile
def test_export_records_a_downsample_it_could_not_probe(tmp_path: Path, monkeypatch) -> None:
    """REGRESSION: `_transcode_ffmpeg` forces ``-ar 44100`` when the source rate
    can't be probed — a REAL resample for a 48 kHz source. The recording site
    then re-called the same deterministic `_probe_samplerate` and required
    >48 kHz, so it was structurally guaranteed to miss exactly the tracks the
    forced ``-ar`` was applied to. The TRACK's declared rate is the probe-free
    signal that closes the gap."""
    import shutil

    import numpy as np
    import soundfile as sf

    import vibechek.cdj_export as cdj

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH — required to exercise the fallback path")

    lib = tmp_path / "lib"
    lib.mkdir()
    flac = lib / "master.flac"
    sf.write(str(flac), np.zeros(12000, dtype="float32"), 48000, format="FLAC")

    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<DJ_PLAYLISTS Version="1.0.0">\n'
        '  <COLLECTION Entries="1">\n'
        f'    <TRACK TrackID="1" Name="master" Kind="FLAC" TotalTime="1" '
        f'SampleRate="48000" BitRate="1536" Location="{path_to_location(flac)}"/>\n'
        "  </COLLECTION>\n</DJ_PLAYLISTS>\n",
        encoding="utf-8")

    # A soundfile-less install whose FLAC header neither ffprobe nor mutagen
    # can read: the transcode still happens, at a forced 44.1 kHz.
    monkeypatch.setattr(cdj, "_soundfile_module", lambda: None)
    monkeypatch.setattr(cdj, "_probe_samplerate", lambda src: None)

    result = export_for_cdj(xml_path, tmp_path / "out")

    assert result.flac_converted == 1 and result.errors == 0
    aiff = next((tmp_path / "out").glob("*.aiff"))
    assert sf.info(str(aiff)).samplerate == 44100      # really was resampled
    assert result.resampled == [str(flac)]             # ... and it is reported


@needs_soundfile
def test_export_does_not_flag_44k_track_as_resampled(tmp_path: Path) -> None:
    """A normal 44.1 kHz FLAC must NOT appear in result.resampled."""
    import numpy as np
    import soundfile as sf

    lib = tmp_path / "lib"
    lib.mkdir()
    flac = lib / "deep.flac"
    sf.write(str(flac), np.zeros(8000, dtype="float32"), 44100, format="FLAC")

    xml = _build_rekordbox_xml(path_to_location(flac), path_to_location(lib / "x.mp3"))
    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(xml, encoding="utf-8")

    result = export_for_cdj(xml_path, tmp_path / "export")
    assert result.flac_converted == 1
    assert result.resampled == []


# ---------------------------------------------------------------------------
# Executable resolution (cwd-hijack)
# ---------------------------------------------------------------------------


def test_ffmpeg_is_resolved_cwd_free_and_absolute(tmp_path: Path, monkeypatch) -> None:
    """An `ffmpeg` sitting in the CURRENT directory must never be run.

    An export runs against a folder of music the user just unpacked; a bare
    `shutil.which("ffmpeg")` searches that folder first on Windows, so a
    planted binary would be executed once per transcoded track. `_ffmpeg_path`
    goes through `utils.find_executable`, which discards a cwd hit.
    """
    import vibechek.cdj_export as cdj
    import vibechek.utils as utils

    planted = tmp_path / "ffmpeg.exe"
    planted.write_text("not really ffmpeg", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # Stand in for the Windows-only cwd entry `shutil.which` prepends, so the
    # rejection is exercised on every platform the suite runs on.
    monkeypatch.setattr(utils.shutil, "which", lambda name: str(planted))

    assert cdj._ffmpeg_path() is None
    assert cdj._have_ffmpeg() is False

    # A hit anywhere else is still honoured, and comes back absolute.
    elsewhere = tmp_path / "bin"
    elsewhere.mkdir()
    real = elsewhere / "ffmpeg.exe"
    real.write_text("not really ffmpeg either", encoding="utf-8")
    monkeypatch.setattr(utils.shutil, "which", lambda name: str(real))
    resolved = cdj._ffmpeg_path()
    assert resolved is not None and Path(resolved).is_absolute()


def test_transcode_ffmpeg_uses_the_resolved_absolute_binary(
    tmp_path: Path, monkeypatch
) -> None:
    """The subprocess gets the ABSOLUTE resolved path, not the bare name.

    A relative program name re-resolves against whatever directory the child
    process starts in, which is the same hijack by another route.
    """
    import vibechek.cdj_export as cdj

    captured: dict[str, list[str]] = {}

    class _FakeProc:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(cdj, "_probe_samplerate", lambda src: 44100)
    monkeypatch.setattr(cdj, "_ffmpeg_path", lambda: "/opt/bin/ffmpeg")
    monkeypatch.setattr(
        cdj.subprocess, "run", lambda cmd, *a, **k: (captured.__setitem__("cmd", cmd), _FakeProc())[1]
    )

    cdj._transcode_ffmpeg(tmp_path / "x.flac", tmp_path / "x.aiff")

    assert captured["cmd"][0] == "/opt/bin/ffmpeg"


def test_transcode_ffmpeg_fails_loud_when_ffmpeg_is_gone(
    tmp_path: Path, monkeypatch
) -> None:
    """No resolvable ffmpeg -> a clear error, never a bare-name subprocess."""
    import vibechek.cdj_export as cdj

    monkeypatch.setattr(cdj, "_ffmpeg_path", lambda: None)

    def _boom(*a, **k):  # pragma: no cover -- must not be reached
        raise AssertionError("subprocess must not run without a resolved ffmpeg")

    monkeypatch.setattr(cdj.subprocess, "run", _boom)

    with pytest.raises(CdjExportError, match="ffmpeg"):
        cdj._transcode_ffmpeg(tmp_path / "x.flac", tmp_path / "x.aiff")


def test_probe_samplerate_skips_ffprobe_when_unresolvable(
    tmp_path: Path, monkeypatch
) -> None:
    """Same cwd-discarding resolution for ffprobe; absent, fall through quietly.

    Falling through to the mutagen header read (rather than shelling out to a
    bare `ffprobe`) is the whole point — the fallback still answers.
    """
    import vibechek.cdj_export as cdj

    monkeypatch.setattr(cdj, "find_executable", lambda name: None)

    def _boom(*a, **k):  # pragma: no cover -- must not be reached
        raise AssertionError("ffprobe must not run when it cannot be resolved")

    monkeypatch.setattr(cdj.subprocess, "run", _boom)

    # No real FLAC either, so mutagen also declines: None, not a crash.
    assert cdj._probe_samplerate(tmp_path / "missing.flac") is None
