import xml.etree.ElementTree as ET

def parse_rekordbox_xml(xml_content: bytes):
    root = ET.fromstring(xml_content)

    tracks = []

    collection = root.find("COLLECTION")

    if collection is None:
        return []

    for track in collection.findall("TRACK"):
        track_name = track.attrib.get("Name", "Unknown")
        artist = track.attrib.get("Artist", "Unknown")

        cues = []

        for cue in track.findall("POSITION_MARK"):
            cues.append({
                "id": f"rekordbox-{track_name}-{cue.attrib.get('Num', '0')}",
                "source": "rekordbox",
                "trackTitle": track_name,
                "artist": artist,
                "cueIndex": int(cue.attrib.get("Num", 0)),
                "cueName": cue.attrib.get("Name") or "Unnamed cue",
                "timeMs": int(float(cue.attrib.get("Start", 0)) * 1000),
                "color": None,
                "cueType": "hotcue",
            })

        tracks.append({
            "trackTitle": track_name,
            "artist": artist,
            "cues": cues,
        })
    return tracks