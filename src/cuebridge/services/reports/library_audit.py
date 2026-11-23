"""Read-only audits of the Apple Music library: duplicates, metadata gaps, playlists, formats, questions, missing files.

Each audit is a pure function on the loaded `MusicLibrary` and returns a `Report`: a few numbers that are
always shown (`facts`), and named lists of items (`sections`) of which the caller pages through one.
Nothing is changed anywhere.

Scope follows the rule that only playlisted tracks matter (tracks in no playlist aren't meant for
Rekordbox): `scope_tracks` leaves the rest out. The playlist audit and the library questions look at the
whole library, because "which tracks are in no playlist" and "what was added recently" are about all of it.
"""

import os
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cuebridge.services.apple_music.xml_parser import MusicLibrary, MusicTrack
from cuebridge.services.reports.library_queries import format_time

LOSSLESS_WORDS = ("wav", "aiff", "aif", "lossless", "flac", "alac")
UNKNOWN = re.compile(r"\bunknown (artist|album)\b", re.IGNORECASE)
# A missing-files report where most files are "missing" almost always means the drive isn't mounted.
UNMOUNTED_RATIO = 0.5


@dataclass
class Report:
    name: str
    facts: dict
    sections: dict[str, list]
    default: str  # the section listed when none is asked for
    notes: list[str] = field(default_factory=list)


def playlisted_ids(music: MusicLibrary) -> set[int]:
    return {i for p in music.user_playlists if not p.is_folder for i in p.track_ids}


def scope_tracks(music: MusicLibrary, playlisted_only: bool) -> tuple[list[MusicTrack], int]:
    """The tracks to audit, and how many were left out for being in no playlist."""
    if not playlisted_only:
        return list(music.tracks), 0

    wanted = playlisted_ids(music)
    kept = [t for t in music.tracks if t.track_id in wanted]

    return kept, len(music.tracks) - len(kept)


def label(track: MusicTrack) -> str:
    return f"{track.artist + ' - ' if track.artist else ''}{track.title or '(no title)'}"


def brief(track: MusicTrack, **extra) -> dict:
    return {
        "trackId": track.track_id, "title": track.title, "artist": track.artist, "album": track.album,
        "path": track.path, **extra,
    }


def _normal(text: str | None) -> str:
    return re.sub(r"\W+", " ", (text or "").casefold()).strip()


def _lossless(track: MusicTrack) -> bool:
    kind = (track.kind or "").casefold()

    return any(word in kind for word in LOSSLESS_WORDS)


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def _group(key: str, tracks: list[MusicTrack]) -> dict:
    return {
        "key": key, "count": len(tracks),
        "tracks": [
            brief(t, kind=t.kind, duration=format_time(t.duration_ms) if t.duration_ms else None, sizeBytes=t.size)
            for t in sorted(tracks, key=lambda t: t.track_id)
        ],
    }


def audit_duplicates(tracks: list[MusicTrack], near_ms: int = 1000) -> Report:
    """The same song listed twice, by title and artist, by file, and by size and length."""
    by_name: dict[tuple[str, str], list[MusicTrack]] = defaultdict(list)
    by_path: dict[str, list[MusicTrack]] = defaultdict(list)
    by_size: dict[int, list[MusicTrack]] = defaultdict(list)
    untestable = 0

    for t in tracks:
        if t.artist and t.title:
            by_name[(_normal(t.artist), _normal(t.title))].append(t)
        else:
            untestable += 1

        if t.path:
            by_path[t.path].append(t)

        if t.size:
            by_size[t.size].append(t)

    same_name = [_group(label(g[0]), g) for k, g in sorted(by_name.items()) if len(g) > 1]
    same_file = [_group(p, g) for p, g in sorted(by_path.items()) if len(g) > 1]
    same_size = []

    for size, group in sorted(by_size.items()):
        group = sorted((t for t in group if t.duration_ms is not None), key=lambda t: t.duration_ms)
        run = [group[0]] if group else []

        for t in group[1:] + [None]:
            if t is not None and t.duration_ms - run[-1].duration_ms <= near_ms:
                run.append(t)
                continue

            names = {(_normal(r.artist), _normal(r.title)) for r in run if r.artist and r.title}
            paths = {r.path for r in run}

            # Not a pair already reported by name (same title and artist) or by file (same path).
            if len(run) > 1 and len(paths) > 1 and len(names) != 1:
                same_size.append(_group(f"{size} bytes, about {format_time(run[0].duration_ms)}", run))

            run = [t] if t is not None else []

    def involved(groups: list[dict]) -> int:
        return sum(g["count"] for g in groups)

    facts = {
        "sameTitleAndArtistGroups": len(same_name), "sameTitleAndArtistTracks": involved(same_name),
        "sameFileGroups": len(same_file), "sameFileTracks": involved(same_file),
        "sameSizeAndLengthGroups": len(same_size), "sameSizeAndLengthTracks": involved(same_size),
        "tracksWithoutTitleOrArtist": untestable,
    }
    notes = [f"{untestable} track(s) have no title or no artist, so they can't be matched by name."] if untestable else []

    return Report(
        "duplicates", facts,
        {"same_title_and_artist": same_name, "same_file": same_file, "same_size_and_length": same_size},
        "same_title_and_artist", notes,
    )


# ---------------------------------------------------------------------------
# Metadata gaps
# ---------------------------------------------------------------------------


def audit_metadata_gaps(tracks: list[MusicTrack], short_ms: int = 15_000) -> Report:
    """Tracks missing an artist, title, album, genre, BPM, year or length, and likely samples or placeholders."""
    checks: dict[str, Callable[[MusicTrack], bool]] = {
        "no_artist": lambda t: not t.artist,
        "no_title": lambda t: not t.title,
        "no_album": lambda t: not t.album,
        "no_genre": lambda t: not t.genre,
        "no_bpm": lambda t: not t.bpm,
        "no_year": lambda t: not t.year,
        "no_length": lambda t: not t.duration_ms,
        "unknown_artist_or_album": lambda t: bool(UNKNOWN.search(" / ".join(filter(None, (t.artist, t.album, t.path))))),
        "very_short": lambda t: bool(t.duration_ms) and t.duration_ms < short_ms,
    }
    sections = {
        name: [
            brief(t, genre=t.genre, bpm=t.bpm, duration=format_time(t.duration_ms) if t.duration_ms else None)
            for t in tracks if test(t)
        ]
        for name, test in checks.items()
    }
    facts = {"tracksAudited": len(tracks), **{name: len(items) for name, items in sections.items()}}
    facts["tracksWithAtLeastOneGap"] = len({
        i["trackId"] for name, items in sections.items() if name != "very_short" for i in items
    })

    return Report("metadata_gaps", facts, sections, "no_artist")


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------


def audit_playlists(music: MusicLibrary, many: int = 3) -> Report:
    """Tracks in no playlist or in many, empty and big playlists, and which playlists overlap most."""
    playlists = [p for p in music.user_playlists if not p.is_folder]
    members: dict[int, list[str]] = defaultdict(list)

    for p in playlists:
        for i in dict.fromkeys(p.track_ids):
            members[i].append(p.name)

    unplaylisted = [t for t in music.tracks if t.track_id not in members]
    in_many = sorted(
        (t for t in music.tracks if len(members.get(t.track_id, ())) >= many),
        key=lambda t: (-len(members[t.track_id]), label(t).casefold()),
    )
    sizes = sorted(
        ({"name": p.name, "tracks": len(p.track_ids), "uniqueTracks": len(set(p.track_ids))} for p in playlists),
        key=lambda p: (-p["tracks"], p["name"].casefold()),
    )
    overlaps = []
    sets = {p.name: set(p.track_ids) for p in playlists if p.track_ids}
    names = sorted(sets)

    for n, a in enumerate(names):
        for b in names[n + 1:]:
            shared = len(sets[a] & sets[b])

            if shared:
                overlaps.append({
                    "playlists": [a, b], "sharedTracks": shared,
                    "percentOfSmaller": round(100 * shared / min(len(sets[a]), len(sets[b]))),
                })

    overlaps.sort(key=lambda o: (-o["sharedTracks"], o["playlists"]))
    empty = [p.name for p in playlists if not p.track_ids]
    facts = {
        "tracks": len(music.tracks), "playlists": len(playlists),
        "tracksInNoPlaylist": len(unplaylisted), "tracksInManyPlaylists": len(in_many), "manyMeans": f"{many} or more",
        "emptyPlaylists": len(empty), "playlistPairsThatShareTracks": len(overlaps),
        "averagePlaylistsPerPlaylistedTrack": round(sum(len(v) for v in members.values()) / len(members), 2) if members else 0,
    }

    return Report(
        "playlists", facts,
        {
            "biggest_playlists": sizes,
            "not_in_any_playlist": [brief(t, genre=t.genre) for t in unplaylisted],
            "in_many_playlists": [brief(t, playlists=members[t.track_id]) for t in in_many],
            "empty_playlists": [{"name": n} for n in empty],
            "overlaps": overlaps,
        },
        "biggest_playlists",
    )


# ---------------------------------------------------------------------------
# Formats and quality
# ---------------------------------------------------------------------------

BIT_RATE_BUCKETS = (
    ("under 128 kbps", lambda r: r < 128), ("128 to 191 kbps", lambda r: r < 192), ("192 to 255 kbps", lambda r: r < 256),
    ("256 to 319 kbps", lambda r: r < 320), ("320 kbps", lambda r: r == 320), ("over 320 kbps", lambda r: True),
)


def audit_formats(tracks: list[MusicTrack], low_bit_rate: int = 192) -> Report:
    """Counts by format, bit rate and sample rate, lossy files below a bit rate, and the biggest files."""
    kinds = Counter(t.kind or "unknown" for t in tracks)
    rates = Counter(t.sample_rate for t in tracks)
    lossy = [t for t in tracks if not _lossless(t) and t.bit_rate]
    buckets = Counter()

    for t in lossy:
        buckets[next(name for name, test in BIT_RATE_BUCKETS if test(t.bit_rate))] += 1

    low = sorted((t for t in lossy if t.bit_rate < low_bit_rate), key=lambda t: (t.bit_rate, label(t).casefold()))
    largest = sorted((t for t in tracks if t.size), key=lambda t: -t.size)
    lossless = sum(1 for t in tracks if _lossless(t))
    facts = {
        "tracksAudited": len(tracks),
        "totalGigabytes": round(sum(t.size or 0 for t in tracks) / 1e9, 1),
        "lossless": lossless, "lossyWithKnownBitRate": len(lossy),
        "lossyBelowThreshold": len(low), "lowBitRateMeans": f"under {low_bit_rate} kbps",
        "averageLossyKbps": round(sum(t.bit_rate for t in lossy) / len(lossy)) if lossy else None,
    }

    return Report(
        "formats", facts,
        {
            "by_format": [{"format": k, "tracks": n} for k, n in kinds.most_common()],
            "by_lossy_bit_rate": [{"bitRate": name, "tracks": buckets[name]} for name, _ in BIT_RATE_BUCKETS if buckets[name]],
            "by_sample_rate": [{"sampleRateHz": k, "tracks": n} for k, n in sorted(rates.items(), key=lambda kv: (kv[0] is None, kv[0]))],
            "low_bit_rate": [brief(t, kind=t.kind, bitRate=t.bit_rate) for t in low],
            "largest_files": [brief(t, kind=t.kind, megabytes=round(t.size / 1e6, 1)) for t in largest],
        },
        "by_format",
    )


# ---------------------------------------------------------------------------
# Library questions
# ---------------------------------------------------------------------------


def audit_questions(tracks: list[MusicTrack], recent_days: int = 30, now: datetime | None = None) -> Report:
    """The library's size, what was added recently, and tracks per genre and per year."""
    now = now or datetime.now()
    since = now - timedelta(days=recent_days)
    dated = [t for t in tracks if t.date_added]
    recent = sorted((t for t in dated if t.date_added >= since), key=lambda t: t.date_added, reverse=True)
    genres = Counter(t.genre or "(no genre)" for t in tracks)
    years = Counter(t.year for t in tracks if t.year)
    facts = {
        "tracks": len(tracks),
        "hours": round(sum(t.duration_ms or 0 for t in tracks) / 3_600_000, 1),
        "gigabytes": round(sum(t.size or 0 for t in tracks) / 1e9, 1),
        "oldestAdded": min(t.date_added for t in dated).date().isoformat() if dated else None,
        "newestAdded": max(t.date_added for t in dated).date().isoformat() if dated else None,
        "addedInTheLastDays": recent_days, "addedRecently": len(recent),
        "genres": len(genres), "tracksWithNoYear": len(tracks) - sum(years.values()),
    }

    return Report(
        "questions", facts,
        {
            "recently_added": [brief(t, added=t.date_added.date().isoformat(), genre=t.genre) for t in recent],
            "by_genre": [{"genre": g, "tracks": n} for g, n in sorted(genres.items(), key=lambda kv: (-kv[1], kv[0].casefold()))],
            "by_year": [{"year": y, "tracks": n} for y, n in sorted(years.items(), reverse=True)],
        },
        "recently_added",
    )


# ---------------------------------------------------------------------------
# Missing files
# ---------------------------------------------------------------------------


def audit_missing_files(tracks: list[MusicTrack], exists: Callable[[str], bool] = os.path.exists) -> Report:
    """Apple Music tracks whose file is not at the path Apple Music has for it."""
    local = [t for t in tracks if t.path]
    missing = [t for t in local if not exists(t.path)]
    facts = {
        "tracksAudited": len(tracks), "withALocalFile": len(local), "withNoLocalFile": len(tracks) - len(local),
        "missing": len(missing),
    }
    notes = []

    if local and len(missing) / len(local) > UNMOUNTED_RATIO:
        notes.append(f"{len(missing)} of {len(local)} files are 'missing': is the drive with the music mounted?")

    return Report("missing_files", facts, {"missing": [brief(t, kind=t.kind) for t in missing]}, "missing", notes)
