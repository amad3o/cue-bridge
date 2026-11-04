"""Where the Rekordbox and Apple Music libraries have drifted apart.

Tracks are paired with the matcher (path, file name, then title and artist).
A Rekordbox track with no clear Apple Music counterpart is "Rekordbox only",
an Apple Music track in a playlist that no Rekordbox track maps to is "Apple Music
only" (tracks in no playlist are kept apart: they aren't meant for Rekordbox), and a
playlist is compared by name and then by the tracks it holds (not their order).
"""

import unicodedata
from dataclasses import dataclass, field

from cuebridge.models import Track
from cuebridge.parsers.apple_music_xml import MusicLibrary, MusicPlaylist, MusicTrack
from cuebridge.parsers.rekordbox_parser import RekordboxPlaylist
from cuebridge.services.track_matcher import AppleMusicIndex, normalize_path

# Rekordbox keeps an empty playlist of this name for its own use.
IGNORED_REKORDBOX_PLAYLISTS = {"cue analysis playlist"}


@dataclass(frozen=True)
class PlaylistDrift:
    name: str
    status: str  # "in_sync", "differs" or "missing_in_rekordbox"
    apple_count: int
    rekordbox_count: int | None
    apple_only: tuple[MusicTrack, ...] = ()  # in the Apple Music playlist, not in the Rekordbox one
    rekordbox_only: tuple[Track, ...] = ()  # in the Rekordbox playlist, not in the Apple Music one


@dataclass
class LibraryDrift:
    matched: int = 0
    apple_only: list[MusicTrack] = field(default_factory=list)  # in a playlist, but not in Rekordbox
    apple_only_unplaylisted: list[MusicTrack] = field(default_factory=list)  # not in Rekordbox, but in no playlist
    rekordbox_only: list[Track] = field(default_factory=list)
    rekordbox_only_in_music_folder: list[Track] = field(default_factory=list)
    ambiguous: list[tuple[Track, tuple[MusicTrack, ...]]] = field(default_factory=list)
    duplicate_entries: list[tuple[MusicTrack, ...]] = field(default_factory=list)  # one file, several entries
    playlists: list[PlaylistDrift] = field(default_factory=list)
    rekordbox_only_playlists: list[RekordboxPlaylist] = field(default_factory=list)


def _same_name(a: str, b: str) -> bool:
    return unicodedata.normalize("NFC", a).casefold().strip() == unicodedata.normalize("NFC", b).casefold().strip()


def _compare_playlist(
    apple: MusicPlaylist,
    rekordbox: RekordboxPlaylist | None,
    music: MusicLibrary,
    rb_for_apple: dict[int, list[str]],
    apple_entries_for_rb: dict[str, set[int]],
    rb_tracks: dict[str, Track],
) -> PlaylistDrift:
    if rekordbox is None:
        return PlaylistDrift(apple.name, "missing_in_rekordbox", len(apple.track_ids), None)

    in_rekordbox = set(rekordbox.track_ids)
    apple_ids = set(apple.track_ids)
    apple_only = tuple(
        music.tracks_by_id[i] for i in dict.fromkeys(apple.track_ids)
        if i in music.tracks_by_id and not in_rekordbox.intersection(rb_for_apple.get(i, ()))
    )
    rekordbox_only = tuple(
        rb_tracks[k] for k in dict.fromkeys(rekordbox.track_ids)
        if k in rb_tracks and not apple_ids.intersection(apple_entries_for_rb.get(k, ()))
    )
    status = "in_sync" if not apple_only and not rekordbox_only else "differs"

    return PlaylistDrift(apple.name, status, len(apple.track_ids), len(rekordbox.track_ids), apple_only, rekordbox_only)


def compare_libraries(
    rekordbox_tracks: list[Track],
    rekordbox_playlists: list[RekordboxPlaylist],
    music: MusicLibrary,
) -> LibraryDrift:
    index = AppleMusicIndex(music)
    drift = LibraryDrift()
    rb_for_apple: dict[int, list[str]] = {}
    apple_entries_for_rb: dict[str, set[int]] = {}
    folder = normalize_path(music.music_folder)

    for track in rekordbox_tracks:
        result = index.match(track)

        if result.track is not None:
            drift.matched += 1

            for entry in (result.track, *result.same_file):
                rb_for_apple.setdefault(entry.track_id, []).append(track.trackId)
                apple_entries_for_rb.setdefault(track.trackId, set()).add(entry.track_id)
        elif result.method == "ambiguous":
            drift.ambiguous.append((track, result.candidates))
        else:
            drift.rekordbox_only.append(track)

            if folder and normalize_path(track.filePath).startswith(folder):
                drift.rekordbox_only_in_music_folder.append(track)

    playlisted = {i for p in music.user_playlists if not p.is_folder for i in p.track_ids}

    for track in music.tracks:
        if track.track_id not in rb_for_apple:
            (drift.apple_only if track.track_id in playlisted else drift.apple_only_unplaylisted).append(track)

    drift.duplicate_entries = index.duplicate_files()

    rb_tracks = {t.trackId: t for t in rekordbox_tracks}
    seen = set()

    for apple in music.user_playlists:
        if apple.is_folder:
            continue

        match = next((p for p in rekordbox_playlists if _same_name(p.name, apple.name)), None)
        seen.add(id(match))
        drift.playlists.append(_compare_playlist(apple, match, music, rb_for_apple, apple_entries_for_rb, rb_tracks))

    drift.rekordbox_only_playlists = [
        p for p in rekordbox_playlists
        if id(p) not in seen and p.name.casefold() not in IGNORED_REKORDBOX_PLAYLISTS
    ]

    return drift
