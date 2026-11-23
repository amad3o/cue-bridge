"""Repairing Rekordbox after Apple Music renames files: moved files and stale duplicates (each with its undo)."""

from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cuebridge.services.rekordbox import db as rekordbox_db
from cuebridge.tools.common import (
    READ_ONLY,
    db_target,
    mcp,
    require_apple_library,
    translate_errors,
)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def fix_moved_files_in_rekordbox(
    dry_run: bool = True,
    expected_fixes: int | None = None,
    database_path: str | None = None,
) -> dict:
    """Point Rekordbox tracks whose file was renamed or moved at the file Apple Music has.

    This edits Rekordbox's own database (master.db). It needs the Apple Music
    library loaded (load_apple_music_library). For every Rekordbox track whose
    file is missing, the real file is found through Apple Music (same title and
    artist, or the same file name), and only the track's path and file name are
    changed. Cues, beat grids, playlists and everything else stay as they are.
    The analysis files Rekordbox keeps under PIONEER/USBANLZ are not touched.

    Not every broken track can be fixed this way, and the result says why:
    - "alreadyInTheCollection": Rekordbox already has another row for the real
      file (a duplicate left behind by the rename). Nothing is changed, because
      repointing would make a second duplicate. Those are for the user to
      decide on (the older row may hold cues or playlist entries).
    - "noFileFound": Apple Music has no matching file.

    - Call it with dry_run=true first (the default). Rekordbox may be open for
      that. Show the user the list.
    - Rekordbox must be closed to write. Then call with dry_run=false and
      expected_fixes set to the "fixes" count from the dry run.
    - Backup, journal and read-back verification work as in
      sync_playlists_to_rekordbox; undo_rekordbox_file_fix reverses it.
    """
    music = require_apple_library()
    db_path = db_target(database_path)
    backend = rekordbox_db.default_backend()
    snapshot = rekordbox_db.read_snapshot(db_path, backend)
    plan = rekordbox_db.plan_relocations(snapshot, music)
    running = rekordbox_db.rekordbox_running()

    result = {
        "dryRun": dry_run,
        "database": str(db_path),
        "rekordboxIsRunning": running,
        "fixes": len(plan.relocations),
        "willFix": [
            {"track": r.title, "artist": r.artist, "from": r.old_path, "to": r.new_path}
            for r in plan.relocations[:50]
        ],
        "alreadyInTheCollection": len(plan.already_in_collection),
        "alreadyInTheCollectionExamples": [
            {"track": t, "realFile": p} for t, p in plan.already_in_collection[:10]
        ],
        "noFileFound": len(plan.no_match),
        "noFileFoundExamples": [{"track": t, "path": p} for t, p in plan.no_match[:10]],
    }

    if not plan.relocations:
        result["summary"] = "Nothing to fix: no missing file has a single, unused real file in Apple Music."
        return result

    if dry_run:
        result["summary"] = (
            f"Would repoint {len(plan.relocations)} track(s) at their real files. Nothing has been written."
            + (" Quit Rekordbox before applying." if running else "")
        )
        return result

    if expected_fixes != len(plan.relocations):
        raise ToolError(
            f"This would fix {len(plan.relocations)} tracks, but expected_fixes was {expected_fixes!r}. "
            "Run a dry run, show the user the number, and pass it as expected_fixes."
        )

    outcome = rekordbox_db.apply_relocations(plan, db_path, music, backend)
    result["fixed"] = outcome.added
    result["backup"] = str(outcome.backup)
    result["journal"] = str(outcome.journal)
    result["summary"] = (
        f"Repointed {outcome.added} track(s) and verified the database. Backup: {outcome.backup}. "
        f"To reverse this, call undo_rekordbox_file_fix (journal: {outcome.journal}). You can open Rekordbox now."
    )
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def undo_rekordbox_file_fix(journal: str | None = None, dry_run: bool = True) -> dict:
    """Put back the old paths that a fix_moved_files_in_rekordbox run changed.

    `journal` is the path it reported; the most recent one is used if omitted.
    A track is only restored if it still has the path that run wrote. Rekordbox
    must be closed unless this is a dry run.
    """
    path = Path(journal).expanduser() if journal else rekordbox_db.latest_journal(prefix=rekordbox_db.PATH_JOURNALS)

    if path is None or not path.is_file():
        raise ToolError("No file-location journal found. Pass the journal path from the fix.")

    outcome = rekordbox_db.undo_relocations(path, rekordbox_db.default_backend(), dry_run=dry_run)

    return {
        "dryRun": dry_run,
        "journal": str(path),
        "restored" if not dry_run else "wouldRestore": outcome.removed,
        "skipped": [{"entry": e, "why": why} for e, why in outcome.skipped],
        "summary": f"{'Would restore' if dry_run else 'Restored'} {len(outcome.removed)} track(s); {len(outcome.skipped)} skipped.",
    }


@mcp.tool(annotations=READ_ONLY)
@translate_errors
def report_stale_duplicates(database_path: str | None = None, limit: int = 25) -> dict:
    """List Rekordbox tracks whose file is missing but whose real file Rekordbox already has under another row.

    Read-only. These are duplicates left behind when files were renamed. For
    each pair it shows what the broken row and the good row have: hot and memory
    cues, and which playlists they are in. "safe_to_remove" means the broken row
    has no cue and no playlist entry that the good row lacks; "needs_merge"
    means it does, so removing it would lose something. Nothing is changed and
    this tool cannot remove anything. Needs the Apple Music library loaded.
    """
    music = require_apple_library()
    snapshot = rekordbox_db.read_snapshot(db_target(database_path), rekordbox_db.default_backend())
    pairs = rekordbox_db.describe_duplicates(rekordbox_db.plan_relocations(snapshot, music), snapshot)
    pairs.sort(key=lambda p: (p.verdict, p.title or ""))

    def row(r) -> dict:
        return {"id": r.content_id, "hotCues": r.hot_cues, "memoryCues": r.memory_cues, "playlists": list(r.playlists)}

    return {
        "pairs": len(pairs),
        "safeToRemove": sum(1 for p in pairs if p.verdict == "safe_to_remove"),
        "needsMerge": sum(1 for p in pairs if p.verdict == "needs_merge"),
        "details": [
            {
                "track": p.title, "artist": p.artist, "verdict": p.verdict,
                "brokenRow": {**row(p.broken), "missingPath": p.broken.path},
                "goodRow": row(p.good),
                "cuesOnlyOnBrokenRow": p.cues_only_on_broken,
                "playlistsOnlyOnBrokenRow": list(p.playlists_only_on_broken),
            }
            for p in pairs[: max(1, min(limit, 100))]
        ],
        "hasMore": len(pairs) > limit,
    }


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
@translate_errors
def clean_up_stale_duplicates(
    dry_run: bool = True,
    expected_removals: int | None = None,
    expected_cue_copies: int | None = None,
    database_path: str | None = None,
) -> dict:
    """Remove stale duplicate rows from Rekordbox's database, and copy cues their twins lack.

    This edits master.db and DELETES rows, so be careful. Needs the Apple
    Music library loaded. It works on the broken rows that report_stale_duplicates
    lists (a missing file whose real file Rekordbox already has under another row):
    - a broken row with no cue and no playlist entry its twin lacks is REMOVED,
      with the cues, analysis-file records, mixer settings and playlist entries
      that belong to it (the audio files and analysis files on disk are never
      touched; CueBridge's own review playlists don't count as use);
    - a broken row with cues its twin lacks has those cues COPIED to the twin
      (both places Rekordbox stores cues) and is KEPT;
    - a broken row that is in a real playlist its twin isn't in is left alone and
      reported.

    - Call it with dry_run=true first (the default). Rekordbox may be open for
      that. Show the user the numbers and the track names.
    - Rekordbox must be closed to write. Then call with dry_run=false and
      expected_removals / expected_cue_copies set to the counts from the dry run.
    - Backup, read-back verification (restoring the backup on any difference) and a
      journal that holds every removed row work as in the other database tools;
      undo_stale_duplicate_cleanup puts the rows back and removes the copied cues.
    """
    music = require_apple_library()
    db_path = db_target(database_path)
    backend = rekordbox_db.default_backend()
    snapshot = rekordbox_db.read_snapshot(db_path, backend)
    plan = rekordbox_db.plan_cleanup(snapshot, music)
    running = rekordbox_db.rekordbox_running()

    result = {
        "dryRun": dry_run,
        "database": str(db_path),
        "rekordboxIsRunning": running,
        "removals": len(plan.removals),
        "willRemove": [{"track": r.title, "artist": r.artist, "missingPath": r.path} for r in plan.removals[:60]],
        "cueCopies": len(plan.merges),
        "willCopyCues": [{"track": m.title, "cues": len(m.cue_ids)} for m in plan.merges],
        "leftAlone": [{"track": t, "why": why} for t, why in plan.skipped],
    }

    if not plan.removals and not plan.merges:
        result["summary"] = "Nothing to clean up."
        return result

    if dry_run:
        result["summary"] = (
            f"Would remove {len(plan.removals)} stale row(s) and copy cues to {len(plan.merges)} track(s). "
            "Nothing has been written." + (" Quit Rekordbox before applying." if running else "")
        )
        return result

    if (expected_removals, expected_cue_copies) != (len(plan.removals), len(plan.merges)):
        raise ToolError(
            f"This would remove {len(plan.removals)} rows and copy cues to {len(plan.merges)} tracks, but "
            f"expected_removals was {expected_removals!r} and expected_cue_copies {expected_cue_copies!r}. "
            "Run a dry run, show the user the numbers, and pass them."
        )

    outcome = rekordbox_db.apply_cleanup(plan, db_path, music, backend)
    result["backup"] = str(outcome.backup)
    result["journal"] = str(outcome.journal)
    result["summary"] = (
        f"Removed {len(plan.removals)} row(s), copied cues to {len(plan.merges)} track(s), and verified the database. "
        f"Backup: {outcome.backup}. To reverse this, call undo_stale_duplicate_cleanup (journal: {outcome.journal}). "
        "You can open Rekordbox now."
    )
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def undo_stale_duplicate_cleanup(journal: str | None = None, dry_run: bool = True) -> dict:
    """Reverse a clean_up_stale_duplicates run: take the copied cues off and put the removed rows back.

    `journal` is the path it reported; the most recent one is used if omitted.
    A removed row is only put back if no track has its ID now; copied cues are
    only removed if they are still there. Rekordbox must be closed unless this is
    a dry run.
    """
    path = Path(journal).expanduser() if journal else rekordbox_db.latest_journal(prefix=rekordbox_db.CLEANUP_JOURNALS)

    if path is None or not path.is_file():
        raise ToolError("No cleanup journal found. Pass the journal path from clean_up_stale_duplicates.")

    outcome = rekordbox_db.undo_cleanup(path, rekordbox_db.default_backend(), dry_run=dry_run)

    return {
        "dryRun": dry_run,
        "journal": str(path),
        "undone" if not dry_run else "wouldUndo": outcome.removed,
        "skipped": [{"item": e, "why": why} for e, why in outcome.skipped],
        "summary": f"{'Would undo' if dry_run else 'Undid'} {len(outcome.removed)} change(s); {len(outcome.skipped)} skipped.",
    }
