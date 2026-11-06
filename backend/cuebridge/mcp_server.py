"""CueBridge MCP server.

Run it over stdio with:  python -m cuebridge.mcp_server

All logic lives in cuebridge.services; the tools here only keep track of the
currently loaded library and translate errors for the assistant.
"""

import functools
from dataclasses import dataclass
from datetime import datetime
from collections import Counter
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cuebridge.services import drift, library_queries, library_reports, playlist_export, rekordbox_db, rekordbox_editor, serato_sync, sync_check, db_library
from cuebridge.parsers.apple_music_xml import MusicLibrary
from cuebridge.services import apple_music_live
from cuebridge.services import rekordbox_service
from cuebridge.services.apple_music_service import get_apple_music_library
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
        "go-ahead, before calling them with dry_run=false. "
        "sync_cues_to_serato goes further and edits the user's audio files: "
        "Serato DJ Pro must be closed, and dry_run=false needs expected_files "
        "from the dry run. undo_serato_sync reverses it."
    ),
)

_current_path: Path | None = None
_apple_path: Path | None = None
_apple_live: MusicLibrary | None = None

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def reset_state() -> None:
    """Forget the loaded library. Used by tests."""
    global _current_path, _apple_path, _apple_live
    _current_path = None
    _apple_path = None
    _apple_live = None


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


def _apple_library() -> MusicLibrary | None:
    if _apple_live is not None:
        return _apple_live

    return None if _apple_path is None else get_apple_music_library(_apple_path)


def _require_apple_library() -> MusicLibrary:
    library = _apple_library()

    if library is None:
        raise ToolError(
            "No Apple Music library is loaded. Call load_apple_music_library first (with no path to read "
            "the open Music app, or with the path to an exported XML)."
        )

    return library


@mcp.tool(annotations=READ_ONLY)
@_translate_errors
def load_apple_music_library(path: str | None = None) -> dict:
    """Load the Apple Music library and return a summary of it.

    With no `path` it reads the Music app directly, so no export is needed: the
    Music app must be open (it is never launched), macOS asks for permission the
    first time, and it takes about half a minute. Nothing in Music is changed.
    Call it again to pick up changes made in Music since.

    With a `path` it reads an XML exported from Music instead (File > Library >
    Export Library): the full path to that .xml file.

    The summary lists the track count, formats, gaps in the metadata and every
    playlist with its size. The library stays loaded for get_playlist_tracks,
    find_missing_files, compare_libraries and the playlist export.
    """
    global _apple_path, _apple_live

    if path:
        library = get_apple_music_library(path)
        _apple_path, _apple_live = library.path, None
    else:
        library = apple_music_live.read_live_library()
        _apple_path, _apple_live = None, library

    summary = library_reports.music_library_summary(library)
    summary["source"] = str(library.path) if library.path else "the Music app (live)"
    summary["summary"] = (
        f"Loaded {summary['trackCount']} tracks and {summary['playlistCount']} playlists "
        f"({summary['totalHours']} hours, {summary['totalGigabytes']} GB)."
    )
    return summary


@mcp.tool(annotations=READ_ONLY)
@_translate_errors
def get_playlist_tracks(name: str, limit: int = 50, offset: int = 0) -> dict:
    """List the tracks of one Apple Music playlist, in playlist order, a page at a time.

    `name` is matched ignoring case; use the names from load_apple_music_library.
    `limit` is capped at 100.
    """
    library = _require_apple_library()
    wanted = name.strip().casefold()
    matches = [p for p in library.user_playlists if p.name.casefold() == wanted]

    if not matches:
        raise ToolError(f"No playlist named {name!r}. Names: " + ", ".join(p.name for p in library.user_playlists))

    playlist = matches[0]
    limit = max(1, min(limit, library_queries.MAX_LIMIT))
    offset = max(0, offset)
    ids = playlist.track_ids[offset : offset + limit]

    return {
        "playlist": playlist.name,
        "total": len(playlist.track_ids),
        "offset": offset,
        "returned": len(ids),
        "hasMore": offset + limit < len(playlist.track_ids),
        "tracks": [library_reports.music_track_to_dict(library.tracks_by_id[i]) for i in ids if i in library.tracks_by_id],
    }


@mcp.tool(annotations=READ_ONLY)
@_translate_errors
def find_missing_files(only_with_cues: bool = True, limit: int = 25, offset: int = 0) -> dict:
    """List Rekordbox tracks whose audio file isn't where Rekordbox says, and where Apple Music has it.

    Needs both libraries loaded. A track is matched to Apple Music by path,
    then file name, then title and artist (a match must be unambiguous).
    `only_with_cues` (default true) limits it to tracks that have cues, which
    are the ones that matter for syncing. `limit` is capped at 100.
    """
    rekordbox, music = _require_library(), _require_apple_library()
    missing = library_reports.find_missing_rekordbox_files(rekordbox.tracks, music, only_with_cues)
    limit = max(1, min(limit, library_queries.MAX_LIMIT))
    offset = max(0, offset)
    page = missing[offset : offset + limit]

    return {
        "missingCount": len(missing),
        "foundInAppleMusic": sum(1 for m in missing if m.suggested_path),
        "notFound": sum(1 for m in missing if not m.suggested_path),
        "offset": offset,
        "returned": len(page),
        "hasMore": offset + limit < len(missing),
        "files": [
            {
                "trackId": m.track.trackId,
                "title": m.track.trackTitle,
                "artist": m.track.artist,
                "cues": len(m.track.cues),
                "rekordboxPath": m.track.filePath,
                "matchedBy": m.match.method,
                "appleMusicPath": m.suggested_path,
            }
            for m in page
        ],
    }


def _drift() -> drift.LibraryDrift:
    rekordbox, music = _require_library(), _require_apple_library()
    return drift.compare_libraries(rekordbox.tracks, rekordbox.playlists, music)


@mcp.tool(annotations=READ_ONLY)
@_translate_errors
def compare_libraries() -> dict:
    """Compare the loaded Rekordbox and Apple Music libraries: tracks and playlists.

    Needs both loaded. Tracks are paired by path, then file name, then title
    and artist. Reports how many tracks are in Apple Music only, in Rekordbox
    only (and how many of those are inside Apple Music's own music folder, which
    means a library file Apple Music has lost), how many Apple Music entries are
    duplicates of the same file, and which playlists differ and how. Use
    list_library_differences for the tracks themselves.
    """
    result = _drift()
    rekordbox, music = _require_library(), _require_apple_library()
    statuses = Counter(p.status for p in result.playlists)

    summary = {
        "rekordboxTracks": len(rekordbox.tracks),
        "appleMusicTracks": len(music.tracks),
        "matched": result.matched,
        "inAppleMusicOnly": len(result.apple_only),
        "inAppleMusicOnlyButInNoPlaylist": len(result.apple_only_unplaylisted),
        "inRekordboxOnly": len(result.rekordbox_only),
        "inRekordboxOnlyButInAppleMusicsFolder": len(result.rekordbox_only_in_music_folder),
        "ambiguous": len(result.ambiguous),
        "appleMusicFilesListedTwice": len(result.duplicate_entries),
        "playlists": {
            "inAppleMusic": len(result.playlists),
            "inSync": statuses["in_sync"],
            "differ": statuses["differs"],
            "missingInRekordbox": statuses["missing_in_rekordbox"],
            "inRekordboxOnly": [p.name for p in result.rekordbox_only_playlists],
        },
        "playlistsNeedingAttention": [
            {
                "name": p.name,
                "status": p.status,
                "appleMusicTracks": p.apple_count,
                "rekordboxTracks": p.rekordbox_count,
                "onlyInAppleMusic": len(p.apple_only),
                "onlyInRekordbox": len(p.rekordbox_only),
            }
            for p in result.playlists
            if p.status != "in_sync"
        ],
    }
    summary["summary"] = (
        f"{result.matched} Rekordbox tracks are in Apple Music; {len(result.apple_only)} playlisted Apple Music tracks "
        f"are not in Rekordbox ({len(result.apple_only_unplaylisted)} more are in no playlist and ignored) and {len(result.rekordbox_only)} Rekordbox tracks are not in Apple Music. "
        f"{statuses['in_sync']} of {len(result.playlists)} playlists are in sync."
    )
    return summary


@mcp.tool(annotations=READ_ONLY)
@_translate_errors
def list_library_differences(
    kind: str,
    playlist: str | None = None,
    music_folder_only: bool = True,
    include_unplaylisted: bool = False,
    limit: int = 25,
    offset: int = 0,
) -> dict:
    """List the tracks behind compare_libraries, a page at a time.

    `kind`: "in_apple_music_only", "in_rekordbox_only" or "duplicate_entries"
    (Apple Music entries that point at one file).
    `playlist`: limit "in_apple_music_only" / "in_rekordbox_only" to one
    playlist, meaning tracks that are in that playlist in one program and not in
    the other's playlist of that name.
    `music_folder_only` (default true): for "in_rekordbox_only" over the whole
    library, list only files inside Apple Music's music folder. The rest are
    usually things that were never in Apple Music (sample packs and the like).
    `include_unplaylisted` (default false): "in_apple_music_only" lists only
    tracks that are in some Apple Music playlist, since those are the ones meant
    for Rekordbox; set it to also list tracks that are in no playlist.
    `limit` is capped at 100.
    """
    result = _drift()
    limit = max(1, min(limit, library_queries.MAX_LIMIT))
    offset = max(0, offset)

    if kind == "duplicate_entries":
        rows = [
            {"title": g[0].title, "artist": g[0].artist, "path": g[0].path, "entries": len(g), "trackIds": [t.track_id for t in g]}
            for g in result.duplicate_entries
        ]
    elif kind in ("in_apple_music_only", "in_rekordbox_only"):
        if playlist:
            match = next((p for p in result.playlists if p.name.casefold() == playlist.strip().casefold()), None)

            if match is None:
                raise ToolError(f"No Apple Music playlist named {playlist!r}.")

            source = match.apple_only if kind == "in_apple_music_only" else match.rekordbox_only
        elif kind == "in_apple_music_only":
            source = result.apple_only + result.apple_only_unplaylisted if include_unplaylisted else result.apple_only
        else:
            source = result.rekordbox_only_in_music_folder if music_folder_only else result.rekordbox_only

        if kind == "in_apple_music_only":
            rows = [library_reports.music_track_to_dict(t) for t in source]
        else:
            rows = [
                {"trackId": t.trackId, "title": t.trackTitle, "artist": t.artist, "cues": len(t.cues), "path": t.filePath}
                for t in source
            ]
    else:
        raise ToolError('kind must be "in_apple_music_only", "in_rekordbox_only" or "duplicate_entries".')

    page = rows[offset : offset + limit]

    return {
        "kind": kind,
        "playlist": playlist,
        "total": len(rows),
        "offset": offset,
        "returned": len(page),
        "hasMore": offset + limit < len(rows),
        "items": page,
    }


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
@_translate_errors
def export_playlists_for_rekordbox(
    playlists: list[str] | None = None,
    all_playlists: bool = False,
    output_path: str | None = None,
    folder_name: str = "Apple Music",
    dry_run: bool = True,
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

    Run it with dry_run=true first and show the user the summary.

    To import (steps for the user): in Rekordbox, with Preferences > Advanced >
    Database > rekordbox xml > Imported Library pointing at the file, show the
    'rekordbox xml' item in the sidebar (reload it), expand it, right-click the
    folder and choose Import Playlist. Whether re-importing a playlist that
    already exists adds a copy or merges has not been tested.
    """
    rekordbox, music = _require_library(), _require_apple_library()
    result = _drift()
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


def _db_target(database_path: str | None) -> Path:
    return Path(database_path).expanduser() if database_path else rekordbox_db.find_database()


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@_translate_errors
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
    music = _require_apple_library()
    db_path = _db_target(database_path)
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
@_translate_errors
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
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@_translate_errors
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
    music = _require_apple_library()
    db_path = _db_target(database_path)
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
@_translate_errors
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
@_translate_errors
def report_stale_duplicates(database_path: str | None = None, limit: int = 25) -> dict:
    """List Rekordbox tracks whose file is missing but whose real file Rekordbox already has under another row.

    Read-only. These are duplicates left behind when files were renamed. For
    each pair it shows what the broken row and the good row have: hot and memory
    cues, and which playlists they are in. "safe_to_remove" means the broken row
    has no cue and no playlist entry that the good row lacks; "needs_merge"
    means it does, so removing it would lose something. Nothing is changed and
    this tool cannot remove anything. Needs the Apple Music library loaded.
    """
    music = _require_apple_library()
    snapshot = rekordbox_db.read_snapshot(_db_target(database_path), rekordbox_db.default_backend())
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
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
)
@_translate_errors
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
    music = _require_apple_library()
    db_path = _db_target(database_path)
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
@_translate_errors
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


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
)
@_translate_errors
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
    music = _require_apple_library()
    db_path = _db_target(database_path)
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
@_translate_errors
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


@mcp.tool(annotations=READ_ONLY)
@_translate_errors
def check_sync_status(
    rekordbox_xml: str | None = None,
    reload_apple_music: bool = False,
    database_path: str | None = None,
) -> dict:
    """One read-only report: are Apple Music, Rekordbox and Serato in sync? Changes nothing.

    It checks, and says which tool would fix each thing it finds:
    - tracks and playlists, Apple Music against Rekordbox (playlisted Apple
      tracks only). Rekordbox's live database is read through a copy when it
      can be, and is preferred over the XML export, which may be days old. Rekordbox
      may stay open.
    - files: Rekordbox tracks whose file is missing, whether the real file can be
      found, and stale duplicate rows.
    - cues, Rekordbox against Serato: what sync_cues_to_serato would change.
      Rekordbox's cues are read from its live database, so no XML export is
      needed (a loaded XML is only a fallback, and the report warns when it is
      older than the database). Serato's side is read from the cue tags in the
      audio files.

    Apple Music is read live (the Music app must be open, and it takes about half
    a minute) unless a library is already loaded; pass reload_apple_music=true to
    read it again. `rekordbox_xml` is optional: an export to fall back on if the
    database can't be read. Each part that can't be checked is
    reported as such instead of failing the whole report. Show the user the
    summary and what needs attention; do not run any fix without asking.
    """
    global _apple_path, _apple_live, _current_path

    if reload_apple_music or _apple_library() is None:
        _apple_path, _apple_live = None, apple_music_live.read_live_library()

    music = _require_apple_library()

    if rekordbox_xml:
        _current_path = get_rekordbox_library(rekordbox_xml).path

    inputs = sync_check.Inputs(music=music)

    try:
        db_path = _db_target(database_path)
        inputs.database_path = db_path
        inputs.snapshot = rekordbox_db.read_snapshot(db_path, rekordbox_db.default_backend())
        inputs.database_modified = datetime.fromtimestamp(
            max(db_path.with_name(n).stat().st_mtime for n in ("master.db", "master.db-wal") if db_path.with_name(n).exists())
        )
    except Exception as error:  # a missing database, SQLCipher not set up, a locked file...
        inputs.snapshot, inputs.snapshot_error = None, str(error)

    if _current_path is not None:
        library = get_rekordbox_library(_current_path)
        inputs.xml_tracks, inputs.xml_playlists, inputs.xml_path = library.tracks, library.playlists, library.path
        inputs.xml_modified = datetime.fromtimestamp(library.path.stat().st_mtime)

    try:
        cues = _cue_source("auto", database_path)
        moved = library_reports.moved_file_paths(library_reports.find_missing_rekordbox_files(cues.tracks, music))
        inputs.serato_plan = serato_sync.build_sync_plan(cues.tracks, moved_files=moved)
        inputs.cues_from, inputs.cues_from_xml, inputs.cue_notes = cues.label, cues.from_xml, cues.notes
    except Exception as error:
        inputs.serato_error = str(error)

    return sync_check.build_report(inputs)


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
    a timestamped backup is saved next to it first. Only the new cue lines are
    inserted, so the rest of the file stays exactly as Rekordbox wrote it. The
    write is atomic, and it is refused if the file changed since the dry run
    (for example after a re-export). Running it again changes nothing, and other
    writing tools build on the same file, so their edits add up.

    `dry_run` is true by default and only reports what would change. Show the
    user that summary, including that the file would be edited in place, and get
    their go-ahead before calling again with dry_run=false. `output_path`
    (optional, must end in .xml, must not exist yet) saves the edited XML there
    instead and leaves the loaded file alone.
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
    the same safeguards as set_default_cue: a timestamped backup first, only the
    new cue lines inserted, an atomic write, and a refusal if the file changed
    since the dry run.

    `dry_run` is true by default and only reports what would change. Show the
    user that summary, including that the file would be edited in place, and get
    their go-ahead before calling again with dry_run=false. `output_path`
    (optional, must end in .xml, must not exist yet) saves the edited XML there
    instead and leaves the loaded file alone.
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


@dataclass
class CueSource:
    tracks: list
    by_id: dict
    label: str
    notes: list[str]
    from_xml: bool


def _cue_source(choice: str = "auto", database_path: str | None = None) -> CueSource:
    """Where to read Rekordbox's cues from: its database (always current) or the loaded XML export."""
    if choice not in ("auto", "database", "xml"):
        raise ToolError("cue_source must be 'auto', 'database' or 'xml'.")

    failure = None

    if choice in ("auto", "database"):
        try:
            snapshot = rekordbox_db.read_snapshot(_db_target(database_path), rekordbox_db.default_backend())
            library = db_library.tracks_from_snapshot(snapshot)
            notes = []

            if library.unverified_cues:
                notes.append(
                    f"{len(library.unverified_cues)} cue(s) in unusual slots (kind 4, or F to H) were left out because "
                    "CueBridge doesn't know how the database numbers them; use cue_source='xml' to include them: "
                    + ", ".join(sorted({t for t, _ in library.unverified_cues})[:5])
                )

            if library.unknown_colors:
                notes.append(
                    f"{len(library.unknown_colors)} cue colour(s) have an index CueBridge can't convert, so those cues "
                    "have no colour: " + ", ".join(sorted({t for t, _ in library.unknown_colors})[:5])
                )

            if library.beat_grids_missing:
                notes.append(f"{library.beat_grids_missing} track(s) with cues have no readable beat grid.")

            return CueSource(library.tracks, library.tracks_by_id, "Rekordbox's database (live)", notes, False)
        except Exception as error:
            if choice == "database":
                raise ToolError(f"Could not read Rekordbox's database: {error}") from None

            failure = str(error)

    if _current_path is None:
        raise ToolError(
            "Rekordbox's cues need either its database"
            + (f" (not readable: {failure})" if failure else "")
            + " or a loaded XML export: call load_rekordbox_library first."
        )

    xml = get_rekordbox_library(_current_path)
    notes = [f"Read from the XML export {xml.path.name}, which may be out of date."]

    if failure:
        notes.append(f"The database couldn't be read: {failure}")

    return CueSource(xml.tracks, xml.tracks_by_id, f"the Rekordbox XML export ({xml.path})", notes, True)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
)
@_translate_errors
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
    library = _cue_source(cue_source)
    music = _apple_library() if use_apple_music_paths else None
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

    result = {
        "dryRun": dry_run,
        "cuesFrom": library.label,
        "cueNotes": library.notes,
        "filesToChange": len(changes),
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

    if dry_run:
        result["summary"] = (
            f"Would update the cues in {len(changes)} audio files (Rekordbox wins). "
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
@_translate_errors
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
