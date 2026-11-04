"""Write Apple Music playlists into a Rekordbox XML file that Rekordbox can import.

Rekordbox can't be told to add playlists from outside. What it can do is load
an XML file (Preferences > Advanced > Database > rekordbox xml) and import a
playlist or folder from it with a right-click. This module builds that file:
one folder holding the playlists that need to get into Rekordbox, with

  * the Rekordbox entry for every track Rekordbox already has, copied as it is
    (cues and beat grids included) so nothing about it changes;
  * a new entry, built from Apple Music's data, for every track Rekordbox
    doesn't have yet.

By default only playlists that differ between the two libraries are included.
The file is a separate, small one: Rekordbox's own export is never touched,
and the library database is never written.
"""

import copy
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from cuebridge.parsers.apple_music_xml import MusicLibrary, MusicTrack
from cuebridge.parsers.rekordbox_parser import parse_rekordbox_playlists
from cuebridge.services.drift import LibraryDrift
from cuebridge.services.rekordbox_editor import OutputPathError, _replace_atomically
from cuebridge.services.rekordbox_service import (
    LibraryError,
    RekordboxLibrary,
    parse_rekordbox_bytes,
    read_library_bytes,
)

WRITTEN_MARKER = b"<!-- Playlists written by CueBridge. -->"
DEFAULT_FOLDER = "Apple Music"
DEFAULT_FILE_NAME = "cuebridge-playlists.xml"
# Keys for tracks Rekordbox doesn't have. They only have to be unique inside the file.
NEW_TRACK_ID_BASE = 900_000_000
MARKER_SEARCH_BYTES = 512

_KINDS = {
    "mpeg audio file": "MP3 File",
    "wav audio file": "WAV File",
    "aiff audio file": "AIFF File",
}


class ExportError(LibraryError):
    """The playlists can't be exported."""


@dataclass(frozen=True)
class PlaylistPlan:
    name: str
    keys: tuple[str, ...]  # Rekordbox TrackIDs, in playlist order
    reason: str  # why it is included


@dataclass
class ExportPlan:
    playlists: list[PlaylistPlan] = field(default_factory=list)
    existing_ids: list[str] = field(default_factory=list)  # Rekordbox tracks to copy as they are
    new_tracks: list[tuple[str, MusicTrack]] = field(default_factory=list)  # (key, Apple Music track)
    relocated: dict[str, str] = field(default_factory=dict)  # Rekordbox track ID -> where its file really is
    skipped: list[tuple[str, str, str]] = field(default_factory=list)  # (playlist, track, why)
    duplicates_removed: int = 0
    in_sync: list[str] = field(default_factory=list)  # playlists left out because they already match


def _kind(track: MusicTrack) -> str:
    kind = (track.kind or "").lower()

    if kind in _KINDS:
        return _KINDS[kind]

    return "M4A File" if "aac" in kind or "apple lossless" in kind or "m4a" in kind else (track.kind or "")


def plan_export(
    rekordbox: RekordboxLibrary,
    music: MusicLibrary,
    drift: LibraryDrift,
    names: list[str] | None = None,
    all_playlists: bool = False,
    exists=os.path.exists,
) -> ExportPlan:
    """Decide which playlists go in the file and which tracks they need."""
    user_playlists = [p for p in music.user_playlists if not p.is_folder]
    drift_for = {p.name: p for p in drift.playlists}
    plan = ExportPlan()

    if names:
        wanted = {n.strip().casefold() for n in names}
        chosen = [p for p in user_playlists if p.name.casefold() in wanted]
        unknown = wanted - {p.name.casefold() for p in chosen}

        if unknown:
            raise ExportError(
                "No Apple Music playlist named " + ", ".join(repr(n) for n in sorted(unknown))
                + ". Names: " + ", ".join(p.name for p in user_playlists)
            )
    elif all_playlists:
        chosen = user_playlists
    else:
        chosen = [p for p in user_playlists if drift_for[p.name].status != "in_sync"]
        plan.in_sync = [p.name for p in user_playlists if drift_for[p.name].status == "in_sync"]

    seen_existing: set[str] = set()
    seen_new: dict[int, str] = {}

    for playlist in chosen:
        keys: list[str] = []
        status = drift_for[playlist.name].status
        reason = {"missing_in_rekordbox": "not in Rekordbox yet", "differs": "differs from Rekordbox"}.get(status, "requested")

        for apple_id in playlist.track_ids:
            track = music.tracks_by_id.get(apple_id)

            if track is None:
                continue

            existing = drift.rekordbox_for_apple.get(apple_id)

            if existing:
                key = existing[0]

                if key not in seen_existing:
                    seen_existing.add(key)
                    plan.existing_ids.append(key)
                    rb_track = rekordbox.tracks_by_id[key]

                    if rb_track.filePath and not exists(rb_track.filePath) and track.path and exists(track.path):
                        plan.relocated[key] = track.path
            elif apple_id in seen_new:
                key = seen_new[apple_id]
            elif not track.path:
                plan.skipped.append((playlist.name, track.title or "(untitled)", "no local file"))
                continue
            elif not exists(track.path):
                plan.skipped.append((playlist.name, track.title or "(untitled)", "file not found"))
                continue
            else:
                key = str(NEW_TRACK_ID_BASE + apple_id)
                seen_new[apple_id] = key
                plan.new_tracks.append((key, track))

            if key in keys:
                plan.duplicates_removed += 1
            else:
                keys.append(key)

        plan.playlists.append(PlaylistPlan(playlist.name, tuple(keys), reason))

    return plan


def _location(path: str) -> str:
    return "file://localhost" + quote(path, safe="/")


def _new_track_element(key: str, track: MusicTrack) -> ET.Element:
    attrs = {
        "TrackID": key,
        "Name": track.title or "",
        "Artist": track.artist or "",
        "Composer": "",
        "Album": track.album or "",
        "Grouping": "",
        "Genre": track.genre or "",
        "Kind": _kind(track),
        "Size": str(track.size or 0),
        "TotalTime": str((track.duration_ms or 0) // 1000),
        "DiscNumber": "0",
        "TrackNumber": "0",
        "Year": str(track.year or 0),
        "AverageBpm": f"{float(track.bpm or 0):.2f}",
        "DateAdded": track.date_added.date().isoformat() if track.date_added else "",
        "BitRate": str(track.bit_rate or 0),
        "SampleRate": str(track.sample_rate or 0),
        "Comments": "",
        "PlayCount": "0",
        "Rating": "0",
        "Location": _location(track.path or ""),
        "Remixer": "",
        "Tonality": "",
        "Label": "",
        "Mix": "",
    }
    return ET.Element("TRACK", attrs)


def build_xml(plan: ExportPlan, source_xml: bytes, folder_name: str = DEFAULT_FOLDER) -> bytes:
    """The importable XML for the plan, built from the Rekordbox export it started from."""
    source = ET.fromstring(source_xml)
    collection_in = source.find("COLLECTION")
    by_id = {t.attrib.get("TrackID"): t for t in collection_in.findall("TRACK")} if collection_in is not None else {}

    root = ET.Element("DJ_PLAYLISTS", {"Version": source.attrib.get("Version", "1.0.0")})
    product = source.find("PRODUCT")

    if product is not None:
        root.append(copy.deepcopy(product))

    collection = ET.SubElement(root, "COLLECTION", {"Entries": str(len(plan.existing_ids) + len(plan.new_tracks))})

    for key in plan.existing_ids:
        element = copy.deepcopy(by_id[key])

        if key in plan.relocated:
            element.set("Location", _location(plan.relocated[key]))

        collection.append(element)

    for key, track in plan.new_tracks:
        collection.append(_new_track_element(key, track))

    playlists = ET.SubElement(root, "PLAYLISTS")
    top = ET.SubElement(playlists, "NODE", {"Type": "0", "Name": "ROOT", "Count": "1"})
    folder = ET.SubElement(top, "NODE", {"Type": "0", "Name": folder_name, "Count": str(len(plan.playlists))})

    for item in plan.playlists:
        node = ET.SubElement(folder, "NODE", {"Name": item.name, "Type": "1", "KeyType": "0", "Entries": str(len(item.keys))})

        for key in item.keys:
            ET.SubElement(node, "TRACK", {"Key": key})

    ET.indent(root, space="  ")

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + WRITTEN_MARKER + b"\n" + ET.tostring(root, encoding="utf-8")


def verify_xml(data: bytes, plan: ExportPlan) -> None:
    """Parse the XML again and check it holds exactly what was planned."""
    tracks = parse_rekordbox_bytes(data)
    playlists = parse_rekordbox_playlists(data)
    ids = {t.trackId for t in tracks}

    if len(tracks) != len(plan.existing_ids) + len(plan.new_tracks) or len(ids) != len(tracks):
        raise ExportError("The export check failed: the track list isn't what was planned.")

    if [(p.name, p.track_ids) for p in playlists] != [(p.name, p.keys) for p in plan.playlists]:
        raise ExportError("The export check failed: the playlists aren't what was planned.")

    if any(k not in ids for p in playlists for k in p.track_ids):
        raise ExportError("The export check failed: a playlist refers to a track that isn't in the file.")


def default_output_path(rekordbox: RekordboxLibrary) -> Path:
    return rekordbox.path.with_name(DEFAULT_FILE_NAME)


def check_output_path(target: Path, rekordbox: RekordboxLibrary) -> Path:
    """Where the file may be written: never over the Rekordbox export, never over a file CueBridge didn't write."""
    target = Path(target).expanduser()

    if target.suffix.lower() != ".xml":
        raise OutputPathError("The output file name must end in .xml.")

    if target.resolve() == rekordbox.path:
        raise OutputPathError("That is the Rekordbox export itself. Choose a different file for the playlists.")

    if not target.parent.is_dir():
        raise OutputPathError(f"The folder {target.parent} does not exist.")

    if target.exists():
        if not target.is_file():
            raise OutputPathError(f"{target} is not a file.")

        with open(target, "rb") as handle:
            if WRITTEN_MARKER not in handle.read(MARKER_SEARCH_BYTES):
                raise OutputPathError(f"{target} exists and wasn't written by CueBridge, so it won't be replaced.")

    return target


def write_export(data: bytes, target: Path) -> None:
    _replace_atomically(target, data)


def export_source_bytes(rekordbox: RekordboxLibrary) -> bytes:
    return read_library_bytes(rekordbox.path)
