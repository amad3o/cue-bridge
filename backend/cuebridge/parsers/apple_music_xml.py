"""Read an Apple Music (or iTunes) library exported as XML.

In Apple Music choose File > Library > Export Library. The file is a property
list: a `Tracks` dictionary keyed by track ID, and a `Playlists` list whose
items refer to tracks by that ID. This module only turns it into plain
dataclasses; it never touches the audio files.
"""

import plistlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cuebridge.parsers.paths import location_to_path


class AppleMusicParseError(ValueError):
    """The data is a property list but not an Apple Music library export."""


@dataclass(frozen=True)
class MusicTrack:
    track_id: int
    persistent_id: str | None
    title: str | None
    artist: str | None
    album: str | None
    genre: str | None
    kind: str | None
    bpm: int | None
    year: int | None
    duration_ms: int | None
    size: int | None
    bit_rate: int | None
    sample_rate: int | None
    date_added: datetime | None
    path: str | None  # None for tracks with no local file (streaming, removed)


@dataclass(frozen=True)
class MusicPlaylist:
    playlist_id: int | None
    persistent_id: str | None
    name: str
    parent_persistent_id: str | None
    is_folder: bool
    kind: str  # "user", or "library" / "system" for the lists Apple Music always has
    track_ids: tuple[int, ...]


@dataclass(frozen=True)
class MusicLibrary:
    path: Path | None
    music_folder: str | None
    tracks: list[MusicTrack]
    playlists: list[MusicPlaylist]
    tracks_by_id: dict[int, MusicTrack] = field(default_factory=dict)

    @property
    def user_playlists(self) -> list[MusicPlaylist]:
        return [p for p in self.playlists if p.kind == "user"]


def _text(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_track(raw: dict) -> MusicTrack | None:
    track_id = _int(raw.get("Track ID"))

    if track_id is None:
        return None

    location = raw.get("Location") if raw.get("Track Type", "File") == "File" else None

    return MusicTrack(
        track_id=track_id,
        persistent_id=_text(raw.get("Persistent ID")),
        title=_text(raw.get("Name")),
        artist=_text(raw.get("Artist")),
        album=_text(raw.get("Album")),
        genre=_text(raw.get("Genre")),
        kind=_text(raw.get("Kind")),
        bpm=_int(raw.get("BPM")),
        year=_int(raw.get("Year")),
        duration_ms=_int(raw.get("Total Time")),
        size=_int(raw.get("Size")),
        bit_rate=_int(raw.get("Bit Rate")),
        sample_rate=_int(raw.get("Sample Rate")),
        date_added=raw.get("Date Added") if isinstance(raw.get("Date Added"), datetime) else None,
        path=location_to_path(location) if isinstance(location, str) else None,
    )


def _parse_playlist(raw: dict) -> MusicPlaylist:
    if raw.get("Master"):
        kind = "library"
    elif "Distinguished Kind" in raw:
        kind = "system"
    else:
        kind = "user"

    items = raw.get("Playlist Items") or []
    track_ids = tuple(i for i in (_int(item.get("Track ID")) for item in items if isinstance(item, dict)) if i is not None)

    return MusicPlaylist(
        playlist_id=_int(raw.get("Playlist ID")),
        persistent_id=_text(raw.get("Playlist Persistent ID")),
        name=_text(raw.get("Name")) or "(unnamed)",
        parent_persistent_id=_text(raw.get("Parent Persistent ID")),
        is_folder=bool(raw.get("Folder")),
        kind=kind,
        track_ids=track_ids,
    )


def parse_apple_music_plist(contents: bytes, path: Path | None = None) -> MusicLibrary:
    """Parse the bytes of an exported library. Raises plistlib's errors for bad plists."""
    data = plistlib.loads(contents)

    if not isinstance(data, dict) or not isinstance(data.get("Tracks"), dict):
        raise AppleMusicParseError("No Tracks dictionary found.")

    tracks = [t for t in (_parse_track(raw) for raw in data["Tracks"].values() if isinstance(raw, dict)) if t]
    playlists = [_parse_playlist(p) for p in data.get("Playlists") or [] if isinstance(p, dict)]

    return MusicLibrary(
        path=path,
        music_folder=location_to_path(data.get("Music Folder")) if isinstance(data.get("Music Folder"), str) else None,
        tracks=tracks,
        playlists=playlists,
        tracks_by_id={t.track_id: t for t in tracks},
    )
