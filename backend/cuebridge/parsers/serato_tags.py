"""Read the Serato Markers2 tag out of audio files (read-only).

MP3 files keep it in the ID3 tag, WAV files in the `id3 ` chunk. Other formats
(M4A, AIFF, FLAC) store Serato data differently and aren't supported yet.
"""

from pathlib import Path

from mutagen import MutagenError
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.wave import WAVE

from cuebridge.parsers.serato_markers import CueEntry, DESCRIPTION, Entry, MarkersError, decode_markers2
from cuebridge.services.rekordbox_service import LibraryError

SUPPORTED_SUFFIXES = (".mp3", ".wav")
_FRAME_KEY = f"GEOB:{DESCRIPTION}"


class UnsupportedFileError(LibraryError):
    """The audio file type or tag can't be read."""


def load_id3(path: str | Path) -> ID3 | None:
    """Open the file's ID3 tag, or return None if it has none."""
    file_path = Path(path).expanduser()
    suffix = file_path.suffix.lower()

    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedFileError(
            f"{file_path.name}: only MP3 and WAV files are supported for Serato cues so far."
        )

    if not file_path.is_file():
        raise UnsupportedFileError(f"File not found: {file_path}")

    try:
        if suffix == ".mp3":
            return ID3(file_path)

        return WAVE(file_path).tags
    except ID3NoHeaderError:
        return None
    except MutagenError as error:
        raise UnsupportedFileError(f"Could not read the tags of {file_path.name}: {error}") from None


def read_markers2(path: str | Path) -> list[Entry] | None:
    """The decoded Markers2 entries, or None if the file has no such tag."""
    tags = load_id3(path)

    if tags is None:
        return None

    frames = tags.getall(_FRAME_KEY)

    if not frames:
        return None

    try:
        return decode_markers2(frames[0].data)
    except MarkersError as error:
        raise UnsupportedFileError(f"{Path(path).name}: {error}") from None


def read_cues(path: str | Path) -> list[CueEntry]:
    """The hot cues stored in the file (an empty list if there are none)."""
    return [e for e in read_markers2(path) or [] if isinstance(e, CueEntry)]
