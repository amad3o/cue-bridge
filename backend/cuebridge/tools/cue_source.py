"""Where the cues come from: Rekordbox's live database, or a loaded XML export (with notes on which and why)."""

from dataclasses import dataclass

from mcp.server.mcpserver.exceptions import ToolError

from cuebridge.services.rekordbox import db as rekordbox_db
from cuebridge.services.rekordbox import db_library
from cuebridge.services.rekordbox.service import get_rekordbox_library
from cuebridge.tools.common import (
    db_target,
    session,
)


@dataclass
class CueSource:
    tracks: list
    by_id: dict
    label: str
    notes: list[str]
    from_xml: bool
    playlisted: set[str] | None = None  # IDs of tracks in a real playlist (known when read from the database)


def read_cue_source(choice: str = "auto", database_path: str | None = None) -> CueSource:
    """Where to read Rekordbox's cues from: its database (always current) or the loaded XML export."""
    if choice not in ("auto", "database", "xml"):
        raise ToolError("cue_source must be 'auto', 'database' or 'xml'.")

    failure = None

    if choice in ("auto", "database"):
        try:
            snapshot = rekordbox_db.read_snapshot(db_target(database_path), rekordbox_db.default_backend())
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

            playlisted = {c for p in snapshot.playlists if p.is_plain for c in p.content_ids}

            return CueSource(library.tracks, library.tracks_by_id, "Rekordbox's database (live)", notes, False, playlisted)
        except Exception as error:
            if choice == "database":
                raise ToolError(f"Could not read Rekordbox's database: {error}") from None

            failure = str(error)

    if session.rekordbox_path is None:
        raise ToolError(
            "Rekordbox's cues need either its database"
            + (f" (not readable: {failure})" if failure else "")
            + " or a loaded XML export: call load_rekordbox_library first."
        )

    xml = get_rekordbox_library(session.rekordbox_path)
    notes = [f"Read from the XML export {xml.path.name}, which may be out of date."]

    if failure:
        notes.append(f"The database couldn't be read: {failure}")

    return CueSource(xml.tracks, xml.tracks_by_id, f"the Rekordbox XML export ({xml.path})", notes, True)
