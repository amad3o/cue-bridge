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


# ---------------------------------------------------------------------------
# The legacy `Serato Markers_` frame
# ---------------------------------------------------------------------------
#
# Older Serato versions keep the first five hot cues in a second frame, and
# Serato DJ Pro still reads it: a file whose Markers2 has three cues but whose
# legacy frame has one shows one cue. The layout, as seen in real files:
#
#     02 05  +  u32 entry count (14)  +  14 entries of 22 bytes  +  07 7f 7f 7f
#
# An entry is: start (5 bytes), end (5), 00, 7f x5, color (4), type, lock.
# Numbers use 7 bits per byte, most significant group first. An empty slot has
# start and end 7f x5, color 0 and type 0. A cue has type 01, no end (7f x5)
# and the color as the 3-byte RGB value in that 7-bit form. The first five
# entries are the cue slots; the rest hold loops and are left as they are.

LEGACY_DESCRIPTION = "Serato Markers_"
LEGACY_CUE_SLOTS = 5
_LEGACY_HEADER = b"\x02\x05"
_LEGACY_ENTRY = 22
_LEGACY_COUNT = 14
_LEGACY_TRAILER = 4
LEGACY_SIZE = 2 + 4 + _LEGACY_COUNT * _LEGACY_ENTRY + _LEGACY_TRAILER  # 318
_UNSET = b"\x7f" * 5


def _pack7(value: int, width: int) -> bytes:
    if not 0 <= value < 1 << (7 * width):
        raise MarkersError(f"{value} doesn't fit the legacy marker encoding.")

    return bytes((value >> (7 * shift)) & 0x7F for shift in reversed(range(width)))


def _unpack7(raw: bytes) -> int:
    value = 0

    for byte in raw:
        if byte > 0x7F:
            raise MarkersError("Legacy marker number isn't 7-bit encoded.")

        value = (value << 7) | byte

    return value


def _legacy_cue_entry(cue: CueEntry) -> bytes:
    rgb = int(cue.color[1:], 16)
    return _pack7(cue.position_ms, 5) + _UNSET + b"\x00" + _UNSET + _pack7(rgb, 4) + b"\x01\x00"


_LEGACY_EMPTY = _UNSET + _UNSET + b"\x00" + _UNSET + b"\x00" * 6


def _check_legacy(data: bytes) -> None:
    if len(data) != LEGACY_SIZE or data[:2] != _LEGACY_HEADER or _unpack7(data[2:6]) != _LEGACY_COUNT:
        raise MarkersError("The legacy Serato Markers_ frame has a layout this tool doesn't know.")


def decode_legacy_cues(data: bytes) -> list[CueEntry]:
    """The cues in a legacy frame (slot 0-4). Names aren't stored there."""
    _check_legacy(data)
    cues = []

    for slot in range(LEGACY_CUE_SLOTS):
        entry = data[6 + slot * _LEGACY_ENTRY : 6 + (slot + 1) * _LEGACY_ENTRY]

        if entry[-2] == 1:
            cues.append(CueEntry(slot, _unpack7(entry[:5]), "#%06x" % _unpack7(entry[16:20])))

    return cues


def encode_legacy(existing: bytes, cues: list[CueEntry]) -> bytes:
    """`existing` with its five cue slots made to hold `cues` (those in slots 0-4).

    A slot that already holds the wanted cue (same time and color), or is
    empty and should be, keeps its bytes exactly: Serato leaves odd bytes in
    some empty slots, and they aren't ours to tidy. Loops, the header and the
    trailer are kept as well. Raises MarkersError if a slot that has to change
    holds something other than a plain cue or an empty slot.
    """
    _check_legacy(existing)
    by_slot = {c.index: c for c in cues if c.index < LEGACY_CUE_SLOTS}
    out = bytearray(existing)

    for slot in range(LEGACY_CUE_SLOTS):
        start = 6 + slot * _LEGACY_ENTRY
        old = existing[start : start + _LEGACY_ENTRY]
        wanted = by_slot.get(slot)
        is_cue = old[-2] == 1

        if wanted is None and old[-2] == 0:
            continue  # empty and staying empty

        if wanted is not None and is_cue:
            have = (_unpack7(old[:5]), _unpack7(old[16:20]))

            if have == (wanted.position_ms, int(wanted.color[1:], 16)):
                continue  # already right

        if old[-2] not in (0, 1) or old[5:10] != _UNSET or old[10] != 0:
            raise MarkersError(f"Legacy marker slot {slot + 1} holds something other than a cue.")

        out[start : start + _LEGACY_ENTRY] = _legacy_cue_entry(wanted) if wanted else _LEGACY_EMPTY

    return bytes(out)
