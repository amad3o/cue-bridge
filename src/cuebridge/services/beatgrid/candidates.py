"""Which Rekordbox tracks to analyse for a beatgrid, and how the analysis compares with the grid Rekordbox already has."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cuebridge.services.rekordbox.db import DbContent, DbSnapshot


@dataclass(frozen=True)
class Candidates:
    tracks: list[DbContent]  # to analyse, in a stable order so that offset/limit paging is repeatable
    not_playlisted: int  # left out for being in no playlist
    already_gridded: int  # left out because Rekordbox has a beat grid for them
    file_missing: int  # left out because the audio file isn't there
    existing_first_beat_ms: dict[str, int]  # content id -> Rekordbox's first beat, for the tracks that were kept with a grid


def select_candidates(
    snapshot: DbSnapshot,
    first_beat: Callable[[str | None], int | None],
    playlisted_only: bool = True,
    include_with_grid: bool = False,
    file_exists: Callable[[str], bool] = lambda p: Path(p).is_file(),
) -> Candidates:
    """The tracks to analyse. `first_beat` reads Rekordbox's first beat from an analysis path (None: no grid)."""
    playlisted = {c for p in snapshot.playlists if p.is_plain for c in p.content_ids}
    kept, not_playlisted, gridded, missing = [], 0, 0, 0
    existing: dict[str, int] = {}

    for content in sorted(snapshot.contents, key=lambda c: (c.artist or "", c.title or "", c.id)):
        if playlisted_only and content.id not in playlisted:
            not_playlisted += 1
            continue

        beat = first_beat(content.analysis_path)

        if beat is not None and not include_with_grid:
            gridded += 1
            continue

        if not content.path or not file_exists(content.path):
            missing += 1
            continue

        kept.append(content)

        if beat is not None:
            existing[content.id] = beat

    return Candidates(kept, not_playlisted, gridded, missing, existing)


def offset_from_existing(our_first_beat_ms: float, their_first_beat_ms: float, bpm: float) -> float:
    """How far our first beat is from Rekordbox's, in ms, folded to within half a beat (the grids may start on different beats)."""
    period = 60000.0 / bpm
    return round((our_first_beat_ms - their_first_beat_ms + period / 2) % period - period / 2, 1)
