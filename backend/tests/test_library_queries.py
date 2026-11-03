from pathlib import Path

import pytest

from cuebridge.services import library_queries as q
from cuebridge.services.rekordbox_service import clear_cache, get_rekordbox_library

FIXTURE = Path(__file__).parent / "fixtures" / "rekordbox_sample.xml"


@pytest.fixture(scope="module")
def library():
    clear_cache()
    return get_rekordbox_library(FIXTURE)


def ids(result):
    return [t["trackId"] for t in result["tracks"]]


class TestFormatTime:
    @pytest.mark.parametrize(
        "ms, expected",
        [(0, "0:00.000"), (32500, "0:32.500"), (67043, "1:07.043"), (3_600_000, "60:00.000")],
    )
    def test_format(self, ms, expected):
        assert q.format_time(ms) == expected


class TestSummary:
    def test_counts(self, library):
        summary = q.library_summary(library)

        assert summary["trackCount"] == 6
        assert summary["tracksWithCues"] == 3
        assert summary["tracksWithoutCues"] == 3
        assert summary["cueCount"] == 6
        assert summary["cuesByType"] == {"hotcue": 1, "memory": 4, "loop": 1}
        assert summary["tracksWithoutArtist"] == 2


class TestSearch:
    def test_lists_everything_by_default(self, library):
        result = q.search_tracks(library)

        assert result["total"] == 6
        assert ids(result) == ["1001", "2001", "3001", "3002", "4001", "5001"]

    def test_text_query_matches_title_artist_and_file_name(self, library):
        assert ids(q.search_tracks(library, "midnight")) == ["2001"]
        assert ids(q.search_tracks(library, "EXAMPLE artist")) == ["2001"]
        assert ids(q.search_tracks(library, "noise.wav")) == ["1001"]

    def test_all_words_must_match(self, library):
        assert ids(q.search_tracks(library, "same artist")) == ["3001", "3002"]
        assert q.search_tracks(library, "same midnight")["total"] == 0

    def test_accented_text(self, library):
        assert ids(q.search_tracks(library, "josé")) == ["4001"]

    def test_has_cues_filter(self, library):
        assert ids(q.search_tracks(library, has_cues=True)) == ["2001", "3001", "3002"]
        assert ids(q.search_tracks(library, has_cues=False)) == ["1001", "4001", "5001"]

    def test_filters_combine(self, library):
        assert ids(q.search_tracks(library, "same", has_cues=False)) == []
        assert ids(q.search_tracks(library, "noise", has_cues=False)) == ["1001"]

    def test_paging(self, library):
        first = q.search_tracks(library, limit=4)
        second = q.search_tracks(library, limit=4, offset=4)

        assert (first["returned"], first["hasMore"]) == (4, True)
        assert (second["returned"], second["hasMore"]) == (2, False)
        assert ids(first) + ids(second) == ["1001", "2001", "3001", "3002", "4001", "5001"]

    def test_limit_and_offset_are_clamped(self, library):
        assert q.search_tracks(library, limit=0)["returned"] == 1
        assert q.search_tracks(library, limit=10_000)["returned"] == 6
        assert q.search_tracks(library, offset=-5)["offset"] == 0

    def test_summary_fields(self, library):
        track = q.search_tracks(library, "midnight")["tracks"][0]

        assert track == {
            "trackId": "2001",
            "title": "Midnight Run",
            "artist": "Example Artist",
            "cueCount": 4,
            "filePath": "/Users/dj/Music/House/Midnight Run.mp3",
        }


class TestTrackDetail:
    def test_cue_fields(self, library):
        detail = q.track_to_detail(library.tracks_by_id["2001"])
        by_type = {c["type"]: c for c in detail["cues"] if c["type"] != "memory"}

        assert by_type["hotcue"] == {
            "type": "hotcue", "slot": 1, "name": "Drop", "time": "0:32.500",
            "timeMs": 32500, "endMs": None, "color": "#28e214",
        }
        assert by_type["loop"]["slot"] == 3
        assert by_type["loop"]["endMs"] == 104000

    def test_memory_cue_has_no_slot(self, library):
        memory = [c for c in q.track_to_detail(library.tracks_by_id["2001"])["cues"] if c["type"] == "memory"]

        assert [c["slot"] for c in memory] == [None, None]


def test_tracks_without_cues(library):
    assert [t.trackId for t in q.tracks_without_cues(library)] == ["1001", "4001", "5001"]
