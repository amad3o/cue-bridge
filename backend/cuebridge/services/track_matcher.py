"""Find the Apple Music track that a Rekordbox track corresponds to.

Paths are the best evidence and are tried first. Files that have moved or been
renamed are then found by file name, and finally by title and artist (with the
duration to break ties). A match is only reported when it is unambiguous: two
equally good candidates give "ambiguous" and no track, because wrongly
matching is worse than not matching.
"""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePath

from cuebridge.models import Track
from cuebridge.parsers.apple_music_xml import MusicLibrary, MusicTrack

DURATION_TOLERANCE_MS = 2000  # Rekordbox stores whole seconds


def normalize_text(value: str | None) -> str:
    """Case, accents, punctuation and spacing don't matter when comparing titles."""
    if not value:
        return ""

    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = text.replace("&", " and ")

    return re.sub(r"[\W_]+", " ", text).strip()


def normalize_path(path: str | None) -> str:
    """macOS and ExFAT ignore case, and the same name can be composed in two Unicode forms."""
    return unicodedata.normalize("NFC", path).casefold() if path else ""


def _file_name(path: str | None) -> str:
    return normalize_text(PurePath(path).stem) if path else ""


@dataclass(frozen=True)
class MatchResult:
    method: str  # "path", "file_name", "title_artist", "ambiguous" or "none"
    track: MusicTrack | None = None
    candidates: tuple[MusicTrack, ...] = ()


class AppleMusicIndex:
    """Lookup tables over an Apple Music library, built once and reused."""

    def __init__(self, library: MusicLibrary):
        self.by_path: dict[str, list[MusicTrack]] = {}
        self.by_file_name: dict[str, list[MusicTrack]] = {}
        self.by_title: dict[str, list[MusicTrack]] = {}

        for track in library.tracks:
            self.by_path.setdefault(normalize_path(track.path), []).append(track)
            self.by_file_name.setdefault(_file_name(track.path), []).append(track)
            self.by_title.setdefault(normalize_text(track.title), []).append(track)

    def match(self, track: Track) -> MatchResult:
        for lookup in (self._by_path, self._by_file_name, self._by_title_and_artist):
            result = lookup(track)

            if result is not None:
                return result

        return MatchResult("none")

    def _by_path(self, track: Track) -> MatchResult | None:
        found = self.by_path.get(normalize_path(track.filePath), []) if track.filePath else []

        return MatchResult("path", found[0]) if len(found) == 1 else None

    def _choose(self, method: str, candidates: list[MusicTrack], track: Track) -> MatchResult | None:
        if not candidates:
            return None

        if len(candidates) > 1 and track.durationMs is not None:
            near = [c for c in candidates if c.duration_ms is not None and abs(c.duration_ms - track.durationMs) <= DURATION_TOLERANCE_MS]
            candidates = near or candidates

        if len(candidates) == 1:
            return MatchResult(method, candidates[0])

        return MatchResult("ambiguous", None, tuple(candidates))

    def _by_file_name(self, track: Track) -> MatchResult | None:
        name = _file_name(track.filePath)

        return self._choose("file_name", list(self.by_file_name.get(name, [])), track) if name else None

    def _by_title_and_artist(self, track: Track) -> MatchResult | None:
        title = normalize_text(track.trackTitle)

        if not title:
            return None

        artist = normalize_text(track.artist)
        candidates = [
            c for c in self.by_title.get(title, [])
            # A blank artist on either side is compatible with anything.
            if not artist or not c.artist or normalize_text(c.artist) == artist
        ]

        return self._choose("title_artist", candidates, track)
