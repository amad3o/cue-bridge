"""Tests against a realistic Rekordbox export (see the comment in the fixture)."""

from pathlib import Path

import pytest

from cuebridge.services.rekordbox_service import parse_rekordbox_file

FIXTURE = Path(__file__).parent / "fixtures" / "rekordbox_sample.xml"


@pytest.fixture(scope="module")
def tracks():
    return {t.trackId: t for t in parse_rekordbox_file(FIXTURE)}


def test_all_collection_tracks_parsed(tracks):
    assert set(tracks) == {"1001", "2001", "3001", "3002", "4001", "5001"}


def test_playlist_track_references_are_not_treated_as_tracks(tracks):
    # <TRACK Key="2001"/> inside PLAYLISTS must not be added to the library.
    assert len(tracks) == 6


def test_empty_artist_is_none(tracks):
    assert tracks["1001"].artist is None
    assert tracks["5001"].artist is None
    assert tracks["2001"].artist == "Example Artist"


def test_tracks_without_cues(tracks):
    assert tracks["1001"].cues == []


def test_tempo_children_do_not_produce_cues(tracks):
    # Track 2001 has two TEMPO elements and four marks.
    assert len(tracks["2001"].cues) == 4


def test_memory_cues(tracks):
    memory = [c for c in tracks["2001"].cues if c.cueType == "memory"]

    assert [c.timeMs for c in memory] == [0, 67043]
    assert all(c.cueIndex is None and c.color is None for c in memory)
    assert all(c.cueName == "Unnamed cue" for c in memory)


def test_cue_at_zero_is_kept(tracks):
    assert tracks["2001"].cues[0].timeMs == 0


def test_hot_cue_and_loop(tracks):
    hot = next(c for c in tracks["2001"].cues if c.cueType == "hotcue")
    loop = next(c for c in tracks["2001"].cues if c.cueType == "loop")

    assert (hot.cueIndex, hot.timeMs, hot.color) == (0, 32500, "#28e214")
    assert (loop.cueIndex, loop.timeMs, loop.endMs, loop.color) == (
        2, 96000, 104000, "#ff0000",
    )


def test_cue_ids_unique_across_library_with_duplicate_titles(tracks):
    ids = [c.id for t in tracks.values() for c in t.cues]

    assert len(ids) == len(set(ids))
    assert tracks["3001"].trackTitle == tracks["3002"].trackTitle
    assert tracks["3001"].cues[0].id != tracks["3002"].cues[0].id


def test_paths_are_decoded(tracks):
    assert tracks["1001"].filePath == "/Users/dj/Music/Sampler/PRESET ONESHOT/NOISE.wav"
    assert tracks["4001"].filePath == "/Users/dj/Music/Café & More/José.mp3"


def test_unicode_and_entities_in_names(tracks):
    assert tracks["4001"].trackTitle == "Café del Mar & Friends"
    assert tracks["4001"].artist == "José González"


def test_cues_carry_track_info(tracks):
    cue = tracks["2001"].cues[0]

    assert cue.trackTitle == "Midnight Run"
    assert cue.artist == "Example Artist"
    assert cue.filePath == tracks["2001"].filePath
