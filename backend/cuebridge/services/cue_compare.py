"""Compare Rekordbox's cues with the cues in Serato's tags, without changing anything.

It answers the questions the sync can't: which tracks have cues in one program and not
the other, which Serato cues Rekordbox lacks (the sync would overwrite those), and where
the two disagree on a position, a colour or a name.

"What Serato should hold" is worked out by the sync's own rules (`serato_sync.serato_cues_for`:
hot cues in their slots, memory cues in the free slots, the timing correction for the file
type, a cue within tolerance left as it is), so a track reported as matching is one the sync
would leave alone, and a difference is one the sync would change.
"""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from cuebridge.models import Track
from cuebridge.parsers.serato_markers import CueEntry, MarkersError
from cuebridge.parsers.serato_tags import UnsupportedFileError, read_markers2
from cuebridge.services import serato_sync
from cuebridge.services.cue_timing import refine_with_beat_grids, timing_offset
from cuebridge.services.library_queries import format_time

# What a difference is.
MISSING_IN_SERATO = "missing_in_serato"  # Rekordbox has the cue, Serato's slot is empty
ONLY_IN_SERATO = "only_in_serato"  # Serato has a cue Rekordbox doesn't (a sync would remove it)
MOVED = "moved"  # both have it, at different times
COLOR = "color"  # both have it, Rekordbox has a colour chosen and Serato's differs
NAME = "name"  # both have it, with different names

# How a track compares.
MATCH = "match"
DIFFERS = "differs"
REKORDBOX_ONLY = "rekordbox_only"  # cues in Rekordbox, none in Serato
SERATO_ONLY = "serato_only"  # cues in Serato, none in Rekordbox
NO_CUES = "no_cues"  # none in either
SKIPPED = "skipped"  # the file couldn't be read


@dataclass(frozen=True)
class CueDifference:
    slot: int  # 1-8, as shown on the pads
    kind: str
    rekordbox: str | None  # what Rekordbox has there, for showing
    serato: str | None
    delta_ms: int | None = None  # Serato's position minus the one Rekordbox implies, for MOVED


@dataclass(frozen=True)
class TrackComparison:
    track_id: str
    title: str | None
    artist: str | None
    path: Path | None
    status: str
    reason: str | None = None  # why it was skipped, or a note
    differences: tuple[CueDifference, ...] = ()
    rekordbox_cues: int = 0  # hot cues, plus memory cues that found a slot
    serato_cues: int = 0
    loops_not_compared: int = 0  # Serato's loop entries aren't read yet
    memory_cues_without_slot: int = 0
    relocated: bool = False


@dataclass
class CueComparison:
    items: list[TrackComparison] = field(default_factory=list)

    def with_status(self, *statuses: str) -> list[TrackComparison]:
        return [i for i in self.items if i.status in statuses]

    @property
    def counts(self) -> Counter:
        return Counter(i.status for i in self.items)

    @property
    def difference_kinds(self) -> Counter:
        return Counter(d.kind for i in self.items for d in i.differences)

    @property
    def skip_reasons(self) -> Counter:
        return Counter(i.reason for i in self.items if i.status == SKIPPED)

    @property
    def loops_not_compared(self) -> int:
        return sum(i.loops_not_compared for i in self.items)


def _describe(cue: CueEntry) -> str:
    name = f' "{cue.name}"' if cue.name else ""
    return f"{format_time(cue.position_ms)} {cue.color}{name}"


def _differences(
    desired: dict[int, CueEntry], existing: dict[int, CueEntry], loops: dict[int, int] | None = None
) -> list[CueDifference]:
    """`loops` maps a slot to the start (ms) of a Rekordbox hot loop there: loops aren't copied to Serato yet."""
    found = []
    loops = loops or {}

    for slot in sorted(desired.keys() | existing.keys()):
        want, have = desired.get(slot), existing.get(slot)

        if have is None:
            found.append(CueDifference(slot + 1, MISSING_IN_SERATO, _describe(want), None))
        elif want is None:
            mine = f"a loop at {format_time(loops[slot])} (loops aren't copied to Serato yet)" if slot in loops else None
            found.append(CueDifference(slot + 1, ONLY_IN_SERATO, mine, _describe(have)))
        else:
            if want.position_ms != have.position_ms:
                found.append(CueDifference(
                    slot + 1, MOVED, format_time(want.position_ms), format_time(have.position_ms),
                    have.position_ms - want.position_ms,
                ))

            if want.color != have.color:
                found.append(CueDifference(slot + 1, COLOR, want.color, have.color))

            if want.name != have.name:
                found.append(CueDifference(slot + 1, NAME, want.name or None, have.name or None))

    return found


def compare_track(
    track: Track,
    moved_files: dict[str, str] | None = None,
    include_memory_cues: bool = True,
) -> TrackComparison:
    """How one track's cues compare. Reads only the audio file's tag; never writes."""
    loops = sum(1 for c in track.cues if c.cueType == "loop")

    def result(status: str, reason: str | None = None, **extra) -> TrackComparison:
        return TrackComparison(
            track.trackId, track.trackTitle, track.artist, path, status, reason,
            loops_not_compared=loops, relocated=relocated, **extra,
        )

    path = Path(track.filePath) if track.filePath else None
    relocated = False

    if path is None:
        return result(SKIPPED, "the track has no file path")

    if not path.is_absolute():
        return result(SKIPPED, "not a local file (a streaming track)")

    if not path.is_file() and moved_files and track.trackId in moved_files:
        path, relocated = Path(moved_files[track.trackId]), True

    if path.suffix.lower() not in (".mp3", ".wav"):
        return result(SKIPPED, f"{path.suffix.lower() or 'this'} files aren't supported yet")

    if not path.is_file():
        return result(SKIPPED, "the file wasn't found at the Rekordbox path")

    try:
        entries = read_markers2(path)
    except (UnsupportedFileError, MarkersError) as error:
        return result(SKIPPED, str(error).split(": ", 1)[-1])

    existing = {e.index: e for e in entries or [] if isinstance(e, CueEntry)}
    desired: dict[int, CueEntry] = {}
    without_slot = 0

    if track.cues:
        try:
            offset = refine_with_beat_grids(timing_offset(path), path, track.firstBeatMs)
            cues, without_slot = serato_sync.serato_cues_for(
                track, offset, existing, include_memory_cues, serato_sync.DEFAULT_COLOR
            )
        except (OSError, UnsupportedFileError, MarkersError) as error:
            return result(SKIPPED, str(error))

        desired = {c.index: c for c in cues}

    loop_slots = {c.cueIndex: c.timeMs for c in track.cues if c.cueType == "loop" and c.cueIndex is not None}
    found = tuple(_differences(desired, existing, loop_slots))
    counts = {"rekordbox_cues": len(desired), "serato_cues": len(existing), "memory_cues_without_slot": without_slot}

    if not desired and not existing:
        return result(NO_CUES, **counts)

    if entries is None:
        return result(REKORDBOX_ONLY, "no Serato cue tag yet (load the track in Serato once)", differences=found, **counts)

    if not existing:
        return result(REKORDBOX_ONLY, differences=found, **counts)

    if not desired:
        return result(SERATO_ONLY, differences=found, **counts)

    return result(DIFFERS if found else MATCH, differences=found, **counts)


def compare_cues(
    tracks: list[Track],
    moved_files: dict[str, str] | None = None,
    include_memory_cues: bool = True,
) -> CueComparison:
    """Compare every given track (the caller chooses which tracks are worth reading)."""
    return CueComparison([compare_track(t, moved_files, include_memory_cues) for t in tracks])
