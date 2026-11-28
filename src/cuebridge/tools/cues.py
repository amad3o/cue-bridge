"""Cue edits in Rekordbox's database: default cues and memory cues to hot cues (with undo)."""

from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cuebridge.services.rekordbox import cue_writes as rekordbox_cue_writes
from cuebridge.services.rekordbox import db as rekordbox_db
from cuebridge.services.rekordbox import db_library
from cuebridge.tools.common import (
    db_target,
    mcp,
    translate_errors,
)


def _cue_write_tool(
    plan_function,
    what: str,
    dry_run: bool,
    expected_cues: int | None,
    playlisted_only: bool,
    database_path: str | None,
) -> dict:
    """The shared flow of the two cue-writing tools: plan, report, and (when asked) apply and verify."""
    db_path = db_target(database_path)
    backend = rekordbox_db.default_backend()
    snapshot = rekordbox_db.read_snapshot(db_path, backend)
    plan = plan_function(snapshot, playlisted_only)
    running = rekordbox_db.rekordbox_running()
    result = {
        "dryRun": dry_run,
        "database": str(db_path),
        "rekordboxIsRunning": running,
        "tracksToChange": len(plan.additions),
        "cuesToAdd": plan.cue_count,
        "alreadyDone": plan.already_done,
        "playlistedTracksOnly": playlisted_only,
        "tracksSkippedForBeingInNoPlaylist": plan.not_playlisted,
        "examples": [
            {"track": a.title, "artist": a.artist, "cues": [{"slot": "ABCDEFGH"[db_library.HOT_CUE_SLOTS[k]], "positionMs": ms} for k, ms in a.cues]}
            for a in plan.additions[:10]
        ],
        "memoryCuesWithNoFreeSlot": sum(n for _, n in plan.no_free_slot),
        "tracksWithNoFreeSlot": [{"track": t, "memoryCuesSkipped": n} for t, n in plan.no_free_slot[:10]],
        "tracksSkippedForUnreadableCues": plan.skipped_unreadable[:10],
    }

    protected = ""

    if plan.has_serato_cues or plan.serato_unchecked:
        unchecked = sum(plan.serato_unchecked.values())
        result["tracksSkippedBecauseTheyHaveSeratoCues"] = len(plan.has_serato_cues)
        result["examplesWithSeratoCues"] = plan.has_serato_cues[:10]
        result["tracksSkippedBecauseSeratoCuesCouldNotBeRuledOut"] = unchecked
        result["whyNotRuledOut"] = dict(plan.serato_unchecked)
        protected = (
            f" Left alone: {len(plan.has_serato_cues)} track(s) with Serato cues, and {unchecked} where Serato cues "
            "couldn't be ruled out."
        )

    if not plan.additions:
        result["summary"] = "Nothing to do: no cues need adding, and nothing was written." + protected
        return result

    if dry_run:
        result["summary"] = (
            f"Would {what}: {plan.cue_count} cue(s) on {len(plan.additions)} track(s), written to Rekordbox's database. "
            "Nothing has been written." + protected + (" Quit Rekordbox before applying." if running else "")
        )
        return result

    if expected_cues != plan.cue_count:
        raise ToolError(
            f"This would add {plan.cue_count} cues, but expected_cues was {expected_cues!r}. "
            "Run a dry run, show the user the numbers, and pass the count."
        )

    outcome = rekordbox_cue_writes.apply_cue_plan(plan, db_path, backend, lambda s: plan_function(s, playlisted_only))
    result["backup"] = str(outcome.backup)
    result["journal"] = str(outcome.journal)
    result["summary"] = (
        f"Did {what}: added {plan.cue_count} cue(s) to {len(plan.additions)} track(s) and verified the database. "
        f"Backup: {outcome.backup}. To reverse this, call undo_rekordbox_cue_additions (journal: {outcome.journal}). "
        "You can open Rekordbox now." + protected
    )
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def set_default_cue(
    dry_run: bool = True,
    expected_cues: int | None = None,
    playlisted_only: bool = True,
    database_path: str | None = None,
) -> dict:
    """Add a hot cue A at 0:00 to every playlisted track that has no cue or loop.

    This edits Rekordbox's own database (master.db), so be careful. Only tracks
    that are in at least one real playlist are touched by default
    (`playlisted_only`), because the collection also holds thousands of
    sample-pack files nobody plays from a playlist. Tracks that already have any
    cue or loop are left alone, so running it again changes nothing.

    To protect Serato's cues, a track is also left alone unless its audio file is
    confirmed to hold none: a later sync_cues_to_serato would replace them with
    this cue. That skips files with Serato cues, and files that can't be checked
    (only MP3 and WAV can be so far; also missing and streaming files). Tell the
    user how many were left alone and why.

    - Call it with dry_run=true first (the default). Rekordbox may be open for
      that. Show the user the number of tracks and a few examples.
    - Rekordbox must be closed to write. Then call with dry_run=false and
      expected_cues set to the count from the dry run.
    - A backup, read-back verification (restoring the backup on any difference)
      and a journal work as in the other database tools; undo_rekordbox_cue_additions
      reverses it.
    """
    return _cue_write_tool(
        rekordbox_cue_writes.plan_default_cues, "add a hot cue A at 0:00 to the tracks with no cues",
        dry_run, expected_cues, playlisted_only, database_path,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def duplicate_memory_cues_to_hot_cues(
    dry_run: bool = True,
    expected_cues: int | None = None,
    playlisted_only: bool = True,
    database_path: str | None = None,
) -> dict:
    """Copy every memory cue to a hot cue at the same position, in Rekordbox's database.

    The memory cues stay. Each one gets a duplicate hot cue in the lowest free
    slot (A-H, i.e. 8 slots), in time order. Slots already used by hot cues or
    hot loops are never reused, a memory cue is skipped if a hot cue is already
    at the exact same time (so running this again changes nothing), and any
    memory cue beyond the 8 slots is reported as overflow and not copied. A
    track with a cue in a slot CueBridge can't read is skipped and reported.
    Only playlisted tracks are touched by default (`playlisted_only`).

    This edits master.db, with the same steps as set_default_cue: dry run first
    (Rekordbox may be open), then Rekordbox closed and dry_run=false with
    expected_cues set to the count from the dry run. A backup, read-back
    verification and a journal are included; undo_rekordbox_cue_additions
    reverses it.
    """
    return _cue_write_tool(
        rekordbox_cue_writes.plan_memory_to_hot_cues, "copy memory cues to hot cues",
        dry_run, expected_cues, playlisted_only, database_path,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def undo_rekordbox_cue_additions(journal: str | None = None, dry_run: bool = True) -> dict:
    """Reverse set_default_cue or duplicate_memory_cues_to_hot_cues: take the cues they added off again.

    `journal` is the path the tool reported; the most recent cue journal is used
    if omitted. Cues that are already gone are skipped. Rekordbox must be closed
    unless this is a dry run.
    """
    path = Path(journal).expanduser() if journal else rekordbox_db.latest_journal(prefix=rekordbox_cue_writes.CUE_JOURNALS)

    if path is None or not path.is_file():
        raise ToolError("No cue journal found. Pass the journal path from set_default_cue or duplicate_memory_cues_to_hot_cues.")

    outcome = rekordbox_cue_writes.undo_cue_additions(path, rekordbox_db.default_backend(), dry_run=dry_run)

    return {
        "dryRun": dry_run,
        "journal": str(path),
        "undone" if not dry_run else "wouldUndo": outcome.removed,
        "skipped": [{"item": e, "why": why} for e, why in outcome.skipped],
        "summary": f"{'Would undo' if dry_run else 'Undid'} {len(outcome.removed)} change(s); {len(outcome.skipped)} skipped.",
    }
