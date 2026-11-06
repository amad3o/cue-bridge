from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from cuebridge import mcp_server as server
from cuebridge.services import db_library as dl, rekordbox_db as rd

FIXTURE = Path(__file__).parent / "fixtures" / "rekordbox_sample.xml"


def snapshot(*cues, contents=None):
    contents = contents or [rd.DbContent("c1", "Song", "Artist", "/Music/song.mp3", 200_000, analysis_path="/PIONEER/USBANLZ/a/ANLZ0000.DAT")]
    return rd.DbSnapshot(tuple(contents), (), tuple(cues))


def cue(kind, ms, **kw):
    return rd.DbCue("c1", kind, ms, f"id{kind}-{ms}", **kw)


def read(*cues, **kw):
    return dl.tracks_from_snapshot(snapshot(*cues), first_beat=lambda path: 111, **kw)


class TestCues:
    def test_memory_and_hot_cues(self):
        (track,) = read(cue(0, 1000), cue(1, 1000), cue(2, 5000)).tracks

        assert [(c.cueType, c.cueIndex, c.timeMs) for c in track.cues] == [
            ("memory", None, 1000), ("hotcue", 0, 1000), ("hotcue", 1, 5000),
        ]

    def test_slots_follow_what_a_real_export_shows(self):
        # Kinds 1-3 are A-C, 4 is skipped by Rekordbox, 5 and 6 are D and E.
        (track,) = read(cue(1, 10), cue(2, 20), cue(3, 30), cue(5, 40), cue(6, 50)).tracks

        assert [c.cueIndex for c in track.cues] == [0, 1, 2, 3, 4]

    def test_kinds_nobody_has_checked_are_left_out_and_reported(self):
        library = read(cue(1, 10), cue(4, 20), cue(7, 30), cue(9, 40))

        assert [c.timeMs for c in library.tracks[0].cues] == [10]
        assert sorted(k for _, k in library.unverified_cues) == [4, 7, 9]

    def test_a_loop_has_an_end(self):
        (track,) = read(cue(0, 1000, out_ms=5000), cue(1, 2000, out_ms=6000)).tracks

        assert [(c.cueType, c.cueIndex, c.timeMs, c.endMs) for c in track.cues] == [
            ("loop", None, 1000, 5000), ("loop", 0, 2000, 6000),
        ]

    def test_the_default_colour_means_no_colour(self):
        (track,) = read(cue(1, 10), cue(2, 20, color_index=0)).tracks

        assert [c.color for c in track.cues] == [None, None]

    def test_a_known_colour_index_is_converted(self):
        (track,) = read(cue(1, 10, color_index=41)).tracks

        assert track.cues[0].color == "#e02823"

    def test_an_unknown_colour_index_is_reported_not_guessed(self):
        library = read(cue(1, 10, color_index=17))

        assert library.tracks[0].cues[0].color is None
        assert library.unknown_colors == [("Song", 17)]

    def test_a_memory_cues_colour_is_ignored(self):
        (track,) = read(cue(0, 10, color_index=41)).tracks

        assert track.cues[0].color is None

    def test_the_comment_becomes_the_cue_name(self):
        (track,) = read(cue(1, 10, comment="Drop")).tracks

        assert track.cues[0].cueName == "Drop"

    def test_ids_match_the_xml_parsers(self):
        (track,) = read(cue(0, 1000), cue(1, 1000)).tracks

        assert [c.id for c in track.cues] == ["rekordbox-c1-memory--1-1000", "rekordbox-c1-hotcue-0-1000"]


class TestTracks:
    def test_path_length_and_first_beat_are_carried(self):
        (track,) = read(cue(1, 10)).tracks

        assert (track.trackId, track.filePath, track.durationMs, track.firstBeatMs) == ("c1", "/Music/song.mp3", 200_000, 111)

    def test_the_beat_grid_is_only_read_for_tracks_with_cues(self):
        asked = []
        contents = [rd.DbContent("c1", "A", "X", "/a.mp3", 1, analysis_path="a"), rd.DbContent("c2", "B", "X", "/b.mp3", 1, analysis_path="b")]

        dl.tracks_from_snapshot(snapshot(cue(1, 10), contents=contents), first_beat=lambda p: asked.append(p) or 5)

        assert asked == ["a"]

    def test_a_track_with_cues_and_no_beat_grid_is_counted(self):
        library = dl.tracks_from_snapshot(snapshot(cue(1, 10)), first_beat=lambda p: None)

        assert library.beat_grids_missing == 1 and library.tracks[0].firstBeatMs is None

    def test_tracks_are_found_by_id(self):
        library = read(cue(1, 10))

        assert library.tracks_by_id["c1"] is library.tracks[0]

    def test_a_missing_analysis_file_is_not_an_error(self, tmp_path):
        assert dl.read_first_beat_ms("/PIONEER/USBANLZ/none/ANLZ0000.DAT", share=tmp_path) is None
        assert dl.read_first_beat_ms(None) is None

    def test_an_unreadable_analysis_file_is_not_an_error(self, tmp_path):
        broken = tmp_path / "PIONEER" / "ANLZ0000.DAT"
        broken.parent.mkdir()
        broken.write_bytes(b"not an analysis file")

        assert dl.read_first_beat_ms("/PIONEER/ANLZ0000.DAT", share=tmp_path) is None


class TestSource:
    @pytest.fixture
    def with_database(self, monkeypatch, tmp_path):
        db = tmp_path / "master.db"
        monkeypatch.setattr(rd, "find_database", lambda: db)
        monkeypatch.setattr(rd, "read_snapshot", lambda path, backend: snapshot(cue(1, 10), cue(4, 20)))
        monkeypatch.setattr(dl, "read_first_beat_ms", lambda *a, **k: 100)
        server.reset_state()
        yield
        server.reset_state()

    def test_auto_prefers_the_database(self, with_database):
        source = server._cue_source("auto")

        assert not source.from_xml and "database" in source.label
        assert [c.timeMs for c in source.tracks[0].cues] == [10]

    def test_cues_left_out_are_explained(self, with_database):
        assert "unusual slots" in " ".join(server._cue_source("database").notes)

    def test_auto_falls_back_to_the_loaded_xml_and_says_why(self, tmp_path, monkeypatch):
        server.reset_state()
        server.load_rekordbox_library(str(FIXTURE))

        source = server._cue_source("auto")

        server.reset_state()
        assert source.from_xml and "XML" in source.label
        assert any("database couldn't be read" in n for n in source.notes)

    def test_asking_for_the_database_does_not_fall_back(self):
        server.reset_state()
        server.load_rekordbox_library(str(FIXTURE))

        with pytest.raises(ToolError, match="Could not read Rekordbox's database"):
            server._cue_source("database")

        server.reset_state()

    def test_asking_for_the_xml_ignores_the_database(self, with_database):
        server.load_rekordbox_library(str(FIXTURE))

        assert server._cue_source("xml").from_xml

    def test_neither_source_is_a_clear_error(self):
        server.reset_state()

        with pytest.raises(ToolError, match="load_rekordbox_library"):
            server._cue_source("auto")

    def test_an_unknown_choice_is_refused(self):
        with pytest.raises(ToolError, match="cue_source"):
            server._cue_source("both")

    def test_the_sync_dry_run_reports_where_the_cues_came_from(self, with_database):
        result = server.sync_cues_to_serato()

        assert "database" in result["cuesFrom"] and result["dryRun"] is True
