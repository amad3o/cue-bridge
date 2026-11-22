"""Read-only reports across the libraries: audits, missing files, differences and the sync check."""

from collections import Counter
from datetime import datetime

from mcp.server.mcpserver.exceptions import ToolError

from cuebridge.services.apple_music import live as apple_music_live
from cuebridge.services.compare import sync_check
from cuebridge.services.rekordbox import db as rekordbox_db
from cuebridge.services.rekordbox.service import get_rekordbox_library
from cuebridge.services.reports import library_audit, library_queries, library_reports
from cuebridge.services.serato import sync as serato_sync
from cuebridge.tools.common import (
    READ_ONLY,
    apple_library,
    current_drift,
    db_target,
    mcp,
    require_apple_library,
    require_library,
    session,
    translate_errors,
)
from cuebridge.tools.cue_source import read_cue_source

AUDIT_REPORTS = ("duplicates", "metadata_gaps", "playlists", "formats", "questions", "missing_files")
# Reports about the whole library by default; the rest follow "only playlisted tracks matter".
AUDIT_WHOLE_LIBRARY = ("playlists", "questions")


@mcp.tool(annotations=READ_ONLY)
@translate_errors
def audit_music_library(
    report: str,
    show: str | None = None,
    limit: int = 25,
    offset: int = 0,
    playlisted_only: bool | None = None,
    recent_days: int = 30,
    low_bit_rate_kbps: int = 192,
    short_seconds: int = 15,
    many_playlists: int = 3,
) -> dict:
    """Read-only audits of the loaded Apple Music library. Changes nothing, anywhere.

    `report` is one of:
    - "duplicates": the same song listed twice, by title and artist
      (`same_title_and_artist`), by file (`same_file`) and by size and length under
      different names (`same_size_and_length`).
    - "metadata_gaps": tracks with no artist, title, album, genre, BPM, year or
      length (`no_artist`, `no_title`, `no_album`, `no_genre`, `no_bpm`, `no_year`,
      `no_length`), "Unknown Artist / Unknown Album" entries and folders
      (`unknown_artist_or_album`), and tracks shorter than `short_seconds`, which
      are probably samples (`very_short`).
    - "playlists": the biggest playlists (`biggest_playlists`), tracks in no playlist
      (`not_in_any_playlist`), tracks in `many_playlists` or more
      (`in_many_playlists`), `empty_playlists`, and which playlists share the most
      tracks (`overlaps`).
    - "formats": counts by format (`by_format`), lossy bit-rate band
      (`by_lossy_bit_rate`) and sample rate (`by_sample_rate`), lossy files under
      `low_bit_rate_kbps` (`low_bit_rate`), and the `largest_files`.
    - "questions": the library's size, what was added in the last `recent_days`
      (`recently_added`), and tracks `by_genre` and `by_year`.
    - "missing_files": tracks whose file isn't at the path Apple Music has (checks
      the disk; if most are missing, the music drive is probably not mounted).

    The numbers for every section always come back; `show` picks which section's
    items are listed (each report has a default), paged with `limit` and `offset`.
    Only playlisted tracks are audited by default, because tracks in no playlist
    aren't meant for Rekordbox; "playlists" and "questions" cover the whole library.
    Pass `playlisted_only` to override either way. Needs load_apple_music_library
    first (reload it to pick up changes made in Music).
    """
    if report not in AUDIT_REPORTS:
        raise ToolError(f"report must be one of: {', '.join(AUDIT_REPORTS)}.")

    music = require_apple_library()

    if playlisted_only is None:
        playlisted_only = report not in AUDIT_WHOLE_LIBRARY

    tracks, left_out = library_audit.scope_tracks(music, playlisted_only)

    if report == "duplicates":
        audit = library_audit.audit_duplicates(tracks)
    elif report == "metadata_gaps":
        audit = library_audit.audit_metadata_gaps(tracks, short_seconds * 1000)
    elif report == "playlists":
        audit = library_audit.audit_playlists(music, many_playlists)
        left_out, playlisted_only = 0, False
    elif report == "formats":
        audit = library_audit.audit_formats(tracks, low_bit_rate_kbps)
    elif report == "questions":
        audit = library_audit.audit_questions(tracks, recent_days)
    else:
        audit = library_audit.audit_missing_files(tracks)

    chosen = show or audit.default

    if chosen not in audit.sections:
        raise ToolError(f"For {report}, show must be one of: {', '.join(audit.sections)}.")

    items = audit.sections[chosen]
    limit = max(1, min(limit, library_queries.MAX_LIMIT))
    offset = max(0, offset)
    page = items[offset : offset + limit]
    scope = "only playlisted tracks" if playlisted_only else "the whole library"

    return {
        "report": report,
        "scope": scope,
        "tracksLeftOutForBeingInNoPlaylist": left_out,
        **audit.facts,
        "sections": {name: len(found) for name, found in audit.sections.items()},
        "shown": chosen,
        "total": len(items),
        "returned": len(page),
        "hasMore": offset + len(page) < len(items),
        "items": page,
        "notes": audit.notes,
        "summary": (
            f"{report} ({scope}): "
            + ", ".join(f"{name} {len(found)}" for name, found in audit.sections.items())
            + f". Showing {len(page)} of {len(items)} in {chosen}. Nothing was changed."
        ),
    }


@mcp.tool(annotations=READ_ONLY)
@translate_errors
def find_missing_files(only_with_cues: bool = True, limit: int = 25, offset: int = 0) -> dict:
    """List Rekordbox tracks whose audio file isn't where Rekordbox says, and where Apple Music has it.

    Needs both libraries loaded. A track is matched to Apple Music by path,
    then file name, then title and artist (a match must be unambiguous).
    `only_with_cues` (default true) limits it to tracks that have cues, which
    are the ones that matter for syncing. `limit` is capped at 100.
    """
    rekordbox, music = require_library(), require_apple_library()
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


@mcp.tool(annotations=READ_ONLY)
@translate_errors
def compare_libraries() -> dict:
    """Compare the loaded Rekordbox and Apple Music libraries: tracks and playlists.

    Needs both loaded. Tracks are paired by path, then file name, then title
    and artist. Reports how many tracks are in Apple Music only, in Rekordbox
    only (and how many of those are inside Apple Music's own music folder, which
    means a library file Apple Music has lost), how many Apple Music entries are
    duplicates of the same file, and which playlists differ and how. Use
    list_library_differences for the tracks themselves.
    """
    result = current_drift()
    rekordbox, music = require_library(), require_apple_library()
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
        f"are not in Rekordbox ({len(result.apple_only_unplaylisted)} more are in no playlist and ignored) "
        f"and {len(result.rekordbox_only)} Rekordbox tracks are not in Apple Music. "
        f"{statuses['in_sync']} of {len(result.playlists)} playlists are in sync."
    )
    return summary


@mcp.tool(annotations=READ_ONLY)
@translate_errors
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
    result = current_drift()
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
@translate_errors
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

    if reload_apple_music or apple_library() is None:
        session.apple_path, session.apple_live = None, apple_music_live.read_live_library()

    music = require_apple_library()

    if rekordbox_xml:
        session.rekordbox_path = get_rekordbox_library(rekordbox_xml).path

    inputs = sync_check.Inputs(music=music)

    try:
        db_path = db_target(database_path)
        inputs.database_path = db_path
        inputs.snapshot = rekordbox_db.read_snapshot(db_path, rekordbox_db.default_backend())
        inputs.database_modified = rekordbox_db.database_modified(db_path)
    except Exception as error:  # a missing database, SQLCipher not set up, a locked file...
        inputs.snapshot, inputs.snapshot_error = None, str(error)

    if session.rekordbox_path is not None:
        library = get_rekordbox_library(session.rekordbox_path)
        inputs.xml_tracks, inputs.xml_playlists, inputs.xml_path = library.tracks, library.playlists, library.path
        inputs.xml_modified = datetime.fromtimestamp(library.path.stat().st_mtime)

    try:
        cues = read_cue_source("auto", database_path)
        moved = library_reports.moved_file_paths(library_reports.find_missing_rekordbox_files(cues.tracks, music))
        inputs.serato_plan = serato_sync.build_sync_plan(cues.tracks, moved_files=moved)
        inputs.cues_from, inputs.cues_from_xml, inputs.cue_notes = cues.label, cues.from_xml, cues.notes
    except Exception as error:
        inputs.serato_error = str(error)

    return sync_check.build_report(inputs)
