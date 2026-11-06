"""Rekordbox's cues, read from its database instead of an XML export.

Rekordbox's own database is always current; the XML export is only as fresh as the
last export. This turns a database snapshot into the same `Track` objects the XML
parser makes, so the Serato sync and the checks work from either.

How the database stores a cue (`djmdCue`):
  * Kind 0 is a memory cue. Kinds 1, 2 and 3 are hot cues A, B and C; Kind 4 is not a
    hot cue; Kinds 5 and 6 are D and E, and 7, 8 and 9 are F, G and H (all checked against a real
    export). Nothing is known about Kinds 4 and 10 and up, so those cues are left out and reported, not guessed into
    a slot. A hot cue that is also a memory cue has two rows at the same position.
  * OutMsec above 0 makes it a loop.
  * A hot cue's colour is an index into Rekordbox's own colour table. 0 or empty is
    Rekordbox's default (the green the XML calls #28e214), which the Serato sync
    already treats as "no colour chosen". CueBridge only knows the colour of the
    indexes listed in COLOR_TABLE (checked against real exports); any other index
    is reported and left without a colour rather than guessed.
The first beat comes from the beat grid in the track's analysis file.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from cuebridge.models import Cue, Track
from cuebridge.services.rekordbox_db import DbCue, DbSnapshot

# Colour index -> colour, only for the ones confirmed by comparing the database with an XML export
# (41 earlier; the other 16 from a test track whose cues were each given a different colour).
COLOR_TABLE = {
    41: "#e02823",
    1: "#305aff",
    5: "#50b4ff",
    9: "#00e0ff",
    14: "#1fa392",
    18: "#10b176",
    22: "#28e214",
    26: "#a5e116",
    30: "#b4be04",
    32: "#c3af04",
    38: "#e0641b",
    42: "#e62828",
    45: "#ff127b",
    49: "#de44cf",
    56: "#b432ff",
    60: "#aa72ff",
    62: "#6473ff",
}
# Cue kind -> hot cue slot (0 is A), for the kinds checked against a real export (7-9 are F-H).
HOT_CUE_SLOTS = {1: 0, 2: 1, 3: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7}
DEFAULT_SHARE = Path("~/Library/Pioneer/rekordbox/share")


@dataclass
class DbLibrary:
    tracks: list[Track]
    tracks_by_id: dict[str, Track]
    unknown_colors: list[tuple[str, int]] = field(default_factory=list)  # (track, colour index) left without a colour
    unverified_cues: list[tuple[str, int]] = field(default_factory=list)  # (track, cue kind) left out
    beat_grids_missing: int = 0  # tracks with cues whose beat grid couldn't be read


def read_first_beat_ms(analysis_path: str | None, share: Path = DEFAULT_SHARE) -> int | None:
    """Where the beat grid puts the first beat, from the track's analysis file, or None."""
    if not analysis_path:
        return None

    path = Path(share).expanduser() / analysis_path.lstrip("/")

    if not path.is_file():
        return None

    try:
        from pyrekordbox.anlz import AnlzFile

        times = AnlzFile.parse_file(path).get("beat_grid")[2]
        return int(round(float(times[0]) * 1000)) if len(times) else None
    except Exception:  # a missing PQTZ tag, an unreadable file, pyrekordbox not installed
        return None


def _cue(track_id: str, title, artist, path, row: DbCue) -> tuple[Cue | None, int | None]:
    is_memory = row.kind <= 0

    if not is_memory and row.kind not in HOT_CUE_SLOTS:
        return None, None

    is_loop = row.out_ms is not None
    cue_type = "loop" if is_loop else "memory" if is_memory else "hotcue"
    index = None if is_memory else HOT_CUE_SLOTS[row.kind]
    unknown = None
    color = None

    if not is_memory and row.color_index:
        color = COLOR_TABLE.get(row.color_index)
        unknown = row.color_index if color is None else None

    return Cue(
        id=f"rekordbox-{track_id}-{cue_type}-{-1 if is_memory else index}-{row.position_ms}",
        source="rekordbox", trackTitle=title, artist=artist, filePath=path,
        cueIndex=index, cueName=row.comment, timeMs=row.position_ms,
        endMs=row.out_ms if is_loop else None, color=color, cueType=cue_type,
    ), unknown


def tracks_from_snapshot(
    snapshot: DbSnapshot,
    first_beat: Callable[[str | None], int | None] | None = None,
) -> DbLibrary:
    """Every track in the database as a `Track`, with its cues, first beat and length."""
    first_beat = first_beat or read_first_beat_ms
    by_content: dict[str, list[DbCue]] = {}

    for cue in snapshot.cues:
        by_content.setdefault(cue.content_id, []).append(cue)

    tracks, unknown, unverified, no_grid = [], [], [], 0

    for content in snapshot.contents:
        rows = sorted(by_content.get(content.id, []), key=lambda c: (c.position_ms, c.kind))
        cues = []

        for row in rows:
            cue, bad_color = _cue(content.id, content.title, content.artist, content.path, row)

            if cue is None:
                unverified.append((content.title or content.id, row.kind))
                continue

            cues.append(cue)

            if bad_color is not None:
                unknown.append((content.title or content.id, bad_color))

        beat = first_beat(content.analysis_path) if cues else None

        if cues and beat is None:
            no_grid += 1

        tracks.append(Track(
            trackId=content.id, trackTitle=content.title, artist=content.artist, filePath=content.path,
            firstBeatMs=beat, durationMs=content.duration_ms, cues=cues,
        ))

    return DbLibrary(tracks, {t.trackId: t for t in tracks}, unknown, unverified, no_grid)
