"""Which XML nodes the CDJ export treats as *tracks*.

A standard Rekordbox export wraps its real tracks in ``<COLLECTION>``; a
non-standard one may omit that wrapper, and `cdj_export._collection_tracks`
falls back to walking the whole tree so those exports still work. That fallback
must not sweep in the ``<PLAYLISTS>`` tree's ``<TRACK Key="1"/>`` *reference*
nodes — they carry no Location, point back into the collection by TrackID, and
counting them inflates the reported `passthrough` with entries that are not
files at all (P05 regression risk).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from vibechek import cdj_export
from vibechek.cdj_export import export_for_cdj, path_to_location

try:
    import soundfile as _sf  # noqa: F401

    HAVE_SOUNDFILE = True
except Exception:  # noqa: BLE001
    HAVE_SOUNDFILE = False

needs_soundfile = pytest.mark.skipif(
    not HAVE_SOUNDFILE,
    reason="soundfile not installed — install the 'cdj' extra to run transcode tests",
)


def _collection_less_xml(flac_location: str) -> str:
    """A COLLECTION-less export that ALSO carries a playlist tree.

    Two playlist reference nodes, one of them a duplicate of the other, so the
    de-duplication has to be by identity rather than by value.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<DJ_PLAYLISTS Version="1.0.0">\n'
        '  <PRODUCT Name="rekordbox" Version="6.7.7" Company="AlphaTheta"/>\n'
        f'  <TRACK TrackID="1" Name="a" Kind="FLAC" TotalTime="100" '
        f'SampleRate="44100" Location="{flac_location}"/>\n'
        "  <PLAYLISTS>\n"
        '    <NODE Type="0" Name="ROOT" Count="1">\n'
        '      <NODE Name="Warmup" Type="1" Entries="2">\n'
        '        <TRACK Key="1"/>\n'
        '        <TRACK Key="1"/>\n'
        "      </NODE>\n"
        "    </NODE>\n"
        "  </PLAYLISTS>\n"
        "</DJ_PLAYLISTS>\n"
    )


def test_collection_less_fallback_ignores_playlist_reference_nodes() -> None:
    """The fallback returns the one real TRACK, not the playlist references."""
    root = ET.fromstring(_collection_less_xml("file://localhost/lib/a.flac"))
    tracks = cdj_export._collection_tracks(root)
    assert [t.get("TrackID") for t in tracks] == ["1"]
    assert all(t.get("Key") is None for t in tracks)


def test_collection_wrapper_still_wins_over_the_fallback() -> None:
    """When <COLLECTION> is present it is the only thing walked — the playlist
    tree is out of scope by construction, exactly as before."""
    root = ET.fromstring(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<DJ_PLAYLISTS Version="1.0.0">\n'
        '  <COLLECTION Entries="1">\n'
        '    <TRACK TrackID="7" Name="a" Kind="MP3" Location="file://localhost/lib/a.mp3"/>\n'
        "  </COLLECTION>\n"
        "  <PLAYLISTS><NODE Name=\"L\" Type=\"1\"><TRACK Key=\"7\"/></NODE></PLAYLISTS>\n"
        "</DJ_PLAYLISTS>\n")
    assert [t.get("TrackID") for t in cdj_export._collection_tracks(root)] == ["7"]


@needs_soundfile
def test_playlist_references_do_not_inflate_passthrough(tmp_path: Path) -> None:
    """End-to-end: a COLLECTION-less export with a playlist tree reports the one
    file it converted and NOTHING as passed through. Before the fix each
    `<TRACK Key="1"/>` was counted as a Location-less passthrough, so the user
    was told about 2 files that do not exist."""
    import numpy as np
    import soundfile as sf

    lib = tmp_path / "lib"
    lib.mkdir()
    flac = lib / "a.flac"
    sf.write(str(flac), np.zeros(4000, dtype="float32"), 44100, format="FLAC")

    xml_path = tmp_path / "rb.xml"
    xml_path.write_text(_collection_less_xml(path_to_location(flac)), encoding="utf-8")

    result = export_for_cdj(xml_path, tmp_path / "out")

    assert result.flac_converted == 1
    assert result.errors == 0
    assert result.passthrough == 0

    # The reference nodes survive the rewrite untouched — they are still plain
    # Key-only pointers, never given a Location or an AIFF Kind.
    written = ET.parse(result.output_xml).getroot()
    refs = [t for node in written.iter("PLAYLISTS") for t in node.iter("TRACK")]
    assert len(refs) == 2
    assert all(dict(r.attrib) == {"Key": "1"} for r in refs)
