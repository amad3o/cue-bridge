import json
import re
import subprocess

import pytest

from cuebridge import mcp_server as server
from cuebridge.services import apple_music_live as live
from cuebridge.services import apple_music_service


def raw_track(pid, name, artist="X", location="/Volumes/Music/Media/Music/a.mp3", **extra):
    return {
        "persistentID": pid, "name": name, "artist": artist, "album": "LP", "genre": "house", "kind": "MPEG audio file",
        "bpm": 128, "year": 2020, "duration": 200.5, "size": 1234, "bitRate": 320, "sampleRate": 44100,
        "dateAdded": "2025-06-04T01:45:32.000Z", "location": location, **extra,
    }


def raw_playlist(name, ids, special="none", **extra):
    return {"name": name, "persistentID": "PL" + name, "specialKind": special, "smart": False, "class": "userPlaylist", "trackIDs": ids, **extra}


REAL_RUN_SCRIPT = live.run_script

SAMPLE = json.dumps({
    "tracks": [
        raw_track("A1", "One", location="/Volumes/Music/Media/Music/X/one.mp3"),
        raw_track("A2", "Two", artist="", location="/Volumes/Music/Media/Music/Y/two.wav"),
        raw_track("A3", "Gone", location=None),
    ],
    "playlists": [
        raw_playlist("Music", ["A1", "A2", "A3"], special="Music"),
        raw_playlist("Set", ["A2", "A1", "NOPE"]),
        raw_playlist("Genres", [], **{"class": "folderPlaylist"}),
    ],
})


class TestParsing:
    def test_tracks(self):
        library = live.parse_live_json(SAMPLE)

        one, two, gone = library.tracks
        assert [t.track_id for t in library.tracks] == [1, 2, 3] and library.tracks_by_id[2] is two
        assert (one.persistent_id, one.title, one.artist, one.genre, one.bpm, one.year) == ("A1", "One", "X", "house", 128, 2020)
        assert one.duration_ms == 200_500 and one.path == "/Volumes/Music/Media/Music/X/one.mp3"
        assert one.date_added.year == 2025 and one.date_added.tzinfo is None
        assert two.artist is None  # blank means none, as in the XML reader
        assert gone.path is None  # a file Music can't find has no location

    def test_playlists(self):
        library = live.parse_live_json(SAMPLE)

        assert [(p.name, p.kind, p.is_folder) for p in library.playlists] == [
            ("Music", "system", False), ("Set", "user", False), ("Genres", "user", True),
        ]
        assert library.playlists[1].track_ids == (2, 1)  # in playlist order; an unknown ID is dropped
        assert [p.name for p in library.user_playlists] == ["Set", "Genres"]

    def test_the_music_folder_is_the_common_folder(self):
        assert live.parse_live_json(SAMPLE).music_folder == "/Volumes/Music/Media/Music/"

    def test_no_common_folder_when_the_files_share_almost_nothing(self):
        data = json.dumps({"tracks": [raw_track("A", "a", location="/a/x.mp3"), raw_track("B", "b", location="/b/y.mp3")], "playlists": []})

        assert live.parse_live_json(data).music_folder is None

    def test_a_non_library_answer_is_an_error(self):
        for text in ("", "not json", "{}", '{"tracks": 3}'):
            with pytest.raises(live.MusicAppError, match="isn't a library"):
                live.parse_live_json(text)


class TestRunning:
    def test_music_must_already_be_open(self, monkeypatch):
        monkeypatch.setattr(live, "is_music_running", lambda: False)
        called = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))

        with pytest.raises(live.MusicAppError, match="isn't open"):
            live.run_script()

        assert called == []  # nothing, in particular no osascript, was started

    def run_with(self, monkeypatch, **result):
        monkeypatch.setattr(live, "is_music_running", lambda: True)
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, **result))

    def test_the_output_is_returned(self, monkeypatch):
        self.run_with(monkeypatch, returncode=0, stdout="{}", stderr="")

        assert live.run_script() == "{}"

    def test_a_permission_refusal_says_where_to_allow_it(self, monkeypatch):
        self.run_with(monkeypatch, returncode=1, stdout="", stderr="execution error: Not authorized to send Apple events to Music. (-1743)")

        with pytest.raises(live.MusicAppError, match="Automation"):
            live.run_script()

    def test_other_failures_carry_the_message(self, monkeypatch):
        self.run_with(monkeypatch, returncode=1, stdout="", stderr="boom")

        with pytest.raises(live.MusicAppError, match="boom"):
            live.run_script()

    def test_a_timeout_is_explained(self, monkeypatch):
        monkeypatch.setattr(live, "is_music_running", lambda: True)

        def slow(*args, **kwargs):
            raise subprocess.TimeoutExpired("osascript", 1)

        monkeypatch.setattr(subprocess, "run", slow)

        with pytest.raises(live.MusicAppError, match="permission"):
            live.run_script()

    def test_the_script_only_reads(self):
        # No call in it can change Music: only property reads, and none of the verbs that edit or play.
        verbs = re.findall(r"\.(\w+)\(", live.SCRIPT)

        assert set(verbs) <= {
            "persistentID", "name", "artist", "album", "genre", "kind", "bpm", "year", "duration", "size", "bitRate",
            "sampleRate", "dateAdded", "location", "specialKind", "smart", "class", "toISOString", "push", "stringify",
        }
        assert not re.search(r"\.\w+\s*=[^=]", live.SCRIPT)  # no property is assigned


class TestTool:
    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        apple_music_service.clear_cache()
        server.reset_state()
        monkeypatch.setattr(live, "run_script", lambda: SAMPLE)
        yield
        server.reset_state()

    def test_no_path_reads_the_music_app(self):
        result = server.load_apple_music_library()

        assert result["source"] == "the Music app (live)" and result["trackCount"] == 3
        assert [p["name"] for p in result["playlists"]] == ["Set", "Genres"]

    def test_the_other_tools_use_the_live_library(self):
        server.load_apple_music_library()

        result = server.get_playlist_tracks("set")

        assert [t["title"] for t in result["tracks"]] == ["Two", "One"]

    def test_loading_again_picks_up_changes(self, monkeypatch):
        server.load_apple_music_library()
        changed = json.loads(SAMPLE)
        changed["tracks"].append(raw_track("A4", "Four", location="/Volumes/Music/Media/Music/Z/four.mp3"))
        monkeypatch.setattr(live, "run_script", lambda: json.dumps(changed))

        assert server.load_apple_music_library()["trackCount"] == 4

    def test_a_path_switches_back_to_a_file(self, tmp_path):
        from apple_helpers import music_track, write_library

        server.load_apple_music_library()
        xml = write_library(tmp_path / "Library.xml", [music_track(1, "Only", "A", "/m/a.mp3")])

        result = server.load_apple_music_library(str(xml))

        assert result["trackCount"] == 1 and result["source"] == str(xml)

    def test_music_not_open_is_a_tool_error(self, monkeypatch):
        from mcp.server.mcpserver.exceptions import ToolError

        monkeypatch.setattr(live, "run_script", REAL_RUN_SCRIPT)
        monkeypatch.setattr(live, "is_music_running", lambda: False)

        with pytest.raises(ToolError, match="isn't open"):
            server.load_apple_music_library()
