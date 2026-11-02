import os
from pathlib import Path

import pytest

from cuebridge.services import rekordbox_service
from cuebridge.services.rekordbox_service import (
    InvalidFileError,
    clear_cache,
    get_rekordbox_library,
)


def xml_with(*names: str) -> bytes:
    tracks = "".join(
        f'<TRACK TrackID="{i}" Name="{name}" Artist="A"/>' for i, name in enumerate(names)
    )
    return f"<DJ_PLAYLISTS><COLLECTION>{tracks}</COLLECTION></DJ_PLAYLISTS>".encode()


def write(path: Path, contents: bytes, mtime_ns: int | None = None) -> Path:
    path.write_bytes(contents)

    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))

    return path


@pytest.fixture(autouse=True)
def fresh_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def parse_calls(monkeypatch):
    """Count how many times a file is actually parsed."""
    calls = []
    real = rekordbox_service.parse_rekordbox_file

    def counting(path):
        calls.append(Path(path))
        return real(path)

    monkeypatch.setattr(rekordbox_service, "parse_rekordbox_file", counting)
    return calls


class TestCaching:
    def test_second_call_reuses_the_parsed_library(self, tmp_path, parse_calls):
        path = write(tmp_path / "rb.xml", xml_with("One"))

        first = get_rekordbox_library(path)
        second = get_rekordbox_library(path)

        assert first is second
        assert len(parse_calls) == 1

    def test_different_spellings_of_a_path_share_an_entry(
        self, tmp_path, parse_calls, monkeypatch
    ):
        path = write(tmp_path / "rb.xml", xml_with("One"))
        link = tmp_path / "link.xml"
        link.symlink_to(path)
        monkeypatch.setenv("HOME", str(tmp_path))

        get_rekordbox_library(path)
        get_rekordbox_library(link)
        get_rekordbox_library("~/rb.xml")
        get_rekordbox_library(str(path))

        assert len(parse_calls) == 1

    def test_separate_files_are_cached_separately(self, tmp_path, parse_calls):
        a = write(tmp_path / "a.xml", xml_with("A"))
        b = write(tmp_path / "b.xml", xml_with("B"))

        assert get_rekordbox_library(a).tracks[0].trackTitle == "A"
        assert get_rekordbox_library(b).tracks[0].trackTitle == "B"
        assert get_rekordbox_library(a).tracks[0].trackTitle == "A"
        assert len(parse_calls) == 2


class TestInvalidation:
    def test_modified_file_is_reparsed(self, tmp_path, parse_calls):
        path = write(tmp_path / "rb.xml", xml_with("One"), mtime_ns=1_000_000_000)
        assert len(get_rekordbox_library(path).tracks) == 1

        # Same size, newer mtime.
        write(path, xml_with("Two"), mtime_ns=2_000_000_000)
        library = get_rekordbox_library(path)

        assert library.tracks[0].trackTitle == "Two"
        assert len(parse_calls) == 2

    def test_changed_size_is_reparsed_even_with_same_mtime(self, tmp_path, parse_calls):
        path = write(tmp_path / "rb.xml", xml_with("One"), mtime_ns=1_000_000_000)
        get_rekordbox_library(path)

        write(path, xml_with("One", "Two"), mtime_ns=1_000_000_000)

        assert len(get_rekordbox_library(path).tracks) == 2
        assert len(parse_calls) == 2

    def test_deleted_file_raises_instead_of_serving_stale_data(self, tmp_path):
        path = write(tmp_path / "rb.xml", xml_with("One"))
        get_rekordbox_library(path)

        path.unlink()

        with pytest.raises(InvalidFileError, match="not found"):
            get_rekordbox_library(path)


class TestErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(InvalidFileError, match="not found"):
            get_rekordbox_library(tmp_path / "nope.xml")

    def test_failed_parse_is_not_cached(self, tmp_path, parse_calls):
        path = write(tmp_path / "rb.xml", b"<DJ_PLAYLISTS><COLLECTION>", mtime_ns=1_000_000_000)

        with pytest.raises(InvalidFileError, match="not valid XML"):
            get_rekordbox_library(path)

        write(path, xml_with("Fixed"), mtime_ns=2_000_000_000)

        assert get_rekordbox_library(path).tracks[0].trackTitle == "Fixed"
        assert len(parse_calls) == 2

    def test_wrong_extension_still_rejected(self, tmp_path):
        path = write(tmp_path / "rb.txt", xml_with("One"))

        with pytest.raises(InvalidFileError, match=".xml"):
            get_rekordbox_library(path)


class TestLibrary:
    def test_lookup_by_track_id(self, tmp_path):
        path = write(tmp_path / "rb.xml", xml_with("Zero", "One"))

        library = get_rekordbox_library(path)

        assert library.tracks_by_id["1"].trackTitle == "One"
        assert library.tracks_by_id["1"] is library.tracks[1]
        assert "missing" not in library.tracks_by_id

    def test_path_is_resolved(self, tmp_path):
        path = write(tmp_path / "rb.xml", xml_with("One"))

        assert get_rekordbox_library(path).path == path.resolve()


class TestEviction:
    def test_oldest_library_is_evicted(self, tmp_path, parse_calls, monkeypatch):
        monkeypatch.setattr(rekordbox_service, "MAX_CACHED_LIBRARIES", 2)
        files = [write(tmp_path / f"{n}.xml", xml_with(n)) for n in "abc"]

        for f in files:
            get_rekordbox_library(f)

        assert len(parse_calls) == 3

        get_rekordbox_library(files[2])  # still cached
        get_rekordbox_library(files[1])  # still cached
        assert len(parse_calls) == 3

        get_rekordbox_library(files[0])  # was evicted
        assert len(parse_calls) == 4

    def test_recently_used_library_survives(self, tmp_path, parse_calls, monkeypatch):
        monkeypatch.setattr(rekordbox_service, "MAX_CACHED_LIBRARIES", 2)
        a, b, c = (write(tmp_path / f"{n}.xml", xml_with(n)) for n in "abc")

        get_rekordbox_library(a)
        get_rekordbox_library(b)
        get_rekordbox_library(a)  # a is now the most recent
        get_rekordbox_library(c)  # evicts b, not a

        get_rekordbox_library(a)
        assert len(parse_calls) == 3

        get_rekordbox_library(b)
        assert len(parse_calls) == 4


def test_clear_cache_forces_a_reparse(tmp_path, parse_calls):
    path = write(tmp_path / "rb.xml", xml_with("One"))
    get_rekordbox_library(path)

    clear_cache()
    get_rekordbox_library(path)

    assert len(parse_calls) == 2
