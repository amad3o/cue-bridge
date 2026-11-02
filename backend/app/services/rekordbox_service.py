"""Load Rekordbox libraries from files or raw bytes.

This is the layer both the HTTP endpoints and the MCP server call. It knows
nothing about FastAPI: it takes paths or bytes, validates them, and returns
Track models or raises a LibraryError subclass.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

from app.models import Track
from app.parsers.rekordbox_parser import RekordboxParseError, parse_rekordbox_xml

MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MB


class LibraryError(Exception):
    """Base class for errors caused by the file the user supplied."""


class InvalidFileError(LibraryError):
    """The file is missing, empty, the wrong type, or not a Rekordbox export."""


class FileTooLargeError(LibraryError):
    """The file is larger than MAX_FILE_BYTES."""


def _too_large() -> FileTooLargeError:
    limit_mb = MAX_FILE_BYTES // (1024 * 1024)
    return FileTooLargeError(f"File is too large (max {limit_mb} MB).")


def _check_filename(filename: str | None) -> None:
    if not (filename or "").lower().endswith(".xml"):
        raise InvalidFileError("Please provide a Rekordbox .xml file.")


def check_size(size: int | None) -> None:
    """Raise if a known file size is over the limit. None means unknown."""
    if size is not None and size > MAX_FILE_BYTES:
        raise _too_large()


def parse_rekordbox_bytes(contents: bytes, filename: str | None = None) -> list[Track]:
    """Validate and parse the contents of a Rekordbox XML export.

    Pass `filename` to also check the .xml extension.
    """
    if filename is not None:
        _check_filename(filename)

    check_size(len(contents))

    if not contents.strip():
        raise InvalidFileError("The file is empty.")

    try:
        parsed = parse_rekordbox_xml(contents)
    except ET.ParseError:
        raise InvalidFileError("The file is not valid XML.") from None
    except RekordboxParseError:
        raise InvalidFileError(
            "This doesn't look like a Rekordbox library export."
        ) from None

    return [Track(**track) for track in parsed]


def parse_rekordbox_file(path: str | Path) -> list[Track]:
    """Validate and parse a Rekordbox XML export on disk."""
    file_path = Path(path).expanduser()

    _check_filename(file_path.name)

    if not file_path.is_file():
        raise InvalidFileError(f"File not found: {file_path}")

    check_size(file_path.stat().st_size)

    try:
        contents = file_path.read_bytes()
    except OSError as error:
        raise InvalidFileError(f"Could not read {file_path}: {error.strerror}") from None

    return parse_rekordbox_bytes(contents)
