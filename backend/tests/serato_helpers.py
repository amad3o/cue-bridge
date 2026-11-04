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


def make_mp3(path: Path, entries=None, audio: bytes = AUDIO_WITH_LAME, version: int = 4) -> Path:
    """An MP3 with an ID3 tag (a title, artwork, optionally the Serato frame) and some 'audio'."""
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Song"))
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="", data=b"\x89PNG" + b"\x07" * 3000))

    if entries is not None:
        tags.add(geob(entries))

    tags.save(path, v2_version=version)

    with open(path, "ab") as handle:
        handle.write(audio * 20)

    return path


def make_wav(path: Path, entries=None) -> Path:
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
