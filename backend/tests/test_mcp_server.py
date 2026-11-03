import asyncio
import json
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
        }

    def test_only_set_default_cue_is_marked_as_writing(self):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}

        assert tools["set_default_cue"].annotations.read_only_hint is False
        assert tools["set_default_cue"].annotations.destructive_hint is False

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


class TestSetDefaultCue:
    def test_dry_run_is_the_default_and_writes_nothing(self, loaded):
        result = server.set_default_cue()

        assert result["dryRun"] is True
        assert result["tracksToChange"] == 3
        assert result["tracksSkippedBecauseTheyHaveCues"] == 3
        assert "Nothing has been written" in result["summary"]
        assert not Path(result["outputFile"]).exists()
        assert [e["trackId"] for e in result["examples"]] == ["1001", "4001", "5001"]

    def test_writes_a_copy_and_leaves_the_original(self, loaded):
        original = Path(loaded).read_bytes()

        result = server.set_default_cue(dry_run=False)

        written = Path(result["outputFile"])
        assert result["dryRun"] is False
        assert written.exists() and written != Path(loaded)
        assert Path(loaded).read_bytes() == original

    def test_custom_output_path(self, loaded, tmp_path):
        target = tmp_path / "mine.xml"

        result = server.set_default_cue(output_path=str(target), dry_run=False)

        assert Path(result["outputFile"]) == target and target.exists()

    def test_loaded_library_is_unchanged_after_writing(self, loaded):
        server.set_default_cue(dry_run=False)

        assert server.find_tracks_without_cues()["total"] == 3

    def test_refuses_to_overwrite(self, loaded):
        server.set_default_cue(dry_run=False)

        with pytest.raises(ToolError, match="already exists"):
            server.set_default_cue(dry_run=False)

    def test_bad_output_path_is_reported_in_a_dry_run_too(self, loaded):
        # The dry run must not promise a write that would fail.
        result = server.set_default_cue(output_path="/nonexistent-folder/x.xml")

        assert result["dryRun"] is True

    def test_nothing_to_do(self, tmp_path):
        path = tmp_path / "rb.xml"
        path.write_bytes(
            b'<DJ_PLAYLISTS><COLLECTION><TRACK TrackID="1" Name="A">'
            b'<POSITION_MARK Type="0" Start="1" Num="0"/></TRACK></COLLECTION></DJ_PLAYLISTS>'
        )
        server.load_rekordbox_library(str(path))

        result = server.set_default_cue(dry_run=False)

        assert result["tracksToChange"] == 0
        assert "Nothing to do" in result["summary"]
        assert not Path(result["outputFile"]).exists()
