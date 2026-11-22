"""CueBridge MCP server.

Run it over stdio with:  python -m cuebridge.mcp_server

All logic lives in cuebridge.services. The tools are in cuebridge.tools, one module per area; importing
them here registers them on the server. This module also re-exports the tool functions, so they can be
called directly as well as through MCP.
"""

from cuebridge.tools.common import mcp, reset_state
from cuebridge.tools.cues import (
    duplicate_memory_cues_to_hot_cues,
    set_default_cue,
    undo_rekordbox_cue_additions,
)
from cuebridge.tools.libraries import (
    find_tracks_without_cues,
    get_playlist_tracks,
    get_track_cues,
    load_apple_music_library,
    load_rekordbox_library,
    search_tracks,
)
from cuebridge.tools.playlists import (
    create_review_playlists,
    export_playlists_for_rekordbox,
    sync_playlists_to_rekordbox,
    undo_rekordbox_playlist_sync,
    undo_rekordbox_review_playlists,
)
from cuebridge.tools.repairs import (
    clean_up_stale_duplicates,
    fix_moved_files_in_rekordbox,
    report_stale_duplicates,
    undo_rekordbox_file_fix,
    undo_stale_duplicate_cleanup,
)
from cuebridge.tools.reports import (
    audit_music_library,
    check_sync_status,
    compare_libraries,
    find_missing_files,
    list_library_differences,
)
from cuebridge.tools.serato import (
    compare_cues,
    sync_cues_to_serato,
    undo_serato_sync,
)


def main() -> None:
    mcp.run()


__all__ = [
    "main",
    "mcp",
    "reset_state",
    "audit_music_library",
    "check_sync_status",
    "clean_up_stale_duplicates",
    "compare_cues",
    "compare_libraries",
    "create_review_playlists",
    "duplicate_memory_cues_to_hot_cues",
    "export_playlists_for_rekordbox",
    "find_missing_files",
    "find_tracks_without_cues",
    "fix_moved_files_in_rekordbox",
    "get_playlist_tracks",
    "get_track_cues",
    "list_library_differences",
    "load_apple_music_library",
    "load_rekordbox_library",
    "report_stale_duplicates",
    "search_tracks",
    "set_default_cue",
    "sync_cues_to_serato",
    "sync_playlists_to_rekordbox",
    "undo_rekordbox_cue_additions",
    "undo_rekordbox_file_fix",
    "undo_rekordbox_playlist_sync",
    "undo_rekordbox_review_playlists",
    "undo_serato_sync",
    "undo_stale_duplicate_cleanup",
]

if __name__ == "__main__":
    main()
