"""Turning the file URLs that Rekordbox and Apple Music store into paths."""

import re
from urllib.parse import unquote


def location_to_path(location: str | None) -> str | None:
    """Turn file://localhost/Users/me/My%20Song.mp3 (or file:///...) into /Users/me/My Song.mp3."""
    if not location:
        return None

    path = re.sub(r"^file://(localhost)?", "", location)
    path = unquote(path)

    # Windows paths arrive as /C:/Music/song.mp3
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]

    return path
