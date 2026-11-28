"""Cue edits in Rekordbox's database: default cues, memory cues to hot cues and Serato's cues (with undo)."""

from functools import partial
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


def _left_out(plan, result: dict) -> str:
    """Report the tracks a plan left out on purpose: into `result`, and as a sentence for the summary."""
    unchecked = sum(plan.serato_unchecked.values())

    if plan.what == "default cue":
        if not plan.has_serato_cues and not unchecked:
            return ""

        result["tracksSkippedBecauseTheyHaveSeratoCues"] = len(plan.has_serato_cues)
        result["examplesWithSeratoCues"] = plan.has_serato_cues[:10]
        result["tracksSkippedBecauseSeratoCuesCouldNotBeRuledOut"] = unchecked
        result["whyNotRuledOut"] = dict(plan.serato_unchecked)

        return (
            f" Left alone: {len(plan.has_serato_cues)} track(s) with Serato cues, and {unchecked} where Serato cues "
            "couldn't be ruled out."
        )

    if plan.what != "Serato cues":
        return ""

    result.update({
        "tracksWithNoSeratoCues": plan.no_serato_cues,
        "tracksWhoseSeratoCuesCouldNotBeRead": unchecked,
        "whyNotRead": dict(plan.serato_unchecked),
        "tracksSkippedForUncertainTiming": len(plan.timing_unsure),
        "ofThoseWithAnEstimateFromBeatGrids": plan.timing_could_be_estimated,
        "examplesWithUncertainTiming": plan.timing_unsure[:10],
        "tracksSkippedBecauseASyncBackWouldChangeTheFile": len(plan.sync_would_differ),
        "examplesASyncBackWouldChange": plan.sync_would_differ[:10],
        "tracksSkippedForASlotRekordboxCannotHold": len(plan.unsupported_slot),
        "tracksUsingEstimatedTiming": plan.timing_estimated,
    })
    parts = [
        f"{len(plan.timing_unsure)} with uncertain timing"
        + (f" ({plan.timing_could_be_estimated} could be estimated)" if plan.timing_could_be_estimated else ""),
        f"{len(plan.sync_would_differ)} that a sync back would change",
        f"{unchecked} whose Serato cues couldn't be read",
    ]

    if plan.unsupported_slot:
        parts.append(f"{len(plan.unsupported_slot)} with a cue in a slot Rekordbox can't hold")

    note = " Left out: " + ", ".join(parts) + "."

    if plan.timing_estimated:
        note += f" {plan.timing_estimated} of the tracks to copy use timing ESTIMATED from beat grids."

    return note


def _cue_write_tool(
    plan_function,
    what: str,
    dry_run: bool,
    expected_cues: int | None,
    playlisted_only: bool,
    database_path: str | None,
    track_ids: list[str] | None = None,
) -> dict:
    """The shared flow of the cue-writing tools: plan, report, and (when asked) apply and verify."""
    db_path = db_target(database_path)
    backend = rekordbox_db.default_backend()
    snapshot = rekordbox_db.read_snapshot(db_path, backend)
    make_plan = partial(plan_function, playlisted_only=playlisted_only, **({} if track_ids is None else {"track_ids": track_ids}))
    plan = make_plan(snapshot)
    running = rekordbox_db.rekordbox_running()
    paths = {c.id: Path(c.path or "").suffix.lower() for c in snapshot.contents}
    result = {
        "dryRun": dry_run,
        "database": str(db_path),
        "rekordboxIsRunning": running,
        "tracksToChange": len(plan.additions),
        "cuesToAdd": plan.cue_count,
        "alreadyDone": plan.already_done,
        "playlistedTracksOnly": playlisted_only,
        "tracksSkippedForBeingInNoPlaylist": plan.not_playlisted,
        "limitedToTrackIds": track_ids,
        "examples": [
            {
                "trackId": a.content_id, "fileType": paths.get(a.content_id, ""), "track": a.title, "artist": a.artist,
                "cues": [{"slot": "ABCDEFGH"[db_library.HOT_CUE_SLOTS[k]], "positionMs": ms} for k, ms in a.cues],
            }
            for a in plan.additions[:10]
        ],
        "memoryCuesWithNoFreeSlot": sum(n for _, n in plan.no_free_slot),
        "tracksWithNoFreeSlot": [{"track": t, "memoryCuesSkipped": n} for t, n in plan.no_free_slot[:10]],
        "tracksSkippedForUnreadableCues": plan.skipped_unreadable[:10],
    }

    left_out = _left_out(plan, result)

    if not plan.additions:
        result["summary"] = "Nothing to do: no cues need adding, and nothing was written." + left_out
        return result

    if dry_run:
        result["summary"] = (
            f"Would {what}: {plan.cue_count} cue(s) on {len(plan.additions)} track(s), written to Rekordbox's database. "
            "Nothing has been written." + left_out + (" Quit Rekordbox before applying." if running else "")
        )
        return result

    if expected_cues != plan.cue_count:
        raise ToolError(
            f"This would add {plan.cue_count} cues, but expected_cues was {expected_cues!r}. "
            "Run a dry run, show the user the numbers, and pass the count."
        )

    outcome = rekordbox_cue_writes.apply_cue_plan(plan, db_path, backend, make_plan)
    result["backup"] = str(outcome.backup)
    result["journal"] = str(outcome.journal)
    result["summary"] = (
        f"Did {what}: added {plan.cue_count} cue(s) to {len(plan.additions)} track(s) and verified the database. "
        f"Backup: {outcome.backup}. To reverse this, call undo_rekordbox_cue_additions (journal: {outcome.journal}). "
        "You can open Rekordbox now." + left_out
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
    track_ids: list[str] | None = None,
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

    `track_ids` limits it to those tracks, for example to try a few and check
    them in Rekordbox before doing the rest. The IDs are the "trackId" values in
    this tool's examples or in compare_cues. Do a dry run with the same
    track_ids before applying.

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
        dry_run, expected_cues, playlisted_only, database_path, track_ids,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def copy_serato_cues_to_rekordbox(
    dry_run: bool = True,
    expected_cues: int | None = None,
    track_ids: list[str] | None = None,
    include_estimated_timing: bool = False,
    playlisted_only: bool = True,
    database_path: str | None = None,
) -> dict:
    """Copy Serato's hot cues into Rekordbox's database, for tracks with no cue or loop in Rekordbox.

    The reverse of sync_cues_to_serato, one way and only into empty tracks: a
    track with any cue in Rekordbox is left as it is, so nothing is merged or
    overwritten. Each cue goes to the same slot (Serato cue 1 is hot cue A) with
    the timing correction for the file type reversed. Colors and names aren't
    copied (cues get Rekordbox's default color, and a later sync keeps Serato's
    colors and names). MP3 and WAV
    only; audio files are only read. Serato's loops aren't copied.

    A track is left out, and counted, if its timing correction isn't measured,
    or if a sync back to Serato would then change its file. For some of the
    uncertain ones the beat grids give an estimate: include_estimated_timing=true
    copies those too, and the user must check them by ear. That check proves the
    two directions agree, not that a cue is in the right place in Rekordbox.

    This edits master.db, with the same steps as set_default_cue: dry run first
    (Rekordbox may be open), then Rekordbox closed and dry_run=false with
    expected_cues set to the count from the dry run. A backup, read-back
    verification and a journal are included; undo_rekordbox_cue_additions
    reverses it.

    `track_ids` limits it to those tracks (the "trackId" values in this tool's
    examples or in compare_cues). Suggest trying two or three first, with both
    MP3 and WAV, and checking them in Rekordbox before copying the rest. Do a
    dry run with the same track_ids before applying.
    """
    return _cue_write_tool(
        partial(rekordbox_cue_writes.plan_serato_cues, include_estimated_timing=include_estimated_timing),
        "copy Serato's hot cues into Rekordbox",
        dry_run, expected_cues, playlisted_only, database_path, track_ids,
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
    """Reverse set_default_cue, copy_serato_cues_to_rekordbox or duplicate_memory_cues_to_hot_cues.

    It takes the cues they added off again.

    `journal` is the path the tool reported; the most recent cue journal is used
    if omitted. Cues that are already gone are skipped. Rekordbox must be closed
    unless this is a dry run.
    """
    path = Path(journal).expanduser() if journal else rekordbox_db.latest_journal(prefix=rekordbox_cue_writes.CUE_JOURNALS)

    if path is None or not path.is_file():
        raise ToolError("No cue journal found. Pass the journal path that the tool which added the cues reported.")

    outcome = rekordbox_cue_writes.undo_cue_additions(path, rekordbox_db.default_backend(), dry_run=dry_run)

    return {
        "dryRun": dry_run,
        "journal": str(path),
        "undone" if not dry_run else "wouldUndo": outcome.removed,
        "skipped": [{"item": e, "why": why} for e, why in outcome.skipped],
        "summary": f"{'Would undo' if dry_run else 'Undid'} {len(outcome.removed)} change(s); {len(outcome.skipped)} skipped.",
    }
