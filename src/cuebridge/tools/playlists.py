"""Playlists: the XML export, the database sync, and the review playlists (each with its undo)."""

from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cuebridge.services.rekordbox import db as rekordbox_db
from cuebridge.services.rekordbox import editor as rekordbox_editor
from cuebridge.services.rekordbox import playlist_export
from cuebridge.services.rekordbox import service as rekordbox_service
from cuebridge.tools.common import (
    current_drift,
    db_target,
    mcp,
    require_apple_library,
    require_library,
    translate_errors,
)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def export_playlists_for_rekordbox(
    playlists: list[str] | None = None,
    all_playlists: bool = False,
    output_path: str | None = None,
    folder_name: str = "Apple Music",
    dry_run: bool = True,
    allow_stale_export: bool = False,
) -> dict:
    """Add Apple Music playlists to the Rekordbox XML, ready to import in one step.

    Needs both libraries loaded. By default it includes only the playlists
    that differ between Apple Music and Rekordbox (new playlists, or ones with
    added or removed tracks); `playlists` names specific ones, and
    `all_playlists` includes every one. Only playlisted tracks are involved.
    The file holds one folder (`folder_name`) with those playlists. Tracks that
    Rekordbox already has are copied as they are, with their cues and beat
    grids; tracks it doesn't have are added from Apple Music's data. Tracks
    whose file can't be found are left out and listed.

    By default the playlists are added to the loaded Rekordbox XML itself (the
    file Rekordbox's 'Imported Library' setting points at), the same way the
    other editing tools work: a timestamped backup of a fresh export is saved
    first, the write is atomic, and a file that changed since the plan is
    refused. The folder and the new tracks are marked as pending, so
    load_rekordbox_library and compare_libraries don't count them as already in
    Rekordbox until the user has imported them and exported afresh. Running it
    again replaces the earlier folder. Pass `output_path` to write a separate
    small .xml file instead and leave the Rekordbox export alone. Rekordbox's own
    database is never touched.

    The loaded export is refused if Rekordbox's database has changed since it
    was made (more than five minutes newer), because a plan built on an old
    export is wrong. Ask the user to export again (File > Export Collection in
    xml format) and load it again. `allow_stale_export=true` overrides that.

    Run it with dry_run=true first and show the user the summary.

    To import (steps for the user): in Rekordbox, with Preferences > Advanced >
    Database > rekordbox xml > Imported Library pointing at the file, show the
    'rekordbox xml' item in the sidebar (reload it), expand it, right-click the
    folder and choose Import Playlist. Whether re-importing a playlist that
    already exists adds a copy or merges has not been tested.
    """
    rekordbox, music = require_library(), require_apple_library()
    stale = None if allow_stale_export else rekordbox_db.stale_export_message(rekordbox.path)

    if stale:
        raise ToolError(stale + " (Or pass allow_stale_export=true to use it anyway.)")

    result = current_drift()
    plan = playlist_export.plan_export(rekordbox, music, result, names=playlists, all_playlists=all_playlists)
    in_place = output_path is None

    summary = {
        "dryRun": dry_run,
        "mode": "added to the Rekordbox XML" if in_place else "separate file",
        "folder": folder_name,
        "playlists": [
            {"name": p.name, "tracks": len(p.keys), "why": p.reason} for p in plan.playlists
        ],
        "alreadyInSync": plan.in_sync,
        "tracksAlreadyInRekordbox": len(plan.existing_ids),
        "newTracksForRekordbox": len(plan.new_tracks),
        "newTrackExamples": [
            f"{t.artist + ' - ' if t.artist else ''}{t.title}" for _, t in plan.new_tracks[:10]
        ],
        "movedFilesFixed": len(plan.relocated),
        "leftOut": [{"playlist": p, "track": t, "why": why} for p, t, why in plan.skipped[:20]],
        "duplicateEntriesRemoved": plan.duplicates_removed,
    }

    if not plan.playlists:
        summary["file"] = str(rekordbox.path)
        summary["summary"] = (
            f"Nothing to export: all {len(plan.in_sync)} playlists already match Rekordbox. "
            "Pass playlists=[...] or all_playlists=true to export some anyway."
        )
        return summary

    what = (
        f"{len(plan.playlists)} playlists ({len(plan.existing_ids)} tracks Rekordbox has, "
        f"{len(plan.new_tracks)} new) in the folder {folder_name!r}"
    )

    if in_place:
        file_plan = playlist_export.build_file_plan(plan, rekordbox, folder_name)
        target, will_backup = file_plan.target.target, rekordbox_editor.needs_backup(file_plan)
        summary.update(
            file=str(target),
            replacesEarlierFolder=file_plan.tracks_replaced > 0 or rekordbox.pending_folder is not None,
            backupWillBeSaved=will_backup,
        )

        if dry_run:
            summary["summary"] = (
                f"Would add {what} to {target}"
                + (" (a backup is saved first)" if will_backup else "")
                + ". Nothing has been written."
            )
            return summary

        written = rekordbox_editor.write_plan(file_plan)
        rekordbox_service.write_pending_note(written.path, folder_name, file_plan.xml)
        summary["backup"] = str(written.backup) if written.backup else None
        summary["summary"] = (
            f"Added {what} to {written.path}"
            + (f" (backup: {written.backup})" if written.backup else "")
            + ". In Rekordbox, reload the 'rekordbox xml' item in the sidebar, right-click the folder and choose Import Playlist."
        )
        return summary

    target = playlist_export.check_output_path(Path(output_path), rekordbox)
    data = playlist_export.build_xml(plan, playlist_export.export_source_bytes(rekordbox), folder_name)
    playlist_export.verify_xml(data, plan)
    summary.update(file=str(target), sizeKilobytes=round(len(data) / 1024, 1), wouldReplaceEarlierFile=target.exists())

    if dry_run:
        summary["summary"] = f"Would write {what} to {target}. Nothing has been written."
        return summary

    playlist_export.write_export(data, target)
    summary["summary"] = (
        f"Wrote {what} to {target}. In Rekordbox, point Preferences > Advanced > Database > rekordbox xml at it, "
        "then right-click the folder in the 'rekordbox xml' sidebar item and choose Import Playlist."
    )
    return summary


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def sync_playlists_to_rekordbox(
    playlists: list[str] | None = None,
    dry_run: bool = True,
    expected_additions: int | None = None,
    database_path: str | None = None,
) -> dict:
    """Add tracks that are in an Apple Music playlist but missing from the same Rekordbox playlist.

    This edits Rekordbox's own database (master.db), so be careful. It needs the
    Apple Music library loaded (load_apple_music_library) and needs no Rekordbox
    XML export: it reads the live database through a copy. It only ever ADDS a
    track that is already in the Rekordbox collection to the END of an existing
    top-level playlist with the same name. It never removes tracks, creates
    playlists, or touches tracks, files, cues or other playlists. Tracks Rekordbox
    doesn't have yet, and playlists it doesn't have, are reported, not created
    (a new track must first be imported, for example with
    export_playlists_for_rekordbox).

    - Call it with dry_run=true first (the default). Rekordbox may be open for
      that. It lists exactly what would be added where. Show the user.
    - Rekordbox must be closed to write. Then call with dry_run=false and
      expected_additions set to the "additions" count from the dry run.
    - Before writing, the database files are copied to ~/.cuebridge/db-backups
      and the copy is checked; afterwards the database is read again and
      everything must be exactly as planned, otherwise the copy is put back.
      undo_rekordbox_playlist_sync removes the added rows.

    `playlists` limits it to some playlist names. `database_path` overrides
    where master.db is (it is found from Rekordbox's settings).
    """
    music = require_apple_library()
    db_path = db_target(database_path)
    backend = rekordbox_db.default_backend()
    snapshot = rekordbox_db.read_snapshot(db_path, backend)
    plan = rekordbox_db.plan_additions(snapshot, music, playlists)
    running = rekordbox_db.rekordbox_running()

    result = {
        "dryRun": dry_run,
        "database": str(db_path),
        "rekordboxIsRunning": running,
        "additions": len(plan.additions),
        "willAdd": [
            {"playlist": a.playlist_name, "track": a.title, "artist": a.artist, "position": a.position}
            for a in plan.additions[:50]
        ],
        "notInTheRekordboxCollection": [{"playlist": p, "track": t} for p, t in plan.not_in_collection[:20]],
        "playlistsRekordboxDoesntHave": plan.missing_playlists,
        "ambiguousPlaylistNames": plan.ambiguous,
        "tracksOnlyRekordboxHas": plan.extra_in_rekordbox,
        "playlistsAlreadyInSync": len(plan.in_sync),
    }

    if not plan.additions:
        result["summary"] = "Nothing to add: the Rekordbox playlists already contain what Apple Music's do (that Rekordbox has)."
        return result

    if dry_run:
        result["summary"] = (
            f"Would add {len(plan.additions)} track(s) to existing Rekordbox playlists. Nothing has been written."
            + (" Quit Rekordbox before applying." if running else "")
        )
        return result

    if expected_additions != len(plan.additions):
        raise ToolError(
            f"This would add {len(plan.additions)} tracks, but expected_additions was {expected_additions!r}. "
            "Run a dry run, show the user the number, and pass it as expected_additions."
        )

    outcome = rekordbox_db.apply_plan(plan, db_path, music, backend, playlists)
    result["added"] = outcome.added
    result["backup"] = str(outcome.backup)
    result["journal"] = str(outcome.journal)
    result["summary"] = (
        f"Added {outcome.added} track(s) and verified the database. Backup: {outcome.backup}. "
        f"To reverse this, call undo_rekordbox_playlist_sync (journal: {outcome.journal}). You can open Rekordbox now."
    )
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def undo_rekordbox_playlist_sync(journal: str | None = None, dry_run: bool = True) -> dict:
    """Remove the playlist entries a sync_playlists_to_rekordbox run added.

    `journal` is the path it reported; the most recent one is used if omitted.
    Only the exact rows that run added are removed, and only if they are still
    there unchanged. Rekordbox must be closed unless this is a dry run.
    """
    path = Path(journal).expanduser() if journal else rekordbox_db.latest_journal()

    if path is None or not path.is_file():
        raise ToolError("No Rekordbox playlist journal found. Pass the journal path from the sync.")

    outcome = rekordbox_db.undo(path, rekordbox_db.default_backend(), dry_run=dry_run)

    return {
        "dryRun": dry_run,
        "journal": str(path),
        "removed" if not dry_run else "wouldRemove": outcome.removed,
        "skipped": [{"entry": e, "why": why} for e, why in outcome.skipped],
        "summary": (
            f"{'Would remove' if dry_run else 'Removed'} {len(outcome.removed)} entr"
            f"{'y' if len(outcome.removed) == 1 else 'ies'}; {len(outcome.skipped)} skipped."
        ),
    }


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
@translate_errors
def create_review_playlists(
    dry_run: bool = True,
    expected_tracks: int | None = None,
    database_path: str | None = None,
) -> dict:
    """Make two temporary Rekordbox playlists so the user can remove stale duplicate rows in Rekordbox itself.

    Needs the Apple Music library loaded. The broken rows that have a twin
    (see report_stale_duplicates) are put in:
    - "CueBridge: remove (safe)": rows with no cue or playlist entry their twin
      lacks. The user selects them all in Rekordbox and removes them from the
      collection.
    - "CueBridge: check cues first": rows that hold cues their twin lacks. The
      user decides what to do with these.
    This only ADDS two playlists to master.db; no track, cue or other playlist is
    changed, and CueBridge never deletes a track. Rekordbox must be closed to
    write; backup, read-back verification (restoring the backup on any
    difference) and a journal work as in sync_playlists_to_rekordbox. Call it with
    dry_run=true first, show the user the numbers, then dry_run=false with
    expected_tracks set to "tracks". When the user has finished, call
    undo_rekordbox_review_playlists to remove the two playlists.
    """
    music = require_apple_library()
    db_path = db_target(database_path)
    backend = rekordbox_db.default_backend()
    snapshot = rekordbox_db.read_snapshot(db_path, backend)
    plan = rekordbox_db.plan_review_playlists(snapshot, music)
    running = rekordbox_db.rekordbox_running()
    total = len(plan.safe) + len(plan.merge)

    def listing(rows):
        return [{"track": t, "artist": a} for _, t, a in rows[:60]]

    result = {
        "dryRun": dry_run,
        "database": str(db_path),
        "rekordboxIsRunning": running,
        "tracks": total,
        "safeToRemove": {"playlist": rekordbox_db.SAFE_PLAYLIST, "count": len(plan.safe), "tracks": listing(plan.safe)},
        "checkCuesFirst": {"playlist": rekordbox_db.MERGE_PLAYLIST, "count": len(plan.merge), "tracks": listing(plan.merge)},
    }

    if not total:
        result["summary"] = "Nothing to review: no broken row has a twin holding its real file."
        return result

    if dry_run:
        result["summary"] = (
            f"Would add two playlists holding {total} track(s). Nothing has been written."
            + (" Quit Rekordbox before applying." if running else "")
        )
        return result

    if expected_tracks != total:
        raise ToolError(
            f"This would list {total} tracks, but expected_tracks was {expected_tracks!r}. "
            "Run a dry run, show the user the number, and pass it as expected_tracks."
        )

    outcome = rekordbox_db.apply_review_playlists(plan, db_path, music, backend)
    result["backup"] = str(outcome.backup)
    result["journal"] = str(outcome.journal)
    result["summary"] = (
        f"Added the review playlists ({total} tracks) and verified the database. Backup: {outcome.backup}. "
        f"When the user is done, call undo_rekordbox_review_playlists (journal: {outcome.journal}). You can open Rekordbox now."
    )
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@translate_errors
def undo_rekordbox_review_playlists(journal: str | None = None, dry_run: bool = True) -> dict:
    """Remove the two review playlists create_review_playlists made (never any track).

    `journal` is the path it reported; the most recent one is used if omitted.
    A playlist is only removed if it still has the name it was given and holds
    only tracks that run put there. Rekordbox must be closed unless this is a
    dry run.
    """
    path = Path(journal).expanduser() if journal else rekordbox_db.latest_journal(prefix=rekordbox_db.REVIEW_JOURNALS)

    if path is None or not path.is_file():
        raise ToolError("No review-playlist journal found. Pass the journal path from create_review_playlists.")

    outcome = rekordbox_db.undo_review_playlists(path, rekordbox_db.default_backend(), dry_run=dry_run)

    return {
        "dryRun": dry_run,
        "journal": str(path),
        "removed" if not dry_run else "wouldRemove": outcome.removed,
        "skipped": [{"playlist": e, "why": why} for e, why in outcome.skipped],
        "summary": f"{'Would remove' if dry_run else 'Removed'} {len(outcome.removed)} playlist(s); {len(outcome.skipped)} skipped.",
    }
