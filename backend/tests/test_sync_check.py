from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from apple_helpers import library_bytes, music_track, playlist, write_library
from cuebridge import mcp_server as server
from cuebridge.parsers.apple_music_xml import parse_apple_music_plist
from cuebridge.services import apple_music_service, rekordbox_db as rd, sync_check


def music(tmp_path):
    real = tmp_path / "real.mp3"

    for name in ("real.mp3", "a.mp3", "b.mp3"):
        (tmp_path / name).write_bytes(b"x")

    return parse_apple_music_plist(library_bytes(
        [music_track(1, "A", "X", tmp_path / "a.mp3"), music_track(2, "B", "X", tmp_path / "b.mp3"),
         music_track(3, "Renamed", "X", real)],
        [playlist("Set", 1, 2), playlist("Warm", 2)],
    )), real


def snapshot(tmp_path, real, set_ids=("c1", "c2"), warm_ids=("c2",), extra=()):
    contents = [
        rd.DbContent("c1", "A", "X", str(tmp_path / "a.mp3"), 200_000),
        rd.DbContent("c2", "B", "X", str(tmp_path / "b.mp3"), 200_000),
        rd.DbContent("c9", "Renamed", "X", str(tmp_path / "old-name.mp3"), 200_000),
        *extra,
    ]
    playlists = [
        rd.DbPlaylist("p1", "Set", None, False, False, (), tuple(f"s{i}" for i, _ in enumerate(set_ids)), tuple(set_ids)),
        rd.DbPlaylist("p2", "Warm", None, False, False, (), tuple(f"w{i}" for i, _ in enumerate(warm_ids)), tuple(warm_ids)),
    ]
    return rd.DbSnapshot(tuple(contents), tuple(playlists))


def serato_plan(changes=0, unchanged=3, skipped=0):
    item = lambda: SimpleNamespace(title="T", artist="X", before=((1, 1000),), after=((1, 2000),))
    return SimpleNamespace(
        items=[object()] * (changes + unchanged + skipped), to_change=[item() for _ in range(changes)],
        with_status=lambda s: {"unchanged": [1] * unchanged, "skip": [1] * skipped}.get(s, []),
        skip_reasons={"x": skipped} if skipped else {}, flagged_timing=[],
    )


def inputs(tmp_path, **kw):
    lib, real = music(tmp_path)
    base = dict(music=lib, snapshot=snapshot(tmp_path, real), database_path=tmp_path / "master.db",
                serato_plan=serato_plan(), xml_path=tmp_path / "rb.xml",
                xml_modified=datetime(2026, 1, 1, 12), database_modified=datetime(2026, 1, 1, 12))
    base.update(kw)
    return sync_check.Inputs(**base)


def attention(report):
    return {(a["area"], a["what"]): a for a in report["needsAttention"]}


class TestReport:
    def test_everything_in_sync(self, tmp_path):
        inp = inputs(tmp_path)
        report = sync_check.build_report(inp)

        # The one broken row is repointable, so "in sync" needs it fixed first.
        assert report["inSync"] is False
        assert [a["fix"] for a in report["needsAttention"]] == ["fix_moved_files_in_rekordbox"]
        assert report["nothingWasChanged"] is True

    def test_nothing_to_report_when_the_files_are_fine(self, tmp_path):
        lib, real = music(tmp_path)
        snap = snapshot(tmp_path, real)
        snap = rd.DbSnapshot(tuple(c if c.id != "c9" else rd.DbContent("c9", "Renamed", "X", str(real), 200_000) for c in snap.contents), snap.playlists)

        report = sync_check.build_report(inputs(tmp_path, snapshot=snap))

        assert report["inSync"] is True and report["needsAttention"] == []
        assert "in sync" in report["summary"]

    def test_a_playlist_that_differs_names_its_fix(self, tmp_path):
        lib, real = music(tmp_path)
        report = sync_check.build_report(inputs(tmp_path, snapshot=snapshot(tmp_path, real, warm_ids=())))

        item = attention(report)[("playlists", "Apple Music playlists that differ from Rekordbox's")]
        assert item["count"] == 1 and item["fix"] == "sync_playlists_to_rekordbox"
        assert report["tracksAndPlaylists"]["playlists"]["differ"][0]["name"] == "Warm"

    def test_a_track_not_in_rekordbox_is_reported(self, tmp_path):
        lib, real = music(tmp_path)
        snap = snapshot(tmp_path, real)
        snap = rd.DbSnapshot(tuple(c for c in snap.contents if c.id != "c2"), snap.playlists)

        report = sync_check.build_report(inputs(tmp_path, snapshot=snap))

        assert ("tracks", "playlisted Apple Music tracks that aren't in Rekordbox") in attention(report)

    def test_cues_that_differ_are_listed(self, tmp_path):
        report = sync_check.build_report(inputs(tmp_path, serato_plan=serato_plan(changes=2)))

        assert report["cues"]["differ"] == 2
        assert report["cues"]["differing"][0]["seratoNow"] != report["cues"]["differing"][0]["rekordboxWouldGive"]
        assert attention(report)[("cues", "tracks whose Serato cues differ from Rekordbox's")]["fix"] == "sync_cues_to_serato"

    def test_an_export_older_than_the_database_is_a_warning(self, tmp_path):
        report = sync_check.build_report(inputs(tmp_path, database_modified=datetime(2026, 1, 2, 18)))

        assert "30 hours" in report["cues"]["warning"]
        assert ("cues", "the Rekordbox XML export is older than the database") in attention(report)

    def test_a_small_gap_is_not_a_warning(self, tmp_path):
        stamp = datetime(2026, 1, 1, 12)
        report = sync_check.build_report(inputs(tmp_path, database_modified=stamp + timedelta(minutes=2)))

        assert "warning" not in report["cues"]

    def test_without_the_database_the_xml_is_used_and_files_cant_be_checked(self, tmp_path):
        lib, real = music(tmp_path)
        from cuebridge.models import Track
        inp = inputs(tmp_path, snapshot=None, snapshot_error="no SQLCipher")
        inp.xml_tracks = [Track(trackId="1", trackTitle="A", artist="X", filePath=str(tmp_path / "a.mp3"), durationMs=200_000, cues=[])]

        report = sync_check.build_report(inp)

        assert "XML export" in report["tracksAndPlaylists"]["comparedWith"] and "no SQLCipher" in report["tracksAndPlaylists"]["comparedWith"]
        assert report["files"] == {"couldNotCheck": "no SQLCipher"}
        assert report["inSync"] is False and "files" in report["summary"]

    def test_without_an_xml_cues_cant_be_checked(self, tmp_path):
        report = sync_check.build_report(inputs(tmp_path, serato_plan=None, xml_path=None, xml_modified=None))

        assert "XML export" in report["cues"]["couldNotCheck"]
        assert report["inSync"] is False

    def test_a_serato_problem_is_reported_not_raised(self, tmp_path):
        report = sync_check.build_report(inputs(tmp_path, serato_plan=None, serato_error="disk unreadable"))

        assert report["cues"] == {"couldNotCheck": "disk unreadable"}

    def test_nothing_at_all_to_compare_with(self, tmp_path):
        report = sync_check.build_report(inputs(tmp_path, snapshot=None, serato_plan=None))

        assert "couldNotCheck" in report["tracksAndPlaylists"]


class TestTool:
    @pytest.fixture(autouse=True)
    def clean(self, tmp_path, monkeypatch):
        apple_music_service.clear_cache()
        server.reset_state()
        monkeypatch.setattr(rd, "find_database", lambda: (_ for _ in ()).throw(rd.DatabaseError("no database here")))
        yield
        server.reset_state()

    def test_the_tool_is_read_only_and_reports_what_it_could_not_check(self, tmp_path):
        lib, real = music(tmp_path)
        xml = write_library(tmp_path / "Lib.xml", [music_track(1, "A", "X", tmp_path / "a.mp3")], [playlist("Set", 1)])
        server.load_apple_music_library(str(xml))
        before = sorted(p.name for p in tmp_path.iterdir())

        report = server.check_sync_status()

        assert report["nothingWasChanged"] is True
        assert report["files"] == {"couldNotCheck": "no database here"}
        assert "XML export" in report["cues"]["couldNotCheck"]
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_it_needs_apple_music(self, monkeypatch):
        from cuebridge.services import apple_music_live

        def fail():
            raise apple_music_live.MusicAppError("Music isn't open")

        monkeypatch.setattr(apple_music_live, "read_live_library", fail)

        with pytest.raises(Exception, match="Music isn't open"):
            server.check_sync_status()
