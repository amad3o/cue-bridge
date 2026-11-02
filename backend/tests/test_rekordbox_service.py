from pathlib import Path

import pytest

from app.models import Track
from app.services import rekordbox_service
from app.services.rekordbox_service import (
    FileTooLargeError,
    InvalidFileError,
    parse_rekordbox_bytes,
    parse_rekordbox_file,
)

VALID_XML = b"""<DJ_PLAYLISTS><COLLECTION>
<TRACK TrackID="1" Name="Song" Artist="Artist" Location="file://localhost/a.mp3">
<POSITION_MARK Name="Drop" Type="0" Start="32.5" Num="0" Red="40" Green="226" Blue="20"/>
<POSITION_MARK Type="4" Start="96" End="104" Num="-1"/>
</TRACK></COLLECTION></DJ_PLAYLISTS>"""


@pytest.fixture
def library_file(tmp_path: Path) -> Path:
    path = tmp_path / "rekordbox.xml"
    path.write_bytes(VALID_XML)
    return path


class TestParseFile:
    def test_returns_track_models(self, library_file):
        tracks = parse_rekordbox_file(library_file)

        assert len(tracks) == 1
        assert isinstance(tracks[0], Track)
        assert tracks[0].trackTitle == "Song"
        assert tracks[0].cues[0].color == "#28e214"
        assert tracks[0].cues[1].cueType == "loop"
        assert tracks[0].cues[1].endMs == 104000

    def test_accepts_string_path(self, library_file):
        assert len(parse_rekordbox_file(str(library_file))) == 1

    def test_expands_home_directory(self, library_file, monkeypatch):
        monkeypatch.setenv("HOME", str(library_file.parent))

        assert len(parse_rekordbox_file("~/rekordbox.xml")) == 1

    def test_missing_file(self, tmp_path):
        with pytest.raises(InvalidFileError, match="not found"):
            parse_rekordbox_file(tmp_path / "nope.xml")

    def test_directory_is_not_a_file(self, tmp_path):
        folder = tmp_path / "folder.xml"
        folder.mkdir()

        with pytest.raises(InvalidFileError, match="not found"):
            parse_rekordbox_file(folder)

    def test_wrong_extension(self, tmp_path):
        path = tmp_path / "library.txt"
        path.write_bytes(VALID_XML)

        with pytest.raises(InvalidFileError, match=".xml"):
            parse_rekordbox_file(path)

    def test_unreadable_file(self, library_file):
        library_file.chmod(0o000)

        try:
            with pytest.raises(InvalidFileError, match="Could not read"):
                parse_rekordbox_file(library_file)
        finally:
            library_file.chmod(0o644)

    def test_too_large(self, library_file, monkeypatch):
        monkeypatch.setattr(rekordbox_service, "MAX_FILE_BYTES", 10)

        with pytest.raises(FileTooLargeError):
            parse_rekordbox_file(library_file)


class TestParseBytes:
    def test_valid(self):
        assert len(parse_rekordbox_bytes(VALID_XML)) == 1

    def test_empty_collection_is_valid(self):
        assert parse_rekordbox_bytes(b"<DJ_PLAYLISTS><COLLECTION/></DJ_PLAYLISTS>") == []

    def test_filename_is_optional(self):
        assert len(parse_rekordbox_bytes(VALID_XML, filename=None)) == 1

    @pytest.mark.parametrize("filename", ["library.txt", "", "library.xml.zip"])
    def test_wrong_filename_rejected(self, filename):
        with pytest.raises(InvalidFileError, match=".xml"):
            parse_rekordbox_bytes(VALID_XML, filename=filename)

    def test_uppercase_extension_accepted(self):
        assert len(parse_rekordbox_bytes(VALID_XML, filename="EXPORT.XML")) == 1

    def test_empty_contents(self):
        with pytest.raises(InvalidFileError, match="empty"):
            parse_rekordbox_bytes(b"  \n")

    def test_malformed_xml(self):
        with pytest.raises(InvalidFileError, match="not valid XML"):
            parse_rekordbox_bytes(b"<DJ_PLAYLISTS><COLLECTION>")

    def test_not_a_rekordbox_export(self):
        with pytest.raises(InvalidFileError, match="Rekordbox"):
            parse_rekordbox_bytes(b"<html><body/></html>")

    def test_too_large(self, monkeypatch):
        monkeypatch.setattr(rekordbox_service, "MAX_FILE_BYTES", 10)

        with pytest.raises(FileTooLargeError):
            parse_rekordbox_bytes(VALID_XML)


class TestCheckSize:
    def test_unknown_size_is_allowed(self):
        rekordbox_service.check_size(None)

    def test_over_limit(self, monkeypatch):
        monkeypatch.setattr(rekordbox_service, "MAX_FILE_BYTES", 10)

        with pytest.raises(FileTooLargeError):
            rekordbox_service.check_size(11)
