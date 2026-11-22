from datetime import datetime

import pytest

from cuebridge.parsers.apple_music_xml import MusicLibrary, MusicPlaylist, MusicTrack
from cuebridge.services.reports import library_audit as la

NOW = datetime(2026, 11, 18, 12, 0)


def mt(i, title="Song", artist="Artist", **kw):
    fields = dict(
        persistent_id=f"P{i}", album="Album", genre="House", kind="MPEG audio file", bpm=120, year=2020,
        duration_ms=200_000, size=5_000_000 + i, bit_rate=320, sample_rate=44100, date_added=datetime(2025, 1, 1),
        path=f"/Music/{i}.mp3",
    )
    fields.update(kw)

    return MusicTrack(track_id=i, title=title, artist=artist, **fields)


def pl(name, *ids, folder=False):
    return MusicPlaylist(len(name), name.upper(), name, None, folder, "user", tuple(ids))


def lib(tracks, *playlists):
    return MusicLibrary(None, "/Music/", list(tracks), list(playlists), {t.track_id: t for t in tracks})


def names(items):
    return [i["title"] for i in items]


class TestScope:
    def test_only_playlisted_tracks_are_audited_by_default(self):
        music = lib([mt(1), mt(2), mt(3)], pl("A", 1, 3))

        tracks, left_out = la.scope_tracks(music, True)

        assert [t.track_id for t in tracks] == [1, 3] and left_out == 1

    def test_a_folder_does_not_make_its_tracks_playlisted(self):
        music = lib([mt(1)], pl("F", 1, folder=True))

        assert la.scope_tracks(music, True) == ([], 1)

    def test_everything_can_be_asked_for(self):
        assert len(la.scope_tracks(lib([mt(1), mt(2)]), False)[0]) == 2


class TestDuplicates:
    def test_the_same_title_and_artist(self):
        report = la.audit_duplicates([mt(1, "Hello", "Adele"), mt(2, "hello!", "ADELE", path="/Music/b.mp3"), mt(3, "Other")])

        (group,) = report.sections["same_title_and_artist"]

        assert group["count"] == 2 and [t["trackId"] for t in group["tracks"]] == [1, 2]
        assert report.facts["sameTitleAndArtistGroups"] == 1 and report.facts["sameTitleAndArtistTracks"] == 2

    def test_a_track_with_no_artist_is_not_matched_by_name(self):
        report = la.audit_duplicates([mt(1, "Intro", None), mt(2, "Intro", None)])

        assert report.sections["same_title_and_artist"] == [] and report.facts["tracksWithoutTitleOrArtist"] == 2
        assert "can't be matched" in report.notes[0]

    def test_the_same_file_listed_twice(self):
        report = la.audit_duplicates([mt(1, path="/Music/a.mp3"), mt(2, "Other", "Other", path="/Music/a.mp3")])

        (group,) = report.sections["same_file"]

        assert group["key"] == "/Music/a.mp3" and group["count"] == 2

    def test_the_same_size_and_length_under_different_names_and_paths(self):
        a = mt(1, "Track 01", "X", size=9_000_000, duration_ms=180_000)
        b = mt(2, "Copy of track", "Y", size=9_000_000, duration_ms=180_400, path="/Music/copy.mp3")

        (group,) = la.audit_duplicates([a, b]).sections["same_size_and_length"]

        assert [t["trackId"] for t in group["tracks"]] == [1, 2]

    def test_a_pair_already_found_by_name_is_not_listed_again_by_size(self):
        a = mt(1, "Same", "X", size=9_000_000, duration_ms=180_000)
        b = mt(2, "Same", "X", size=9_000_000, duration_ms=180_000, path="/Music/b.mp3")

        report = la.audit_duplicates([a, b])

        assert report.sections["same_size_and_length"] == [] and len(report.sections["same_title_and_artist"]) == 1

    def test_the_same_size_but_a_different_length_is_not_a_duplicate(self):
        a = mt(1, "A", "X", size=9_000_000, duration_ms=180_000)
        b = mt(2, "B", "Y", size=9_000_000, duration_ms=240_000, path="/Music/b.mp3")

        assert la.audit_duplicates([a, b]).sections["same_size_and_length"] == []

    def test_a_run_of_three_with_close_lengths_is_one_group(self):
        tracks = [mt(i, f"T{i}", f"A{i}", size=7_000_000, duration_ms=100_000 + i * 300, path=f"/Music/{i}.mp3") for i in (1, 2, 3)]

        (group,) = la.audit_duplicates(tracks).sections["same_size_and_length"]

        assert group["count"] == 3

    def test_no_duplicates(self):
        report = la.audit_duplicates([mt(1, "A", "X"), mt(2, "B", "Y")])

        assert all(not items for items in report.sections.values())


class TestMetadataGaps:
    def test_each_gap_is_listed_and_counted(self):
        tracks = [
            mt(1), mt(2, artist=None), mt(3, album=None, genre=None), mt(4, bpm=None, year=None), mt(5, duration_ms=None),
        ]

        report = la.audit_metadata_gaps(tracks)

        assert names(report.sections["no_artist"]) == ["Song"] and report.facts["no_artist"] == 1
        assert [i["trackId"] for i in report.sections["no_album"]] == [3]
        assert [i["trackId"] for i in report.sections["no_genre"]] == [3]
        assert [i["trackId"] for i in report.sections["no_bpm"]] == [4]
        assert [i["trackId"] for i in report.sections["no_year"]] == [4]
        assert [i["trackId"] for i in report.sections["no_length"]] == [5]
        assert report.facts["tracksWithAtLeastOneGap"] == 4

    def test_unknown_artist_or_album_in_the_tags_or_the_folder(self):
        tracks = [
            mt(1, artist="Unknown Artist"), mt(2, album="Unknown Album"),
            mt(3, path="/Music/Unknown Artist/Unknown Album/x.mp3"), mt(4),
        ]

        assert [i["trackId"] for i in la.audit_metadata_gaps(tracks).sections["unknown_artist_or_album"]] == [1, 2, 3]

    def test_very_short_tracks_are_probably_samples(self):
        tracks = [mt(1, duration_ms=2_000), mt(2, duration_ms=15_000), mt(3, duration_ms=None)]

        report = la.audit_metadata_gaps(tracks)

        assert [i["trackId"] for i in report.sections["very_short"]] == [1]

    def test_a_short_track_alone_is_not_counted_as_a_gap(self):
        assert la.audit_metadata_gaps([mt(1, duration_ms=2_000)]).facts["tracksWithAtLeastOneGap"] == 0


class TestPlaylists:
    def music(self):
        tracks = [mt(i, f"T{i}") for i in range(1, 7)]

        return lib(tracks, pl("House", 1, 2, 3), pl("Warm", 2, 3, 4), pl("Peak", 3, 4), pl("Empty"), pl("Folder", folder=True))

    def test_tracks_in_no_playlist(self):
        report = la.audit_playlists(self.music())

        assert names(report.sections["not_in_any_playlist"]) == ["T5", "T6"] and report.facts["tracksInNoPlaylist"] == 2

    def test_tracks_in_many_playlists_say_which(self):
        report = la.audit_playlists(self.music(), many=3)

        (item,) = report.sections["in_many_playlists"]

        assert item["title"] == "T3" and item["playlists"] == ["House", "Warm", "Peak"]

    def test_empty_playlists_but_not_folders(self):
        assert la.audit_playlists(self.music()).sections["empty_playlists"] == [{"name": "Empty"}]

    def test_the_biggest_playlists_first(self):
        sizes = la.audit_playlists(self.music()).sections["biggest_playlists"]

        assert [(s["name"], s["tracks"]) for s in sizes][:3] == [("House", 3), ("Warm", 3), ("Peak", 2)]

    def test_overlap_between_playlists_is_ranked(self):
        overlaps = la.audit_playlists(self.music()).sections["overlaps"]

        assert overlaps[0] == {"playlists": ["House", "Warm"], "sharedTracks": 2, "percentOfSmaller": 67}
        assert {tuple(o["playlists"]) for o in overlaps} == {("House", "Warm"), ("House", "Peak"), ("Peak", "Warm")}

    def test_a_track_listed_twice_in_one_playlist_counts_once_for_membership(self):
        music = lib([mt(1)], pl("A", 1, 1), pl("B", 1))

        report = la.audit_playlists(music, many=2)

        assert report.sections["in_many_playlists"][0]["playlists"] == ["A", "B"]
        assert la.audit_playlists(music).sections["biggest_playlists"][0]["uniqueTracks"] == 1


class TestFormats:
    def test_counts_by_format_and_sample_rate(self):
        tracks = [mt(1), mt(2), mt(3, kind="WAV audio file", bit_rate=1411, sample_rate=48000)]

        report = la.audit_formats(tracks)

        assert report.sections["by_format"][0] == {"format": "MPEG audio file", "tracks": 2}
        assert report.sections["by_sample_rate"] == [{"sampleRateHz": 44100, "tracks": 2}, {"sampleRateHz": 48000, "tracks": 1}]
        assert report.facts["lossless"] == 1

    def test_low_bit_rate_is_only_about_lossy_files(self):
        tracks = [mt(1, bit_rate=128), mt(2, bit_rate=192), mt(3, kind="Apple Lossless audio file", bit_rate=96), mt(4, bit_rate=None)]

        report = la.audit_formats(tracks, low_bit_rate=192)

        assert [i["trackId"] for i in report.sections["low_bit_rate"]] == [1]
        assert report.facts["lossyBelowThreshold"] == 1 and report.facts["lossyWithKnownBitRate"] == 2

    def test_bit_rate_buckets(self):
        tracks = [mt(1, bit_rate=96), mt(2, bit_rate=128), mt(3, bit_rate=256), mt(4, bit_rate=320), mt(5, bit_rate=320)]

        buckets = {b["bitRate"]: b["tracks"] for b in la.audit_formats(tracks).sections["by_lossy_bit_rate"]}

        assert buckets == {"under 128 kbps": 1, "128 to 191 kbps": 1, "256 to 319 kbps": 1, "320 kbps": 2}

    def test_the_largest_files_first(self):
        tracks = [mt(1, size=5_000_000), mt(2, size=90_000_000), mt(3, size=None)]

        assert [i["trackId"] for i in la.audit_formats(tracks).sections["largest_files"]] == [2, 1]


class TestQuestions:
    def test_recently_added_newest_first(self):
        tracks = [
            mt(1, date_added=datetime(2026, 11, 1)), mt(2, date_added=datetime(2026, 11, 15)),
            mt(3, date_added=datetime(2026, 9, 1)), mt(4, date_added=None),
        ]

        report = la.audit_questions(tracks, recent_days=30, now=NOW)

        assert [i["trackId"] for i in report.sections["recently_added"]] == [2, 1]
        assert report.facts["addedRecently"] == 2 and report.facts["oldestAdded"] == "2026-09-01"

    def test_tracks_per_genre_and_year(self):
        tracks = [mt(1, genre="House", year=2020), mt(2, genre="House", year=2021), mt(3, genre=None, year=2021), mt(4, year=None)]

        report = la.audit_questions(tracks, now=NOW)

        assert report.sections["by_genre"][0] == {"genre": "House", "tracks": 3}
        assert {"genre": "(no genre)", "tracks": 1} in report.sections["by_genre"]
        assert report.sections["by_year"] == [{"year": 2021, "tracks": 2}, {"year": 2020, "tracks": 1}]
        assert report.facts["tracksWithNoYear"] == 1

    def test_size_and_hours(self):
        tracks = [mt(1, duration_ms=3_600_000, size=2_000_000_000)]

        assert (la.audit_questions(tracks, now=NOW).facts["hours"], la.audit_questions(tracks, now=NOW).facts["gigabytes"]) == (1.0, 2.0)


class TestMissingFiles:
    def test_the_files_not_where_apple_music_says(self):
        tracks = [mt(1, path="/Music/here.mp3"), mt(2, path="/Music/gone.mp3"), mt(3, path=None)]

        report = la.audit_missing_files(tracks, exists=lambda p: p.endswith("here.mp3"))

        assert [i["trackId"] for i in report.sections["missing"]] == [2]
        assert report.facts == {"tracksAudited": 3, "withALocalFile": 2, "withNoLocalFile": 1, "missing": 1}

    def test_almost_everything_missing_suggests_an_unmounted_drive(self):
        tracks = [mt(i) for i in range(1, 6)]

        assert "mounted" in la.audit_missing_files(tracks, exists=lambda p: False).notes[0]
        assert la.audit_missing_files(tracks, exists=lambda p: True).notes == []


class TestTool:
    """audit_music_library over a small exported library."""

    @pytest.fixture(autouse=True)
    def clean(self):
        from cuebridge import mcp_server

        mcp_server.reset_state()
        yield
        mcp_server.reset_state()

    @pytest.fixture
    def loaded(self, tmp_path):
        from apple_helpers import music_track, playlist, write_library

        from cuebridge import mcp_server

        here = tmp_path / "here.mp3"
        here.write_bytes(b"x")
        tracks = [
            music_track(1, "Hello", "Adele", here, Size=5_000_000, BPM=120, Genre="Pop", **{"Bit Rate": 128}),
            music_track(2, "hello", "adele", tmp_path / "gone.mp3", Size=6_000_000),
            music_track(3, "Orphan", "Nobody", tmp_path / "orphan.mp3"),  # in no playlist
        ]
        path = write_library(tmp_path / "Library.xml", tracks, [playlist("Pop", 1, 2), playlist("Empty")])
        mcp_server.load_apple_music_library(str(path))

        return tmp_path

    def test_the_scope_is_playlisted_tracks_for_most_reports(self, loaded):
        from cuebridge import mcp_server

        out = mcp_server.audit_music_library("duplicates")

        assert out["scope"] == "only playlisted tracks" and out["tracksLeftOutForBeingInNoPlaylist"] == 1
        assert out["sameTitleAndArtistGroups"] == 1 and out["items"][0]["count"] == 2

    def test_the_playlist_report_covers_the_whole_library(self, loaded):
        from cuebridge import mcp_server

        out = mcp_server.audit_music_library("playlists", show="not_in_any_playlist")

        assert out["scope"] == "the whole library" and [i["title"] for i in out["items"]] == ["Orphan"]
        assert out["emptyPlaylists"] == 1 and out["sections"]["empty_playlists"] == 1

    def test_playlisted_only_can_be_overridden(self, loaded):
        from cuebridge import mcp_server

        assert mcp_server.audit_music_library("metadata_gaps", playlisted_only=False)["tracksAudited"] == 3
        assert mcp_server.audit_music_library("metadata_gaps")["tracksAudited"] == 2

    def test_missing_files_checks_the_disk(self, loaded):
        from cuebridge import mcp_server

        out = mcp_server.audit_music_library("missing_files")

        assert [i["title"] for i in out["items"]] == ["hello"] and out["missing"] == 1

    def test_show_picks_a_section_and_results_are_paged(self, loaded):
        from cuebridge import mcp_server

        out = mcp_server.audit_music_library("formats", show="largest_files", limit=1)

        assert (out["shown"], out["total"], out["returned"], out["hasMore"]) == ("largest_files", 2, 1, True)
        assert mcp_server.audit_music_library("formats", show="largest_files", limit=1, offset=1)["hasMore"] is False

    def test_every_report_runs(self, loaded):
        from cuebridge import mcp_server

        for report in mcp_server.AUDIT_REPORTS:
            out = mcp_server.audit_music_library(report)

            assert out["report"] == report and "Nothing was changed" in out["summary"]

    def test_a_bad_report_or_section_is_refused(self, loaded):
        from mcp.server.mcpserver.exceptions import ToolError

        from cuebridge import mcp_server

        with pytest.raises(ToolError, match="report must be one of"):
            mcp_server.audit_music_library("nope")

        with pytest.raises(ToolError, match="show must be one of"):
            mcp_server.audit_music_library("duplicates", show="nope")

    def test_it_needs_a_loaded_library(self):
        from mcp.server.mcpserver.exceptions import ToolError

        from cuebridge import mcp_server

        with pytest.raises(ToolError, match="load_apple_music_library"):
            mcp_server.audit_music_library("duplicates")
