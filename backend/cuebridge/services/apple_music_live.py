"""Read the library straight from the Music app, so no export is needed.

The Music app can be scripted. A short JavaScript for Automation script, run
with `osascript`, returns every track and every playlist as JSON in about half
a minute for a library of a couple of thousand tracks. This only reads: nothing
in Music is changed. Music must already be open (it is never launched from
here), and macOS asks once for permission the first time.

The result is the same MusicLibrary the XML export gives, except that Music
doesn't hand out numeric track IDs, so they are numbered in library order, and
playlist folders and the Music folder aren't exposed (the folder is inferred).
"""

import json
import os
import subprocess
from datetime import datetime
from typing import Callable

from cuebridge.parsers.apple_music_xml import MusicLibrary, MusicPlaylist, MusicTrack
from cuebridge.services.rekordbox_service import LibraryError

TIMEOUT_SECONDS = 240

SCRIPT = """
const music = Application("Music");
const t = music.libraryPlaylists[0].tracks;
const p = {
  persistentID: t.persistentID(), name: t.name(), artist: t.artist(), album: t.album(), genre: t.genre(),
  kind: t.kind(), bpm: t.bpm(), year: t.year(), duration: t.duration(), size: t.size(),
  bitRate: t.bitRate(), sampleRate: t.sampleRate(), dateAdded: t.dateAdded(), location: t.location(),
};
const tracks = [];
for (let i = 0; i < p.persistentID.length; i++) {
  const loc = p.location[i];
  tracks.push({
    persistentID: p.persistentID[i], name: p.name[i], artist: p.artist[i], album: p.album[i], genre: p.genre[i],
    kind: p.kind[i], bpm: p.bpm[i], year: p.year[i], duration: p.duration[i], size: p.size[i],
    bitRate: p.bitRate[i], sampleRate: p.sampleRate[i],
    dateAdded: p.dateAdded[i] ? p.dateAdded[i].toISOString() : null,
    location: loc ? String(loc) : null,
  });
}
const lists = music.userPlaylists;
const names = lists.name(), ids = lists.persistentID(), kinds = lists.specialKind(), smart = lists.smart(), classes = lists.class();
const playlists = [];
for (let i = 0; i < names.length; i++) {
  let items = [];
  try { items = lists[i].tracks.persistentID(); } catch (e) {}
  playlists.push({ name: names[i], persistentID: ids[i], specialKind: kinds[i], smart: smart[i], class: classes[i], trackIDs: items });
}
JSON.stringify({ tracks, playlists });
"""


class MusicAppError(LibraryError):
    """The Music app couldn't be read."""


def is_music_running() -> bool:
    return subprocess.run(["pgrep", "-x", "Music"], capture_output=True).returncode == 0


def run_script() -> str:
    """Run the script against the Music app and return its JSON output."""
    if not is_music_running():
        raise MusicAppError("The Music app isn't open. Open Music and try again (CueBridge never launches it).")

    try:
        done = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", SCRIPT], capture_output=True, text=True, timeout=TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        raise MusicAppError(
            f"Music didn't answer within {TIMEOUT_SECONDS} s. If macOS is asking for permission, allow it and try again."
        ) from None
    except FileNotFoundError:
        raise MusicAppError("Reading the Music app needs macOS (osascript was not found).") from None

    if done.returncode != 0:
        message = done.stderr.strip()

        if "-1743" in message or "not authorized" in message.lower():
            raise MusicAppError(
                "macOS hasn't allowed this program to control Music. Allow it in System Settings > Privacy & "
                "Security > Automation, then try again."
            )

        raise MusicAppError(f"Reading the Music app failed: {message or 'unknown error'}")

    return done.stdout


def _common_folder(paths: list[str]) -> str | None:
    """The deepest folder that holds every file, as a stand-in for the Music folder."""
    if not paths:
        return None

    folder = os.path.commonpath([os.path.dirname(p) for p in paths])

    return folder + "/" if folder.count("/") >= 3 else None


def parse_live_json(text: str) -> MusicLibrary:
    try:
        data = json.loads(text)
        raw_tracks, raw_playlists = data["tracks"], data["playlists"]
    except (ValueError, KeyError, TypeError):
        raise MusicAppError("The Music app returned something that isn't a library.") from None

    tracks: list[MusicTrack] = []
    ids: dict[str, int] = {}

    for number, raw in enumerate(raw_tracks, start=1):
        persistent_id = raw.get("persistentID")
        ids[persistent_id] = number
        added = raw.get("dateAdded")
        duration = raw.get("duration")
        location = raw.get("location")
        tracks.append(MusicTrack(
            track_id=number,
            persistent_id=persistent_id,
            title=(raw.get("name") or "").strip() or None,
            artist=(raw.get("artist") or "").strip() or None,
            album=(raw.get("album") or "").strip() or None,
            genre=(raw.get("genre") or "").strip() or None,
            kind=(raw.get("kind") or "").strip() or None,
            bpm=raw.get("bpm") or None,
            year=raw.get("year") or None,
            duration_ms=round(duration * 1000) if isinstance(duration, (int, float)) else None,
            size=raw.get("size") or None,
            bit_rate=raw.get("bitRate") or None,
            sample_rate=raw.get("sampleRate") or None,
            date_added=datetime.fromisoformat(added.replace("Z", "+00:00")).replace(tzinfo=None) if added else None,
            path=location if isinstance(location, str) and location.startswith("/") else None,
        ))

    playlists = []

    for number, raw in enumerate(raw_playlists, start=1):
        special = str(raw.get("specialKind") or "none")
        playlists.append(MusicPlaylist(
            playlist_id=number,
            persistent_id=raw.get("persistentID"),
            name=(raw.get("name") or "").strip() or "(unnamed)",
            parent_persistent_id=None,
            is_folder=raw.get("class") == "folderPlaylist",
            kind="user" if special == "none" else "system",
            track_ids=tuple(ids[i] for i in raw.get("trackIDs") or [] if i in ids),
        ))

    return MusicLibrary(
        path=None,
        music_folder=_common_folder([t.path for t in tracks if t.path]),
        tracks=tracks,
        playlists=playlists,
        tracks_by_id={t.track_id: t for t in tracks},
    )


def read_live_library(runner: Callable[[], str] | None = None) -> MusicLibrary:
    return parse_live_json((runner or run_script)())
