"""CueBridge MCP server.

Run it over stdio with:  python -m cuebridge.mcp_server

All logic lives in cuebridge.services; the tools here only keep track of the
currently loaded library and translate errors for the assistant.
"""

import functools
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cuebridge.services import library_queries, rekordbox_editor
from cuebridge.services.rekordbox_service import (
    LibraryError,
    RekordboxLibrary,
    get_rekordbox_library,
)

mcp = MCPServer(
    "cuebridge",
    instructions=(
        "CueBridge reads Rekordbox library exports (the XML file from Rekordbox's "
        "File > Export Collection in xml format) and inspects their cue points. "
        "Call load_rekordbox_library first with the path to the XML file. Tools "
        "return summaries and pages of results, so use limit and offset to see "
        "more. set_default_cue creates a new edited copy of the file and never "
        "modifies the original."
    ),
)

_current_path: Path | None = None

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def reset_state() -> None:
    """Forget the loaded library. Used by tests."""
    global _current_path
    _current_path = None


def _translate_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except LibraryError as error:
            raise ToolError(str(error)) from None

    return wrapper


def _require_library() -> RekordboxLibrary:
    if _current_path is None:
        raise ToolError(
            "No library is loaded. Call load_rekordbox_library with the path to a "
            "Rekordbox XML export first."
        )

    # Cheap when the file is unchanged, and re-parses if it was re-exported.
    return get_rekordbox_library(_current_path)


@mcp.tool(annotations=READ_ONLY)
@_translate_errors
def load_rekordbox_library(path: str) -> dict:
    """Load a Rekordbox XML export and return a summary of it.

    `path` is the full path to the XML file on this computer (a leading ~ is
    expanded). The library stays loaded for the other tools. Loading another
    path replaces it.
    """
    global _current_path

    library = get_rekordbox_library(path)
    _current_path = library.path

    summary = library_queries.library_summary(library)
    summary["summary"] = (
        f"Loaded {summary['trackCount']} tracks. {summary['tracksWithCues']} have "
        f"cues ({summary['cueCount']} cues in total) and "
        f"{summary['tracksWithoutCues']} have none."
    )
    return summary


@mcp.tool(annotations=READ_ONLY)
@_translate_errors
def search_tracks(
    query: str = "",
    has_cues: bool | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict:
    """List tracks in the loaded library, a page at a time.

    `query`: words that must all appear in the title, artist or file name
    (case-insensitive). Leave empty to list everything.
    `has_cues`: true for only tracks with cues, false for only tracks without,
    omit for both.
    `limit` is capped at 100. Use `offset` to get the next page (see hasMore).
    """
    return library_queries.search_tracks(_require_library(), query, has_cues, limit, offset)


@mcp.tool(annotations=READ_ONLY)
@_translate_errors
def get_track_cues(track_id: str) -> dict:
    """Get one track with all of its cues. `track_id` comes from search_tracks."""
    track = _require_library().tracks_by_id.get(track_id)

    if track is None:
        raise ToolError(f"No track with ID {track_id!r}. Use search_tracks to find IDs.")

    return library_queries.track_to_detail(track)


@mcp.tool(annotations=READ_ONLY)
@_translate_errors
def find_tracks_without_cues(limit: int = 25, offset: int = 0) -> dict:
    """List tracks that have no cues, a page at a time."""
    return library_queries.search_tracks(_require_library(), has_cues=False, limit=limit, offset=offset)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
@_translate_errors
def set_default_cue(output_path: str | None = None, dry_run: bool = True) -> dict:
    """Add a hot cue A at 0:00 to every track that has no cue or loop.

    This never modifies the loaded library file. It writes a new, edited copy
    (by default next to the original, named like rekordbox.with-default-cues.xml)
    and refuses to overwrite an existing file. Tracks that already have any cue
    or loop are left alone.

    `dry_run` is true by default and only reports what would change. Show the
    user that summary and get their go-ahead before calling again with
    dry_run=false. `output_path` must end in .xml.
    """
    library = _require_library()
    plan = rekordbox_editor.build_default_cue_plan(library.path)

    count = len(plan.changed_track_ids)
    destination = (
        Path(output_path).expanduser() if output_path else rekordbox_editor.default_output_path(plan.source)
    )

    result = {
        "dryRun": dry_run,
        "cueAdded": "hot cue A at 0:00.000",
        "tracksToChange": count,
        "tracksSkippedBecauseTheyHaveCues": plan.skipped_with_cues,
        "sourceFile": str(plan.source),
        "outputFile": str(destination),
    }

    by_id = library.tracks_by_id
    result["examples"] = [
        library_queries.track_to_summary(by_id[i]) for i in plan.changed_track_ids[:10] if i in by_id
    ]

    if count == 0:
        result["summary"] = "Every track already has a cue. Nothing to do, and no file was written."
        return result

    if dry_run:
        result["summary"] = (
            f"Would add a hot cue A at 0:00 to {count} tracks and write the result to "
            f"{destination}. The original file would not be modified. Nothing has been written."
        )
        return result

    written = rekordbox_editor.write_plan(plan, output_path)
    result["outputFile"] = str(written)
    result["summary"] = (
        f"Added a hot cue A at 0:00 to {count} tracks and wrote the copy to {written}. "
        "The original file was not modified."
    )
    return result


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
