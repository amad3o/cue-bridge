"""One read-only report on whether Apple Music, Rekordbox and Serato agree.

Nothing here reads a file or a database itself: the caller hands in what it has
already read, so each part can be missing and the report says so instead of
failing. Nothing is ever changed.

  * Tracks and playlists, Apple Music against Rekordbox. Rekordbox's live
    database is the truth when it can be read (the XML export can be days old);
    otherwise the XML export is used and the report says so.
  * Files: Rekordbox tracks whose audio file is missing, and what could be done.
  * Cues, Rekordbox against Serato. Cues come from the Rekordbox XML export, the
    only place CueBridge reads them from, so a stale export gives stale answers;
    Serato's side is read from the cue tags in the audio files.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from cuebridge.models import Track
from cuebridge.parsers.apple_music_xml import MusicLibrary
from cuebridge.parsers.rekordbox_parser import RekordboxPlaylist
from cuebridge.services import drift, rekordbox_db, serato_sync

MAX_LISTED = 15
STALE_AFTER = timedelta(minutes=5)  # how much newer the database may be than the export before cues are doubted


@dataclass
class Inputs:
    music: MusicLibrary
    snapshot: rekordbox_db.DbSnapshot | None = None
    snapshot_error: str | None = None
    database_path: Path | None = None
    xml_tracks: list[Track] | None = None
    xml_playlists: list[RekordboxPlaylist] = field(default_factory=list)
    xml_path: Path | None = None
    xml_modified: datetime | None = None
    database_modified: datetime | None = None
    serato_plan: object | None = None  # a serato_sync plan
    serato_error: str | None = None


def _attention(items: list[dict], area: str, what: str, count: int, fix: str) -> None:
    if count:
        items.append({"area": area, "what": what, "count": count, "fix": fix})


def _playlists_section(inputs: Inputs, attention: list[dict]) -> dict:
    if inputs.snapshot is not None:
        tracks = [
            Track(trackId=c.id, trackTitle=c.title, artist=c.artist, filePath=c.path, durationMs=c.duration_ms, cues=[])
            for c in inputs.snapshot.contents
        ]
        playlists = [RekordboxPlaylist(p.name, p.folders, p.content_ids) for p in inputs.snapshot.playlists if p.is_plain]
        source = "Rekordbox's database (live)"
    elif inputs.xml_tracks is not None:
        tracks, playlists = inputs.xml_tracks, inputs.xml_playlists
        source = "the Rekordbox XML export" + (f" (the database couldn't be read: {inputs.snapshot_error})" if inputs.snapshot_error else "")
    else:
        return {"couldNotCheck": inputs.snapshot_error or "no Rekordbox database or XML export is available"}

    result = drift.compare_libraries(tracks, playlists, inputs.music)
    differing = [p for p in result.playlists if p.status == "differs"]
    missing = [p for p in result.playlists if p.status == "missing_in_rekordbox"]

    _attention(attention, "playlists", "Apple Music playlists that differ from Rekordbox's", len(differing),
               "sync_playlists_to_rekordbox")
    _attention(attention, "playlists", "Apple Music playlists Rekordbox doesn't have", len(missing),
               "export_playlists_for_rekordbox, then import in Rekordbox")
    _attention(attention, "tracks", "playlisted Apple Music tracks that aren't in Rekordbox", len(result.apple_only),
               "export_playlists_for_rekordbox, then import in Rekordbox")
    _attention(attention, "tracks", "tracks that couldn't be matched unambiguously", len(result.ambiguous),
               "list_library_differences(kind='ambiguous')")

    return {
        "comparedWith": source,
        "matchedTracks": result.matched,
        "playlistedAppleTracksNotInRekordbox": len(result.apple_only),
        "ignoredAppleTracksInNoPlaylist": len(result.apple_only_unplaylisted),
        "rekordboxTracksNotInAppleMusic": len(result.rekordbox_only),
        "ambiguous": len(result.ambiguous),
        "playlists": {
            "inAppleMusic": len(result.playlists),
            "inSync": sum(1 for p in result.playlists if p.status == "in_sync"),
            "differ": [
                {"name": p.name, "onlyInAppleMusic": len(p.apple_only), "onlyInRekordbox": len(p.rekordbox_only)}
                for p in differing[:MAX_LISTED]
            ],
            "missingInRekordbox": [p.name for p in missing[:MAX_LISTED]],
            "onlyInRekordbox": [p.name for p in result.rekordbox_only_playlists][:MAX_LISTED],
        },
    }


def _files_section(inputs: Inputs, attention: list[dict]) -> dict:
    if inputs.snapshot is None:
        return {"couldNotCheck": inputs.snapshot_error or "Rekordbox's database isn't available"}

    plan = rekordbox_db.plan_relocations(inputs.snapshot, inputs.music)
    pairs = rekordbox_db.describe_duplicates(plan, inputs.snapshot)
    local_paths = sum(
        1 for c in inputs.snapshot.contents if c.path and c.path.startswith("/")
    )
    _attention(attention, "files", "Rekordbox tracks whose renamed or moved file can be found", len(plan.relocations),
               "fix_moved_files_in_rekordbox")
    _attention(attention, "files", "broken Rekordbox rows that duplicate a row with the real file", len(pairs),
               "clean_up_stale_duplicates")
    _attention(attention, "files", "Rekordbox tracks whose file is missing and not in Apple Music", len(plan.no_match),
               "remove them in Rekordbox, or find the files")

    return {
        "rekordboxTracksWithAFilePath": local_paths,
        "missingButFound": len(plan.relocations),
        "staleDuplicates": len(pairs),
        "missingAndNotInAppleMusic": len(plan.no_match),
        "examplesNotInAppleMusic": [{"track": t, "path": p} for t, p in plan.no_match[:MAX_LISTED]],
    }


def _cues_section(inputs: Inputs, attention: list[dict]) -> dict:
    if inputs.serato_plan is None:
        return {"couldNotCheck": inputs.serato_error or "no Rekordbox XML export is loaded (cues are read from it)"}

    plan = inputs.serato_plan
    changes = plan.to_change
    section = {
        "cuesComeFrom": str(inputs.xml_path) if inputs.xml_path else "the loaded Rekordbox XML",
        "tracksChecked": len(plan.items),
        "differ": len(changes),
        "inSync": len(plan.with_status("unchanged")),
        "skipped": len(plan.with_status("skip")),
        "skippedBecause": dict(plan.skip_reasons),
        "timingNotVerified": len(plan.flagged_timing),
        "differing": [
            {
                "title": i.title, "artist": i.artist,
                "seratoNow": serato_sync.describe_times(i.before), "rekordboxWouldGive": serato_sync.describe_times(i.after),
            }
            for i in changes[:MAX_LISTED]
        ],
    }

    if inputs.xml_modified and inputs.database_modified and inputs.database_modified - inputs.xml_modified > STALE_AFTER:
        age = inputs.database_modified - inputs.xml_modified
        section["warning"] = (
            f"The Rekordbox database was changed {_ago(age)} after the XML export these cues come from, "
            "so cues edited since then aren't included. Export the collection again for a current answer."
        )
        attention.append({"area": "cues", "what": "the Rekordbox XML export is older than the database", "count": 1,
                          "fix": "export the collection from Rekordbox again (File > Export Collection in xml format)"})

    _attention(attention, "cues", "tracks whose Serato cues differ from Rekordbox's", len(changes), "sync_cues_to_serato")

    return section


def _ago(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600

    return f"{hours:.0f} hours" if hours >= 2 else f"{delta.total_seconds() / 60:.0f} minutes"


def build_report(inputs: Inputs) -> dict:
    attention: list[dict] = []
    sections = {
        "tracksAndPlaylists": _playlists_section(inputs, attention),
        "files": _files_section(inputs, attention),
        "cues": _cues_section(inputs, attention),
    }
    unchecked = [name for name, section in sections.items() if "couldNotCheck" in section]
    in_sync = not attention and not unchecked

    if in_sync:
        summary = "Apple Music, Rekordbox and Serato are in sync."
    else:
        parts = []

        if attention:
            parts.append(f"{len(attention)} thing(s) need attention")

        if unchecked:
            parts.append("couldn't check: " + ", ".join(unchecked))

        summary = "; ".join(parts).capitalize() + "."

    return {
        "inSync": in_sync,
        "summary": summary,
        "needsAttention": attention,
        **sections,
        "sources": {
            "appleMusic": str(inputs.music.path) if inputs.music.path else "the Music app (live)",
            "rekordboxDatabase": str(inputs.database_path) if inputs.database_path and inputs.snapshot else None,
            "rekordboxXml": str(inputs.xml_path) if inputs.xml_path else None,
            "rekordboxXmlExportedAt": inputs.xml_modified.isoformat(timespec="minutes") if inputs.xml_modified else None,
        },
        "nothingWasChanged": True,
    }
