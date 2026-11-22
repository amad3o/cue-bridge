import pytest
from serato_helpers import AUDIO_WITH_LAME, BPMLOCK, COLOR, make_mp3, make_wav, rb_cue, rb_track

from cuebridge.parsers.serato_markers import CueEntry
from cuebridge.services.compare import cue_compare as cc


def wav(tmp_path, *cues, name="a.wav"):
    return make_wav(tmp_path / name, [COLOR, *cues, BPMLOCK])


def kinds(item):
    return [(d.slot, d.kind) for d in item.differences]


class TestStatus:
    def test_the_same_cues_match(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 1000, "#cc0000"), CueEntry(2, 5000, "#00ff00"))

        item = cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000), rb_cue(slot=2, ms=5000)))

        assert item.status == cc.MATCH and item.differences == ()
        assert (item.rekordbox_cues, item.serato_cues) == (2, 2)

    def test_a_cue_within_the_sync_tolerance_still_matches(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 1001, "#cc0000"))

        assert cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000))).status == cc.MATCH

    def test_rekordbox_cues_that_serato_lacks(self, tmp_path):
        path = wav(tmp_path)

        item = cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000), rb_cue(slot=1, ms=2000)))

        assert item.status == cc.REKORDBOX_ONLY and kinds(item) == [(1, cc.MISSING_IN_SERATO), (2, cc.MISSING_IN_SERATO)]
        assert item.differences[0].rekordbox.startswith("0:01.000")

    def test_serato_cues_that_rekordbox_lacks(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 1000, "#cc0000"), CueEntry(4, 9000, "#0000ff", "Drop"))

        item = cc.compare_track(rb_track(path))

        assert item.status == cc.SERATO_ONLY and kinds(item) == [(1, cc.ONLY_IN_SERATO), (5, cc.ONLY_IN_SERATO)]
        assert item.differences[1].serato == '0:09.000 #0000ff "Drop"'

    def test_a_serato_cue_a_sync_would_remove_shows_beside_the_ones_that_match(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 1000, "#cc0000"), CueEntry(3, 7000, "#0000ff"))

        item = cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000)))

        assert item.status == cc.DIFFERS and kinds(item) == [(4, cc.ONLY_IN_SERATO)]

    def test_no_cues_anywhere(self, tmp_path):
        assert cc.compare_track(rb_track(wav(tmp_path))).status == cc.NO_CUES

    def test_a_file_with_no_serato_tag_at_all(self, tmp_path):
        path = make_wav(tmp_path / "plain.wav")

        item = cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000)))

        assert item.status == cc.REKORDBOX_ONLY and "no Serato cue tag" in item.reason

    def test_a_plain_file_with_no_rekordbox_cues_has_none_anywhere(self, tmp_path):
        assert cc.compare_track(rb_track(make_wav(tmp_path / "plain.wav"))).status == cc.NO_CUES


class TestDifferences:
    def test_a_moved_cue_says_by_how_much(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 400, "#cc0000"))

        (diff,) = cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000))).differences

        assert (diff.kind, diff.slot, diff.delta_ms) == (cc.MOVED, 1, -600)
        assert (diff.rekordbox, diff.serato) == ("0:01.000", "0:00.400")

    def test_the_mp3_timing_correction_is_applied_before_comparing(self, tmp_path):
        synced = make_mp3(tmp_path / "s.mp3", [COLOR, CueEntry(0, 995, "#cc0000"), BPMLOCK], audio=AUDIO_WITH_LAME)
        stale = make_mp3(tmp_path / "t.mp3", [COLOR, CueEntry(0, 1000, "#cc0000"), BPMLOCK], audio=AUDIO_WITH_LAME)

        assert cc.compare_track(rb_track(synced, rb_cue(slot=0, ms=1000))).status == cc.MATCH
        (diff,) = cc.compare_track(rb_track(stale, rb_cue(slot=0, ms=1000))).differences
        assert (diff.kind, diff.delta_ms) == (cc.MOVED, 5)

    def test_a_chosen_colour_that_differs(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 1000, "#cc0000"))

        (diff,) = cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000, color="#00ff00"))).differences

        assert (diff.kind, diff.rekordbox, diff.serato) == (cc.COLOR, "#00ff00", "#cc0000")

    def test_rekordbox_s_default_green_is_not_a_chosen_colour(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 1000, "#cc0000"))

        assert cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000, color="#28e214"))).status == cc.MATCH

    def test_no_colour_in_rekordbox_never_counts_as_a_difference(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 1000, "#123456"))

        assert cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000, color=None))).status == cc.MATCH

    def test_a_name_that_differs(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 1000, "#cc0000", "Intro"))

        (diff,) = cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000, name="Drop"))).differences

        assert (diff.kind, diff.rekordbox, diff.serato) == (cc.NAME, "Drop", "Intro")

    def test_a_rekordbox_cue_without_a_name_keeps_serato_s(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 1000, "#cc0000", "Intro"))

        assert cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000))).status == cc.MATCH

    def test_memory_cues_are_compared_in_the_free_slots_as_the_sync_places_them(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 1000, "#cc0000"), CueEntry(1, 3000, "#cc0000"))
        track = rb_track(path, rb_cue(slot=0, ms=1000), rb_cue(kind="memory", slot=None, ms=3000))

        assert cc.compare_track(track).status == cc.MATCH
        assert cc.compare_track(track, include_memory_cues=False).differences[0].kind == cc.ONLY_IN_SERATO

    def test_memory_cues_with_no_free_slot_are_counted(self, tmp_path):
        path = wav(tmp_path)
        cues = [rb_cue(slot=s, ms=s * 1000 + 1000) for s in range(8)] + [rb_cue(kind="memory", slot=None, ms=99_000)]

        assert cc.compare_track(rb_track(path, *cues)).memory_cues_without_slot == 1

    def test_a_serato_cue_where_rekordbox_has_a_hot_loop_says_so(self, tmp_path):
        path = wav(tmp_path, CueEntry(7, 259_986, "#1fa392"))

        item = cc.compare_track(rb_track(path, rb_cue(kind="loop", slot=7, ms=259_972), rb_cue(slot=0, ms=1000)))
        loop_diff = next(d for d in item.differences if d.slot == 8)

        assert loop_diff.kind == cc.ONLY_IN_SERATO and "a loop at 4:19.972" in loop_diff.rekordbox

    def test_loops_are_counted_not_compared(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 1000, "#cc0000"))

        item = cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000), rb_cue(kind="loop", slot=7, ms=5000)))

        assert item.status == cc.MATCH and item.loops_not_compared == 1


class TestSkips:
    def test_no_path(self):
        assert cc.compare_track(rb_track(None, rb_cue())).reason == "the track has no file path"

    def test_a_streaming_track(self):
        track = rb_track(None, rb_cue())
        track.filePath = "tidal:123"

        assert "streaming" in cc.compare_track(track).reason

    def test_a_missing_file(self, tmp_path):
        item = cc.compare_track(rb_track(tmp_path / "gone.wav", rb_cue()))

        assert item.status == cc.SKIPPED and "wasn't found" in item.reason

    def test_a_missing_file_is_found_through_the_moved_files(self, tmp_path):
        real = wav(tmp_path, CueEntry(0, 1000, "#cc0000"))
        track = rb_track(tmp_path / "old-name.wav", rb_cue(slot=0, ms=1000))

        item = cc.compare_track(track, moved_files={"1": str(real)})

        assert item.status == cc.MATCH and item.relocated and item.path == real

    def test_an_unsupported_format(self, tmp_path):
        path = tmp_path / "a.flac"
        path.write_bytes(b"x")

        assert ".flac" in cc.compare_track(rb_track(path, rb_cue())).reason

    def test_an_unreadable_serato_tag_is_skipped_with_the_reason(self, tmp_path):
        path = make_wav(tmp_path / "bad.wav", [COLOR])
        import cuebridge.services.compare.cue_compare as module
        from cuebridge.parsers.serato_markers import MarkersError

        def broken(_):
            raise MarkersError("bad.wav: the Serato tag is corrupt")

        original, module.read_markers2 = module.read_markers2, broken

        try:
            item = cc.compare_track(rb_track(path, rb_cue()))
        finally:
            module.read_markers2 = original

        assert item.status == cc.SKIPPED and item.reason == "the Serato tag is corrupt"


class TestNeverWrites:
    def test_the_file_is_untouched(self, tmp_path):
        path = wav(tmp_path, CueEntry(0, 400, "#cc0000"))
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns

        cc.compare_track(rb_track(path, rb_cue(slot=0, ms=1000), rb_cue(slot=3, ms=9000)))

        assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


class TestWholeComparison:
    def test_counts_by_status_and_kind(self, tmp_path):
        a = wav(tmp_path, CueEntry(0, 1000, "#cc0000"), name="a.wav")
        b = wav(tmp_path, CueEntry(0, 400, "#cc0000"), name="b.wav")
        c = wav(tmp_path, CueEntry(0, 1000, "#cc0000"), name="c.wav")
        tracks = [
            rb_track(a, rb_cue(slot=0, ms=1000), track_id="1"),
            rb_track(b, rb_cue(slot=0, ms=1000), track_id="2"),
            rb_track(c, track_id="3"),
            rb_track(None, rb_cue(), track_id="4"),
        ]

        result = cc.compare_cues(tracks)

        assert dict(result.counts) == {cc.MATCH: 1, cc.DIFFERS: 1, cc.SERATO_ONLY: 1, cc.SKIPPED: 1}
        assert dict(result.difference_kinds) == {cc.MOVED: 1, cc.ONLY_IN_SERATO: 1}
        assert dict(result.skip_reasons) == {"the track has no file path": 1}
        assert [i.track_id for i in result.with_status(cc.DIFFERS, cc.SERATO_ONLY)] == ["2", "3"]


class TestTool:
    """compare_cues over Rekordbox's (fake) database and over an XML export, with real tagged files."""

    @pytest.fixture(autouse=True)
    def clean(self):
        from cuebridge import mcp_server
        from cuebridge.services.rekordbox.service import clear_cache

        clear_cache()
        mcp_server.reset_state()
        yield
        clear_cache()
        mcp_server.reset_state()

    @pytest.fixture
    def database(self, tmp_path, monkeypatch):
        import json

        from test_rekordbox_db import FakeBackend

        from cuebridge.services.rekordbox import db as rd

        files = {
            "c1": wav(tmp_path, CueEntry(0, 400, "#cc0000"), name="one.wav"),  # in a playlist; Serato's cue is off
            "c2": wav(tmp_path, CueEntry(0, 1000, "#cc0000"), name="two.wav"),  # in a playlist; no Rekordbox cue
            "c3": wav(tmp_path, CueEntry(0, 1000, "#cc0000"), name="three.wav"),  # in no playlist
            "c4": wav(tmp_path, CueEntry(0, 1000, "#cc0000"), name="four.wav"),  # in a playlist; matches
        }
        state = {
            "contents": [{"id": c, "title": c.upper(), "artist": "X", "path": str(p)} for c, p in files.items()],
            "playlists": [{"id": "p1", "name": "Set", "songs": [["s1", "c1"], ["s2", "c2"], ["s3", "c4"]]}],
            "cues": [["c1", 1, 1000, "k1"], ["c4", 1, 1000, "k2"]],
        }
        db = tmp_path / "master.db"
        db.write_text(json.dumps(state))
        backend = FakeBackend()
        monkeypatch.setattr(rd, "default_backend", lambda: backend)
        monkeypatch.setattr(rd, "find_database", lambda: db)
        return files

    def test_it_reports_the_counts_and_lists_the_differences(self, database):
        from cuebridge import mcp_server as server

        result = server.compare_cues(use_apple_music_paths=False)

        assert result["cuesFrom"].startswith("Rekordbox's database")
        assert (result["tracksCompared"], result["match"], result["differ"], result["cuesOnlyInSerato"]) == (3, 1, 1, 1)
        assert [(t["title"], t["status"]) for t in result["tracks"]] == [("C1", "differs"), ("C2", "serato_only")]
        assert result["tracks"][0]["differences"][0]["deltaMs"] == -600
        assert "Nothing was changed" in result["summary"] and "leaves them alone" in result["summary"]

    def test_a_track_in_no_playlist_is_not_read_unless_asked(self, database):
        from cuebridge import mcp_server as server

        assert server.compare_cues(show="all", use_apple_music_paths=False)["tracksCompared"] == 3
        everything = server.compare_cues(show="all", playlisted_only=False, use_apple_music_paths=False)

        assert everything["tracksCompared"] == 4 and "C3" in [t["title"] for t in everything["tracks"]]

    def test_show_picks_one_kind_and_pages(self, database):
        from cuebridge import mcp_server as server

        only = server.compare_cues(show="serato_only", use_apple_music_paths=False)
        page = server.compare_cues(show="all", limit=1, offset=1, use_apple_music_paths=False)

        assert [t["title"] for t in only["tracks"]] == ["C2"]
        assert (page["total"], page["returned"], page["hasMore"]) == (3, 1, True)

    def test_a_bad_show_value_is_refused(self, database):
        from mcp.server.mcpserver.exceptions import ToolError

        from cuebridge import mcp_server as server

        with pytest.raises(ToolError, match="show must be one of"):
            server.compare_cues(show="nope")

    def test_it_changes_no_file(self, database):
        from cuebridge import mcp_server as server

        before = {c: (p.read_bytes(), p.stat().st_mtime_ns) for c, p in database.items()}

        server.compare_cues(show="all", playlisted_only=False, use_apple_music_paths=False)

        assert before == {c: (p.read_bytes(), p.stat().st_mtime_ns) for c, p in database.items()}

    def test_from_an_xml_export_only_tracks_with_cues_are_read(self, tmp_path):
        from cuebridge import mcp_server as server

        one = wav(tmp_path, CueEntry(0, 1000, "#cc0000"), name="one.wav")
        two = wav(tmp_path, CueEntry(0, 1000, "#cc0000"), name="two.wav")
        xml = tmp_path / "rekordbox.xml"
        xml.write_text(
            "<DJ_PLAYLISTS><COLLECTION>"
            f'<TRACK TrackID="1" Name="One" Location="file://localhost{one}"><POSITION_MARK Type="0" Start="1.0" Num="0"/></TRACK>'
            f'<TRACK TrackID="2" Name="Two" Location="file://localhost{two}"/>'
            "</COLLECTION></DJ_PLAYLISTS>"
        )
        server.load_rekordbox_library(str(xml))

        result = server.compare_cues(use_apple_music_paths=False)

        assert result["tracksCompared"] == 1 and result["match"] == 1
        assert any("no playlist information" in n for n in result["cueNotes"])
