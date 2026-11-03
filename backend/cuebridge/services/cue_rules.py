"""Rules for turning memory cues into hot cues.

Pure functions on parsed cues, shared by every writer (Rekordbox XML, Serato
tags), so they all assign the same slots.
"""

from dataclasses import dataclass

from cuebridge.models import Cue

# Pads on Rekordbox and Serato: slots 0-7, shown as 1-8 (or A-H).
MAX_HOT_CUE_SLOTS = 8


@dataclass(frozen=True)
class HotCueAssignment:
    """A memory cue and the free hot cue slot its duplicate should go in."""

    cue: Cue
    slot: int


@dataclass(frozen=True)
class DuplicationResult:
    assignments: tuple[HotCueAssignment, ...]
    # Memory cues skipped because a hot cue already sits at the same time.
    already_duplicated: int
    # Memory cues that found no free slot.
    overflow: tuple[Cue, ...]


def plan_memory_to_hot(cues: list[Cue], max_slots: int = MAX_HOT_CUE_SLOTS) -> DuplicationResult:
    """Pick a free hot cue slot for each memory cue that has no hot cue yet.

    - Memory cues are handled in time order and take the lowest free slot.
    - Slots used by hot cues and hot loops are never reused.
    - A memory cue is skipped if a hot cue is already at its exact time, which
      also makes running this twice a no-op.
    - Loops are left alone, and so are memory cues at a time another memory cue
      already took.
    """
    taken = {c.cueIndex for c in cues if c.cueType in ("hotcue", "loop") and c.cueIndex is not None}
    covered = {c.timeMs for c in cues if c.cueType == "hotcue"}

    assignments: list[HotCueAssignment] = []
    overflow: list[Cue] = []
    already = 0

    for cue in sorted((c for c in cues if c.cueType == "memory"), key=lambda c: c.timeMs):
        if cue.timeMs in covered:
            already += 1
            continue

        free = next((s for s in range(max_slots) if s not in taken), None)

        if free is None:
            overflow.append(cue)
            continue

        taken.add(free)
        covered.add(cue.timeMs)
        assignments.append(HotCueAssignment(cue, free))

    return DuplicationResult(tuple(assignments), already, tuple(overflow))
