"""Serato: comparing Rekordbox's cues with Serato's, writing them into the audio files, and undo."""

from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cuebridge.services.compare import cue_compare
from cuebridge.services.reports import library_reports
from cuebridge.services.reports.library_queries import format_time
from cuebridge.services.serato import sync as serato_sync
from cuebridge.tools.common import (
    READ_ONLY,
    apple_library,
    mcp,
    translate_errors,
)
from cuebridge.tools.cue_source import read_cue_source

COMPARE_SHOW = ("differences", "differs", "rekordbox_only", "serato_only", "skipped", "match", "all")


@mcp.tool(annotations=READ_ONLY)
@translate_errors
def compare_cues(
    show: str = "differences",
    limit: int = 25,
    offset: int = 0,
    cue_source: str = "auto",
    include_memory_cues: bool = True,
    use_apple_music_paths: bool = True,
    playlisted_only: bool = True,
    database_path: str | None = None,
) -> dict:
    """Compare Rekordbox's cues with the cues in Serato's tags. Read-only: nothing is changed.

    Reports, per track: cues Rekordbox has and Serato lacks, Serato cues Rekordbox
    lacks (a sync_cues_to_serato run would remove those, so look here before syncing),
    and cues in both that differ in position (with how many ms), colour or name.
    "What Serato should hold" follows the sync's own rules (hot cues in their slots,
    memory cues in the free slots when include_memory_cues is true, the timing
    correction for the file type), so a track that matches is one the sync would
    leave alone. MP3 and WAV only; loops aren't compared yet (they are counted).

    `show` picks which tracks to list: "differences" (the default: any track that
    differs, has cues only in Rekordbox, or only in Serato), or one of "differs",
    "rekordbox_only", "serato_only", "skipped" (files that couldn't be read, with
    why), "match", or "all". The counts always cover every track compared. Results
    are paged with `limit` and `offset`.

    Rekordbox's cues come from its live database when it can be read (see
    `cue_source`, as in sync_cues_to_serato). Tracks with cues in Rekordbox are
    always compared; tracks without any are compared too if they are in a playlist
    (that is how Serato-only cues are found), or every track with
    `playlisted_only=false` (thousands of files, so slower). With an XML export
    there is no playlist information, so only tracks with Rekordbox cues are read.
    Reading the files' tags takes a moment for a big library.
    """
    if show not in COMPARE_SHOW:
        raise ToolError(f"show must be one of: {', '.join(COMPARE_SHOW)}.")

    library = read_cue_source(cue_source, database_path)
    music = apple_library() if use_apple_music_paths else None
    moved = (
        library_reports.moved_file_paths(library_reports.find_missing_rekordbox_files(library.tracks, music))
        if music
        else None
    )
    scope_note = None

    if playlisted_only and library.playlisted is not None:
        tracks = [t for t in library.tracks if t.cues or t.trackId in library.playlisted]
    elif playlisted_only:
        tracks = [t for t in library.tracks if t.cues]
        scope_note = "Read from an XML export, which has no playlist information: only tracks with Rekordbox cues were compared."
    else:
        tracks = list(library.tracks)

    comparison = cue_compare.compare_cues(tracks, moved, include_memory_cues)
    wanted = {
        "differences": (cue_compare.DIFFERS, cue_compare.REKORDBOX_ONLY, cue_compare.SERATO_ONLY),
        "all": tuple(i.status for i in comparison.items),
    }.get(show, (show,))
    order = {cue_compare.DIFFERS: 0, cue_compare.REKORDBOX_ONLY: 1, cue_compare.SERATO_ONLY: 2}
    matching = sorted(
        comparison.with_status(*wanted), key=lambda i: (order.get(i.status, 3), (i.title or "").casefold(), i.track_id)
    )
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    page = matching[offset : offset + limit]
    counts = comparison.counts

    return {
        "cuesFrom": library.label,
        "cueNotes": library.notes + ([scope_note] if scope_note else []),
        "tracksCompared": len(comparison.items),
        "match": counts[cue_compare.MATCH],
        "differ": counts[cue_compare.DIFFERS],
        "cuesOnlyInRekordbox": counts[cue_compare.REKORDBOX_ONLY],
        "cuesOnlyInSerato": counts[cue_compare.SERATO_ONLY],
        "noCuesAnywhere": counts[cue_compare.NO_CUES],
        "skipped": counts[cue_compare.SKIPPED],
        "skippedBecause": dict(comparison.skip_reasons),
        "differencesByKind": dict(comparison.difference_kinds),
        "loopsNotCompared": comparison.loops_not_compared,
        "shown": show,
        "total": len(matching),
        "returned": len(page),
        "hasMore": offset + len(page) < len(matching),
        "tracks": [
            {
                "trackId": i.track_id,
                "title": i.title,
                "artist": i.artist,
                "file": str(i.path) if i.path else None,
                "status": i.status,
                "note": i.reason,
                "rekordboxCues": i.rekordbox_cues,
                "seratoCues": i.serato_cues,
                "differences": [
                    {"slot": d.slot, "kind": d.kind, "rekordbox": d.rekordbox, "serato": d.serato, "deltaMs": d.delta_ms}
                    for d in i.differences
                ],
            }
            for i in page
        ],
        "summary": (
            f"Compared {len(comparison.items)} tracks: {counts[cue_compare.MATCH]} match, {counts[cue_compare.DIFFERS]} differ, "
            f"{counts[cue_compare.REKORDBOX_ONLY]} have cues only in Rekordbox, {counts[cue_compare.SERATO_ONLY]} only in Serato, "
            f"{counts[cue_compare.SKIPPED]} skipped. Nothing was changed."
            + (
                f" sync_cues_to_serato would rewrite Serato's cues on {counts[cue_compare.DIFFERS] + counts[cue_compare.REKORDBOX_ONLY]} of "
                "these tracks (Rekordbox wins, so Serato-only cues on them are removed)."
                if counts[cue_compare.DIFFERS] + counts[cue_compare.REKORDBOX_ONLY] else ""
            )
            + (
                f" The {counts[cue_compare.SERATO_ONLY]} Serato-only tracks have no Rekordbox cues, so the sync leaves them alone."
                if counts[cue_compare.SERATO_ONLY] else ""
            )
        ),
    }


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def sync_cues_to_serato(
    dry_run: bool = True,
    expected_files: int | None = None,
    track_ids: list[str] | None = None,
    include_memory_cues: bool = True,
    use_apple_music_paths: bool = True,
    cue_source: str = "auto",
) -> dict:
    """Copy Rekordbox's cues into the Serato tags of the audio files.

    The cues are read from Rekordbox's live database when it can be read (no XML
    export needed, and always current), otherwise from the loaded XML export.
    `cue_source` forces one: "database" or "xml" ("auto" is the default).

    Rekordbox wins: for every track that has cues in Rekordbox, the hot cues in
    the file's Serato tag are replaced by the Rekordbox hot cues (same slots),
    plus the memory cues in the free slots when include_memory_cues is true.
    Positions get the timing correction for the file type. Tracks with no cues
    in Rekordbox are never touched. MP3 and WAV only.

    This edits the user's audio files, so be careful:
    - Call it with dry_run=true first (the default). It changes nothing and
      reports how many files would change, which are skipped and why, and which
      files had a timing correction it could not verify.
    - It also counts the existing Serato cues that would be removed or moved to
      another time. Always tell the user those numbers.
    - Show the user that summary and get their go-ahead. Then call again with
      dry_run=false and expected_files set to the "filesToChange" number from
      the dry run. If the number no longer matches, nothing is written.
    - Serato DJ Pro must be closed, otherwise it can overwrite the change. The
      dry run reports whether it is running.
    - Only the 470-byte cue tag is overwritten. The original bytes are saved to
      a journal first, each file is read back to check nothing else changed, a
      file that fails the check is restored at once, and undo_serato_sync
      reverses a whole run.

    `track_ids` limits the sync to some tracks (IDs from search_tracks).
    `use_apple_music_paths`: when an Apple Music library is loaded, a track
    whose file is missing at the Rekordbox path is synced at the path Apple
    Music has for it (see find_missing_files).
    """
    library = read_cue_source(cue_source)
    music = apple_library() if use_apple_music_paths else None
    moved = (
        library_reports.moved_file_paths(library_reports.find_missing_rekordbox_files(library.tracks, music))
        if music
        else None
    )
    plan = serato_sync.build_sync_plan(
        library.tracks, track_ids=track_ids, include_memory_cues=include_memory_cues, moved_files=moved
    )

    changes = plan.to_change
    running = serato_sync.is_serato_running()
    losing = [i for i in changes if i.removed or i.moved]
    removed = sum(len(i.removed) for i in changes)
    moved_count = sum(len(i.moved) for i in changes)

    result = {
        "dryRun": dry_run,
        "cuesFrom": library.label,
        "cueNotes": library.notes,
        "filesToChange": len(changes),
        "existingSeratoCuesRemoved": removed,
        "existingSeratoCuesMoved": moved_count,
        "filesWhereExistingCuesAreRemovedOrMoved": len(losing),
        "removedOrMovedExamples": [
            {
                "title": i.title,
                "artist": i.artist,
                "removed": serato_sync.describe_times(i.removed),
                "moved": [
                    f"slot {slot}: {format_time(old)} to {format_time(new)}" for slot, old, new in i.moved
                ],
            }
            for i in sorted(losing, key=lambda i: -len(i.removed))[:10]
        ],
        "filesAlreadyUpToDate": len(plan.with_status("unchanged")),
        "filesSkipped": len(plan.with_status("skip")),
        "skippedBecause": dict(plan.skip_reasons),
        "seratoIsRunning": running,
        "loopsIgnored": plan.loops_ignored,
        "foundViaAppleMusic": sum(1 for i in plan.items if i.relocated_from and i.status != "skip"),
        "tracksNotPlannedBecauseOfTheFileLimit": plan.not_planned,
        "timingNotVerified": [
            {"title": i.title, "artist": i.artist, "note": i.timing.note} for i in plan.flagged_timing[:10]
        ],
        "examples": [
            {
                "title": i.title,
                "artist": i.artist,
                "file": str(i.path),
                "seratoNow": serato_sync.describe_times(i.before),
                "afterSync": serato_sync.describe_times(i.after),
            }
            for i in changes[:5]
        ],
    }

    if not changes:
        result["summary"] = "No Serato file needs to change, and nothing was written."
        return result

    warning = " Serato DJ Pro is running; quit it before applying." if running else ""
    loss = (
        f"Warning: this removes {removed} existing Serato cue(s) and moves {moved_count}, on {len(losing)} file(s). "
        if losing
        else ""
    )

    if dry_run:
        result["summary"] = (
            f"{loss}Would update the cues in {len(changes)} audio files (Rekordbox wins). "
            f"{len(plan.with_status('unchanged'))} already match and {len(plan.with_status('skip'))} are skipped. "
            f"Nothing has been written.{warning}"
        )
        return result

    if expected_files != len(changes):
        raise ToolError(
            f"This would change {len(changes)} files, but expected_files was {expected_files!r}. "
            "Run a dry run, show the user the number, and pass it as expected_files."
        )

    outcome = serato_sync.apply_sync_plan(plan)

    result["filesWritten"] = len(outcome.verified)
    result["filesThatFailedAndWereRestored"] = [
        {"title": i.title, "file": str(i.path), "problem": why} for i, why in outcome.failed
    ]
    result["journal"] = str(outcome.journal)
    result["summary"] = (
        f"Updated and verified the cues in {len(outcome.verified)} audio files"
        + (f"; {len(outcome.failed)} failed and were left as they were" if outcome.failed else "")
        + f". To reverse this, call undo_serato_sync (journal: {outcome.journal})."
    )
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def undo_serato_sync(journal: str | None = None, dry_run: bool = True) -> dict:
    """Reverse a sync_cues_to_serato run by putting back the original cue bytes.

    `journal` is the journal path the sync reported; by default the most recent
    one. A file whose cue tag was changed again afterwards (for example by Serato)
    is left alone. `dry_run` is true by default and only reports what would be
    restored; ask the user before calling it with dry_run=false. Serato DJ Pro
    must be closed.
    """
    path = Path(journal).expanduser() if journal else serato_sync.latest_journal()

    if path is None:
        raise ToolError("There is no sync journal to undo.")

    outcome = serato_sync.undo_sync(path, dry_run=dry_run)

    return {
        "dryRun": dry_run,
        "journal": str(path),
        "filesRestored" if not dry_run else "filesThatWouldBeRestored": len(outcome.restored),
        "filesAlreadyOriginal": len(outcome.already_original),
        "filesLeftAlone": [{"file": f, "why": why} for f, why in outcome.skipped],
        "summary": (
            f"{'Would restore' if dry_run else 'Restored'} the original cues in {len(outcome.restored)} files; "
            f"{len(outcome.already_original)} were already original and {len(outcome.skipped)} were left alone."
        ),
    }
