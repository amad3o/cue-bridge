import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from cuebridge import mcp_server as server
from cuebridge.services.rekordbox_service import clear_cache

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
            "set_default_cue",
            "duplicate_memory_cues_to_hot_cues",
        }

    def test_only_the_writing_tools_are_marked_as_writing(self):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}

        for name in ("set_default_cue", "duplicate_memory_cues_to_hot_cues"):
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

    def test_a_dry_run_after_the_first_edit_needs_no_backup(self, loaded):
        server.set_default_cue(dry_run=False)

        assert server.duplicate_memory_cues_to_hot_cues()["backupFirst"] is False

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

        with pytest.raises(ToolError, match="wasn't written by CueBridge"):
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
        assert first["backup"] is not None and second["backup"] is None

        summary = server.load_rekordbox_library(loaded)

        # 6 original cues + 3 default cues + 4 duplicated hot cues.
        assert summary["cueCount"] == 13
        assert summary["tracksWithoutCues"] == 0
        assert summary["cuesByType"]["hotcue"] == 8

    def test_the_backup_is_the_untouched_export(self, loaded):
        original = Path(loaded).read_bytes()

        server.set_default_cue(dry_run=False)
        server.duplicate_memory_cues_to_hot_cues(dry_run=False)

        (backup,) = backups_of(loaded)
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
