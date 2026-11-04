"""Load Rekordbox libraries from files or raw bytes.

This is the layer both the HTTP endpoints and the MCP server call. It knows
nothing about FastAPI: it takes paths or bytes, validates them, and returns
Track models or raises a LibraryError subclass.
"""

import hashlib
import json
import threading
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from cuebridge.models import Track
from cuebridge.parsers.rekordbox_parser import (
    RekordboxParseError,
    RekordboxPlaylist,
    parse_rekordbox_playlists,
    parse_rekordbox_xml,
)

MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MB

# When CueBridge adds playlists to a Rekordbox XML for importing, the tracks it
# adds get IDs from this number up, and the folder name is recorded in a small
# side file next to the XML (the XML itself carries no marker, because Rekordbox
# is picky about its input). The side file holds a checksum of the XML as it was
# written, so it only applies to that exact file: a fresh export, or any other
# change, makes it stale and it is ignored. Until Rekordbox has imported the
# playlists and the file is exported afresh, they are not part of what Rekordbox
# has, so they are left out.
PENDING_TRACK_ID_BASE = 900_000_000
PENDING_SUFFIX = ".cuebridge.json"
MAX_CACHED_LIBRARIES = 4


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


def read_library_bytes(path: str | Path) -> bytes:
    """Read a Rekordbox XML file from disk, checking name, existence and size."""
    file_path = Path(path).expanduser()

    _check_filename(file_path.name)

    if not file_path.is_file():
        raise InvalidFileError(f"File not found: {file_path}")

    check_size(file_path.stat().st_size)

    try:
        return file_path.read_bytes()
    except OSError as error:
        raise InvalidFileError(f"Could not read {file_path}: {error.strerror}") from None


def parse_rekordbox_file(path: str | Path) -> list[Track]:
    """Validate and parse a Rekordbox XML export on disk."""
    return parse_rekordbox_bytes(read_library_bytes(path))


def pending_note_path(xml_path: Path) -> Path:
    return xml_path.with_name(xml_path.name + PENDING_SUFFIX)


def pending_folder(xml_path: Path, contents: bytes) -> str | None:
    """The folder CueBridge added for importing, if this exact file is the one it wrote."""
    try:
        note = json.loads(pending_note_path(xml_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not isinstance(note, dict) or note.get("sha256") != hashlib.sha256(contents).hexdigest():
        return None

    folder = note.get("folder")

    return folder if isinstance(folder, str) and folder else None


def write_pending_note(xml_path: Path, folder: str, contents: bytes) -> None:
    note = {"folder": folder, "sha256": hashlib.sha256(contents).hexdigest(), "trackIdBase": PENDING_TRACK_ID_BASE}
    pending_note_path(xml_path).write_text(json.dumps(note, indent=1), encoding="utf-8")


def has_pending_playlists(xml_path: Path) -> bool:
    try:
        return pending_folder(xml_path, xml_path.read_bytes()) is not None
    except OSError:
        return False


def is_pending_track_id(track_id: str) -> bool:
    return track_id.isdigit() and int(track_id) >= PENDING_TRACK_ID_BASE


@dataclass(frozen=True)
class RekordboxLibrary:
    """A parsed Rekordbox library, with a lookup by track ID."""

    path: Path
    tracks: list[Track]
    tracks_by_id: dict[str, Track]
    playlists: list[RekordboxPlaylist] = field(default_factory=list)
    pending_folder: str | None = None  # a folder added for importing that Rekordbox may not have yet


# Resolved path -> ((mtime_ns, size) when parsed, library). Oldest entry first.
_cache: OrderedDict[Path, tuple[tuple[int, int], RekordboxLibrary]] = OrderedDict()
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def get_rekordbox_library(path: str | Path) -> RekordboxLibrary:
    """Parse a Rekordbox XML file, reusing the previous result if it is unchanged.

    The cache is keyed by the resolved path, and an entry is reused only while
    the file's modification time and size match what they were when it was
    parsed. Re-exporting from Rekordbox therefore invalidates it automatically.
    Failed parses are never cached.
    """
    file_path = Path(path).expanduser()

    try:
        resolved = file_path.resolve()
        stat = resolved.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        # Missing or unreadable: let parse_rekordbox_file raise the clear error.
        return _build_library(file_path, file_path)

    with _cache_lock:
        cached = _cache.get(resolved)

        if cached is not None and cached[0] == signature:
            _cache.move_to_end(resolved)
            return cached[1]

    # The signature was read before parsing. If the file changes mid-parse, the
    # stored signature is older than the content, so the next call re-parses.
    library = _build_library(file_path, resolved)

    with _cache_lock:
        _cache[resolved] = (signature, library)
        _cache.move_to_end(resolved)

        while len(_cache) > MAX_CACHED_LIBRARIES:
            _cache.popitem(last=False)

    return library


def _build_library(file_path: Path, resolved: Path) -> RekordboxLibrary:
    contents = read_library_bytes(file_path)
    tracks = parse_rekordbox_bytes(contents)
    playlists = parse_rekordbox_playlists(contents)
    folder = pending_folder(resolved, contents)

    if folder is not None:
        tracks = [t for t in tracks if not is_pending_track_id(t.trackId)]
        playlists = [p for p in playlists if p.folders[:1] != (folder,)]

    return RekordboxLibrary(
        path=resolved,
        tracks=tracks,
        tracks_by_id={track.trackId: track for track in tracks},
        playlists=playlists,
        pending_folder=folder,
    )
