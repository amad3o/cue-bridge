"""Read the Serato Markers2 tag out of audio files (read-only).

MP3 files keep it in the ID3 tag, WAV files in the `id3 ` chunk. Other formats
(M4A, AIFF, FLAC) store Serato data differently and aren't supported yet.
"""

import struct
from dataclasses import dataclass
from pathlib import Path

from mutagen import MutagenError
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.wave import WAVE

from cuebridge.parsers.serato_markers import (
    CueEntry,
    DESCRIPTION,
    Entry,
    LEGACY_DESCRIPTION,
    MarkersError,
    decode_markers2,
)
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


def read_first_beat(path: str | Path) -> tuple[float, float] | None:
    """(position in ms, BPM) of the first marker of Serato's beat grid, or None.

    The `Serato BeatGrid` frame is `01 00`, a 4-byte marker count, then 8-byte
    markers (a float position in seconds, then a float BPM for the last marker
    or a beat count for the others), all big-endian.
    """
    tags = load_id3(path)
    frames = tags.getall("GEOB:Serato BeatGrid") if tags is not None else []

    if not frames or len(frames[0].data) < 6 + 8:
        return None

    data = frames[0].data
    count = struct.unpack(">I", data[2:6])[0]

    if data[:2] != b"\x01\x00" or count < 1 or len(data) < 6 + 8 * count:
        return None

    first = struct.unpack(">f", data[6:10])[0]
    last_bpm = struct.unpack(">f", data[6 + 8 * (count - 1) + 4 : 6 + 8 * count])[0]

    return (first * 1000.0, last_bpm) if last_bpm > 0 else None


# ---------------------------------------------------------------------------
# Locating the tag's bytes, for editing it in place.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Markers2Location:
    """Where the Markers2 payload sits in the file, and what it holds now."""

    offset: int  # absolute file offset of the first payload byte
    data: bytes  # the payload as it is now

    @property
    def length(self) -> int:
        return len(self.data)


def _syncsafe(raw: bytes) -> int:
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


def _wav_id3_chunk(handle) -> tuple[int, int] | None:
    """(offset, size) of the `id3 ` chunk in a WAV file, found by seeking chunk headers."""
    handle.seek(0)
    riff = handle.read(12)

    if riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
        raise UnsupportedFileError("Not a WAV file.")

    while True:
        header = handle.read(8)

        if len(header) < 8:
            return None

        size = struct.unpack("<I", header[4:])[0]

        if header[:4].lower() == b"id3 ":
            return handle.tell(), size

        handle.seek(size + (size & 1), 1)


def locate_markers2(path: str | Path) -> Markers2Location | None:
    """Find the Markers2 payload so it can be overwritten without moving anything.

    Returns None if the file has no such tag. Raises UnsupportedFileError for
    layouts that can't be edited safely in place (unsynchronised or compressed
    frames, UTF-16 descriptions, several Markers2 frames, ...).
    """
    return locate_frame(path, DESCRIPTION)


def locate_legacy_markers(path: str | Path) -> Markers2Location | None:
    """The same for the older `Serato Markers_` frame (see serato_markers.py)."""
    return locate_frame(path, LEGACY_DESCRIPTION)


def locate_frame(path: str | Path, description: str) -> Markers2Location | None:
    """Find the payload of the GEOB frame with this description, for in-place editing."""
    file_path = Path(path).expanduser()
    suffix = file_path.suffix.lower()

    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedFileError(
            f"{file_path.name}: only MP3 and WAV files are supported for Serato cues so far."
        )

    try:
        with open(file_path, "rb") as handle:
            if suffix == ".wav":
                chunk = _wav_id3_chunk(handle)

                if chunk is None:
                    return None

                base, limit = chunk
            else:
                base, limit = 0, None

            handle.seek(base)
            header = handle.read(10)

            if header[:3] != b"ID3":
                return None

            version, flags = header[3], header[5]

            if version not in (3, 4):
                raise UnsupportedFileError(f"{file_path.name}: ID3v2.{version} tags aren't supported.")

            if flags & 0x80:
                raise UnsupportedFileError(f"{file_path.name}: the tag is unsynchronised, so it can't be edited in place.")

            size = _syncsafe(header[6:10])

            if limit is not None:
                size = min(size, limit - 10)

            tag = handle.read(size)
    except OSError as error:
        raise UnsupportedFileError(f"Could not read {file_path.name}: {error.strerror}") from None

    position = 0

    if flags & 0x40:  # extended header
        ext = struct.unpack(">I", tag[:4])[0] if version == 3 else _syncsafe(tag[:4])
        position = ext + 4 if version == 3 else ext

    found: list[Markers2Location] = []

    while position + 10 <= len(tag) and tag[position] != 0:
        frame_id = tag[position : position + 4]
        raw_size = tag[position + 4 : position + 8]
        frame_size = struct.unpack(">I", raw_size)[0] if version == 3 else _syncsafe(raw_size)
        frame_flags = tag[position + 8 : position + 10]
        body_start = position + 10
        body = tag[body_start : body_start + frame_size]
        position = body_start + frame_size

        if frame_id != b"GEOB" or len(body) < frame_size:
            continue

        parts = body[1:].split(b"\x00", 3)

        if len(parts) < 4 or parts[2].decode("latin-1") != description:
            continue

        if body[0] not in (0, 3):
            raise UnsupportedFileError(f"{file_path.name}: the {description} frame uses an unsupported text encoding.")

        if frame_flags != b"\x00\x00":
            raise UnsupportedFileError(
                f"{file_path.name}: the {description} frame is compressed, encrypted or unsynchronised."
            )

        data = parts[3]
        found.append(Markers2Location(offset=base + 10 + body_start + frame_size - len(data), data=data))

    if len(found) > 1:
        raise UnsupportedFileError(f"{file_path.name}: has {len(found)} {description} frames.")

    return found[0] if found else None
