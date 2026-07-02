"""Trust-UX #3: tag priors — Rekordbox XML import, key surfacing, MIK energy.

Design ground truth (measured, internal/bughunt/score_tag_priors.py on the
72-track gold corpus): embedded key/BPM tags are other tools' ALGORITHMIC
reads — tag key 49% exact vs audio 63% on the same tracks, audio right 10:1
on disagreement — so priors NEVER change ml_key/ml_bpm; they surface. Genre is
the editorial tier where tags win (the shipping prefer_tag policy), so the
Rekordbox XML genre participates there, marked genre_origin="rekordbox".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from vibechek import analyzer, library_state, tag_priors
from vibechek.cdj_export import CdjExportError
from vibechek.rpc import _import_tag_priors

# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

RB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
  <PRODUCT Name="rekordbox" Version="6.7.7" Company="AlphaTheta"/>
  <COLLECTION Entries="3">
    <TRACK TrackID="1" Name="Saving Up" Artist="Dom Dolla"
           Genre="Tech House" Tonality="2d" AverageBpm="126.00"
           Comments="8A - Energy 7"
           Location="file://localhost/D:/My%20Music/Dom%20Dolla%20-%20Saving%20Up.aiff"/>
    <TRACK TrackID="2" Name="No Fields" Artist="Nobody"
           Location="file://localhost/D:/My%20Music/plain.mp3"/>
    <TRACK TrackID="3" Name="Unix Track" Artist="Someone" Genre="Trance"
           Location="file://localhost/home/dj/music/track.flac"/>
  </COLLECTION>
  <PLAYLISTS>
    <NODE Type="0" Name="ROOT" Count="1">
      <NODE Name="Set" Type="1" Entries="1"><TRACK Key="1"/></NODE>
    </NODE>
  </PLAYLISTS>
</DJ_PLAYLISTS>
"""


def _write_xml(tmp_path: Path, body: str = RB_XML) -> Path:
    p = tmp_path / "collection.xml"
    p.write_text(body, encoding="utf-8")
    return p


class TestParseRekordboxCollection:
    def test_parses_fields_and_decodes_locations(self, tmp_path: Path) -> None:
        priors = tag_priors.parse_rekordbox_collection(_write_xml(tmp_path))
        key = tag_priors.norm_path("D:/My Music/Dom Dolla - Saving Up.aiff")
        assert key in priors
        assert priors[key] == {"genre": "Tech House", "key": "2d", "energy_mik": 7}

    def test_track_without_usable_fields_dropped(self, tmp_path: Path) -> None:
        priors = tag_priors.parse_rekordbox_collection(_write_xml(tmp_path))
        assert tag_priors.norm_path("D:/My Music/plain.mp3") not in priors
        assert len(priors) == 2

    def test_playlist_track_refs_ignored(self, tmp_path: Path) -> None:
        # PLAYLISTS <TRACK Key="1"/> has no Location and must not blow up or
        # produce phantom entries even though it shares the TRACK tag name.
        priors = tag_priors.parse_rekordbox_collection(_write_xml(tmp_path))
        assert len(priors) == 2

    def test_unix_location(self, tmp_path: Path) -> None:
        priors = tag_priors.parse_rekordbox_collection(_write_xml(tmp_path))
        assert tag_priors.norm_path("/home/dj/music/track.flac") in priors

    def test_network_location_keeps_the_host_as_unc(self) -> None:
        """file://<server>/… is a NAS library (a real DJ setup). Dropping the
        host yielded "/music/x.mp3", which can never match the analyzed
        ``\\\\MYNAS\\music`` paths — every NAS track silently failed to match
        ("matched 0 of N") with no hint why."""
        assert tag_priors._location_to_path("file://MYNAS/music/track.mp3") == (
            "//MYNAS/music/track.mp3"
        )
        # The UNC form must land on the same norm_path as the analyzed
        # backslash form, or matching still fails after the rebuild. Windows
        # only: norm_path is deliberately platform-semantic (os.path.normpath
        # — backslash is not a separator on POSIX), and backslashed UNC
        # analyzed-paths only occur on Windows anyway.
        if sys.platform == "win32":
            assert tag_priors.norm_path("//MYNAS/music/track.mp3") == (
                tag_priors.norm_path(r"\\MYNAS\music\track.mp3")
            )

    def test_localhost_location_still_strips_the_host(self) -> None:
        assert tag_priors._location_to_path(
            "file://localhost/D:/My%20Music/x.flac"
        ) == "D:/My Music/x.flac"

    def test_not_rekordbox_xml_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "other.xml"
        p.write_text("<root/>", encoding="utf-8")
        with pytest.raises(CdjExportError):
            tag_priors.parse_rekordbox_collection(p)


# ---------------------------------------------------------------------------
# merge semantics
# ---------------------------------------------------------------------------


class TestMergePrior:
    def test_genre_supersedes_and_records_origin(self) -> None:
        rec = {"existing_tags": {"genre": "Electro"}}
        changed = tag_priors.merge_prior_into_record(rec, {"genre": "Tech House"})
        assert changed
        assert rec["existing_tags"]["genre"] == "Tech House"
        assert rec["existing_tags"]["genre_origin"] == "rekordbox"

    def test_same_genre_case_insensitive_no_change(self) -> None:
        rec = {"existing_tags": {"genre": "tech house"}}
        assert not tag_priors.merge_prior_into_record(rec, {"genre": "Tech House"})
        assert "genre_origin" not in rec["existing_tags"]

    def test_key_and_energy_fill_only(self) -> None:
        rec = {"existing_tags": {"key": "9A", "energy_mik": 5}}
        assert not tag_priors.merge_prior_into_record(
            rec, {"key": "2d", "energy_mik": 7}
        )
        assert rec["existing_tags"]["key"] == "9A"
        assert rec["existing_tags"]["energy_mik"] == 5

    def test_key_and_energy_fill_when_absent(self) -> None:
        rec = {"existing_tags": {}}
        assert tag_priors.merge_prior_into_record(rec, {"key": "2d", "energy_mik": 7})
        assert rec["existing_tags"]["key"] == "2d"
        assert rec["existing_tags"]["energy_mik"] == 7

    def test_idempotent(self) -> None:
        rec = {"existing_tags": {"genre": "Electro"}}
        prior = {"genre": "Tech House", "key": "2d"}
        assert tag_priors.merge_prior_into_record(rec, prior)
        assert not tag_priors.merge_prior_into_record(rec, prior)


# ---------------------------------------------------------------------------
# sidecar persistence
# ---------------------------------------------------------------------------


class TestSidecar:
    def test_round_trip(self, tmp_path: Path) -> None:
        analysis = tmp_path / "abc123.json"
        priors = {tag_priors.norm_path("D:/x.mp3"): {"genre": "House"}}
        path = tag_priors.save_priors(analysis, priors)
        assert path.name == "abc123.priors.json"
        assert tag_priors.load_priors(analysis) == priors

    def test_missing_is_empty(self, tmp_path: Path) -> None:
        assert tag_priors.load_priors(tmp_path / "none.json") == {}

    def test_corrupt_is_empty_not_fatal(self, tmp_path: Path) -> None:
        analysis = tmp_path / "abc.json"
        tag_priors.priors_path_for(analysis).write_text("{broken", encoding="utf-8")
        assert tag_priors.load_priors(analysis) == {}

    def test_non_object_entries_dropped(self, tmp_path: Path) -> None:
        analysis = tmp_path / "abc.json"
        tag_priors.priors_path_for(analysis).write_text(
            json.dumps({"a": {"genre": "House"}, "b": "junk"}), encoding="utf-8"
        )
        assert tag_priors.load_priors(analysis) == {"a": {"genre": "House"}}


# ---------------------------------------------------------------------------
# re-reconcile on a saved report
# ---------------------------------------------------------------------------


def _analyzed_track(path: str, *, tag_genre: str | None = None, ml_key: str = "1A") -> dict:
    return {
        "path": path,
        "filename": Path(path).name,
        "existing_tags": {"genre": tag_genre},
        "ml_analysis": {
            "ml_genre": "Techno",
            "ml_subgenre": "Techno",
            "ml_genre_audio": "Techno",
            "ml_subgenre_audio": "Techno",
            "ml_genre_raw_confidence": 0.6,
            "ml_genre_confidence": 0.6,
            "ml_genre_source": "ml",
            "ml_key": ml_key,
        },
    }


class TestApplyPriorsToReport:
    def test_imported_genre_wins_at_tag_tier(self) -> None:
        t = _analyzed_track("D:/My Music/a.mp3")
        report = {"tracks": [t]}
        priors = {tag_priors.norm_path("D:/My Music/a.mp3"): {"genre": "House"}}
        updated, matched = tag_priors.apply_priors_to_report(report, priors, "prefer_tag", 0.90)
        assert matched == 1 and len(updated) == 1
        ml = t["ml_analysis"]
        assert ml["ml_genre"] == "House"
        assert ml["ml_genre_source"] == "tag"
        assert ml["ml_genre_conflict"] is True  # House tag vs Techno audio
        assert t["existing_tags"]["genre_origin"] == "rekordbox"

    def test_imported_key_surfaces_but_never_overrides(self) -> None:
        t = _analyzed_track("D:/b.mp3", ml_key="1A")
        report = {"tracks": [t]}
        priors = {tag_priors.norm_path("D:/b.mp3"): {"key": "2d"}}  # Open Key → 9B
        updated, matched = tag_priors.apply_priors_to_report(report, priors, "prefer_tag", 0.90)
        assert matched == 1 and len(updated) == 1
        ml = t["ml_analysis"]
        assert ml["ml_key"] == "1A"          # audio stays effective — measured better
        assert ml["ml_key_tag"] == "9B"
        assert ml["ml_key_conflict"] is True

    def test_stored_web_tier_survives_offline_re_reconcile(self) -> None:
        t = _analyzed_track("D:/c.mp3")
        t["ml_analysis"]["ml_genre_web"] = "Trance"
        t["ml_analysis"]["ml_genre_web_grounded"] = True
        report = {"tracks": [t]}
        priors = {tag_priors.norm_path("D:/c.mp3"): {"genre": "House"}}
        tag_priors.apply_priors_to_report(report, priors, "prefer_tag", 0.90)
        ml = t["ml_analysis"]
        # The previously-fetched web read is still there and still participates
        # (grounded web overrides a disagreeing tag in reconcile_genre).
        assert ml["ml_genre_web"] == "Trance"
        assert ml["ml_genre_source"] == "web_override"

    def test_unmatched_tracks_untouched(self) -> None:
        t = _analyzed_track("D:/d.mp3")
        report = {"tracks": [t]}
        updated, matched = tag_priors.apply_priors_to_report(
            report, {tag_priors.norm_path("D:/elsewhere.mp3"): {"genre": "House"}},
            "prefer_tag", 0.90,
        )
        assert matched == 0 and updated == []
        assert t["ml_analysis"]["ml_genre"] == "Techno"

    def test_key_only_prior_does_not_reopen_a_reverted_decision(self) -> None:
        """A user's Revert-to-tag is the highest-trust data in the system. A
        key-only prior used to re-run the GENRE reconcile (merge reported one
        bool for any change), which recomputed the reverted record back to
        `ml_override` — flipping the genre to the ML value and re-opening the
        review queue for a track whose genre the import never touched."""
        t = _analyzed_track("D:/e.mp3", tag_genre="House")
        ml = t["ml_analysis"]
        # State after resolve_genre_conflicts "revert": tag won, conflict
        # cleared — but the stashed audio read would override again on a
        # from-scratch reconcile (confidence above the 0.90 gate).
        ml.update({
            "ml_genre": "House", "ml_subgenre": "House",
            "ml_genre_source": "tag", "ml_genre_conflict": False,
            "ml_genre_raw_confidence": 0.97, "ml_genre_confidence": 0.97,
        })
        report = {"tracks": [t]}
        priors = {tag_priors.norm_path("D:/e.mp3"): {"key": "2d"}}
        updated, matched = tag_priors.apply_priors_to_report(
            report, priors, "prefer_tag", 0.90,
        )
        assert matched == 1 and len(updated) == 1
        assert ml["ml_genre"] == "House"            # revert honored
        assert ml["ml_genre_source"] == "tag"
        assert ml["ml_genre_conflict"] is False     # NOT back in the queue
        assert ml["ml_key_tag"] == "9B"             # the key DID surface

    def test_key_only_prior_leaves_an_approved_decision_alone(self) -> None:
        """Same contract for Approve: _reconcile_record_genre's guard, but the
        field-level merge means the genre reconcile isn't even invoked."""
        t = _analyzed_track("D:/f.mp3", tag_genre="House")
        ml = t["ml_analysis"]
        ml.update({"ml_genre_source": "approved", "ml_genre_conflict": False})
        report = {"tracks": [t]}
        priors = {tag_priors.norm_path("D:/f.mp3"): {"key": "2d"}}
        tag_priors.apply_priors_to_report(report, priors, "prefer_tag", 0.90)
        assert ml["ml_genre_source"] == "approved"
        assert ml["ml_genre_conflict"] is False

    def test_merge_reports_changed_fields(self) -> None:
        rec = {"existing_tags": {"genre": "Electro"}}
        assert tag_priors.merge_prior_into_record(
            rec, {"genre": "Tech House", "key": "2d", "energy_mik": 7}
        ) == {"genre", "key", "energy_mik"}
        assert tag_priors.merge_prior_into_record(rec, {"key": "3d"}) == set()


class TestMergeIntoResults:
    def test_counts_only_changed(self) -> None:
        r1 = _analyzed_track("D:/a.mp3")
        r2 = _analyzed_track("D:/b.mp3", tag_genre="House")
        priors = {
            tag_priors.norm_path("D:/a.mp3"): {"genre": "House"},
            tag_priors.norm_path("D:/b.mp3"): {"genre": "House"},  # already equal
        }
        assert tag_priors.merge_priors_into_results([r1, r2], priors) == 1


# ---------------------------------------------------------------------------
# _reconcile_record_key (analyzer)
# ---------------------------------------------------------------------------


class TestReconcileRecordKey:
    def test_agreement(self) -> None:
        r = {"existing_tags": {"key": "8A"}, "ml_analysis": {"ml_key": "8A"}}
        analyzer._reconcile_record_key(r)
        assert r["ml_analysis"]["ml_key_tag"] == "8A"
        assert r["ml_analysis"]["ml_key_conflict"] is False

    def test_conflict_open_key_notation(self) -> None:
        r = {"existing_tags": {"key": "2d"}, "ml_analysis": {"ml_key": "1A"}}
        analyzer._reconcile_record_key(r)
        assert r["ml_analysis"]["ml_key_tag"] == "9B"
        assert r["ml_analysis"]["ml_key_conflict"] is True

    def test_effective_key_never_changes(self) -> None:
        r = {"existing_tags": {"key": "2d"}, "ml_analysis": {"ml_key": "1A"}}
        analyzer._reconcile_record_key(r)
        assert r["ml_analysis"]["ml_key"] == "1A"

    def test_unparseable_tag_leaves_fields_unset(self) -> None:
        r = {"existing_tags": {"key": "Ambient"}, "ml_analysis": {"ml_key": "1A"}}
        analyzer._reconcile_record_key(r)
        assert "ml_key_tag" not in r["ml_analysis"]
        assert "ml_key_conflict" not in r["ml_analysis"]

    def test_no_audio_key_no_conflict_claim(self) -> None:
        r = {"existing_tags": {"key": "8A"}, "ml_analysis": {"ml_key": None}}
        analyzer._reconcile_record_key(r)
        assert r["ml_analysis"]["ml_key_tag"] == "8A"
        assert r["ml_analysis"]["ml_key_conflict"] is False

    def test_stale_fields_cleared_when_tag_gone(self) -> None:
        r = {
            "existing_tags": {},
            "ml_analysis": {"ml_key": "1A", "ml_key_tag": "9B", "ml_key_conflict": True},
        }
        analyzer._reconcile_record_key(r)
        assert "ml_key_tag" not in r["ml_analysis"]
        assert "ml_key_conflict" not in r["ml_analysis"]

    def test_no_ml_analysis_noop(self) -> None:
        r = {"existing_tags": {"key": "8A"}, "ml_analysis": None}
        analyzer._reconcile_record_key(r)  # must not raise


# ---------------------------------------------------------------------------
# MIK energy + unified ID3 reads (_read_existing_tags)
# ---------------------------------------------------------------------------


def _make_silent_mp3(path: Path) -> None:
    frame = bytes([0xFF, 0xFB, 0x90, 0xC0]) + bytes(413)
    path.write_bytes(frame * 40)


def _make_silent_wav(path: Path) -> None:
    import struct
    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(struct.pack("<h", 0) * 22050)


class TestMikEnergyParse:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("8A - Energy 7", 7),
            ("Energy 10", 10),
            ("energy: 3", 3),
            ("ENERGY=5", 5),
            ("no energy here", None),
            ("Energy 11", None),   # out of the 1-10 MIK range
            ("Energy 0", None),
            ("", None),
            (None, None),
            ("High Energy Anthems", None),  # word "Energy" without a number
        ],
    )
    def test_parse(self, text, expected) -> None:
        assert analyzer._parse_mik_energy(text) == expected


class TestReadExistingTagsMik:
    def test_mp3_comm_energy(self, tmp_path: Path) -> None:
        from mutagen.id3 import COMM, ID3

        p = tmp_path / "t.mp3"
        _make_silent_mp3(p)
        id3 = ID3()
        id3.add(COMM(encoding=3, lang="eng", desc="", text=["8A - Energy 7"]))
        id3.save(p)
        tags = analyzer._read_existing_tags(p)
        assert tags["energy_mik"] == 7
        assert tags["energy"] is None  # Vibechek's own 0-5 field untouched

    def test_mp3_grouping_pure_energy_is_prior_not_subgenre(self, tmp_path: Path) -> None:
        from mutagen.id3 import ID3, TIT1

        p = tmp_path / "t.mp3"
        _make_silent_mp3(p)
        id3 = ID3()
        id3.add(TIT1(encoding=3, text=["Energy 8"]))
        id3.save(p)
        tags = analyzer._read_existing_tags(p)
        assert tags["energy_mik"] == 8
        assert tags["subgenre"] is None

    def test_mp3_grouping_subgenre_stays_subgenre(self, tmp_path: Path) -> None:
        from mutagen.id3 import ID3, TIT1

        p = tmp_path / "t.mp3"
        _make_silent_mp3(p)
        id3 = ID3()
        id3.add(TIT1(encoding=3, text=["Progressive House"]))
        id3.save(p)
        tags = analyzer._read_existing_tags(p)
        assert tags["subgenre"] == "Progressive House"
        assert tags["energy_mik"] is None

    def test_wav_id3_custom_tags_read_back(self, tmp_path: Path) -> None:
        """The tagger writes ID3 frames to WAV/AIFF (_apply_aiff_wav); the
        reader must see them again — this was silently broken (mp3-only)."""
        from mutagen.id3 import TXXX
        from mutagen.wave import WAVE

        p = tmp_path / "t.wav"
        _make_silent_wav(p)
        audio = WAVE(p)
        audio.add_tags()
        audio.tags.add(TXXX(encoding=3, desc="ENERGY", text=["3"]))
        audio.tags.add(TXXX(encoding=3, desc="MOOD", text=["Happy"]))
        audio.save()
        tags = analyzer._read_existing_tags(p)
        assert tags["energy"] == 3
        assert tags["mood"] == "Happy"


# ---------------------------------------------------------------------------
# the import_tag_priors RPC
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(library_state, "STATE_FILE", tmp_path / "library_state.json")
    monkeypatch.setattr(library_state, "ANALYSES_DIR", tmp_path / "analyses")
    return tmp_path


def _seed(library_path: str, tracks: list[dict]) -> None:
    report = {"tracks": tracks,
              "summary": {"total_files": len(tracks), "analyzed": len(tracks)}}
    library_state.record_analysis(library_path, report)


class TestImportTagPriorsRpc:
    def _xml_for(self, tmp_path: Path, location: str, genre: str = "House") -> Path:
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
  <COLLECTION Entries="1">
    <TRACK TrackID="1" Genre="{genre}" Tonality="2d" Comments="Energy 6"
           Location="{location}"/>
  </COLLECTION>
</DJ_PLAYLISTS>
"""
        p = tmp_path / "rb.xml"
        p.write_text(body, encoding="utf-8")
        return p

    def test_import_updates_persists_and_writes_sidecar(self, tmp_path: Path) -> None:
        track_path = str(tmp_path / "lib" / "a.mp3")
        _seed(str(tmp_path / "lib"), [_analyzed_track(track_path)])
        xml = self._xml_for(tmp_path, Path(track_path).as_uri().replace("file:///", "file://localhost/"))

        out = _import_tag_priors({"library_path": str(tmp_path / "lib"), "xml_path": str(xml)})
        assert out["ok"] is True
        assert out["xml_tracks"] == 1
        assert out["matched"] == 1
        assert out["updated"] == 1
        ml = out["tracks"][0]["ml_analysis"]
        assert ml["ml_genre"] == "House"
        assert ml["ml_genre_source"] == "tag"
        assert out["tracks"][0]["existing_tags"]["genre_origin"] == "rekordbox"
        assert out["tracks"][0]["existing_tags"]["energy_mik"] == 6

        # Persisted: reload from disk shows the same state.
        record = next(r for r in library_state.load_state().recent
                      if r.path == str(tmp_path / "lib"))
        report = library_state.load_analysis(record)
        saved = report["tracks"][0]
        assert saved["existing_tags"]["genre_origin"] == "rekordbox"
        # Sidecar exists and carries the prior for the future analyze hook.
        sidecar = tag_priors.load_priors(record.analysis_path)
        assert tag_priors.norm_path(track_path) in sidecar

    def test_reimport_is_idempotent(self, tmp_path: Path) -> None:
        track_path = str(tmp_path / "lib" / "a.mp3")
        _seed(str(tmp_path / "lib"), [_analyzed_track(track_path)])
        xml = self._xml_for(tmp_path, Path(track_path).as_uri().replace("file:///", "file://localhost/"))
        params = {"library_path": str(tmp_path / "lib"), "xml_path": str(xml)}
        assert _import_tag_priors(params)["updated"] == 1
        again = _import_tag_priors(params)
        assert again["ok"] is True
        assert again["matched"] == 1
        assert again["updated"] == 0

    def test_unknown_library(self, tmp_path: Path) -> None:
        xml = self._xml_for(tmp_path, "file://localhost/D:/x.mp3")
        out = _import_tag_priors({"library_path": "/nope", "xml_path": str(xml)})
        assert out["ok"] is False
        assert "recents" in out["reason"]

    def test_bad_xml(self, tmp_path: Path) -> None:
        _seed("/lib", [_analyzed_track("/lib/a.mp3")])
        bad = tmp_path / "bad.xml"
        bad.write_text("<root/>", encoding="utf-8")
        out = _import_tag_priors({"library_path": "/lib", "xml_path": str(bad)})
        assert out["ok"] is False

    def test_missing_params(self) -> None:
        assert _import_tag_priors({})["ok"] is False


class TestAnalyzeTimeSidecarHook:
    """rpc._analyze_directory must load the priors sidecar for a known library
    and hand it to analyze_directory, so re-analysis keeps an earlier import."""

    def test_sidecar_reaches_analyze(self, tmp_path: Path, monkeypatch) -> None:
        from vibechek import rpc

        lib = str(tmp_path / "lib")
        _seed(lib, [_analyzed_track(str(tmp_path / "lib" / "a.mp3"))])
        record = next(r for r in library_state.load_state().recent if r.path == lib)
        priors = {tag_priors.norm_path(str(tmp_path / "lib" / "a.mp3")): {"genre": "House"}}
        tag_priors.save_priors(record.analysis_path, priors)

        captured: dict = {}

        def fake_analyze(library_path, config=None, tag_priors=None, **kw):
            captured["tag_priors"] = tag_priors
            return {"summary": {}, "tracks": []}

        monkeypatch.setattr("vibechek.analyzer.analyze_directory", fake_analyze)
        rpc._analyze_directory({"path": lib, "auto_save": False})
        assert captured["tag_priors"] == priors

    def test_unknown_library_passes_none(self, tmp_path: Path, monkeypatch) -> None:
        from vibechek import rpc

        captured: dict = {}

        def fake_analyze(library_path, config=None, tag_priors=None, **kw):
            captured["tag_priors"] = tag_priors
            return {"summary": {}, "tracks": []}

        monkeypatch.setattr("vibechek.analyzer.analyze_directory", fake_analyze)
        rpc._analyze_directory({"path": str(tmp_path / "fresh"), "auto_save": False})
        assert captured["tag_priors"] is None
