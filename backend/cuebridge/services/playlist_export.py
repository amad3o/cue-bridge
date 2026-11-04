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
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from cuebridge.parsers.apple_music_xml import MusicLibrary, MusicTrack
from cuebridge.parsers.rekordbox_parser import parse_rekordbox_playlists
from cuebridge.services.drift import LibraryDrift
from cuebridge.services.rekordbox_editor import (
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


# Rekordbox leaves these unencoded in a Location (and encodes spaces, &, ' and brackets).
_LOCATION_SAFE = "/()!$,#+=:"


def _location(path: str) -> str:
    return "file://localhost" + quote(path, safe=_LOCATION_SAFE)


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


def _attr(value: str) -> str:
    """Escape an attribute value the way Rekordbox writes it."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")
    )


def _track_line(key: str, track: MusicTrack) -> str:
    attributes = " ".join(f'{name}="{_attr(value)}"' for name, value in _new_track_element(key, track).attrib.items())
    return f"    <TRACK {attributes}/>"


def _folder_block(plan: ExportPlan, folder_name: str) -> list[str]:
    lines = [f'      <NODE Type="0" Name="{_attr(folder_name)}" Count="{len(plan.playlists)}">']

    for item in plan.playlists:
        head = f'        <NODE Name="{_attr(item.name)}" Type="1" KeyType="0" Entries="{len(item.keys)}"'

        if not item.keys:
            lines.append(head + "/>")
            continue

        lines.append(head + ">")
        lines.extend(f'          <TRACK Key="{_attr(key)}"/>' for key in item.keys)
        lines.append("        </NODE>")

    lines.append("      </NODE>")
    return lines


_NODE_TOKEN = re.compile(r"<NODE\b[^>]*?(/?)>|</NODE>")
_COLLECTION_COUNT = re.compile(r'(<COLLECTION Entries=")(\d+)(")')
_ROOT_COUNT = re.compile(r'(<NODE Type="0" Name="ROOT" Count=")(\d+)(")')
_PENDING_TRACK = re.compile(r'[ \t]*<TRACK TrackID="(\d+)"[^>]*/>\r?\n')


def _remove_pending_tracks(text: str) -> tuple[str, int]:
    removed = 0

    def drop(match):
        nonlocal removed

        if is_pending_track_id(match.group(1)):
            removed += 1
            return ""

        return match.group(0)

    return _PENDING_TRACK.sub(drop, text), removed


def _remove_folder(text: str, folder_name: str) -> tuple[str, bool]:
    """Take out a folder node (and everything in it) that CueBridge added earlier."""
    opening = re.search(rf'[ \t]*<NODE Type="0" Name="{re.escape(_attr(folder_name))}"[^>]*>\r?\n', text)

    if opening is None:
        return text, False

    depth = 0

    for token in _NODE_TOKEN.finditer(text, opening.start()):
        if token.group(0) == "</NODE>":
            depth -= 1
        elif token.group(1) != "/":
            depth += 1

        if depth == 0:
            end = text.index("\n", token.end()) + 1
            return text[: opening.start()] + text[end:], True

    raise ExportError("The folder CueBridge added earlier isn't well formed, so it wasn't replaced.")


def _set_count(pattern: re.Pattern, text: str, change=None, value: int | None = None) -> str:
    found = pattern.search(text)

    if found is None:
        return text  # a file without that count has nothing to keep up to date

    new = value if value is not None else int(found.group(2)) + change

    return text[: found.start(2)] + str(new) + text[found.end(2) :]


def _strip_additions(text: str, folder_name: str | None, relocated: dict[str, str]) -> str:
    """The file with everything CueBridge adds taken out and the counts and moved paths blanked, for comparing."""
    text, _ = _remove_pending_tracks(text)

    if folder_name:
        text, _ = _remove_folder(text, folder_name)

    text = _COLLECTION_COUNT.sub(r"\g<1>N\g<3>", text)
    text = _ROOT_COUNT.sub(r"\g<1>N\g<3>", text)

    for key in relocated:
        text = re.sub(rf'(<TRACK TrackID="{re.escape(key)}"[^>]*?Location=")[^"]*(")', r"\g<1>L\g<2>", text, flags=re.S)

    return text


def _insert_before(text: str, index: int, lines: list[str], newline: str) -> str:
    """Insert whole lines before the tag at `index`: at the start of its line, or just before it if it shares one."""
    start = text.rfind("\n", 0, index) + 1

    if text[start:index].strip() == "":
        return text[:start] + "".join(line + newline for line in lines) + text[start:]

    return text[:index] + newline.join(lines) + newline + text[index:]


def patch_xml(original: bytes, plan: ExportPlan, folder_name: str, previous_folder: str | None) -> bytes:
    """Add the planned tracks and folder to Rekordbox's XML as text, leaving every other byte as it is.

    The file is not parsed and written back: a round trip through an XML library
    changes the declaration, line endings, escaping and spacing, and Rekordbox
    turned out not to accept that. New lines are written in Rekordbox's own style
    and inserted, and nothing else is touched.
    """
    text = original.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"

    # What an earlier run added is replaced by the new content.
    text, removed_tracks = _remove_pending_tracks(text)
    removed_folder = False

    if previous_folder is not None:
        text, removed_folder = _remove_folder(text, previous_folder)

    clash = re.search(rf'<NODE Type="0" Name="{re.escape(_attr(folder_name))}"', text) or re.search(
        rf'<NODE Name="{re.escape(_attr(folder_name))}" Type="1"', text
    )

    if clash:
        raise ExportError(
            f"The Rekordbox file already has a folder or playlist called {folder_name!r} that CueBridge didn't add. "
            "Choose a different folder_name."
        )

    for key, path in plan.relocated.items():
        text, changed = re.subn(
            rf'(<TRACK TrackID="{re.escape(key)}"[^>]*?Location=")[^"]*(")',
            lambda m: m.group(1) + _attr(_location(path)) + m.group(2),
            text, count=1, flags=re.S,
        )

        if not changed:
            raise ExportError(f"Track {key} wasn't found in the Rekordbox file.")

    end = text.find("</COLLECTION>")

    if end < 0:
        raise ExportError("The Rekordbox file has no COLLECTION.")

    if plan.new_tracks:
        text = _insert_before(text, end, [_track_line(key, track) for key, track in plan.new_tracks], newline)

    entries = len(re.findall(r'<TRACK TrackID="', text[: text.find("</COLLECTION>")]))
    text = _set_count(_COLLECTION_COUNT, text, value=entries)

    playlists_end = text.find("</PLAYLISTS>")
    root_close = text.rfind("</NODE>", 0, playlists_end)

    if playlists_end < 0 or root_close < 0:
        raise ExportError("The Rekordbox file has no playlists section to add the folder to.")

    text = _insert_before(text, root_close, _folder_block(plan, folder_name), newline)
    text = _set_count(_ROOT_COUNT, text, change=1 - (1 if removed_folder else 0))

    return text.encode("utf-8")


def build_file_plan(
    plan: ExportPlan,
    rekordbox: RekordboxLibrary,
    folder_name: str = DEFAULT_FOLDER,
    output_path: str | Path | None = None,
) -> FilePlan:
    """Add the planned playlists (a folder of them) and new tracks to the loaded Rekordbox XML."""
    source_path, original, stat = _read_source(rekordbox.path)
    target = resolve_target(source_path, stat, output_path)
    previous = rekordbox.pending_folder
    edited = patch_xml(original, plan, folder_name, previous)

    # Taking the additions back out must give the original file, byte for byte (counts and moved paths aside).
    if _strip_additions(edited.decode("utf-8"), folder_name, plan.relocated) != _strip_additions(
        original.decode("utf-8"), previous, plan.relocated
    ):
        raise ExportError("The edit check failed: something other than the additions changed in the file.")

    _verify_file(original, edited, plan, folder_name, previous)

    return FilePlan(target, edited, folder_name, len(plan.new_tracks), 0, len(plan.relocated))


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
