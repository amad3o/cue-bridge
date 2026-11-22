"""Timing differences between Rekordbox and Serato cue positions.

Measured on a real library by comparing cues that an earlier sync had copied
from Rekordbox into Serato (Rekordbox time minus Serato time):

    WAV                                    0 ms      (27 cues, all exactly 0)
    MP3 with a LAME header                ~5.5 ms    (5 or 6 ms)
    MP3 without any Xing/Info frame       ~5.5 ms    (5 or 6 ms, 6 files)
    MP3 with an Xing/Info frame, no LAME  -26 ms     (three files, one MP3 frame)
                                          -52 ms     (one file, two frames)

For the last group the shift is a whole number of MP3 frames, but not always the
same number. The beat grids of both programs tell which: Serato's first beat
minus Rekordbox's came out at +24, +26, +27 and +53 ms on those four files, and
a cue placed by ear in Serato matched the two-frame one to within 6 ms. So for
those files the offset is estimated from the grids and rounded to whole frames,
and the file stays flagged. Without usable grids no correction is applied.
Everything here is empirical, not derived.
"""

import math
from dataclasses import dataclass
from pathlib import Path

from cuebridge.parsers.serato_tags import UnsupportedFileError, read_first_beat

# Serato's position = Rekordbox's position - this.
MP3_OFFSET_MS = 5.5

_FRAME_SEARCH_BYTES = 4096
_FRAME_HEAD_BYTES = 600


@dataclass(frozen=True)
class TimingOffset:
    """How much to subtract from a Rekordbox cue position for Serato."""

    offset_ms: float
    # "measured": the rule comes from many files. "unverified": a guess, so the file is flagged.
    confidence: str
    note: str
    # True when the beat grids can still pin the offset down (see refine_with_beat_grids).
    refinable: bool = False

    @property
    def flagged(self) -> bool:
        return self.confidence != "measured"


def _first_mp3_frame(path: Path) -> bytes | None:
    """The start of the first audio frame, after any ID3v2 tag."""
    with open(path, "rb") as handle:
        head = handle.read(10)
        start = 0

        if head[:3] == b"ID3":
            size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
            start = 10 + size + (10 if head[5] & 0x10 else 0)

        handle.seek(start)
        chunk = handle.read(_FRAME_SEARCH_BYTES + _FRAME_HEAD_BYTES)

    for i in range(min(len(chunk) - 4, _FRAME_SEARCH_BYTES)):
        # MPEG frame sync: 11 set bits, a valid version and layer.
        if chunk[i] == 0xFF and (chunk[i + 1] & 0xE0) == 0xE0 and (chunk[i + 1] >> 3) & 3 != 1 and (chunk[i + 1] >> 1) & 3 != 0:
            return chunk[i : i + _FRAME_HEAD_BYTES]

    return None


def timing_offset(path: str | Path) -> TimingOffset:
    """The Rekordbox-to-Serato timing offset for this audio file."""
    file_path = Path(path)
    suffix = file_path.suffix.lower()

    if suffix == ".wav":
        return TimingOffset(0.0, "measured", "WAV: both programs agree exactly.")

    if suffix != ".mp3":
        return TimingOffset(0.0, "unverified", f"No timing data for {suffix} files.")

    try:
        frame = _first_mp3_frame(file_path)
    except OSError:
        frame = None

    if frame is None:
        return TimingOffset(0.0, "unverified", "Could not find the first MP3 frame.")

    has_info_frame = b"Xing" in frame or b"Info" in frame
    has_lame = b"LAME" in frame

    if has_info_frame and not has_lame:
        return TimingOffset(
            0.0,
            "unverified",
            "MP3 with an Xing/Info frame but no LAME tag: the programs differed by about 26 ms "
            "in the few files measured, so no correction was applied.",
            refinable=True,
        )

    kind = "with a LAME header" if has_lame else "with no Xing/Info frame"
    return TimingOffset(MP3_OFFSET_MS, "measured", f"MP3 {kind}: Serato is about {MP3_OFFSET_MS} ms earlier.")


MAX_FRAMES = 3
GRID_TOLERANCE_MS = 8.0  # how far the grid difference may be from a whole number of frames
_SAMPLE_RATES = (44100, 48000, 32000)


def _frame_ms(frame: bytes) -> float | None:
    """Length of one MPEG-1 Layer III frame in ms, from the first frame's header."""
    if len(frame) < 4 or (frame[1] >> 3) & 3 != 3 or (frame[1] >> 1) & 3 != 1:
        return None

    rate_index = (frame[2] >> 2) & 3

    if rate_index > 2:
        return None

    return 1152 / _SAMPLE_RATES[rate_index] * 1000.0


def refine_with_beat_grids(offset: TimingOffset, path: str | Path, rekordbox_first_beat_ms: int | None) -> TimingOffset:
    """Estimate the offset of an Xing/Info-without-LAME MP3 from the two beat grids.

    Serato's first beat minus Rekordbox's (taken modulo one beat) should be a
    whole number of MP3 frames. If it is, that many frames is the offset;
    otherwise the offset is returned unchanged.
    """
    if not offset.refinable or rekordbox_first_beat_ms is None:
        return offset

    try:
        grid = read_first_beat(path)
        frame = _first_mp3_frame(Path(path))
    except (OSError, UnsupportedFileError):
        return offset

    frame_ms = _frame_ms(frame) if frame else None

    if grid is None or frame_ms is None:
        return offset

    first_beat_ms, bpm = grid
    period = 60000.0 / bpm
    difference = (first_beat_ms - rekordbox_first_beat_ms + period / 2) % period - period / 2
    frames = round(difference / frame_ms)

    if not 1 <= frames <= MAX_FRAMES or abs(difference - frames * frame_ms) > GRID_TOLERANCE_MS:
        return offset

    shift = frames * frame_ms

    return TimingOffset(
        -shift,
        "estimated",
        f"MP3 with an Xing/Info frame but no LAME tag: the beat grids put Serato {frames} MP3 "
        f"frame{'s' if frames > 1 else ''} ({shift:.0f} ms) later than Rekordbox. Estimated, not measured.",
    )


def to_serato_ms(rekordbox_ms: int, offset: TimingOffset) -> int:
    """A Rekordbox cue position converted to Serato's: rounded half up, never below zero."""
    return max(0, math.floor(rekordbox_ms - offset.offset_ms + 0.5))
