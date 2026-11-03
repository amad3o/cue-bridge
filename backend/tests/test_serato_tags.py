import wave

import pytest
from mutagen.id3 import GEOB, ID3, TIT2
from mutagen.wave import WAVE

from cuebridge.parsers import serato_markers as m
from cuebridge.parsers.serato_markers import CueEntry, RawEntry
from cuebridge.parsers.serato_tags import UnsupportedFileError, read_cues, read_markers2

COLOR = RawEntry("COLOR", bytes.fromhex("00ffffff"))
BPMLOCK = RawEntry("BPMLOCK", b"\x00")
CUES = [CueEntry(0, 235, "#cc0000"), CueEntry(1, 581, "#00cc00", "Drop")]
ENTRIES = [COLOR, *CUES, BPMLOCK]


def geob(entries) -> GEOB:
    return GEOB(
        encoding=0, mime="application/octet-stream", filename="",
        desc=m.DESCRIPTION, data=m.encode_markers2(entries),
    )


def make_mp3(path, entries=None, title="Song"):
    """A file with an ID3v2.4 tag (and no audio, which is enough for reading tags)."""
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))

    if entries is not None:
        tags.add(geob(entries))

    tags.save(path, v2_version=4)
    return path


def make_wav(path, entries=None):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x00" * 4410)

    if entries is not None:
        wav = WAVE(path)
        wav.add_tags()
        wav.tags.add(geob(entries))
        wav.save()

    return path


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
