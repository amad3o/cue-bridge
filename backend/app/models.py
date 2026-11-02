from typing import Literal

from pydantic import BaseModel

CueType = Literal["hotcue", "memory", "loop"]


class Cue(BaseModel):
    id: str
    source: Literal["rekordbox", "serato"]
    trackTitle: str
    artist: str | None = None
    filePath: str | None = None
    # Hot cue / hot loop slot. None for memory cues and memory loops.
    cueIndex: int | None = None
    cueName: str | None = None
    timeMs: int
    # Loop end time. None for anything that isn't a loop.
    endMs: int | None = None
    # Hex color like "#28e214", or None when the source has no color.
    color: str | None = None
    cueType: CueType


class Track(BaseModel):
    trackId: str
    trackTitle: str
    artist: str
    filePath: str | None = None
    cues: list[Cue]


class RekordboxUploadResponse(BaseModel):
    filename: str
    tracksFound: int
    tracks: list[Track]
