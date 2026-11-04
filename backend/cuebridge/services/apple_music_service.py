"""Load an Apple Music XML export from disk, with validation and a small cache."""

import plistlib
import threading
from collections import OrderedDict
from pathlib import Path

from cuebridge.parsers.apple_music_xml import AppleMusicParseError, MusicLibrary, parse_apple_music_plist
from cuebridge.services.rekordbox_service import MAX_FILE_BYTES, InvalidFileError, check_size

MAX_CACHED_LIBRARIES = 2

_cache: OrderedDict[Path, tuple[tuple[int, int], MusicLibrary]] = OrderedDict()
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def parse_apple_music_file(path: str | Path) -> MusicLibrary:
    file_path = Path(path).expanduser()

    if not file_path.name.lower().endswith(".xml"):
        raise InvalidFileError("Please provide an Apple Music library .xml file (File > Library > Export Library).")

    if not file_path.is_file():
        raise InvalidFileError(f"File not found: {file_path}")

    check_size(file_path.stat().st_size)

    try:
        contents = file_path.read_bytes()
    except OSError as error:
        raise InvalidFileError(f"Could not read {file_path}: {error.strerror}") from None

    if not contents.strip():
        raise InvalidFileError("The file is empty.")

    try:
        return parse_apple_music_plist(contents, file_path.resolve())
    except (plistlib.InvalidFileException, ValueError, KeyError) as error:
        if isinstance(error, AppleMusicParseError):
            raise InvalidFileError("This doesn't look like an Apple Music library export.") from None

        raise InvalidFileError("The file is not a valid property list (XML) export.") from None


def get_apple_music_library(path: str | Path) -> MusicLibrary:
    """Parse the file, reusing the last result while its modified time and size are unchanged."""
    file_path = Path(path).expanduser()

    try:
        resolved = file_path.resolve()
        stat = resolved.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return parse_apple_music_file(file_path)  # raises the clear error

    with _cache_lock:
        cached = _cache.get(resolved)

        if cached is not None and cached[0] == signature:
            _cache.move_to_end(resolved)
            return cached[1]

    library = parse_apple_music_file(file_path)

    with _cache_lock:
        _cache[resolved] = (signature, library)
        _cache.move_to_end(resolved)

        while len(_cache) > MAX_CACHED_LIBRARIES:
            _cache.popitem(last=False)

    return library
