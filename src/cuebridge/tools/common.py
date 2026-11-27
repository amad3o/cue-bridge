"""What every tool module shares: the server, the loaded-library state, and the error translation."""

import functools
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cuebridge.instructions import GUIDANCE
from cuebridge.services.apple_music.service import get_apple_music_library
from cuebridge.services.apple_music.xml_parser import MusicLibrary
from cuebridge.services.compare import drift
from cuebridge.services.rekordbox import db as rekordbox_db
from cuebridge.services.rekordbox.service import LibraryError, RekordboxLibrary, get_rekordbox_library

mcp = MCPServer("cuebridge", instructions=GUIDANCE)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@dataclass
class Session:
    """The libraries the assistant has loaded so far. Tools read it and the load tools set it."""

    rekordbox_path: Path | None = None
    apple_path: Path | None = None
    apple_live: MusicLibrary | None = None


session = Session()


def reset_state() -> None:
    """Forget the loaded libraries. Used by tests."""
    session.rekordbox_path = None
    session.apple_path = None
    session.apple_live = None


def translate_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except LibraryError as error:
            raise ToolError(str(error)) from None

    return wrapper


def require_library() -> RekordboxLibrary:
    if session.rekordbox_path is None:
        raise ToolError(
            "No library is loaded. Call load_rekordbox_library with the path to a "
            "Rekordbox XML export first."
        )

    # Cheap when the file is unchanged, and re-parses if it was re-exported.
    return get_rekordbox_library(session.rekordbox_path)


def apple_library() -> MusicLibrary | None:
    if session.apple_live is not None:
        return session.apple_live

    return None if session.apple_path is None else get_apple_music_library(session.apple_path)


def require_apple_library() -> MusicLibrary:
    library = apple_library()

    if library is None:
        raise ToolError(
            "No Apple Music library is loaded. Call load_apple_music_library first (with no path to read "
            "the open Music app, or with the path to an exported XML)."
        )

    return library


def current_drift() -> drift.LibraryDrift:
    rekordbox, music = require_library(), require_apple_library()
    return drift.compare_libraries(rekordbox.tracks, rekordbox.playlists, music)


def db_target(database_path: str | None) -> Path:
    return Path(database_path).expanduser() if database_path else rekordbox_db.find_database()
