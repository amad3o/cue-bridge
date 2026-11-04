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
from cuebridge.services.rekordbox_editor import (
    EDIT_MARKER,
    EditTarget,
    OutputPathError,
    _read_source,
    _replace_atomically,
    resolve_target,
)
from cuebridge.services.rekordbox_service import (
    PENDING_TRACK_ID_BASE,
    LibraryError,
    RekordboxLibrary,
    is_pending_track_id,
    parse_rekordbox_bytes,
    read_library_bytes,
)

WRITTEN_MARKER = b"<!-- Playlists written by CueBridge. -->"
DEFAULT_FOLDER = "Apple Music"
DEFAULT_FILE_NAME = "cuebridge-playlists.xml"
# Keys for tracks Rekordbox doesn't have. They only have to be unique inside the file.
NEW_TRACK_ID_BASE = PENDING_TRACK_ID_BASE
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


# ---------------------------------------------------------------------------
# Adding the playlists to the Rekordbox XML itself
# ---------------------------------------------------------------------------
#
# Rekordbox's "Imported Library" setting points at one XML file, usually the
# export the user already has. Writing the playlists into that file means
# nothing has to be re-pointed. It is an edit of a file the user cares about,
# so it goes through the same backup, atomic write and "changed since planned"
# checks as the other editing tools.


@dataclass(frozen=True)
class FilePlan:
    """The edited Rekordbox XML, ready to be written by rekordbox_editor.write_plan."""

    target: EditTarget
    xml: bytes
    folder_name: str
    tracks_added: int
    tracks_replaced: int  # from an earlier run
    relocated: int


def _indent_unit(parent: ET.Element) -> str:
    text = parent.text or ""
    return text if text.strip() == "" and "\n" in text else "\n  "


def _append_indented(parent: ET.Element, child: ET.Element) -> None:
    """Add `child` as the last child of `parent`, with whitespace that matches its siblings."""
    inner = _indent_unit(parent)
    outer = inner[: -2] if len(inner) > 2 else "\n"
    level = max(0, (len(inner) - 1) // 2)
    ET.indent(child, space="  ", level=level)
    children = list(parent)

    if children:
        children[-1].tail = inner
    else:
        parent.text = inner

    child.tail = outer
    parent.append(child)


def _is_pending(element: ET.Element) -> bool:
    return is_pending_track_id(element.attrib.get("TrackID", ""))


def _playlist_root(root: ET.Element) -> ET.Element:
    playlists = root.find("PLAYLISTS")

    if playlists is None:
        playlists = ET.SubElement(root, "PLAYLISTS")

    top = playlists.find("NODE")

    if top is None:
        top = ET.Element("NODE", {"Type": "0", "Name": "ROOT", "Count": "0"})
        _append_indented(playlists, top)

    return top


def build_file_plan(
    plan: ExportPlan,
    rekordbox: RekordboxLibrary,
    folder_name: str = DEFAULT_FOLDER,
    output_path: str | Path | None = None,
) -> FilePlan:
    """Add the planned playlists (a folder of them) and new tracks to the loaded Rekordbox XML."""
    source_path, original, stat = _read_source(rekordbox.path)
    target = resolve_target(source_path, stat, output_path)
    before = parse_rekordbox_bytes(original)
    root = ET.fromstring(original)
    collection = root.find("COLLECTION")

    if collection is None:
        raise ExportError("The Rekordbox file has no COLLECTION.")

    top = _playlist_root(root)
    previous = rekordbox.pending_folder
    # Only a folder CueBridge itself added earlier (named in the file's note) is ever removed.
    stale = [n for n in top.findall("NODE") if previous is not None and n.attrib.get("Name") == previous]
    clash = [n for n in top.findall("NODE") if n.attrib.get("Name") == folder_name and n not in stale]

    if clash:
        raise ExportError(
            f"The Rekordbox file already has a folder or playlist called {folder_name!r} that CueBridge didn't add. "
            "Choose a different folder_name."
        )

    # Take out what an earlier run added; the new content replaces it.
    for node in stale:
        top.remove(node)

    old_pending = [t for t in collection.findall("TRACK") if _is_pending(t)]

    for element in old_pending:
        collection.remove(element)

    by_id = {t.attrib.get("TrackID"): t for t in collection.findall("TRACK")}

    for key, path in plan.relocated.items():
        by_id[key].set("Location", _location(path))

    for key, track in plan.new_tracks:
        _append_indented(collection, _new_track_element(key, track))

    collection.set("Entries", str(len(collection.findall("TRACK"))))

    folder = ET.Element("NODE", {"Type": "0", "Name": folder_name, "Count": str(len(plan.playlists))})

    for item in plan.playlists:
        node = ET.SubElement(folder, "NODE", {"Name": item.name, "Type": "1", "KeyType": "0", "Entries": str(len(item.keys))})

        for key in item.keys:
            ET.SubElement(node, "TRACK", {"Key": key})

    _append_indented(top, folder)
    top.set("Count", str(len(top.findall("NODE"))))

    xml = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    declaration, _, rest = xml.partition(b"\n")
    note = b"<!-- CueBridge playlists folder: " + folder_name.encode("utf-8") + b" -->"
    edited = declaration + b"\n" + EDIT_MARKER + b"\n" + note + b"\n" + rest

    _verify_file(original, edited, plan, folder_name, previous)

    return FilePlan(target, edited, folder_name, len(plan.new_tracks), len(old_pending), len(plan.relocated))


def _verify_file(original: bytes, edited: bytes, plan: ExportPlan, folder_name: str, previous_folder: str | None) -> None:
    """The edited file must hold the old library untouched, plus exactly the planned additions."""
    before = parse_rekordbox_bytes(original)
    after = parse_rekordbox_bytes(edited)
    old_real = [t for t in before if not is_pending_track_id(t.trackId)]
    new_real = [t for t in after if not is_pending_track_id(t.trackId)]

    if [t.trackId for t in new_real] != [t.trackId for t in old_real]:
        raise ExportError("The edit check failed: the existing tracks changed.")

    def strip(track):
        return track.model_copy(update={"filePath": None, "cues": [c.model_copy(update={"filePath": None}) for c in track.cues]})

    for was, now in zip(old_real, new_real):
        # A moved file may get its new path; everything else, cues included, must be identical.
        if strip(was) != strip(now) or (was.filePath != now.filePath and was.trackId not in plan.relocated):
            raise ExportError(f"The edit check failed: track {was.trackId} changed.")

    if sorted(t.trackId for t in after if is_pending_track_id(t.trackId)) != sorted(k for k, _ in plan.new_tracks):
        raise ExportError("The edit check failed: the new tracks aren't what was planned.")

    playlists = parse_rekordbox_playlists(edited)
    ours = [p for p in playlists if p.folders[:1] == (folder_name,)]
    kept_before = [p for p in parse_rekordbox_playlists(original) if previous_folder is None or p.folders[:1] != (previous_folder,)]
    kept_after = [p for p in playlists if p.folders[:1] != (folder_name,)]

    if kept_before != kept_after:
        raise ExportError("The edit check failed: the existing playlists changed.")

    if [(p.name, p.track_ids) for p in ours] != [(p.name, p.keys) for p in plan.playlists]:
        raise ExportError("The edit check failed: the playlists aren't what was planned.")

    ids = {t.trackId for t in after}

    if any(k not in ids for p in playlists for k in p.track_ids if p in ours):
        raise ExportError("The edit check failed: a playlist refers to a track that isn't in the file.")
