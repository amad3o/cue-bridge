"""Loading the libraries, and looking up tracks and playlists in them."""

from dataclasses import replace

from mcp.server.mcpserver.exceptions import ToolError

from cuebridge.services.apple_music import live as apple_music_live
from cuebridge.services.apple_music.service import get_apple_music_library
from cuebridge.services.rekordbox.service import get_rekordbox_library
from cuebridge.services.reports import library_queries, library_reports
from cuebridge.tools.common import (
    READ_ONLY,
    mcp,
    require_apple_library,
    session,
    translate_errors,
)
from cuebridge.tools.cue_source import CueSource, read_cue_source


@mcp.tool(annotations=READ_ONLY)
@translate_errors
def load_rekordbox_library(path: str) -> dict:
    """Load a Rekordbox XML export and return a summary of it.

    `path` is the full path to the XML file on this computer (a leading ~ is
    expanded). The library stays loaded for the other tools. Loading another
    path replaces it.
    """

    library = get_rekordbox_library(path)
    session.rekordbox_path = library.path

    summary = library_queries.library_summary(library)
    summary["summary"] = (
        f"Loaded {summary['trackCount']} tracks. {summary['tracksWithCues']} have "
        f"cues ({summary['cueCount']} cues in total) and "
        f"{summary['tracksWithoutCues']} have none."
    )
    return summary


@mcp.tool(annotations=READ_ONLY)
@translate_errors
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

    if path:
        library = get_apple_music_library(path)
        session.apple_path, session.apple_live = library.path, None
    else:
        library = apple_music_live.read_live_library()
        session.apple_path, session.apple_live = None, library

    summary = library_reports.music_library_summary(library)
    summary["source"] = str(library.path) if library.path else "the Music app (live)"
    summary["summary"] = (
        f"Loaded {summary['trackCount']} tracks and {summary['playlistCount']} playlists "
        f"({summary['totalHours']} hours, {summary['totalGigabytes']} GB)."
    )
    return summary


@mcp.tool(annotations=READ_ONLY)
@translate_errors
def get_playlist_tracks(name: str, limit: int = 50, offset: int = 0) -> dict:
    """List the tracks of one Apple Music playlist, in playlist order, a page at a time.

    `name` is matched ignoring case; use the names from load_apple_music_library.
    `limit` is capped at 100.
    """
    library = require_apple_library()
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


def _where_from(source: CueSource) -> dict:
    """Says where a lookup was read from, with anything the user should know about it."""
    return {"readFrom": source.label, **({"notes": source.notes} if source.notes else {})}


@mcp.tool(annotations=READ_ONLY)
@translate_errors
def search_tracks(
    query: str = "",
    has_cues: bool | None = None,
    limit: int = 25,
    offset: int = 0,
    cue_source: str = "auto",
) -> dict:
    """List Rekordbox's tracks, a page at a time.

    Read from Rekordbox's live database, so no export is needed and it is current
    (Rekordbox may be open). A loaded XML export is used only if the database
    can't be read; `cue_source` forces "database" or "xml". `readFrom` says
    which was used.

    `query`: words that must all appear in the title, artist or file name
    (case-insensitive). Leave empty to list everything, including tracks in no
    playlist.
    `has_cues`: true for only tracks with cues, false for only tracks without,
    omit for both.
    `limit` is capped at 100. Use `offset` to get the next page (see hasMore).
    """
    source = read_cue_source(cue_source)

    return {**library_queries.search_tracks(source, query, has_cues, limit, offset), **_where_from(source)}


@mcp.tool(annotations=READ_ONLY)
@translate_errors
def get_track_cues(track_id: str, cue_source: str = "auto") -> dict:
    """Get one Rekordbox track with all of its cues. `track_id` comes from search_tracks.

    Read from Rekordbox's live database, like search_tracks.
    """
    source = read_cue_source(cue_source)
    track = source.by_id.get(track_id)

    if track is None:
        raise ToolError(f"No track with ID {track_id!r} in {source.label}. Use search_tracks to find IDs.")

    return {**library_queries.track_to_detail(track), **_where_from(source)}


@mcp.tool(annotations=READ_ONLY)
@translate_errors
def find_tracks_without_cues(
    limit: int = 25, offset: int = 0, playlisted_only: bool = True, cue_source: str = "auto"
) -> dict:
    """List Rekordbox tracks that have no cues, a page at a time.

    Read from Rekordbox's live database, like search_tracks. Only tracks in a
    playlist by default (`playlisted_only`). An XML export used as the fallback
    can't say which those are, so then every track is listed and
    `playlistedTracksOnly` is false. This looks at Rekordbox's cues only: a track
    listed here may still have cues in Serato.
    """
    source = read_cue_source(cue_source)
    limited = playlisted_only and source.playlisted is not None

    if limited:
        source = replace(source, tracks=[t for t in source.tracks if t.trackId in source.playlisted])

    found = library_queries.search_tracks(source, has_cues=False, limit=limit, offset=offset)

    return {**found, "playlistedTracksOnly": limited, **_where_from(source)}
