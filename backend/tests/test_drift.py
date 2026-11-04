import pytest
from mcp.server.mcpserver.exceptions import ToolError

from apple_helpers import library_bytes, music_track, playlist, write_library
from cuebridge import mcp_server as server
from cuebridge.parsers.apple_music_xml import parse_apple_music_plist
from cuebridge.parsers.rekordbox_parser import parse_rekordbox_playlists
from cuebridge.services import apple_music_service, drift
from cuebridge.services.rekordbox_service import clear_cache, get_rekordbox_library
from cuebridge.services.track_matcher import AppleMusicIndex
from test_apple_music import rb


def rb_xml(tracks: str, playlists: str) -> bytes:
    return (
        f"<DJ_PLAYLISTS><COLLECTION>{tracks}</COLLECTION>"
        f'<PLAYLISTS><NODE Type="0" Name="ROOT" Count="9">{playlists}</NODE></PLAYLISTS></DJ_PLAYLISTS>'
    ).encode()


def rb_track_xml(track_id, name, artist, path, cue=False):
    mark = '<POSITION_MARK Type="0" Start="1.0" Num="0"/>' if cue else ""
    return f'<TRACK TrackID="{track_id}" Name="{name}" Artist="{artist}" Location="file://localhost{path}">{mark}</TRACK>'


def rb_playlist_xml(name, *ids):
    keys = "".join(f'<TRACK Key="{i}"/>' for i in ids)
    return f'<NODE Type="1" Name="{name}" KeyType="0" Entries="{len(ids)}">{keys}</NODE>'


class TestRekordboxPlaylists:
    def test_playlists_and_their_tracks_in_order(self):
        playlists = parse_rekordbox_playlists(rb_xml("", rb_playlist_xml("Set", 3, 1) + rb_playlist_xml("Empty")))

        assert [(p.name, p.track_ids, p.folders) for p in playlists] == [("Set", ("3", "1"), ()), ("Empty", (), ())]

    def test_folders_are_walked(self):
        nested = f'<NODE Type="0" Name="Genres" Count="1">{rb_playlist_xml("House", 7)}</NODE>'

        (playlist_,) = parse_rekordbox_playlists(rb_xml("", nested))

        assert (playlist_.name, playlist_.folders, playlist_.track_ids) == ("House", ("Genres",), ("7",))

    def test_an_export_without_playlists(self):
        assert parse_rekordbox_playlists(b"<DJ_PLAYLISTS><COLLECTION/></DJ_PLAYLISTS>") == []

    def test_the_library_carries_them(self, tmp_path):
        path = tmp_path / "rekordbox.xml"
        path.write_bytes(rb_xml(rb_track_xml(1, "A", "X", "/a.mp3"), rb_playlist_xml("Set", 1)))
        clear_cache()

        assert [p.name for p in get_rekordbox_library(path).playlists] == ["Set"]


class TestSameFileTwice:
    def test_two_entries_for_one_file_are_a_path_match_not_ambiguity(self):
        music = parse_apple_music_plist(library_bytes([
            music_track(1, "Song", "Artist", "/Music/s.mp3"), music_track(2, "Song", "Artist", "/Music/s.mp3"),
        ]))

        found = AppleMusicIndex(music).match(rb(path="/Music/s.mp3"))

        assert found.method == "path" and found.track.track_id == 1
        assert [t.track_id for t in found.same_file] == [2]

    def test_duplicate_files_are_listed(self):
        music = parse_apple_music_plist(library_bytes([
            music_track(1, "Song", "Artist", "/Music/s.mp3"), music_track(2, "Song", "Artist", "/Music/s.mp3"),
            music_track(3, "Other", "Artist", "/Music/o.mp3"),
        ]))

        (group,) = AppleMusicIndex(music).duplicate_files()

        assert [t.track_id for t in group] == [1, 2]


@pytest.fixture
def libraries(tmp_path):
    """Apple Music and Rekordbox, drifted apart in known ways."""
    music = parse_apple_music_plist(library_bytes(
        [
            music_track(1, "A", "X", "/Music/a.mp3"),
            music_track(2, "B", "X", "/Music/b.mp3"),
            music_track(3, "B", "X", "/Music/b.mp3"),  # the same file listed again
            music_track(4, "D", "X", "/Music/d.mp3"),  # not in Rekordbox
            music_track(5, "E", "X", "/Music/e.mp3"),
            music_track(6, "F", "X", "/Music/f.mp3"),  # in no playlist
        ],
        [playlist("Set", 1, 2), playlist("Warm", 2, 4, 5), playlist("OnlyApple", 1)],
    ))
    xml = rb_xml(
        rb_track_xml(10, "A", "X", "/Music/a.mp3") + rb_track_xml(20, "B", "X", "/Music/b.mp3")
        + rb_track_xml(30, "E", "X", "/Music/e.mp3") + rb_track_xml(40, "Lost", "X", "/Music/lost.mp3")
        + rb_track_xml(50, "Kick", "X", "/Samples/kick.wav"),
        rb_playlist_xml("CUE Analysis Playlist") + rb_playlist_xml("set", 10, 20)
        + rb_playlist_xml("Warm", 20, 30, 40) + rb_playlist_xml("Extra", 10),
    )
    path = tmp_path / "rekordbox.xml"
    path.write_bytes(xml)
    clear_cache()

    return music, get_rekordbox_library(path)


class TestCompare:
    def test_tracks(self, libraries):
        music, rekordbox = libraries

        result = drift.compare_libraries(rekordbox.tracks, rekordbox.playlists, music)

        assert result.matched == 3 and result.ambiguous == []
        assert [t.title for t in result.apple_only] == ["D"]
        assert [t.title for t in result.apple_only_unplaylisted] == ["F"]
        assert [t.trackTitle for t in result.rekordbox_only] == ["Lost", "Kick"]
        assert [t.trackTitle for t in result.rekordbox_only_in_music_folder] == ["Lost"]
        assert [[t.track_id for t in g] for g in result.duplicate_entries] == [[2, 3]]

    def test_playlists(self, libraries):
        music, rekordbox = libraries

        result = drift.compare_libraries(rekordbox.tracks, rekordbox.playlists, music)
        by_name = {p.name: p for p in result.playlists}

        assert by_name["Set"].status == "in_sync"  # names match ignoring case; the duplicate entry doesn't matter
        assert by_name["Warm"].status == "differs"
        assert [t.title for t in by_name["Warm"].apple_only] == ["D"]
        assert [t.trackTitle for t in by_name["Warm"].rekordbox_only] == ["Lost"]
        assert by_name["OnlyApple"].status == "missing_in_rekordbox" and by_name["OnlyApple"].rekordbox_count is None
        assert [p.name for p in result.rekordbox_only_playlists] == ["Extra"]  # the Rekordbox housekeeping list is ignored

    def test_identical_libraries_have_no_drift(self, tmp_path):
        music = parse_apple_music_plist(library_bytes([music_track(1, "A", "X", "/Music/a.mp3")], [playlist("Set", 1)]))
        path = tmp_path / "r.xml"
        path.write_bytes(rb_xml(rb_track_xml(1, "A", "X", "/Music/a.mp3"), rb_playlist_xml("Set", 1)))
        clear_cache()
        rekordbox = get_rekordbox_library(path)

        result = drift.compare_libraries(rekordbox.tracks, rekordbox.playlists, music)

        assert not result.apple_only and not result.rekordbox_only
        assert [p.status for p in result.playlists] == ["in_sync"]


class TestTools:
    @pytest.fixture(autouse=True)
    def clean(self):
        clear_cache()
        apple_music_service.clear_cache()
        server.reset_state()
        yield
        clear_cache()
        apple_music_service.clear_cache()
        server.reset_state()

    @pytest.fixture
    def loaded(self, tmp_path, libraries):
        music, _ = libraries
        apple = write_library(
            tmp_path / "Library.xml",
            [
                music_track(1, "A", "X", "/Music/a.mp3"), music_track(2, "B", "X", "/Music/b.mp3"),
                music_track(3, "B", "X", "/Music/b.mp3"), music_track(4, "D", "X", "/Music/d.mp3"),
                music_track(5, "E", "X", "/Music/e.mp3"),
                music_track(6, "F", "X", "/Music/f.mp3"),
            ],
            [playlist("Set", 1, 2), playlist("Warm", 2, 4, 5), playlist("OnlyApple", 1)],
        )
        server.load_rekordbox_library(str(tmp_path / "rekordbox.xml"))
        server.load_apple_music_library(str(apple))

    def test_the_comparison_summary(self, loaded):
        result = server.compare_libraries()

        assert (result["matched"], result["inAppleMusicOnly"], result["inRekordboxOnly"]) == (3, 1, 2)
        assert result["inAppleMusicOnlyButInNoPlaylist"] == 1 and "ignored" in result["summary"]
        assert result["inRekordboxOnlyButInAppleMusicsFolder"] == 1 and result["appleMusicFilesListedTwice"] == 1
        assert result["playlists"] == {
            "inAppleMusic": 3, "inSync": 1, "differ": 1, "missingInRekordbox": 1, "inRekordboxOnly": ["Extra"],
        }
        assert [p["name"] for p in result["playlistsNeedingAttention"]] == ["Warm", "OnlyApple"]

    def test_listing_the_tracks_on_each_side(self, loaded):
        only_apple = server.list_library_differences("in_apple_music_only")
        only_rb = server.list_library_differences("in_rekordbox_only")
        every_rb = server.list_library_differences("in_rekordbox_only", music_folder_only=False)

        assert [t["title"] for t in only_apple["items"]] == ["D"]
        everything = server.list_library_differences("in_apple_music_only", include_unplaylisted=True)
        assert [t["title"] for t in everything["items"]] == ["D", "F"]
        assert [t["title"] for t in only_rb["items"]] == ["Lost"]
        assert [t["title"] for t in every_rb["items"]] == ["Lost", "Kick"]

    def test_a_playlist_narrows_the_listing(self, loaded):
        assert [t["title"] for t in server.list_library_differences("in_apple_music_only", playlist="warm")["items"]] == ["D"]
        assert [t["title"] for t in server.list_library_differences("in_rekordbox_only", playlist="Warm")["items"]] == ["Lost"]
        assert server.list_library_differences("in_apple_music_only", playlist="Set")["items"] == []

    def test_duplicate_entries(self, loaded):
        (row,) = server.list_library_differences("duplicate_entries")["items"]

        assert row["entries"] == 2 and row["path"] == "/Music/b.mp3"

    def test_paging(self, loaded):
        page = server.list_library_differences("in_rekordbox_only", music_folder_only=False, limit=1)

        assert page["returned"] == 1 and page["hasMore"] is True and page["total"] == 2

    def test_bad_arguments(self, loaded):
        with pytest.raises(ToolError, match="kind"):
            server.list_library_differences("nonsense")

        with pytest.raises(ToolError, match="No Apple Music playlist"):
            server.list_library_differences("in_apple_music_only", playlist="nope")

    def test_both_libraries_are_needed(self, tmp_path, libraries):
        with pytest.raises(ToolError, match="load_rekordbox_library"):
            server.compare_libraries()

        server.load_rekordbox_library(str(tmp_path / "rekordbox.xml"))

        with pytest.raises(ToolError, match="load_apple_music_library"):
            server.compare_libraries()
