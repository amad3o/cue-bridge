"""Builders for small audio files carrying a Serato Markers2 tag."""

import wave
from pathlib import Path

from mutagen.id3 import APIC, GEOB, ID3, TIT2
from mutagen.wave import WAVE

from cuebridge.models import Cue, Track
from cuebridge.parsers import serato_markers as m
from cuebridge.parsers.serato_markers import CueEntry, RawEntry

COLOR = RawEntry("COLOR", bytes.fromhex("00ffffff"))
BPMLOCK = RawEntry("BPMLOCK", b"\x00")

# The first bytes of an MP3: a frame header, then (optionally) the Xing/Info and LAME tags.
_HEADER = b"\xff\xfb\x90\x00"
AUDIO_WITH_LAME = _HEADER + b"\x00" * 32 + b"Info" + b"\x00" * 100 + b"LAME3.99r" + b"\x00" * 300
AUDIO_INFO_NO_LAME = _HEADER + b"\x00" * 32 + b"Info" + b"\x00" * 400
AUDIO_PLAIN = _HEADER + b"\x00" * 500


def geob(entries) -> GEOB:
    return GEOB(
        encoding=0, mime="application/octet-stream", filename="",
        desc=m.DESCRIPTION, data=m.encode_markers2(entries),
    )


def legacy_geob(data: bytes) -> GEOB:
    return GEOB(encoding=0, mime="application/octet-stream", filename="", desc=m.LEGACY_DESCRIPTION, data=data)


def make_mp3(
    path: Path, entries=None, audio: bytes = AUDIO_WITH_LAME, version: int = 4, legacy: bytes | None = None
) -> Path:
    """An MP3 with an ID3 tag (a title, artwork, optionally the Serato frame) and some 'audio'."""
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Song"))
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="", data=b"\x89PNG" + b"\x07" * 3000))

    if entries is not None:
        tags.add(geob(entries))

    if legacy is not None:
        tags.add(legacy_geob(legacy))

    tags.save(path, v2_version=version)

    with open(path, "ab") as handle:
        handle.write(audio * 20)

    return path


def make_wav(path: Path, entries=None, legacy: bytes | None = None) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(bytes(range(256)) * 40)

    if entries is not None:
        wav = WAVE(path)
        wav.add_tags()
        wav.tags.add(APIC(encoding=3, mime="image/png", type=3, desc="", data=b"\x89PNG" + b"\x07" * 3000))
        wav.tags.add(geob(entries))

        if legacy is not None:
            wav.tags.add(legacy_geob(legacy))

        wav.save()

    return path


def rb_cue(kind="hotcue", slot=0, ms=1000, color=None, name=None) -> Cue:
    return Cue(
        id=f"rb-{kind}-{slot}-{ms}", source="rekordbox", trackTitle="Song", cueIndex=slot,
        cueName=name, timeMs=ms, color=color, cueType=kind,
    )


def rb_track(path: Path | None, *cues: Cue, track_id: str = "1", title: str = "Song") -> Track:
    return Track(
        trackId=track_id, trackTitle=title, artist="Artist",
        filePath=None if path is None else str(path), cues=list(cues),
    )


# A real legacy `Serato Markers_` payload (from "02 Pax.mp3"): five cues, then the loop slots.
_UNSET = "7f" * 5
_LEGACY_LOOP_SLOT = _UNSET + _UNSET + "00" + _UNSET + "0000000003" + "00"
REAL_LEGACY = bytes.fromhex(
    "02050000000e"
    # the five cue entries, as captured
    "000000017e7f7f7f7f7f007f7f7f7f7f063000000100"
    "00000156227f7f7f7f7f007f7f7f7f7f000318000100"
    "00000156297f7f7f7f7f007f7f7f7f7f0630014c0100"
    "0000065d687f7f7f7f7f007f7f7f7f7f0003194c0100"
    "000007 6a1e7f7f7f7f7f007f7f7f7f7f0420014c0100".replace(" ", "")
    + _LEGACY_LOOP_SLOT * 9
    + "077f7f7f"
)
REAL_LEGACY_CUES = [
    CueEntry(0, 254, "#cc0000"),
    CueEntry(1, 27426, "#00cc00"),
    CueEntry(2, 27433, "#cc00cc"),
    CueEntry(3, 110312, "#00cccc"),
    CueEntry(4, 128286, "#8800cc"),
]

# The same frame with every cue slot empty, as Serato writes a track with no cues.
EMPTY_LEGACY = m.encode_legacy(REAL_LEGACY, [])
