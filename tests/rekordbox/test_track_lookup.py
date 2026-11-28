"""search_tracks, get_track_cues and find_tracks_without_cues, read from the live database (a fake one here)."""

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_rekordbox_db import FakeBackend, db_state

from cuebridge import mcp_server as server
from cuebridge.services.rekordbox import db as rd
from cuebridge.services.rekordbox.service import clear_cache

XML = Path(__file__).parents[1] / "fixtures" / "rekordbox_sample.xml"


@pytest.fixture(autouse=True)
def clean_state():
    clear_cache()
    server.reset_state()
    yield
    clear_cache()
    server.reset_state()


@pytest.fixture
def database(tmp_path, monkeypatch):
    """c1, c2 and c4 are in playlists and c3 is in none; c1 has one hot cue."""
    path = tmp_path / "master.db"
    state = db_state()
    state["cues"] = [["c1", 1, 1500, "q1"]]
    path.write_text(json.dumps(state))
    backend = FakeBackend()
    monkeypatch.setattr(rd, "default_backend", lambda: backend)
    monkeypatch.setattr(rd, "find_database", lambda: path)

    return {"path": path, "state": state}


def ids(result):
    return [t["trackId"] for t in result["tracks"]]


def test_search_needs_no_export(database):
    result = server.search_tracks(query="lost")

    assert ids(result) == ["c4"] and "database" in result["readFrom"]


def test_search_finds_tracks_in_no_playlist_too(database):
    assert ids(server.search_tracks()) == ["c1", "c2", "c3", "c4"]


def test_a_found_track_s_cues_come_from_the_same_place(database):
    result = server.get_track_cues(ids(server.search_tracks(has_cues=True))[0])

    assert [(c["slot"], c["timeMs"]) for c in result["cues"]] == [(1, 1500)]
    assert "database" in result["readFrom"]


def test_a_change_in_the_database_shows_at_once(database):
    assert server.search_tracks(has_cues=True)["total"] == 1

    database["state"]["cues"].append(["c2", 1, 0, "q2"])
    database["path"].write_text(json.dumps(database["state"]))

    assert server.search_tracks(has_cues=True)["total"] == 2


def test_the_database_is_preferred_over_a_loaded_export(database):
    server.load_rekordbox_library(str(XML))

    assert ids(server.search_tracks()) == ["c1", "c2", "c3", "c4"]
    assert server.search_tracks(cue_source="xml")["total"] == 6


def test_tracks_without_cues_are_the_playlisted_ones_by_default(database):
    result = server.find_tracks_without_cues()

    assert ids(result) == ["c2", "c4"] and result["playlistedTracksOnly"] is True
    assert ids(server.find_tracks_without_cues(playlisted_only=False)) == ["c2", "c3", "c4"]


def test_the_export_is_the_fallback_and_says_it_cannot_tell_what_is_playlisted():
    server.load_rekordbox_library(str(XML))  # the conftest fixture leaves no database to find

    result = server.find_tracks_without_cues()

    assert result["total"] == 3 and result["playlistedTracksOnly"] is False
    assert "XML export" in result["readFrom"] and any("couldn't be read" in note for note in result["notes"])


def test_an_unknown_id_names_where_it_looked(database):
    with pytest.raises(ToolError, match="No track with ID 'nope' in Rekordbox's database"):
        server.get_track_cues("nope")


def test_asking_for_the_database_when_it_cannot_be_read_is_an_error():
    server.load_rekordbox_library(str(XML))

    with pytest.raises(ToolError, match="Could not read Rekordbox's database"):
        server.search_tracks(cue_source="database")
