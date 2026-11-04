import plistlib

import pytest

from apple_helpers import library_bytes, music_track, playlist, write_library
from cuebridge.models import Track
from cuebridge.parsers.apple_music_xml import AppleMusicParseError, parse_apple_music_plist
from cuebridge.services import apple_music_service as service
from cuebridge.services import library_reports as reports
from cuebridge.services.rekordbox_service import InvalidFileError
from cuebridge.services.track_matcher import AppleMusicIndex, normalize_path, normalize_text


def rb(track_id="1", title="Song", artist="Artist", path="/x/song.mp3", cues=(), **extra):
    return Track(trackId=track_id, trackTitle=title, artist=artist, filePath=path, cues=list(cues), **extra)


class TestParser:
    def test_tracks_and_fields(self):
        lib = parse_apple_music_plist(library_bytes([
            music_track(1, "Song", "Artist", "/Music/My Song.mp3", BPM=128, Year=2020, Genre="house", Album="LP"),
        ]))

        (track,) = lib.tracks
        assert (track.title, track.artist, track.album, track.genre, track.bpm, track.year) == ("Song", "Artist", "LP", "house", 128, 2020)
        assert track.path == "/Music/My Song.mp3" and track.duration_ms == 200_000
        assert lib.tracks_by_id[1] is track and lib.music_folder == "/Music/"

    def test_paths_with_special_characters(self):
        lib = parse_apple_music_plist(library_bytes([music_track(1, "A", path="/Music/Shaka & Co/1-01 FYA dub (v2) #1.m4a")]))

        assert lib.tracks[0].path == "/Music/Shaka & Co/1-01 FYA dub (v2) #1.m4a"

    def test_blank_and_missing_fields_are_none(self):
        lib = parse_apple_music_plist(library_bytes([music_track(1, "  ", artist="")]))

        assert lib.tracks[0].title is None and lib.tracks[0].artist is None and lib.tracks[0].path is None

    def test_a_track_with_no_local_file_has_no_path(self):
        raw = music_track(1, "Streamed", path="/Music/a.mp3")
        raw["Track Type"] = "Remote"

        assert parse_apple_music_plist(library_bytes([raw])).tracks[0].path is None

    def test_playlists_keep_their_order_and_kind(self):
        lib = parse_apple_music_plist(library_bytes(
            [music_track(i, f"T{i}") for i in (1, 2, 3)], [playlist("Set", 3, 1), playlist("Empty")]
        ))

        assert [p.kind for p in lib.playlists] == ["library", "system", "user", "user"]
        assert [p.name for p in lib.user_playlists] == ["Set", "Empty"]
        assert lib.user_playlists[0].track_ids == (3, 1)

    def test_a_folder_is_marked(self):
        lib = parse_apple_music_plist(library_bytes([], [playlist("Genres", Folder=True)]))

        assert lib.user_playlists[0].is_folder

    def test_a_plist_that_is_not_a_library_is_rejected(self):
        with pytest.raises(AppleMusicParseError):
            parse_apple_music_plist(plistlib.dumps({"hello": 1}))


class TestService:
    def test_loads_a_file(self, tmp_path):
        path = write_library(tmp_path / "Library.xml", [music_track(1, "A", path="/m/a.mp3")])

        assert len(service.get_apple_music_library(path).tracks) == 1

    @pytest.mark.parametrize("name,content,message", [
        ("Library.txt", b"", "xml"),
        ("missing.xml", None, "not found"),
        ("empty.xml", b"  ", "empty"),
        ("junk.xml", b"<not a plist", "valid property list"),
        ("other.xml", plistlib.dumps({"a": 1}), "Apple Music"),
    ])
    def test_bad_files_give_a_clear_error(self, tmp_path, name, content, message):
        path = tmp_path / name

        if content is not None:
            path.write_bytes(content)

        with pytest.raises(InvalidFileError, match=message):
            service.get_apple_music_library(path)

    def test_the_cache_is_reused_until_the_file_changes(self, tmp_path):
        service.clear_cache()
        path = write_library(tmp_path / "Library.xml", [music_track(1, "A")])

        first = service.get_apple_music_library(path)

        assert service.get_apple_music_library(path) is first

        write_library(path, [music_track(1, "A"), music_track(2, "B")])

        assert len(service.get_apple_music_library(path).tracks) == 2


class TestNormalising:
    def test_text(self):
        assert normalize_text("  Jåzz & BLUES!! ") == "jazz and blues"
        assert normalize_text("Café") == normalize_text("Café")
        assert normalize_text(None) == ""

    def test_paths_ignore_case_and_unicode_form(self):
        assert normalize_path("/Music/Café.mp3") == normalize_path("/music/Café.MP3")


def index(*tracks):
    return AppleMusicIndex(parse_apple_music_plist(library_bytes(list(tracks))))


class TestMatching:
    def test_by_path_first(self):
        found = index(music_track(1, "Other", path="/x/song.mp3"), music_track(2, "Song", "Artist", "/y/song.mp3")).match(rb())

        assert (found.method, found.track.track_id) == ("path", 1)

    def test_a_moved_file_is_found_by_name(self):
        found = index(music_track(1, "Whatever", path="/new/place/song.mp3")).match(rb(title="Different"))

        assert (found.method, found.track.track_id) == ("file_name", 1)

    def test_by_title_and_artist_when_the_file_was_renamed(self):
        found = index(music_track(1, "Song", "ARTIST", "/new/renamed.mp3")).match(rb())

        assert (found.method, found.track.track_id) == ("title_artist", 1)

    def test_a_blank_artist_is_compatible(self):
        assert index(music_track(1, "Song", None, "/n/a.mp3")).match(rb()).track.track_id == 1
        assert index(music_track(1, "Song", "Artist", "/n/a.mp3")).match(rb(artist=None)).track.track_id == 1

    def test_a_different_artist_is_not(self):
        assert index(music_track(1, "Song", "Someone Else", "/n/a.mp3")).match(rb()).method == "none"

    def test_two_candidates_are_ambiguous_not_guessed(self):
        found = index(music_track(1, "Song", "Artist", "/a/1.mp3"), music_track(2, "Song", "Artist", "/b/2.mp3")).match(rb())

        assert found.method == "ambiguous" and found.track is None and len(found.candidates) == 2

    def test_duration_breaks_a_tie(self):
        found = index(
            music_track(1, "Song", "Artist", "/a/1.mp3", ms=180_000),
            music_track(2, "Song", "Artist", "/b/2.mp3", ms=240_000),
        ).match(rb(durationMs=240_000))

        assert (found.method, found.track.track_id) == ("title_artist", 2)

    def test_nothing_matches(self):
        assert index(music_track(1, "Unrelated", "X", "/a/z.mp3")).match(rb()).method == "none"

    def test_a_track_without_a_title_or_path_matches_nothing(self):
        assert index(music_track(1, "Song", "Artist", "/a/z.mp3")).match(rb(title=None, path=None)).method == "none"


class TestMissingFiles:
    def setup_method(self):
        self.on_disk = {"/real/song.mp3", "/x/there.mp3"}
        self.exists = lambda p: p in self.on_disk
        self.music = parse_apple_music_plist(library_bytes([music_track(1, "Song", "Artist", "/real/song.mp3")]))
        self.cue = [__import__("serato_helpers").rb_cue()]

    def find(self, tracks, **kwargs):
        return reports.find_missing_rekordbox_files(tracks, self.music, exists=self.exists, **kwargs)

    def test_a_moved_file_is_found(self):
        (missing,) = self.find([rb(path="/x/old name.mp3", cues=self.cue)])

        assert missing.suggested_path == "/real/song.mp3" and missing.match.method == "title_artist"
        assert reports.moved_file_paths([missing]) == {"1": "/real/song.mp3"}

    def test_a_file_that_is_there_is_not_reported(self):
        assert self.find([rb(path="/x/there.mp3", cues=self.cue)]) == []

    def test_a_streaming_track_is_not_a_lost_file(self):
        assert self.find([rb(path="tidal:tracks:368603871", cues=self.cue)]) == []

    def test_tracks_without_cues_are_skipped_by_default(self):
        assert self.find([rb()]) == []
        assert len(self.find([rb()], only_with_cues=False)) == 1

    def test_no_suggestion_when_apple_music_s_file_is_missing_too(self):
        self.on_disk.discard("/real/song.mp3")

        (missing,) = self.find([rb(path="/x/old name.mp3", cues=self.cue)])

        assert missing.suggested_path is None and reports.moved_file_paths([missing]) == {}

    def test_no_suggestion_for_an_unmatched_track(self):
        (missing,) = self.find([rb(title="Nope", path="/x/old name.mp3", cues=self.cue)])

        assert missing.suggested_path is None and missing.match.method == "none"


class TestSummary:
    def test_the_summary_counts_things(self):
        lib = parse_apple_music_plist(library_bytes(
            [music_track(1, "A", "X", "/m/a.mp3", BPM=120, Size=1_000_000_000), music_track(2, "B", None, "/m/b.mp3")],
            [playlist("Set", 1, 2)],
        ))

        summary = reports.music_library_summary(lib)

        assert summary["trackCount"] == 2 and summary["tracksWithoutArtist"] == 1 and summary["tracksWithoutBpm"] == 1
        assert summary["playlists"] == [{"name": "Set", "tracks": 2, "isFolder": False}]
        assert summary["totalGigabytes"] == 1.0 and summary["newestAdded"] == "2025-01-02"
