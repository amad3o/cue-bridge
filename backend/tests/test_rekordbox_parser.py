import xml.etree.ElementTree as ET

import pytest

from cuebridge.parsers.rekordbox_parser import (
    RekordboxParseError,
    _parse_color,
    _parse_location,
    parse_rekordbox_xml,
)


def make_xml(*tracks: str) -> bytes:
    body = "".join(tracks)
    return f"<DJ_PLAYLISTS><COLLECTION>{body}</COLLECTION></DJ_PLAYLISTS>".encode()


def make_track(marks: str = "", **attrs) -> str:
    attrs = {"TrackID": "1", "Name": "Song", "Artist": "Artist", **attrs}
    attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    return f"<TRACK {attr_str}>{marks}</TRACK>"


def parse_single_cue(mark: str) -> dict:
    tracks = parse_rekordbox_xml(make_xml(make_track(mark)))
    return tracks[0]["cues"][0]


class TestTrackParsing:
    def test_raises_without_collection(self):
        with pytest.raises(RekordboxParseError):
            parse_rekordbox_xml(b"<DJ_PLAYLISTS></DJ_PLAYLISTS>")

    def test_empty_collection_returns_no_tracks(self):
        assert parse_rekordbox_xml(make_xml()) == []

    def test_parses_track_fields(self):
        tracks = parse_rekordbox_xml(
            make_xml(make_track(Location="file://localhost/Users/me/song.mp3"))
        )

        assert len(tracks) == 1
        assert tracks[0]["trackId"] == "1"
        assert tracks[0]["trackTitle"] == "Song"
        assert tracks[0]["artist"] == "Artist"
        assert tracks[0]["filePath"] == "/Users/me/song.mp3"
        assert tracks[0]["cues"] == []

    def test_missing_name_and_artist_are_none(self):
        tracks = parse_rekordbox_xml(make_xml('<TRACK TrackID="9"/>'))

        assert tracks[0]["trackTitle"] is None
        assert tracks[0]["artist"] is None
        assert tracks[0]["filePath"] is None

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_name_and_artist_are_none(self, value):
        # Rekordbox writes Artist="" for tracks with no artist tag.
        tracks = parse_rekordbox_xml(make_xml(make_track(Name=value, Artist=value)))

        assert tracks[0]["trackTitle"] is None
        assert tracks[0]["artist"] is None

    def test_missing_artist_and_title_are_none_on_cues(self):
        tracks = parse_rekordbox_xml(
            make_xml(
                make_track(
                    '<POSITION_MARK Type="0" Start="1" Num="0"/>', Name="", Artist=""
                )
            )
        )

        assert tracks[0]["cues"][0]["trackTitle"] is None
        assert tracks[0]["cues"][0]["artist"] is None

    def test_surrounding_whitespace_is_stripped(self):
        tracks = parse_rekordbox_xml(make_xml(make_track(Name=" Song ", Artist=" A ")))

        assert tracks[0]["trackTitle"] == "Song"
        assert tracks[0]["artist"] == "A"

    def test_missing_track_id_falls_back_to_position(self):
        tracks = parse_rekordbox_xml(make_xml("<TRACK/><TRACK/>"))

        assert [t["trackId"] for t in tracks] == ["idx0", "idx1"]

    def test_malformed_xml_raises(self):
        with pytest.raises(ET.ParseError):
            parse_rekordbox_xml(b"<DJ_PLAYLISTS><COLLECTION>")


class TestFirstBeat:
    XML = (
        b'<DJ_PLAYLISTS><COLLECTION Entries="2">'
        b'<TRACK TrackID="1" Name="A"><TEMPO Inizio="0.020" Bpm="117.85" Metro="4/4" Battito="3"/>'
        b'<TEMPO Inizio="0.540" Bpm="117.85" Metro="4/4" Battito="4"/></TRACK>'
        b'<TRACK TrackID="2" Name="B"/></COLLECTION></DJ_PLAYLISTS>'
    )

    def test_the_first_tempo_marker_is_the_first_beat(self):
        tracks = parse_rekordbox_xml(self.XML)

        assert tracks[0]["firstBeatMs"] == 20

    def test_a_track_without_a_beat_grid_has_none(self):
        assert parse_rekordbox_xml(self.XML)[1]["firstBeatMs"] is None


class TestCueTypes:
    def test_hot_cue(self):
        cue = parse_single_cue('<POSITION_MARK Name="Drop" Type="0" Start="32.5" Num="3"/>')

        assert cue["cueType"] == "hotcue"
        assert cue["cueIndex"] == 3
        assert cue["cueName"] == "Drop"
        assert cue["timeMs"] == 32500
        assert cue["endMs"] is None

    def test_memory_cue_has_no_index(self):
        cue = parse_single_cue('<POSITION_MARK Name="" Type="0" Start="64" Num="-1"/>')

        assert cue["cueType"] == "memory"
        assert cue["cueIndex"] is None

    def test_hot_loop_keeps_slot_and_end(self):
        cue = parse_single_cue('<POSITION_MARK Type="4" Start="96" End="104.5" Num="2"/>')

        assert cue["cueType"] == "loop"
        assert cue["cueIndex"] == 2
        assert cue["timeMs"] == 96000
        assert cue["endMs"] == 104500

    def test_memory_loop_has_no_index(self):
        cue = parse_single_cue('<POSITION_MARK Type="4" Start="10" End="12" Num="-1"/>')

        assert cue["cueType"] == "loop"
        assert cue["cueIndex"] is None
        assert cue["endMs"] == 12000

    @pytest.mark.parametrize("mark_type", ["1", "2", "3"])
    def test_fade_and_load_markers_are_skipped(self, mark_type):
        tracks = parse_rekordbox_xml(
            make_xml(make_track(f'<POSITION_MARK Type="{mark_type}" Start="1" Num="-1"/>'))
        )

        assert tracks[0]["cues"] == []

    @pytest.mark.parametrize(
        "mark",
        [
            '<POSITION_MARK Type="0" Start="1" Num="0"/>',
            '<POSITION_MARK Name="" Type="0" Start="1" Num="0"/>',
            '<POSITION_MARK Name="  " Type="0" Start="1" Num="0"/>',
        ],
    )
    def test_unnamed_cue_has_no_name(self, mark):
        assert parse_single_cue(mark)["cueName"] is None


class TestCueIds:
    def test_ids_unique_across_tracks_with_same_title(self):
        mark = '<POSITION_MARK Type="0" Start="64" Num="-1"/>'
        tracks = parse_rekordbox_xml(
            make_xml(make_track(mark, TrackID="1"), make_track(mark, TrackID="2"))
        )

        ids = [t["cues"][0]["id"] for t in tracks]

        assert len(set(ids)) == 2

    def test_ids_unique_for_memory_cues_in_one_track(self):
        marks = (
            '<POSITION_MARK Type="0" Start="10" Num="-1"/>'
            '<POSITION_MARK Type="0" Start="20" Num="-1"/>'
        )
        cues = parse_rekordbox_xml(make_xml(make_track(marks)))[0]["cues"]

        assert len({c["id"] for c in cues}) == 2

    def test_id_format(self):
        cue = parse_single_cue('<POSITION_MARK Type="0" Start="32.5" Num="0"/>')

        assert cue["id"] == "rekordbox-1-hotcue-0-32500"


class TestColor:
    def test_rgb_converted_to_hex(self):
        cue = parse_single_cue(
            '<POSITION_MARK Type="0" Start="1" Num="0" Red="40" Green="226" Blue="20"/>'
        )

        assert cue["color"] == "#28e214"

    def test_missing_color_is_none(self):
        cue = parse_single_cue('<POSITION_MARK Type="0" Start="1" Num="0"/>')

        assert cue["color"] is None

    def test_partial_color_is_none(self):
        assert _parse_color({"Red": "255", "Green": "0"}) is None

    def test_invalid_color_is_none(self):
        assert _parse_color({"Red": "x", "Green": "0", "Blue": "0"}) is None

    def test_out_of_range_channels_are_clamped(self):
        assert _parse_color({"Red": "300", "Green": "-5", "Blue": "0"}) == "#ff0000"


class TestFilePath:
    def test_url_decoding(self):
        assert (
            _parse_location("file://localhost/Users/me/My%20Song.mp3")
            == "/Users/me/My Song.mp3"
        )

    def test_windows_drive_path(self):
        assert _parse_location("file://localhost/C:/Music/a.mp3") == "C:/Music/a.mp3"

    def test_without_localhost(self):
        assert _parse_location("file:///Users/me/a.mp3") == "/Users/me/a.mp3"

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_location_is_none(self, value):
        assert _parse_location(value) is None

    def test_file_path_copied_onto_cues(self):
        tracks = parse_rekordbox_xml(
            make_xml(
                make_track(
                    '<POSITION_MARK Type="0" Start="1" Num="0"/>',
                    Location="file://localhost/Users/me/a.mp3",
                )
            )
        )

        assert tracks[0]["cues"][0]["filePath"] == "/Users/me/a.mp3"


class TestBadData:
    @pytest.mark.parametrize(
        "mark",
        [
            '<POSITION_MARK Type="0" Num="0"/>',
            '<POSITION_MARK Type="0" Start="abc" Num="0"/>',
        ],
    )
    def test_mark_without_valid_start_is_skipped(self, mark):
        tracks = parse_rekordbox_xml(make_xml(make_track(mark)))

        assert tracks[0]["cues"] == []

    def test_bad_mark_does_not_drop_good_ones(self):
        marks = (
            '<POSITION_MARK Type="0" Start="bad" Num="0"/>'
            '<POSITION_MARK Type="0" Start="5" Num="1"/>'
        )
        cues = parse_rekordbox_xml(make_xml(make_track(marks)))[0]["cues"]

        assert len(cues) == 1
        assert cues[0]["cueIndex"] == 1

    def test_non_numeric_num_treated_as_memory(self):
        cue = parse_single_cue('<POSITION_MARK Type="0" Start="1" Num="x"/>')

        assert cue["cueType"] == "memory"

    def test_invalid_loop_end_is_none(self):
        cue = parse_single_cue('<POSITION_MARK Type="4" Start="1" End="bad" Num="0"/>')

        assert cue["endMs"] is None
