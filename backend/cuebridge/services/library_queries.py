"""Read-only questions about a parsed library.

Everything here returns small, JSON-friendly dicts, because the callers (MCP
tools) hand the result to an AI assistant with a limited context window.
"""

from collections import Counter
from pathlib import PurePath

from cuebridge.models import Cue, Track
from cuebridge.services.rekordbox_service import RekordboxLibrary

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def format_time(ms: int) -> str:
    minutes, rest = divmod(ms, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{minutes}:{seconds:02d}.{millis:03d}"


def cue_to_dict(cue: Cue) -> dict:
    """A cue without the fields repeated from its track."""
    return {
        "type": cue.cueType,
        # Pad number as shown on hardware (1-based). None for memory cues.
        "slot": None if cue.cueIndex is None else cue.cueIndex + 1,
        "name": cue.cueName,
        "time": format_time(cue.timeMs),
        "timeMs": cue.timeMs,
        "endMs": cue.endMs,
        "color": cue.color,
    }


def track_to_summary(track: Track) -> dict:
    return {
        "trackId": track.trackId,
        "title": track.trackTitle,
        "artist": track.artist,
        "cueCount": len(track.cues),
        "filePath": track.filePath,
    }


def track_to_detail(track: Track) -> dict:
    return {**track_to_summary(track), "cues": [cue_to_dict(c) for c in track.cues]}


def library_summary(library: RekordboxLibrary) -> dict:
    tracks = library.tracks
    with_cues = sum(1 for t in tracks if t.cues)
    cue_types = Counter(c.cueType for t in tracks for c in t.cues)

    return {
        "path": str(library.path),
        "trackCount": len(tracks),
        "tracksWithCues": with_cues,
        "tracksWithoutCues": len(tracks) - with_cues,
        "cueCount": sum(cue_types.values()),
        "cuesByType": {k: cue_types.get(k, 0) for k in ("hotcue", "memory", "loop")},
        "tracksWithoutArtist": sum(1 for t in tracks if t.artist is None),
    }


def _clamp(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, MAX_LIMIT)), max(0, offset)


def _matches(track: Track, needle: str) -> bool:
    if not needle:
        return True

    file_name = PurePath(track.filePath).name if track.filePath else ""
    haystack = f"{track.trackTitle or ''} {track.artist or ''} {file_name}".lower()

    return all(word in haystack for word in needle.lower().split())


def search_tracks(
    library: RekordboxLibrary,
    query: str = "",
    has_cues: bool | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """Page through tracks, optionally filtered by text and by having cues.

    Every word of `query` must appear in the title, artist or file name.
    """
    limit, offset = _clamp(limit, offset)

    matching = [
        t
        for t in library.tracks
        if _matches(t, query) and (has_cues is None or bool(t.cues) == has_cues)
    ]
    page = matching[offset : offset + limit]

    return {
        "total": len(matching),
        "offset": offset,
        "returned": len(page),
        "hasMore": offset + len(page) < len(matching),
        "tracks": [track_to_summary(t) for t in page],
    }


def tracks_without_cues(library: RekordboxLibrary) -> list[Track]:
    return [t for t in library.tracks if not t.cues]
