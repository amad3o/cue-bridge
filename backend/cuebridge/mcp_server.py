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
        "more. The writing tools (set_default_cue and "
        "duplicate_memory_cues_to_hot_cues) edit the loaded XML file itself, "
        "after saving a backup of a fresh Rekordbox export. They default to a "
        "dry run: always show the user what would change, and get their "
        "go-ahead, before calling them with dry_run=false."
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


def _edit_result(plan, dry_run: bool) -> dict:
    """Fields every writing tool reports about what file it changes."""
    t = plan.target

    result = {
        "dryRun": dry_run,
        "file": str(t.target),
        "mode": "edits the loaded file in place" if t.in_place else "saves an edited copy elsewhere",
    }

    if t.in_place:
        result["backupFirst"] = rekordbox_editor.needs_backup(plan)
    else:
        result["sourceFile"] = str(t.source)

    return result


def _finish_edit(plan, result: dict, changed: int, what: str, dry_run: bool) -> dict:
    """Fill in the summary, and save the XML unless this is a dry run."""
    t = plan.target

    if changed == 0:
        result["summary"] = "Nothing to do for this edit, and no file was written."
        return result

    if dry_run:
        if t.in_place:
            backup = " A backup of the current file would be saved next to it first." if result["backupFirst"] else ""
            result["summary"] = (
                f"Would {what} by editing {t.target} in place.{backup} Nothing has been written."
            )
        else:
            result["summary"] = (
                f"Would {what} and save the result to {t.target}. "
                f"{t.source.name} would not be modified. Nothing has been written."
            )
        return result

    written = rekordbox_editor.write_plan(plan)
    result["file"] = str(written.path)
    result["backup"] = str(written.backup) if written.backup else None

    if t.in_place:
        saved = f"Did {what} by editing {written.path} in place."
        saved += f" The previous file was backed up to {written.backup}." if written.backup else ""
    else:
        saved = f"Did {what} and saved the result to {written.path}. {t.source.name} was not modified."

    result["summary"] = saved + " Load the file again to see the changes."
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@_translate_errors
def set_default_cue(output_path: str | None = None, dry_run: bool = True) -> dict:
    """Add a hot cue A at 0:00 to every track that has no cue or loop.

    By default this edits the loaded Rekordbox XML file itself, in place. If
    that file is a fresh Rekordbox export, a timestamped backup is saved next to
    it first. The write is atomic, and it is refused if the file changed since
    the dry run (for example after a re-export). Running it again changes
    nothing, and other writing tools build on the same file, so their edits add
    up.

    `dry_run` is true by default and only reports what would change. Show the
    user that summary, including that the file would be edited in place, and get
    their go-ahead before calling again with dry_run=false. `output_path`
    (optional, must end in .xml) saves the edited XML there instead and leaves
    the loaded file alone.
    """
    library = _require_library()
    plan = rekordbox_editor.build_default_cue_plan(library.path, output_path=output_path)

    count = len(plan.changed_track_ids)
    by_id = library.tracks_by_id

    result = _edit_result(plan, dry_run)
    result.update(
        {
            "cueAdded": "hot cue A at 0:00.000",
            "tracksToChange": count,
            "tracksSkippedBecauseTheyHaveCues": plan.skipped_with_cues,
            "examples": [
                library_queries.track_to_summary(by_id[i])
                for i in plan.changed_track_ids[:10]
                if i in by_id
            ],
        }
    )

    return _finish_edit(plan, result, count, f"add a hot cue A at 0:00 to {count} tracks", dry_run)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@_translate_errors
def duplicate_memory_cues_to_hot_cues(output_path: str | None = None, dry_run: bool = True) -> dict:
    """Copy every memory cue to a hot cue at the same position.

    The memory cues stay. Each one gets a duplicate hot cue in the lowest free
    slot (A-H, i.e. 8 slots), in time order. Slots already used by hot cues or
    hot loops are never reused, a memory cue is skipped if a hot cue is already
    at the exact same time (so running this again changes nothing), and any
    memory cue beyond the 8 slots is reported as overflow and not copied.

    By default this edits the loaded Rekordbox XML file itself, in place, with
    the same safeguards as set_default_cue: a timestamped backup of a fresh
    Rekordbox export first, an atomic write, and a refusal if the file changed
    since the dry run.

    `dry_run` is true by default and only reports what would change. Show the
    user that summary, including that the file would be edited in place, and get
    their go-ahead before calling again with dry_run=false. `output_path`
    (optional, must end in .xml) saves the edited XML there instead and leaves
    the loaded file alone.
    """
    library = _require_library()
    plan = rekordbox_editor.build_hot_cue_duplication_plan(library.path, output_path=output_path)

    count = len(plan.changed_track_ids)
    by_id = library.tracks_by_id

    result = _edit_result(plan, dry_run)
    result.update(
        {
            "hotCuesToAdd": plan.cues_added,
            "tracksToChange": count,
            "memoryCuesAlreadyHavingAHotCue": plan.already_duplicated,
            "memoryCuesWithNoFreeSlot": sum(n for _, n in plan.overflow),
            "tracksWithNoFreeSlot": [
                {**library_queries.track_to_summary(by_id[i]), "memoryCuesSkipped": n}
                for i, n in plan.overflow[:10]
                if i in by_id
            ],
            "examples": [
                library_queries.track_to_summary(by_id[i])
                for i in plan.changed_track_ids[:10]
                if i in by_id
            ],
        }
    )

    return _finish_edit(
        plan, result, count, f"add {plan.cues_added} hot cues to {count} tracks", dry_run
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
