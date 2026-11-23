import xml.etree.ElementTree as ET
from dataclasses import dataclass

from cuebridge.parsers.paths import location_to_path

# Rekordbox POSITION_MARK Type values.
# 0 = cue, 1 = fade-in, 2 = fade-out, 3 = load marker, 4 = loop.
# Only cues and loops are parsed; fades and load markers have no Serato equivalent.
TYPE_CUE = "0"
TYPE_LOOP = "4"


@dataclass(frozen=True)
class RekordboxPlaylist:
    name: str
    folders: tuple[str, ...]  # the folders it sits in, outermost first (not counting ROOT)
    track_ids: tuple[str, ...]  # Rekordbox TrackIDs, in playlist order


class RekordboxParseError(ValueError):
    """The XML is well-formed but is not a Rekordbox library export."""


def _to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value):
    """Return stripped text, or None when the attribute is missing or blank."""
    if value is None:
        return None

    return value.strip() or None


def _seconds_to_ms(value):
    try:
        return int(float(value) * 1000)
    except (TypeError, ValueError):
        return None


def _parse_color(attrib):
    channels = [_to_int(attrib.get(c)) for c in ("Red", "Green", "Blue")]

    if any(c is None for c in channels):
        return None

    r, g, b = (max(0, min(255, c)) for c in channels)
    return f"#{r:02x}{g:02x}{b:02x}"


def _parse_cue(mark, track_id, track_name, artist, file_path):
    mark_type = mark.attrib.get("Type")

    if mark_type not in (TYPE_CUE, TYPE_LOOP):
        return None

    time_ms = _seconds_to_ms(mark.attrib.get("Start"))

    if time_ms is None:
        return None

    num = _to_int(mark.attrib.get("Num"), -1)
    is_memory = num < 0

    if mark_type == TYPE_LOOP:
        cue_type = "loop"
    elif is_memory:
        cue_type = "memory"
    else:
        cue_type = "hotcue"

    return {
        "id": f"rekordbox-{track_id}-{cue_type}-{num}-{time_ms}",
        "source": "rekordbox",
        "trackTitle": track_name,
        "artist": artist,
        "filePath": file_path,
        "cueIndex": None if is_memory else num,
        "cueName": _text(mark.attrib.get("Name")),
        "timeMs": time_ms,
        "endMs": _seconds_to_ms(mark.attrib.get("End")) if mark_type == TYPE_LOOP else None,
        "color": _parse_color(mark.attrib),
        "cueType": cue_type,
    }


def parse_rekordbox_xml(xml_content: bytes):
    root = ET.fromstring(xml_content)

    collection = root.find("COLLECTION")

    if collection is None:
        raise RekordboxParseError("No <COLLECTION> element found.")

    tracks = []

    for position, track in enumerate(collection.findall("TRACK")):
        track_id = track.attrib.get("TrackID") or f"idx{position}"
        track_name = _text(track.attrib.get("Name"))
        artist = _text(track.attrib.get("Artist"))
        file_path = location_to_path(track.attrib.get("Location"))

        cues = []

        for mark in track.findall("POSITION_MARK"):
            cue = _parse_cue(mark, track_id, track_name, artist, file_path)

            if cue is not None:
                cues.append(cue)

        first_tempo = track.find("TEMPO")

        tracks.append({
            "trackId": track_id,
            "trackTitle": track_name,
            "artist": artist,
            "filePath": file_path,
            "durationMs": _seconds_to_ms(track.attrib.get("TotalTime")),
            "firstBeatMs": _seconds_to_ms(first_tempo.attrib.get("Inizio")) if first_tempo is not None else None,
            "cues": cues,
        })

    return tracks


def parse_rekordbox_playlists(xml_content: bytes) -> list[RekordboxPlaylist]:
    """The playlists in the export's PLAYLISTS tree (folders are walked, not returned)."""
    root = ET.fromstring(xml_content)
    top = root.find("PLAYLISTS")
    found: list[RekordboxPlaylist] = []

    def walk(node, folders: tuple[str, ...]):
        for child in node.findall("NODE"):
            name = _text(child.attrib.get("Name")) or "(unnamed)"

            if child.attrib.get("Type") == "0":  # a folder
                walk(child, folders if not folders and name == "ROOT" else folders + (name,))
            else:
                keys = tuple(k for k in (t.attrib.get("Key") for t in child.findall("TRACK")) if k)
                found.append(RekordboxPlaylist(name, folders, keys))

    if top is not None:
        walk(top, ())

    return found
