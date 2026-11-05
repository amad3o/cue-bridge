import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from cuebridge import mcp_server as server
from cuebridge.parsers.serato_markers import CueEntry
from cuebridge.parsers.serato_tags import read_cues
from cuebridge.services import serato_sync
from cuebridge.services.rekordbox_service import clear_cache
from apple_helpers import music_track, playlist, write_library
from serato_helpers import AUDIO_INFO_NO_LAME, AUDIO_WITH_LAME, BPMLOCK, COLOR, make_mp3, make_wav

FIXTURE = Path(__file__).parent / "fixtures" / "rekordbox_sample.xml"


@pytest.fixture(autouse=True)
def clean_state():
    clear_cache()
    server.reset_state()
    yield
    clear_cache()
    server.reset_state()


@pytest.fixture
def library_path(tmp_path) -> str:
    path = tmp_path / "rekordbox.xml"
    path.write_bytes(FIXTURE.read_bytes())
    return str(path)


@pytest.fixture
def loaded(library_path) -> str:
    server.load_rekordbox_library(library_path)
    return library_path


def as_json(result) -> dict:
    return json.loads(result.content[0].text)


class TestRegistration:
    def test_tools_are_registered(self):
        tools = asyncio.run(server.mcp.list_tools())

        assert {t.name for t in tools} == {
            "load_rekordbox_library",
            "search_tracks",
            "get_track_cues",
            "find_tracks_without_cues",
            "load_apple_music_library",
            "get_playlist_tracks",
            "find_missing_files",
            "compare_libraries",
            "list_library_differences",
            "export_playlists_for_rekordbox",
            "sync_playlists_to_rekordbox",
            "undo_rekordbox_playlist_sync",
            "fix_moved_files_in_rekordbox",
            "undo_rekordbox_file_fix",
            "set_default_cue",
            "duplicate_memory_cues_to_hot_cues",
            "sync_cues_to_serato",
            "undo_serato_sync",
        }

    def test_only_the_writing_tools_are_marked_as_writing(self):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}

        for name in ("set_default_cue", "duplicate_memory_cues_to_hot_cues", "sync_cues_to_serato", "undo_serato_sync"):
            assert tools[name].annotations.read_only_hint is False
            # They edit the loaded file in place (after a backup), so clients should confirm.
            assert tools[name].annotations.destructive_hint is True

        for name in ("load_rekordbox_library", "search_tracks", "get_track_cues", "find_tracks_without_cues"):
            assert tools[name].annotations.read_only_hint is True


class TestOverTheProtocol:
    """Call the server the way a real MCP client does."""

    def test_load_and_search(self, library_path):
        async def run():
            async with Client(server.mcp) as client:
                loaded = await client.call_tool("load_rekordbox_library", {"path": library_path})
                found = await client.call_tool("search_tracks", {"query": "midnight"})
                return loaded, found

        loaded, found = asyncio.run(run())

        assert not loaded.is_error
        assert as_json(loaded)["trackCount"] == 6
        assert as_json(found)["tracks"][0]["trackId"] == "2001"

    def test_tool_errors_are_reported_not_raised(self):
        async def run():
            async with Client(server.mcp) as client:
                return await client.call_tool("search_tracks", {})

        result = asyncio.run(run())

        assert result.is_error
        assert "load_rekordbox_library" in json.dumps([c.model_dump() for c in result.content])


class TestLoad:
    def test_summary(self, library_path):
        result = server.load_rekordbox_library(library_path)

        assert result["trackCount"] == 6
        assert result["tracksWithoutCues"] == 3
        assert "Loaded 6 tracks" in result["summary"]

    def test_bad_path(self, tmp_path):
        with pytest.raises(ToolError, match="not found"):
            server.load_rekordbox_library(str(tmp_path / "nope.xml"))

    def test_failed_load_keeps_previous_library(self, loaded, tmp_path):
        with pytest.raises(ToolError):
            server.load_rekordbox_library(str(tmp_path / "nope.xml"))

        assert server.search_tracks()["total"] == 6


class TestReadTools:
    @pytest.mark.parametrize(
        "call",
        [
            lambda: server.search_tracks(),
            lambda: server.get_track_cues("2001"),
            lambda: server.find_tracks_without_cues(),
            lambda: server.set_default_cue(),
            lambda: server.duplicate_memory_cues_to_hot_cues(),
            lambda: server.sync_cues_to_serato(),
        ],
    )
    def test_require_a_loaded_library(self, call):
        with pytest.raises(ToolError, match="load_rekordbox_library"):
            call()

    def test_search_tracks(self, loaded):
        result = server.search_tracks(query="same", limit=1)

        assert (result["total"], result["returned"], result["hasMore"]) == (2, 1, True)

    def test_get_track_cues(self, loaded):
        track = server.get_track_cues("2001")

        assert track["title"] == "Midnight Run"
        assert len(track["cues"]) == 4

    def test_unknown_track_id(self, loaded):
        with pytest.raises(ToolError, match="search_tracks"):
            server.get_track_cues("nope")

    def test_find_tracks_without_cues(self, loaded):
        result = server.find_tracks_without_cues()

        assert [t["trackId"] for t in result["tracks"]] == ["1001", "4001", "5001"]

    def test_reexported_file_is_picked_up(self, loaded):
        Path(loaded).write_bytes(
            b'<DJ_PLAYLISTS><COLLECTION><TRACK TrackID="1" Name="Only"/></COLLECTION></DJ_PLAYLISTS>'
        )

        assert server.search_tracks()["total"] == 1


def backups_of(library_path: str) -> list[Path]:
    path = Path(library_path)
    return sorted(path.parent.glob(f"{path.name}.backup-*"))


class TestSetDefaultCue:
    def test_dry_run_is_the_default_and_writes_nothing(self, loaded):
        original = Path(loaded).read_bytes()

        result = server.set_default_cue()

        assert result["dryRun"] is True
        assert result["mode"] == "edits the loaded file in place"
        assert result["backupFirst"] is True
        assert result["tracksToChange"] == 3
        assert result["tracksSkippedBecauseTheyHaveCues"] == 3
        assert "in place" in result["summary"] and "Nothing has been written" in result["summary"]
        assert Path(result["file"]) == Path(loaded).resolve()
        assert [e["trackId"] for e in result["examples"]] == ["1001", "4001", "5001"]
        assert Path(loaded).read_bytes() == original and backups_of(loaded) == []

    def test_edits_the_file_in_place_and_saves_a_backup(self, loaded):
        original = Path(loaded).read_bytes()

        result = server.set_default_cue(dry_run=False)

        assert result["dryRun"] is False
        assert Path(result["backup"]).read_bytes() == original
        assert Path(loaded).read_bytes() != original
        assert "backed up" in result["summary"]

    def test_the_loaded_library_sees_the_edit_straight_away(self, loaded):
        assert server.find_tracks_without_cues()["total"] == 3

        server.set_default_cue(dry_run=False)

        assert server.find_tracks_without_cues()["total"] == 0

    def test_running_again_changes_nothing_and_makes_no_new_backup(self, loaded):
        server.set_default_cue(dry_run=False)

        result = server.set_default_cue(dry_run=False)

        assert result["tracksToChange"] == 0
        assert "Nothing to do" in result["summary"]
        assert len(backups_of(loaded)) == 1

    def test_every_edit_in_place_backs_up_first(self, loaded):
        server.set_default_cue(dry_run=False)

        assert server.duplicate_memory_cues_to_hot_cues()["backupFirst"] is True

    def test_output_path_saves_elsewhere_and_leaves_the_loaded_file(self, loaded, tmp_path):
        original = Path(loaded).read_bytes()
        target = tmp_path / "mine.xml"

        result = server.set_default_cue(output_path=str(target), dry_run=False)

        assert result["mode"] == "saves an edited copy elsewhere"
        assert Path(result["file"]) == target.resolve() and target.exists()
        assert result["backup"] is None
        assert Path(loaded).read_bytes() == original and backups_of(loaded) == []

    def test_refuses_to_overwrite_a_file_it_did_not_write(self, loaded, tmp_path):
        existing = tmp_path / "existing.xml"
        existing.write_bytes(b"precious")

        with pytest.raises(ToolError, match="already exists"):
            server.set_default_cue(output_path=str(existing), dry_run=False)

        assert existing.read_bytes() == b"precious"

    def test_a_bad_output_path_is_reported_in_a_dry_run_too(self, loaded):
        # The dry run must not promise a write that would fail.
        with pytest.raises(ToolError, match="does not exist"):
            server.set_default_cue(output_path="/nonexistent-folder/x.xml")

    def test_nothing_to_do(self, tmp_path):
        path = tmp_path / "rb.xml"
        path.write_bytes(
            b'<DJ_PLAYLISTS><COLLECTION><TRACK TrackID="1" Name="A">'
            b'<POSITION_MARK Type="0" Start="1" Num="0"/></TRACK></COLLECTION></DJ_PLAYLISTS>'
        )
        before = path.read_bytes()
        server.load_rekordbox_library(str(path))

        result = server.set_default_cue(dry_run=False)

        assert result["tracksToChange"] == 0
        assert "Nothing to do" in result["summary"]
        assert path.read_bytes() == before and backups_of(str(path)) == []


class TestDuplicateMemoryCues:
    def test_dry_run_is_the_default_and_writes_nothing(self, loaded):
        original = Path(loaded).read_bytes()

        result = server.duplicate_memory_cues_to_hot_cues()

        assert result["dryRun"] is True
        assert result["hotCuesToAdd"] == 4
        assert result["tracksToChange"] == 3
        assert "in place" in result["summary"] and "Nothing has been written" in result["summary"]
        assert Path(loaded).read_bytes() == original and backups_of(loaded) == []

    def test_edits_the_file_in_place(self, loaded):
        original = Path(loaded).read_bytes()

        result = server.duplicate_memory_cues_to_hot_cues(dry_run=False)

        assert Path(result["backup"]).read_bytes() == original

        # Track 2001 had a hot cue A, two memory cues and a loop on C.
        cues = server.get_track_cues("2001")["cues"]
        hot = sorted((c["slot"], c["time"]) for c in cues if c["type"] == "hotcue")

        assert hot == [(1, "0:32.500"), (2, "0:00.000"), (4, "1:07.043")]

    def test_running_again_changes_nothing(self, loaded):
        server.duplicate_memory_cues_to_hot_cues(dry_run=False)

        result = server.duplicate_memory_cues_to_hot_cues()

        assert result["tracksToChange"] == 0
        assert result["memoryCuesAlreadyHavingAHotCue"] == 4

    def test_reports_memory_cues_that_do_not_fit(self, tmp_path):
        marks = "".join(f'<POSITION_MARK Type="0" Start="{i + 1}" Num="-1"/>' for i in range(10))
        path = tmp_path / "rb.xml"
        path.write_bytes(
            f'<DJ_PLAYLISTS><COLLECTION><TRACK TrackID="1" Name="Busy">{marks}</TRACK>'
            "</COLLECTION></DJ_PLAYLISTS>".encode()
        )
        server.load_rekordbox_library(str(path))

        result = server.duplicate_memory_cues_to_hot_cues()

        assert result["hotCuesToAdd"] == 8
        assert result["memoryCuesWithNoFreeSlot"] == 2
        assert result["tracksWithNoFreeSlot"][0]["memoryCuesSkipped"] == 2

    def test_nothing_to_do(self, tmp_path):
        path = tmp_path / "rb.xml"
        path.write_bytes(b'<DJ_PLAYLISTS><COLLECTION><TRACK TrackID="1" Name="A"/></COLLECTION></DJ_PLAYLISTS>')
        before = path.read_bytes()
        server.load_rekordbox_library(str(path))

        result = server.duplicate_memory_cues_to_hot_cues(dry_run=False)

        assert result["tracksToChange"] == 0 and "Nothing to do" in result["summary"]
        assert path.read_bytes() == before


class TestBothToolsEditTheSameFile:
    def test_edits_from_both_tools_end_up_in_the_loaded_file(self, loaded):
        first = server.set_default_cue(dry_run=False)
        second = server.duplicate_memory_cues_to_hot_cues(dry_run=False)

        assert first["file"] == second["file"] == str(Path(loaded).resolve())
        assert first["backup"] is not None and second["backup"] is not None

        summary = server.load_rekordbox_library(loaded)

        # 6 original cues + 3 default cues + 4 duplicated hot cues.
        assert summary["cueCount"] == 13
        assert summary["tracksWithoutCues"] == 0
        assert summary["cuesByType"]["hotcue"] == 8

    def test_the_backup_is_the_untouched_export(self, loaded):
        original = Path(loaded).read_bytes()

        server.set_default_cue(dry_run=False)
        server.duplicate_memory_cues_to_hot_cues(dry_run=False)

        backup, _ = backups_of(loaded)  # the first one is from before any edit
        assert backup.read_bytes() == original

    def test_every_call_plans_from_the_current_file(self, loaded):
        # A re-export between two calls is simply picked up. The "changed since
        # planned" refusal covers the short gap inside one call (see the editor tests).
        server.set_default_cue()  # dry run
        Path(loaded).write_bytes(Path(loaded).read_bytes().replace(b"Midnight Run", b"Midnight Run2"))
        os.utime(loaded, (1, 1))

        result = server.set_default_cue(dry_run=False)

        assert result["dryRun"] is False
        assert "Midnight Run2" in Path(loaded).read_text(encoding="utf-8")


class TestSyncToSerato:
    """The tools that edit the audio files. Real tags on throwaway files; Serato is never 'running'."""

    @pytest.fixture(autouse=True)
    def safe_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CUEBRIDGE_HOME", str(tmp_path / "cuebridge-home"))
        monkeypatch.setattr(serato_sync, "is_serato_running", lambda: False)

    @pytest.fixture
    def library(self, tmp_path):
        """A Rekordbox library of two tracks, each with a real Serato-tagged file, and one with no file."""
        wav = make_wav(tmp_path / "one.wav", [COLOR, CueEntry(0, 500, "#cc0000"), BPMLOCK])
        mp3 = make_mp3(tmp_path / "two.mp3", [COLOR, BPMLOCK], audio=AUDIO_WITH_LAME)
        xml = tmp_path / "rekordbox.xml"
        xml.write_text(
            "<DJ_PLAYLISTS><COLLECTION>"
            f'<TRACK TrackID="1" Name="One" Artist="A" Location="file://localhost{wav}">'
            '<POSITION_MARK Name="Drop" Type="0" Start="12.345" Num="0" Red="40" Green="226" Blue="20"/>'
            '<POSITION_MARK Type="0" Start="30.5" Num="-1"/></TRACK>'
            f'<TRACK TrackID="2" Name="Two" Artist="B" Location="file://localhost{mp3}">'
            '<POSITION_MARK Type="0" Start="1.0" Num="0"/></TRACK>'
            '<TRACK TrackID="3" Name="Three" Artist="C" Location="file://localhost/nowhere/three.mp3">'
            '<POSITION_MARK Type="0" Start="2.0" Num="0"/></TRACK>'
            "</COLLECTION></DJ_PLAYLISTS>"
        )
        server.load_rekordbox_library(str(xml))
        return {"wav": wav, "mp3": mp3, "xml": xml}

    def test_a_dry_run_is_the_default_and_changes_nothing(self, library):
        before = {k: v.read_bytes() for k, v in library.items()}

        result = server.sync_cues_to_serato()

        assert result["dryRun"] is True
        assert result["filesToChange"] == 2 and result["filesSkipped"] == 1
        assert result["skippedBecause"] == {"the file wasn't found at the Rekordbox path": 1}
        assert "Nothing has been written" in result["summary"]
        assert {k: v.read_bytes() for k, v in library.items()} == before
        assert not serato_sync.journal_dir().exists()

    def test_the_preview_shows_before_and_after(self, library):
        example = next(e for e in server.sync_cues_to_serato()["examples"] if e["title"] == "One")

        assert example["seratoNow"] == "1: 0:00.500"
        assert example["afterSync"] == "1: 0:12.345, 2: 0:30.500"

    def test_applying_requires_the_file_count_from_the_dry_run(self, library):
        before = library["wav"].read_bytes()

        with pytest.raises(ToolError, match="expected_files"):
            server.sync_cues_to_serato(dry_run=False)

        with pytest.raises(ToolError, match="would change 2 files, but expected_files was 5"):
            server.sync_cues_to_serato(dry_run=False, expected_files=5)

        assert library["wav"].read_bytes() == before

    def test_applying_writes_verifies_and_reports_the_journal(self, library):
        result = server.sync_cues_to_serato(dry_run=False, expected_files=2)

        assert result["filesWritten"] == 2 and result["filesThatFailedAndWereRestored"] == []
        assert Path(result["journal"]).exists() and "undo_serato_sync" in result["summary"]
        assert [(c.index, c.position_ms) for c in read_cues(library["wav"])] == [(0, 12345), (1, 30500)]
        assert [(c.index, c.position_ms) for c in read_cues(library["mp3"])] == [(0, 995)]

    def test_running_it_again_finds_nothing_to_do(self, library):
        server.sync_cues_to_serato(dry_run=False, expected_files=2)

        again = server.sync_cues_to_serato()

        assert again["filesToChange"] == 0 and again["filesAlreadyUpToDate"] == 2
        assert "No Serato file needs to change" in again["summary"]

    def test_serato_running_is_reported_and_blocks_the_write(self, library, monkeypatch):
        monkeypatch.setattr(serato_sync, "is_serato_running", lambda: True)
        before = library["wav"].read_bytes()

        preview = server.sync_cues_to_serato()
        assert preview["seratoIsRunning"] is True and "quit it" in preview["summary"]

        with pytest.raises(ToolError, match="Serato DJ Pro is running"):
            server.sync_cues_to_serato(dry_run=False, expected_files=2)

        assert library["wav"].read_bytes() == before

    def test_it_can_be_limited_to_some_tracks(self, library):
        result = server.sync_cues_to_serato(track_ids=["2"])

        assert result["filesToChange"] == 1 and result["filesSkipped"] == 0

    def test_memory_cues_can_be_excluded(self, library):
        result = server.sync_cues_to_serato(include_memory_cues=False, track_ids=["1"])

        assert result["examples"][0]["afterSync"] == "1: 0:12.345"

    def test_unverified_timing_is_reported(self, library, tmp_path):
        odd = make_mp3(tmp_path / "odd.mp3", [COLOR, BPMLOCK], audio=AUDIO_INFO_NO_LAME)
        library["xml"].write_text(
            "<DJ_PLAYLISTS><COLLECTION>"
            f'<TRACK TrackID="9" Name="Odd" Artist="X" Location="file://localhost{odd}">'
            '<POSITION_MARK Type="0" Start="1.0" Num="0"/></TRACK></COLLECTION></DJ_PLAYLISTS>'
        )
        os.utime(library["xml"], (1, 1))

        result = server.sync_cues_to_serato()

        assert result["timingNotVerified"][0]["title"] == "Odd"
        assert "26 ms" in result["timingNotVerified"][0]["note"]

    def test_undo_restores_the_files(self, library):
        original = {k: library[k].read_bytes() for k in ("wav", "mp3")}
        synced = server.sync_cues_to_serato(dry_run=False, expected_files=2)

        preview = server.undo_serato_sync()
        assert preview["dryRun"] is True and preview["filesThatWouldBeRestored"] == 2
        assert library["wav"].read_bytes() != original["wav"]

        done = server.undo_serato_sync(journal=synced["journal"], dry_run=False)

        assert done["filesRestored"] == 2 and done["filesLeftAlone"] == []
        assert {k: library[k].read_bytes() for k in ("wav", "mp3")} == original

    def test_undo_with_no_journal(self, library):
        with pytest.raises(ToolError, match="no sync journal"):
            server.undo_serato_sync()

    def test_undo_uses_the_latest_journal_by_default(self, library):
        original = library["wav"].read_bytes()
        server.sync_cues_to_serato(dry_run=False, expected_files=2)

        server.undo_serato_sync(dry_run=False)

        assert library["wav"].read_bytes() == original


class TestAppleMusicTools:
    @pytest.fixture(autouse=True)
    def safe_environment(self, tmp_path, monkeypatch):
        from cuebridge.services import apple_music_service

        apple_music_service.clear_cache()
        monkeypatch.setenv("CUEBRIDGE_HOME", str(tmp_path / "cuebridge-home"))
        monkeypatch.setattr(serato_sync, "is_serato_running", lambda: False)

    @pytest.fixture
    def libraries(self, tmp_path):
        """Rekordbox thinks "Three" is in /nowhere; Apple Music (rightly) has it as moved.wav."""
        moved = make_wav(tmp_path / "moved.wav", [COLOR, CueEntry(0, 500, "#cc0000"), BPMLOCK])
        xml = tmp_path / "rekordbox.xml"
        xml.write_text(
            "<DJ_PLAYLISTS><COLLECTION>"
            '<TRACK TrackID="3" Name="Three" Artist="C" Location="file://localhost/nowhere/three.wav">'
            '<POSITION_MARK Type="0" Start="2.0" Num="0"/></TRACK>'
            '<TRACK TrackID="4" Name="Four" Artist="D" Location="file://localhost/nowhere/four.wav">'
            '<POSITION_MARK Type="0" Start="3.0" Num="0"/></TRACK>'
            "</COLLECTION></DJ_PLAYLISTS>"
        )
        apple = write_library(
            tmp_path / "Library.xml",
            [music_track(1, "Three", "C", moved), music_track(2, "Other", "Z", tmp_path / "other.wav")],
            [playlist("Warmup", 2, 1), playlist("Empty")],
        )
        server.load_rekordbox_library(str(xml))
        return {"moved": moved, "apple": str(apple)}

    def test_loading_gives_a_summary_with_the_playlists(self, libraries):
        result = server.load_apple_music_library(libraries["apple"])

        assert result["trackCount"] == 2 and result["playlistCount"] == 2
        assert [p["name"] for p in result["playlists"]] == ["Warmup", "Empty"]
        assert "Loaded 2 tracks and 2 playlists" in result["summary"]

    def test_a_bad_path_is_a_tool_error(self, tmp_path):
        with pytest.raises(ToolError, match="not found"):
            server.load_apple_music_library(str(tmp_path / "nope.xml"))

    def test_a_playlist_lists_its_tracks_in_order(self, libraries):
        server.load_apple_music_library(libraries["apple"])

        result = server.get_playlist_tracks("warmup")

        assert [t["title"] for t in result["tracks"]] == ["Other", "Three"]
        assert result["total"] == 2 and result["hasMore"] is False

    def test_playlists_are_paged(self, libraries):
        server.load_apple_music_library(libraries["apple"])

        result = server.get_playlist_tracks("Warmup", limit=1, offset=1)

        assert [t["title"] for t in result["tracks"]] == ["Three"] and result["hasMore"] is False
        assert server.get_playlist_tracks("Warmup", limit=1)["hasMore"] is True

    def test_an_unknown_playlist_lists_the_names(self, libraries):
        server.load_apple_music_library(libraries["apple"])

        with pytest.raises(ToolError, match="Warmup"):
            server.get_playlist_tracks("nope")

    def test_the_tools_need_the_library_loaded_first(self, libraries):
        for call in (lambda: server.get_playlist_tracks("x"), server.find_missing_files):
            with pytest.raises(ToolError, match="load_apple_music_library"):
                call()

    def test_missing_files_are_listed_with_where_they_are(self, libraries):
        server.load_apple_music_library(libraries["apple"])

        result = server.find_missing_files()

        assert result["missingCount"] == 2 and result["foundInAppleMusic"] == 1 and result["notFound"] == 1
        found = next(f for f in result["files"] if f["title"] == "Three")
        assert found["appleMusicPath"] == str(libraries["moved"]) and found["matchedBy"] == "title_artist"
        assert next(f for f in result["files"] if f["title"] == "Four")["appleMusicPath"] is None

    def test_the_sync_uses_apple_music_s_path_for_a_moved_file(self, libraries):
        server.load_apple_music_library(libraries["apple"])

        dry = server.sync_cues_to_serato()

        assert dry["filesToChange"] == 1 and dry["foundViaAppleMusic"] == 1
        assert dry["skippedBecause"] == {"the file wasn't found at the Rekordbox path": 1}
        assert dry["examples"][0]["file"] == str(libraries["moved"])

        done = server.sync_cues_to_serato(dry_run=False, expected_files=1)

        assert done["filesWritten"] == 1
        assert [(c.index, c.position_ms) for c in read_cues(libraries["moved"])] == [(0, 2000)]

    def test_the_sync_can_ignore_apple_music(self, libraries):
        server.load_apple_music_library(libraries["apple"])

        result = server.sync_cues_to_serato(use_apple_music_paths=False)

        assert result["filesToChange"] == 0 and result["foundViaAppleMusic"] == 0

    def test_without_an_apple_music_library_the_sync_is_unchanged(self, libraries):
        assert server.sync_cues_to_serato()["filesToChange"] == 0
