"""What every tool module shares: the server, the loaded-library state, and the error translation."""

import functools
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cuebridge.parsers.apple_music_xml import MusicLibrary
from cuebridge.services.apple_music.service import get_apple_music_library
from cuebridge.services.compare import drift
from cuebridge.services.rekordbox import db as rekordbox_db
from cuebridge.services.rekordbox.service import LibraryError, RekordboxLibrary, get_rekordbox_library

mcp = MCPServer(
    "cuebridge",
    instructions=(
        "CueBridge reads Rekordbox library exports (the XML file from Rekordbox's "
        "File > Export Collection in xml format) and inspects their cue points. "
        "Call load_rekordbox_library first with the path to the XML file. Tools "
        "return summaries and pages of results, so use limit and offset to see "
        "more. The writing tools (set_default_cue and "
        "duplicate_memory_cues_to_hot_cues) edit Rekordbox's database, which "
        "must be closed. They default to a dry run: always show the user what "
        "would change, and get their go-ahead, before calling them with "
        "dry_run=false and expected_cues from the dry run. "
        "sync_cues_to_serato goes further and edits the user's audio files: "
        "Serato DJ Pro must be closed, and dry_run=false needs expected_files "
        "from the dry run. undo_serato_sync reverses it."
    ),
)

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
