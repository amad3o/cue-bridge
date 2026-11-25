"""Beatgrid analysis: read-only. It reads audio files and Rekordbox's database, and changes nothing."""

from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from cuebridge.services.beatgrid import analyzer, candidates
from cuebridge.services.rekordbox import db as rekordbox_db
from cuebridge.services.rekordbox import db_library
from cuebridge.tools.common import READ_ONLY, db_target, mcp, translate_errors

MAX_TRACKS = 25  # each track takes a few seconds, so a call analyses a small page
MIN_CONFIDENCE = 0.6


def _verdict(result: analyzer.BeatgridResult) -> str:
    if not result.steady:
        return "leave alone: the beats don't sit on one constant grid"

    if result.confidence < MIN_CONFIDENCE:
        return "leave alone: low confidence"

    return "usable"


def _describe(result: analyzer.BeatgridResult, existing_ms: int | None) -> dict:
    described = {
        "bpm": result.bpm,
        "firstBeatMs": result.first_beat_ms,
        "confidence": result.confidence,
        "steady": result.steady,
        "spreadMs": result.spread_ms,
        "beatsOnGrid": result.hit_fraction,
        "verdict": _verdict(result),
    }

    if existing_ms is not None:
        described["rekordboxFirstBeatMs"] = existing_ms
        described["offsetFromRekordboxMs"] = candidates.offset_from_existing(result.first_beat_ms, existing_ms, result.bpm)

    return described


@mcp.tool(annotations=READ_ONLY)
@translate_errors
def analyze_beatgrid(
    paths: list[str] | None = None,
    playlisted_only: bool = True,
    include_with_grid: bool = False,
    limit: int = 5,
    offset: int = 0,
    database_path: str | None = None,
) -> dict:
    """Work out each track's BPM and first beat from the audio itself. Read-only: no file or database is changed.

    Either pass `paths` (audio files to analyse), or leave it out to take the tracks from Rekordbox's database:
    playlisted tracks only by default (`playlisted_only`), skipping tracks that already have a beat grid unless
    `include_with_grid` is true (then each result also shows how far our first beat is from Rekordbox's).

    Each track gets a BPM, the first beat in ms, a confidence from 0 to 1, and a verdict: "usable", or "leave alone"
    when the beats don't sit on one constant grid (live drums, tempo changes, a long beatless intro) or the confidence
    is low. The result is an estimate to check by ear, not something to write blindly. Every track takes a few
    seconds, so a call analyses `limit` tracks (at most 25) starting at `offset`; page through for more.
    Needs the optional beatgrid packages (`pip install -e ".[beatgrid]"`).
    """
    limit = max(1, min(limit, MAX_TRACKS))
    offset = max(0, offset)

    if paths:
        listed = [{"title": Path(p).name, "artist": None, "path": str(Path(p).expanduser()), "existing": None} for p in paths]
        counts: dict = {"filesGiven": len(paths)}
    else:
        try:
            snapshot = rekordbox_db.read_snapshot(db_target(database_path), rekordbox_db.default_backend())
        except Exception as error:
            raise ToolError(f"Could not read Rekordbox's database (or pass paths): {error}") from None

        chosen = candidates.select_candidates(snapshot, db_library.read_first_beat_ms, playlisted_only, include_with_grid)
        listed = [
            {"title": c.title, "artist": c.artist, "path": c.path, "existing": chosen.existing_first_beat_ms.get(c.id)}
            for c in chosen.tracks
        ]
        counts = {
            "skippedForBeingInNoPlaylist": chosen.not_playlisted,
            "skippedForAlreadyHavingAGrid": chosen.already_gridded,
            "skippedForAMissingFile": chosen.file_missing,
        }

    page = listed[offset : offset + limit]
    results, usable, left_alone, failed = [], 0, 0, 0

    for item in page:
        entry = {"track": item["title"], "artist": item["artist"], "path": item["path"]}

        try:
            result = analyzer.analyze_file(item["path"])
        except analyzer.AnalysisError as error:
            if "not installed" in str(error):
                raise ToolError(str(error)) from None

            entry["error"] = str(error)
            failed += 1
        else:
            entry.update(_describe(result, item["existing"]))
            usable += entry["verdict"] == "usable"
            left_alone += entry["verdict"] != "usable"

        results.append(entry)

    return {
        **counts,
        "toAnalyse": len(listed),
        "offset": offset,
        "analysed": len(results),
        "hasMore": offset + limit < len(listed),
        "usable": usable,
        "leaveAlone": left_alone,
        "couldNotAnalyse": failed,
        "tracks": results,
        "summary": (
            f"Analysed {len(results)} of {len(listed)} track(s): {usable} usable, {left_alone} to leave alone, {failed} failed. "
            "Nothing was written. These are estimates; check a few by ear before any grid is written."
            + (f" Call again with offset={offset + limit} for more." if offset + limit < len(listed) else "")
        ),
    }
