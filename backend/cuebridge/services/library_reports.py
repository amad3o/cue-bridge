"""Read-only reports that compare the Rekordbox and Apple Music libraries."""

import os
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from cuebridge.models import Track
from cuebridge.parsers.apple_music_xml import MusicLibrary, MusicTrack
from cuebridge.services.track_matcher import AppleMusicIndex, MatchResult, normalize_path


@dataclass(frozen=True)
class MissingFile:
    track: Track
    match: MatchResult
    suggested_path: str | None  # where the file really is, when Apple Music knows and it exists


def find_missing_rekordbox_files(
    tracks: list[Track],
    music: MusicLibrary,
    only_with_cues: bool = True,
    exists: Callable[[str], bool] = os.path.exists,
) -> list[MissingFile]:
    """Rekordbox tracks whose file isn't at the stored path, with where Apple Music has it."""
    index = AppleMusicIndex(music)
    found = []

    for track in tracks:
        # A path that doesn't start with / is a streaming track (tidal:tracks:123), not a lost file.
        if not track.filePath or not track.filePath.startswith("/") or exists(track.filePath):
            continue

        if only_with_cues and not track.cues:
            continue

        match = index.match(track)
        suggestion = None

        if match.track and match.track.path and exists(match.track.path):
            if normalize_path(match.track.path) != normalize_path(track.filePath):
                suggestion = match.track.path

        found.append(MissingFile(track, match, suggestion))

    return found


def moved_file_paths(missing: list[MissingFile]) -> dict[str, str]:
    """track ID -> the path to use instead, for the tracks whose real file was found."""
    return {m.track.trackId: m.suggested_path for m in missing if m.suggested_path}


def playlist_summaries(music: MusicLibrary) -> list[dict]:
    return [
        {"name": p.name, "tracks": len(p.track_ids), "isFolder": p.is_folder}
        for p in music.user_playlists
    ]


def music_library_summary(music: MusicLibrary) -> dict:
    kinds = Counter(t.kind or "unknown" for t in music.tracks)
    added = [t.date_added for t in music.tracks if t.date_added]

    return {
        "path": str(music.path) if music.path else None,
        "musicFolder": music.music_folder,
        "trackCount": len(music.tracks),
        "tracksWithoutArtist": sum(1 for t in music.tracks if not t.artist),
        "tracksWithoutBpm": sum(1 for t in music.tracks if not t.bpm),
        "tracksWithoutLocalFile": sum(1 for t in music.tracks if not t.path),
        "totalHours": round(sum(t.duration_ms or 0 for t in music.tracks) / 3_600_000, 1),
        "totalGigabytes": round(sum(t.size or 0 for t in music.tracks) / 1e9, 1),
        "formats": dict(kinds.most_common()),
        "oldestAdded": min(added).date().isoformat() if added else None,
        "newestAdded": max(added).date().isoformat() if added else None,
        "playlistCount": len(music.user_playlists),
        "playlists": playlist_summaries(music),
    }


def music_track_to_dict(track: MusicTrack) -> dict:
    return {
        "trackId": track.track_id,
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "genre": track.genre,
        "bpm": track.bpm,
        "kind": track.kind,
        "path": track.path,
    }
