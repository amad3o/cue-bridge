"""CueBridge MCP server.

Run it over stdio with:  python -m cuebridge.mcp_server

All logic lives in cuebridge.services; the tools here only keep track of the
currently loaded library and translate errors for the assistant.
"""

import functools
from collections import Counter
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cuebridge.services import drift, library_queries, library_reports, rekordbox_editor, serato_sync
from cuebridge.parsers.apple_music_xml import MusicLibrary
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

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def reset_state() -> None:
    """Forget the loaded library. Used by tests."""
    global _current_path, _apple_path
    _current_path = None
    _apple_path = None


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
    return None if _apple_path is None else get_apple_music_library(_apple_path)


def _require_apple_library() -> MusicLibrary:
    library = _apple_library()

    if library is None:
        raise ToolError(
            "No Apple Music library is loaded. Call load_apple_music_library with the path to the "
            "XML exported by Apple Music (File > Library > Export Library) first."
        )

    return library


@mcp.tool(annotations=READ_ONLY)
@_translate_errors
def load_apple_music_library(path: str) -> dict:
    """Load an Apple Music library exported as XML and return a summary of it.

    In Apple Music: File > Library > Export Library. `path` is the full path to
    that .xml file. The summary lists the track count, formats, gaps in the
    metadata and every playlist with its size. The library stays loaded for
    get_playlist_tracks, find_missing_files and sync_cues_to_serato.
    """
    global _apple_path

    library = get_apple_music_library(path)
    _apple_path = library.path
    summary = library_reports.music_library_summary(library)
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
) -> dict:
    """Copy the loaded Rekordbox library's cues into the Serato tags of the audio files.

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
    library = _require_library()
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
