"""Builders for small Apple Music library exports."""

import plistlib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


def music_track(track_id, name, artist=None, path=None, ms=200_000, **extra):
    raw = {
        "Track ID": track_id, "Name": name, "Kind": "MPEG audio file", "Total Time": ms,
        "Persistent ID": f"P{track_id:04d}", "Track Type": "File",
        "Date Added": datetime(2025, 1, 2, 3, 4, 5), **extra,
    }

    if artist is not None:
        raw["Artist"] = artist

    if path is not None:
        raw["Location"] = "file://" + quote(str(path), safe="/&")

    return raw


def playlist(name, *track_ids, **extra):
    return {
        "Name": name, "Playlist ID": abs(hash(name)) % 10_000, "Playlist Persistent ID": name.upper(),
        "Playlist Items": [{"Track ID": i} for i in track_ids], **extra,
    }


def library_bytes(tracks, playlists=()):
    return plistlib.dumps({
        "Major Version": 1, "Music Folder": "file:///Music/",
        "Tracks": {str(t["Track ID"]): t for t in tracks},
        "Playlists": [
            {"Name": "Library", "Master": True, "Playlist ID": 1, "Playlist Items": [{"Track ID": t["Track ID"]} for t in tracks]},
            {"Name": "Music", "Distinguished Kind": 4, "Playlist ID": 2, "Playlist Items": []},
            *playlists,
        ],
    })


def write_library(path: Path, tracks, playlists=()) -> Path:
    path.write_bytes(library_bytes(tracks, playlists))
    return path
