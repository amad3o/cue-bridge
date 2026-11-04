import pytest
from mutagen.id3 import GEOB, ID3

from cuebridge.parsers import serato_markers as m
from cuebridge.parsers.serato_markers import CueEntry
from cuebridge.parsers.serato_tags import (
    UnsupportedFileError,
    locate_markers2,
    read_cues,
    read_first_beat,
    read_markers2,
)
from serato_helpers import BPMLOCK, COLOR, make_mp3, make_wav

CUES = [CueEntry(0, 235, "#cc0000"), CueEntry(1, 581, "#00cc00", "Drop")]
ENTRIES = [COLOR, *CUES, BPMLOCK]


@pytest.fixture(params=["mp3", "wav"])
def make_file(request, tmp_path):
    """Build a file of either type, so every test covers MP3 and WAV."""
    maker = make_mp3 if request.param == "mp3" else make_wav
    return lambda entries=None: maker(tmp_path / f"track.{request.param}", entries)


class TestReading:
    def test_reads_all_entries_in_order(self, make_file):
        assert read_markers2(make_file(ENTRIES)) == ENTRIES

    def test_reads_only_the_cues(self, make_file):
        assert read_cues(make_file(ENTRIES)) == CUES

    def test_a_tag_with_no_cues(self, make_file):
        path = make_file([COLOR, BPMLOCK])

        assert read_markers2(path) == [COLOR, BPMLOCK]
        assert read_cues(path) == []

    def test_a_file_without_a_serato_tag(self, make_file):
        assert read_markers2(make_file(None)) is None
        assert read_cues(make_file(None)) == []

    def test_accepts_string_paths(self, make_file):
        assert read_cues(str(make_file(ENTRIES))) == CUES

    def test_reading_does_not_change_the_file(self, make_file):
        path = make_file(ENTRIES)
        before = path.read_bytes()

        read_cues(path)

        assert path.read_bytes() == before


class TestFilesWithoutTags:
    def test_mp3_with_no_id3_header(self, tmp_path):
        path = tmp_path / "plain.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 200)

        assert read_markers2(path) is None

    def test_wav_with_no_id3_chunk(self, tmp_path):
        assert read_markers2(make_wav(tmp_path / "plain.wav")) is None


class TestErrors:
    @pytest.mark.parametrize("name", ["song.m4a", "song.flac", "song.aiff", "song"])
    def test_unsupported_formats(self, tmp_path, name):
        path = tmp_path / name
        path.write_bytes(b"x")

        with pytest.raises(UnsupportedFileError, match="only MP3 and WAV"):
            read_markers2(path)

    def test_missing_file(self, tmp_path):
        with pytest.raises(UnsupportedFileError, match="not found"):
            read_markers2(tmp_path / "nope.mp3")

    def test_corrupt_wav(self, tmp_path):
        path = tmp_path / "bad.wav"
        path.write_bytes(b"this is not a wav file")

        with pytest.raises(UnsupportedFileError, match="Could not read"):
            read_markers2(path)

    def test_corrupt_markers2_data_is_reported_with_the_file_name(self, tmp_path):
        path = tmp_path / "bad.mp3"
        tags = ID3()
        tags.add(GEOB(encoding=0, mime="application/octet-stream", filename="", desc=m.DESCRIPTION, data=b"garbage"))
        tags.save(path, v2_version=4)

        with pytest.raises(UnsupportedFileError, match="bad.mp3"):
            read_markers2(path)

    def test_uppercase_extension(self, tmp_path):
        path = tmp_path / "TRACK.MP3"
        make_mp3(path, ENTRIES)

        assert read_cues(path) == CUES


class TestLocating:
    def test_finds_the_exact_bytes_in_both_formats(self, make_file):
        path = make_file(ENTRIES)

        location = locate_markers2(path)

        assert location.length == m.FRAME_SIZE
        assert path.read_bytes()[location.offset : location.offset + location.length] == location.data
        assert m.decode_markers2(location.data) == ENTRIES

    def test_no_tag_means_none(self, make_file):
        assert locate_markers2(make_file(None)) is None

    def test_plain_files_have_nothing_to_locate(self, tmp_path):
        mp3 = tmp_path / "plain.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)

        assert locate_markers2(mp3) is None
        assert locate_markers2(make_wav(tmp_path / "plain.wav")) is None

    def test_works_on_id3v23_tags(self, tmp_path):
        path = make_mp3(tmp_path / "v23.mp3", ENTRIES, version=3)

        location = locate_markers2(path)

        assert m.decode_markers2(location.data) == ENTRIES
        assert path.read_bytes()[location.offset : location.offset + location.length] == location.data

    def test_overwriting_the_located_bytes_is_seen_by_mutagen(self, make_file):
        path = make_file(ENTRIES)
        location = locate_markers2(path)
        new = m.encode_markers2([COLOR, CueEntry(3, 99_000, "#00cccc"), BPMLOCK])

        with open(path, "r+b") as handle:
            handle.seek(location.offset)
            handle.write(new)

        assert read_cues(path) == [CueEntry(3, 99_000, "#00cccc")]

    def test_refuses_unsynchronised_tags(self, tmp_path):
        path = make_mp3(tmp_path / "unsync.mp3", ENTRIES)
        data = bytearray(path.read_bytes())
        data[5] |= 0x80
        path.write_bytes(bytes(data))

        with pytest.raises(UnsupportedFileError, match="unsynchronised"):
            locate_markers2(path)

    def test_refuses_a_frame_with_compression_or_other_flags(self, tmp_path):
        path = make_mp3(tmp_path / "flags.mp3", ENTRIES)
        offset = locate_markers2(path).offset
        data = bytearray(path.read_bytes())
        # frame header = 10 bytes before the frame body; the body has a 43-byte prefix before the payload
        data[offset - 43 - 10 + 9] |= 0x08
        path.write_bytes(bytes(data))

        with pytest.raises(UnsupportedFileError, match="compressed"):
            locate_markers2(path)

    def test_refuses_other_formats(self, tmp_path):
        path = tmp_path / "song.m4a"
        path.write_bytes(b"x")

        with pytest.raises(UnsupportedFileError, match="only MP3 and WAV"):
            locate_markers2(path)

    def test_a_wav_that_is_not_a_wav(self, tmp_path):
        path = tmp_path / "bad.wav"
        path.write_bytes(b"not a wav at all")

        with pytest.raises(UnsupportedFileError, match="Not a WAV"):
            locate_markers2(path)

    def test_a_tag_without_serato_frames(self, tmp_path):
        path = make_mp3(tmp_path / "other.mp3", None)

        assert locate_markers2(path) is None


class TestBeatGrid:
    def test_the_first_marker_and_bpm_are_read(self, tmp_path):
        path = make_mp3(tmp_path / "g.mp3", [COLOR], beatgrid=(73.0, 118.0))

        position, bpm = read_first_beat(path)

        assert position == pytest.approx(73.0, abs=0.01) and bpm == pytest.approx(118.0)

    def test_a_file_without_a_grid_gives_none(self, tmp_path):
        assert read_first_beat(make_mp3(tmp_path / "n.mp3", [COLOR])) is None

    def test_a_file_without_tags_gives_none(self, tmp_path):
        path = tmp_path / "bare.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" * 100)

        assert read_first_beat(path) is None
