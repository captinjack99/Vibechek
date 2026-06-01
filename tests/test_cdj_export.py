"""Tests for vibechek.cdj_export — FLAC→CDJ (AIFF + rewritten Rekordbox XML).

Two layers:
  - XML rewrite + URI handling: fully exercisable with stdlib + a synthetic
    Rekordbox XML built in-test (no audio, no optional deps).
  - Transcode + orchestration: needs ``soundfile`` to synthesize a tiny FLAC
    and verify sample-accuracy; skipped with a clear reason when it's absent.
"""

from __future__ import annotations

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
