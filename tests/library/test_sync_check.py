from collections import Counter
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from apple_helpers import library_bytes, music_track, playlist, write_library

from cuebridge import mcp_server as server
from cuebridge.models import Track
from cuebridge.services.apple_music import service as apple_music_service
from cuebridge.services.apple_music.xml_parser import parse_apple_music_plist
from cuebridge.services.compare import sync_check
from cuebridge.services.rekordbox import db as rd
from cuebridge.services.serato import sync as serato_sync


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


def serato_plan(changes=0, unchanged=3, skipped=0, removed=0):
    def item():
        return SimpleNamespace(title="T", artist="X", before=((1, 1000),), after=((1, 2000),), removed=((2, 5000),) * removed)

    return SimpleNamespace(
        items=[object()] * (changes + unchanged + skipped), to_change=[item() for _ in range(changes)],
        with_status=lambda s: {"unchanged": [1] * unchanged, "skip": [1] * skipped}.get(s, []),
        skip_reasons={"x": skipped} if skipped else {}, flagged_timing=[],
    )


def inputs(tmp_path, **kw):
    lib, real = music(tmp_path)
    base = dict(music=lib, snapshot=snapshot(tmp_path, real), database_path=tmp_path / "master.db",
                serato_plan=serato_plan(), serato_only=serato_sync.SeratoOnly(checked=2), xml_path=tmp_path / "rb.xml",
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

    @pytest.mark.parametrize("set_ids,listed,what", [
        (("c2", "c1"), "sameTracksDifferentOrder", "playlists with the same tracks in a different order"),
        (("c1", "c2", "c2"), "sameTracksDifferentRepeats",
         "playlists with the same tracks repeated a different number of times"),
    ])
    def test_order_and_repeats_stop_it_being_in_sync(self, tmp_path, set_ids, listed, what):
        lib, real = music(tmp_path)
        snap = snapshot(tmp_path, real, set_ids=set_ids)
        snap = rd.DbSnapshot(tuple(c if c.id != "c9" else rd.DbContent("c9", "Renamed", "X", str(real), 200_000) for c in snap.contents), snap.playlists)

        report = sync_check.build_report(inputs(tmp_path, snapshot=snap))
        playlists = report["tracksAndPlaylists"]["playlists"]

        assert report["inSync"] is False
        assert playlists[listed] == ["Set"] and playlists["differ"] == [] and playlists["inSync"] == 1
        item = attention(report)[("playlists", what)]
        assert item["count"] == 1 and item["fix"].startswith("no tool")

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

    def in_sync_otherwise(self, tmp_path, **kw):
        """A report whose only possible complaints come from the arguments: the one broken row is fine here."""
        lib, real = music(tmp_path)
        snap = snapshot(tmp_path, real)
        snap = rd.DbSnapshot(tuple(c if c.id != "c9" else rd.DbContent("c9", "Renamed", "X", str(real), 200_000) for c in snap.contents), snap.playlists)

        return sync_check.build_report(inputs(tmp_path, snapshot=snap, **kw))

    def test_cues_only_in_serato_mean_not_in_sync(self, tmp_path):
        only = serato_sync.SeratoOnly(with_cues=[Track(trackId="c2", trackTitle="Only", artist="X", cues=[])], checked=5)
        report = self.in_sync_otherwise(tmp_path, serato_only=only)

        assert report["inSync"] is False
        assert report["cues"]["onlyInSerato"]["tracks"] == 1
        assert report["cues"]["onlyInSerato"]["examples"] == [{"title": "Only", "artist": "X"}]
        item = attention(report)[("cues", "tracks with cues in Serato but none in Rekordbox")]
        assert item["count"] == 1 and "compare_cues" in item["fix"]

    def test_not_looking_for_serato_only_cues_is_never_called_in_sync(self, tmp_path):
        report = self.in_sync_otherwise(tmp_path, serato_only=None)

        assert report["inSync"] is False
        assert "cues that are only in serato" in report["summary"].lower()
        assert "seratoOnlyNotChecked" in report["cues"]

    def test_files_that_could_not_be_ruled_out_are_mentioned_in_the_summary(self, tmp_path):
        only = serato_sync.SeratoOnly(unchecked=Counter({".m4a files can't be checked yet": 3}), checked=3)
        report = self.in_sync_otherwise(tmp_path, serato_only=only)

        assert report["inSync"] is True
        assert "couldn't be ruled out on 3 track(s)" in report["summary"]

    def test_a_sync_that_would_remove_serato_cues_says_so(self, tmp_path):
        report = sync_check.build_report(inputs(tmp_path, serato_plan=serato_plan(changes=2, removed=3)))

        assert report["cues"]["existingSeratoCuesASyncWouldRemove"] == 6
        assert "remove 6 Serato cue(s)" in attention(report)[("cues", "tracks whose Serato cues differ from Rekordbox's")]["fix"]

    def test_an_export_older_than_the_database_is_a_warning(self, tmp_path):
        report = sync_check.build_report(inputs(tmp_path, cues_from_xml=True, database_modified=datetime(2026, 1, 2, 18)))

        assert "30 hours" in report["cues"]["warning"]
        assert ("cues", "the Rekordbox XML export is older than the database") in attention(report)

    def test_cues_from_the_database_never_warn_about_the_export(self, tmp_path):
        report = sync_check.build_report(inputs(tmp_path, cues_from_xml=False, database_modified=datetime(2026, 1, 2, 18)))

        assert "warning" not in report["cues"]

    def test_notes_about_cues_that_could_not_be_read_need_attention(self, tmp_path):
        report = sync_check.build_report(inputs(tmp_path, cue_notes=["2 cues in unusual slots were left out"]))

        assert report["cues"]["notes"] == ["2 cues in unusual slots were left out"]
        assert ("cues", "notes about cues CueBridge couldn't fully read") in attention(report)

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
        from cuebridge.services.apple_music import live as apple_music_live

        def fail():
            raise apple_music_live.MusicAppError("Music isn't open")

        monkeypatch.setattr(apple_music_live, "read_live_library", fail)

        with pytest.raises(Exception, match="Music isn't open"):
            server.check_sync_status()
