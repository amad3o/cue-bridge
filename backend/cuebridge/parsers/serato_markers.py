"""Codec for the `Serato Markers2` tag payload.

Serato stores hot cues in a GEOB frame (ID3) called "Serato Markers2". The data
after the frame's description is laid out as:

    01 01  +  base64 text (lines of 72 characters, no "=" padding)  +  NUL padding

padded so the whole payload is exactly 470 bytes. The base64 text decodes to:

    01 01  +  entries  +  00

where each entry is `name NUL`, a 4-byte big-endian length, then the payload.
Seen in real files: COLOR, CUE and BPMLOCK. A CUE payload is 13 bytes: a zero
byte, the slot (0-based), the position in ms (4 bytes, big-endian), a zero byte,
the RGB color (3 bytes), two zero bytes, and the name followed by NUL.

This module is pure: bytes in, entries out, and back. It must reproduce every
real tag byte for byte, so anything it can't model exactly is kept verbatim as
a RawEntry instead of being reinterpreted.
"""

import base64
import re
import struct
from dataclasses import dataclass

DESCRIPTION = "Serato Markers2"
FRAME_SIZE = 470  # every real tag we've seen has exactly this many bytes of payload
LINE_WIDTH = 72

_PREFIX = b"\x01\x01"
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class MarkersError(ValueError):
    """The Markers2 data is malformed, or can't be written."""


@dataclass(frozen=True)
class CueEntry:
    """A hot cue. `index` is the 0-based pad slot, `color` is "#rrggbb"."""

    index: int
    position_ms: int
    color: str
    name: str = ""

    def __post_init__(self):
        if not 0 <= self.index <= 255:
            raise MarkersError(f"Cue slot {self.index} is out of range.")

        if not 0 <= self.position_ms <= 0xFFFFFFFF:
            raise MarkersError(f"Cue position {self.position_ms} ms is out of range.")

        if not _COLOR.match(self.color):
            raise MarkersError(f"Cue color {self.color!r} must look like #rrggbb.")

        if "\x00" in self.name:
            raise MarkersError("A cue name can't contain a NUL character.")


@dataclass(frozen=True)
class RawEntry:
    """Any other entry (COLOR, BPMLOCK, LOOP, ...), kept exactly as read."""

    name: str
    payload: bytes


Entry = CueEntry | RawEntry


def _cue_payload(cue: CueEntry) -> bytes:
    red, green, blue = (int(cue.color[i : i + 2], 16) for i in (1, 3, 5))

    return (
        b"\x00"
        + bytes([cue.index])
        + struct.pack(">I", cue.position_ms)
        + b"\x00"
        + bytes([red, green, blue])
        + b"\x00\x00"
        + cue.name.encode("utf-8")
        + b"\x00"
    )


def _parse_cue(payload: bytes) -> Entry:
    """Read a CUE payload, or keep it verbatim if re-encoding wouldn't match it."""
    if len(payload) < 13 or not payload.endswith(b"\x00"):
        return RawEntry("CUE", payload)

    try:
        cue = CueEntry(
            index=payload[1],
            position_ms=struct.unpack(">I", payload[2:6])[0],
            color="#%02x%02x%02x" % tuple(payload[7:10]),
            name=payload[12:-1].decode("utf-8"),
        )
    except (MarkersError, UnicodeDecodeError):
        return RawEntry("CUE", payload)

    return cue if _cue_payload(cue) == payload else RawEntry("CUE", payload)


def decode_markers2(data: bytes) -> list[Entry]:
    """Decode the payload that follows the frame description."""
    if data[:2] != _PREFIX:
        raise MarkersError("Markers2 data doesn't start with 01 01.")

    text = data[2:].split(b"\x00", 1)[0].replace(b"\n", b"").replace(b"\r", b"")

    try:
        raw = base64.b64decode(text + b"=" * (-len(text) % 4), validate=True)
    except ValueError:
        raise MarkersError("Markers2 data isn't valid base64.") from None

    if raw[:2] != _PREFIX:
        raise MarkersError("Decoded Markers2 data doesn't start with 01 01.")

    entries: list[Entry] = []
    position = 2

    while position < len(raw) and raw[position] != 0:
        end = raw.find(b"\x00", position)

        if end < 0 or end + 5 > len(raw):
            raise MarkersError("Markers2 data ends inside an entry header.")

        name = raw[position:end].decode("latin-1")
        (length,) = struct.unpack(">I", raw[end + 1 : end + 5])
        payload = raw[end + 5 : end + 5 + length]

        if len(payload) != length:
            raise MarkersError(f"Markers2 entry {name!r} is shorter than its length says.")

        entries.append(_parse_cue(payload) if name == "CUE" else RawEntry(name, payload))
        position = end + 5 + length

    return entries


def encode_markers2(entries: list[Entry], size: int = FRAME_SIZE) -> bytes:
    """Encode entries as a Markers2 payload of exactly `size` bytes."""
    body = bytearray(_PREFIX)

    for entry in entries:
        name, payload = ("CUE", _cue_payload(entry)) if isinstance(entry, CueEntry) else (entry.name, entry.payload)
        body += name.encode("latin-1") + b"\x00" + struct.pack(">I", len(payload)) + payload

    body += b"\x00"

    text = base64.b64encode(bytes(body)).rstrip(b"=")
    wrapped = b"\n".join(text[i : i + LINE_WIDTH] for i in range(0, len(text), LINE_WIDTH))
    data = _PREFIX + wrapped

    if len(data) > size:
        raise MarkersError(
            f"These cues need {len(data)} bytes, but Serato's Markers2 tag holds {size}. "
            "Fewer or shorter cue names would fit."
        )

    return data + b"\x00" * (size - len(data))


def replace_cues(entries: list[Entry], cues: list[CueEntry]) -> list[Entry]:
    """Return `entries` with all cues replaced by `cues`, sorted by slot.

    The new cues go where the first old cue was, or just after COLOR if there
    were none, so the entry order stays what Serato writes. Every other entry
    is kept as is.
    """
    if len({c.index for c in cues}) != len(cues):
        raise MarkersError("Two cues share the same slot.")

    def is_cue(entry: Entry) -> bool:
        return isinstance(entry, CueEntry) or entry.name == "CUE"

    new = sorted(cues, key=lambda c: c.index)
    kept = [e for e in entries if not is_cue(e)]
    first_cue = next((i for i, e in enumerate(entries) if is_cue(e)), None)

    if first_cue is not None:
        # Everything before the first cue is kept, so its index is the position.
        insert_at = first_cue
    else:
        insert_at = next((i + 1 for i, e in enumerate(kept) if e.name == "COLOR"), 0)

    return kept[:insert_at] + new + kept[insert_at:]
