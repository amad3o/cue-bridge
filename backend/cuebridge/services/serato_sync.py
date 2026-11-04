"""Copy Rekordbox cues into Serato's tags, with Rekordbox winning.

For every track that has cues in Rekordbox, the hot cues in the audio file's
`Serato Markers2` tag are replaced by the Rekordbox ones (hot cues in their own
slots, plus memory cues in the free slots), with the timing correction for the
file type. Tracks without Rekordbox cues are never touched.

Serato keeps the cues twice: in `Markers2` and in the older `Markers_` frame,
which holds the first five cues and which Serato DJ Pro still reads. Both are
kept in step.

Writing is deliberately conservative. Almost every Serato-tagged file has a
Markers2 payload of exactly 470 bytes and a legacy payload of 318, so the new
payloads are written over the old ones in place: nothing else in the file moves
or is re-serialised. Before
anything is written, the original bytes of every file go into a journal on disk,
and after each write the file is read back to prove that only those bytes
changed. Anything that doesn't check out is put back immediately, and a whole
run can be undone from its journal.
"""

import hashlib
import json
import os
import subprocess
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from cuebridge.models import Track
from cuebridge.parsers.serato_markers import (
    CueEntry,
    MarkersError,
    DESCRIPTION,
    LEGACY_DESCRIPTION,
    decode_markers2,
    encode_legacy,
    encode_markers2,
    replace_cues,
)
from cuebridge.parsers.serato_tags import (
    Markers2Location,
    UnsupportedFileError,
    locate_frame,
    locate_legacy_markers,
    locate_markers2,
)
from cuebridge.services.cue_rules import MAX_HOT_CUE_SLOTS, plan_memory_to_hot
from cuebridge.services.cue_timing import TimingOffset, timing_offset, to_serato_ms
from cuebridge.services.library_queries import format_time
from cuebridge.services.rekordbox_service import LibraryError

DEFAULT_COLOR = "#cc0000"  # Serato's first hot cue color, used only for a slot with no color anywhere

# A Serato cue that is already within this many ms of what Rekordbox implies is
# left exactly as it is. The measured timing model matches real synced cues to
# within 1 ms. Where the timing model is unverified, one MP3 frame (about 26 ms)
# is allowed, so values an earlier sync already got right are not made worse.
TOLERANCE_MS = 1
UNVERIFIED_TOLERANCE_MS = 30
DEFAULT_MAX_FILES = 500
JOURNAL_VERSION = 2
HASH_CHUNK = 1024 * 1024


class SyncError(LibraryError):
    """The sync can't run, or an undo can't be done."""


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Region:
    """A stretch of a file that the sync overwrites."""

    description: str  # which Serato frame it is
    offset: int
    old: bytes
    new: bytes

    @property
    def length(self) -> int:
        return len(self.old)


@dataclass(frozen=True)
class SyncItem:
    track_id: str
    title: str | None
    artist: str | None
    path: Path | None
    status: str  # "change", "unchanged" or "skip"
    reason: str | None = None  # why it was skipped
    before: tuple[tuple[int, int], ...] = ()  # (slot 1-8, ms) now in Serato
    after: tuple[tuple[int, int], ...] = ()  # (slot 1-8, ms) after the sync
    timing: TimingOffset | None = None
    location: Markers2Location | None = None
    new_data: bytes | None = None
    legacy_location: Markers2Location | None = None
    legacy_new_data: bytes | None = None
    memory_cues_without_slot: int = 0

    @property
    def regions(self) -> tuple[Region, ...]:
        """The parts of the file that would change (none for an unchanged file)."""
        found = []

        if self.location is not None and self.new_data != self.location.data:
            found.append(Region(DESCRIPTION, self.location.offset, self.location.data, self.new_data))

        if self.legacy_location is not None and self.legacy_new_data != self.legacy_location.data:
            found.append(
                Region(LEGACY_DESCRIPTION, self.legacy_location.offset, self.legacy_location.data, self.legacy_new_data)
            )

        return tuple(found)


@dataclass(frozen=True)
class SyncPlan:
    items: tuple[SyncItem, ...]
    loops_ignored: int
    not_planned: int  # tracks left out because of the max_files limit

    def with_status(self, status: str) -> list[SyncItem]:
        return [i for i in self.items if i.status == status]

    @property
    def to_change(self) -> list[SyncItem]:
        return self.with_status("change")

    @property
    def skip_reasons(self) -> Counter:
        return Counter(i.reason for i in self.with_status("skip"))

    @property
    def flagged_timing(self) -> list[SyncItem]:
        return [i for i in self.items if i.status == "change" and i.timing and i.timing.flagged]


def serato_cues_for(
    track: Track,
    offset: TimingOffset,
    existing: dict[int, CueEntry],
    include_memory_cues: bool,
    default_color: str,
) -> tuple[list[CueEntry], int]:
    """The Serato cues a Rekordbox track should have, and how many memory cues found no slot.

    Rekordbox wins on which cues exist and where they are, but only changes what
    differs: a cue already in the right slot at (nearly) the right time keeps its
    exact bytes, color included. A color or name comes from Rekordbox when it has
    one, otherwise from the cue already in that slot, otherwise from the default.
    """
    chosen: list[tuple[int, object]] = [
        (c.cueIndex, c)
        for c in track.cues
        if c.cueType == "hotcue" and c.cueIndex is not None and 0 <= c.cueIndex < MAX_HOT_CUE_SLOTS
    ]
    overflow = 0

    if include_memory_cues:
        result = plan_memory_to_hot(track.cues)
        chosen += [(a.slot, a.cue) for a in result.assignments]
        overflow = len(result.overflow)

    tolerance = UNVERIFIED_TOLERANCE_MS if offset.flagged else TOLERANCE_MS
    cues: list[CueEntry] = []

    for slot, cue in chosen:
        position = to_serato_ms(cue.timeMs, offset)
        color = cue.color.lower() if cue.color else None
        name = cue.cueName or None
        old = existing.get(slot)

        if (
            old is not None
            and abs(old.position_ms - position) <= tolerance
            and color in (None, old.color)
            and name in (None, old.name)
        ):
            cues.append(old)
            continue

        cues.append(
            CueEntry(
                index=slot,
                position_ms=position,
                color=color or (old.color if old else default_color),
                name=name or (old.name if old else ""),
            )
        )

    return cues, overflow


def _times(cues) -> tuple[tuple[int, int], ...]:
    return tuple(sorted((c.index + 1, c.position_ms) for c in cues))


def _plan_track(track: Track, include_memory_cues: bool, default_color: str) -> SyncItem:
    def skip(reason: str, **extra) -> SyncItem:
        return SyncItem(track.trackId, track.trackTitle, track.artist, path, "skip", reason, **extra)

    path = Path(track.filePath) if track.filePath else None

    if path is None:
        return skip("the track has no file path")

    if path.suffix.lower() not in (".mp3", ".wav"):
        return skip(f"{path.suffix.lower() or 'this'} files aren't supported yet")

    if not path.is_file():
        return skip("the file wasn't found at the Rekordbox path")

    try:
        location = locate_markers2(path)
        legacy_location = locate_legacy_markers(path) if location is not None else None
    except UnsupportedFileError as error:
        return skip(str(error).split(": ", 1)[-1])

    if location is None:
        return skip("no Serato cue tag yet (load the track in Serato once)")

    try:
        entries = decode_markers2(location.data)
        offset = timing_offset(path)
        existing = {e.index: e for e in entries if isinstance(e, CueEntry)}
        cues, without_slot = serato_cues_for(track, offset, existing, include_memory_cues, default_color)

        if not cues:
            return skip("Rekordbox has no hot or memory cues to copy")

        before = _times(e for e in entries if isinstance(e, CueEntry))
        new_data = encode_markers2(replace_cues(entries, cues), size=location.length)
        legacy_new_data = encode_legacy(legacy_location.data, cues) if legacy_location else None
    except MarkersError as error:
        return skip(str(error))

    item = SyncItem(
        track.trackId, track.trackTitle, track.artist, path, "unchanged",
        before=before, after=_times(cues), timing=offset, location=location,
        new_data=new_data, legacy_location=legacy_location, legacy_new_data=legacy_new_data,
        memory_cues_without_slot=without_slot,
    )

    return replace(item, status="change") if item.regions else item


def build_sync_plan(
    tracks: list[Track],
    track_ids: list[str] | None = None,
    include_memory_cues: bool = True,
    default_color: str = DEFAULT_COLOR,
    max_files: int = DEFAULT_MAX_FILES,
) -> SyncPlan:
    """Work out, without changing anything, what the sync would do to each file."""
    wanted = set(track_ids) if track_ids else None
    candidates = [t for t in tracks if t.cues and (wanted is None or t.trackId in wanted)]
    loops = sum(1 for t in candidates for c in t.cues if c.cueType == "loop")

    items = [_plan_track(t, include_memory_cues, default_color) for t in candidates[:max_files]]

    return SyncPlan(tuple(items), loops_ignored=loops, not_planned=max(0, len(candidates) - max_files))


# ---------------------------------------------------------------------------
# Applying and undoing
# ---------------------------------------------------------------------------


def is_serato_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-x", "Serato DJ Pro"], capture_output=True).returncode == 0
    except OSError:
        return False


def journal_dir() -> Path:
    home = os.environ.get("CUEBRIDGE_HOME")
    return (Path(home) if home else Path.home() / ".cuebridge") / "undo"


def _require_serato_closed() -> None:
    if is_serato_running():
        raise SyncError(
            "Serato DJ Pro is running. It keeps cues in memory and can overwrite a change when a "
            "track is ejected or the app quits, so quit Serato first."
        )


def _hash_outside(path: Path, spans: list[tuple[int, int]]) -> str:
    """SHA-256 of the whole file except the (offset, length) spans that may change."""
    digest = hashlib.sha256()
    position = 0

    with open(path, "rb") as handle:
        for offset, length in sorted(spans):
            handle.seek(position)
            remaining = offset - position

            while remaining > 0:
                chunk = handle.read(min(HASH_CHUNK, remaining))

                if not chunk:
                    break

                digest.update(chunk)
                remaining -= len(chunk)

            position = offset + length

        handle.seek(position)

        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)

    return digest.hexdigest()


def _read_region(path: Path, offset: int, length: int) -> bytes:
    with open(path, "rb") as handle:
        handle.seek(offset)
        return handle.read(length)


def _write_region(path: Path, offset: int, data: bytes, mtime_ns: int, atime_ns: int) -> None:
    with open(path, "r+b") as handle:
        handle.seek(offset)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())

    # Serato keeps a file's modified time when it writes tags, so do the same.
    os.utime(path, ns=(atime_ns, mtime_ns))


def _save_journal(path: Path, journal: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")

    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(journal, handle, indent=1)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(temporary, path)


@dataclass
class SyncResult:
    journal: Path | None
    verified: list[SyncItem] = field(default_factory=list)
    failed: list[tuple[SyncItem, str]] = field(default_factory=list)


def apply_sync_plan(plan: SyncPlan, directory: Path | None = None) -> SyncResult:
    """Write the planned payloads, with a journal, read-back verification and revert on failure."""
    _require_serato_closed()

    items = plan.to_change

    if not items:
        return SyncResult(journal=None)

    directory = directory or journal_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # A fixed-width counter keeps the names in chronological order when sorted as text.
    stamp, counter = f"{datetime.now():%Y%m%d-%H%M%S}", 0
    journal_path = directory / f"serato-sync-{stamp}-{counter:02d}.json"

    while journal_path.exists():
        counter += 1
        journal_path = directory / f"serato-sync-{stamp}-{counter:02d}.json"

    entries = []

    for item in items:
        stat = item.path.stat()
        entries.append({
            "path": str(item.path),
            "regions": [
                {"description": r.description, "offset": r.offset, "length": r.length,
                 "old": r.old.hex(), "new": r.new.hex()}
                for r in item.regions
            ],
            "mtime_ns": stat.st_mtime_ns, "atime_ns": stat.st_atime_ns, "size": stat.st_size,
            "state": "pending",
        })

    journal = {"version": JOURNAL_VERSION, "createdAt": datetime.now().isoformat(timespec="seconds"), "entries": entries}
    _save_journal(journal_path, journal)  # written and synced before any audio file is touched

    result = SyncResult(journal=journal_path)

    for item, entry in zip(items, entries):
        path, regions = item.path, item.regions
        spans = [(r.offset, r.length) for r in regions]

        def restore() -> None:
            for r in regions:
                _write_region(path, r.offset, r.old, entry["mtime_ns"], entry["atime_ns"])

        try:
            if any(_read_region(path, r.offset, r.length) != r.old for r in regions) or path.stat().st_size != entry["size"]:
                raise SyncError("the file changed since it was planned")

            outside_before = _hash_outside(path, spans)
            entry["state"] = "writing"
            _save_journal(journal_path, journal)  # so a crash mid-write is known to need a restore

            for r in regions:
                _write_region(path, r.offset, r.new, entry["mtime_ns"], entry["atime_ns"])

            entry["state"] = "written"
            _save_journal(journal_path, journal)

            problems = []

            if any(_read_region(path, r.offset, r.length) != r.new for r in regions):
                problems.append("the new bytes didn't read back")

            if _hash_outside(path, spans) != outside_before or path.stat().st_size != entry["size"]:
                problems.append("something outside the cue tags changed")

            for r in regions:
                relocated = locate_frame(path, r.description)

                if relocated is None or relocated.offset != r.offset or relocated.data != r.new:
                    problems.append(f"the {r.description} tag no longer parses to what was written")

            if problems:
                restore()
                entry["state"] = "reverted"
                _save_journal(journal_path, journal)
                result.failed.append((item, "verification failed and the file was restored: " + "; ".join(problems)))
                continue

            entry["state"] = "verified"
            result.verified.append(item)
        except (SyncError, OSError, UnsupportedFileError) as error:
            reason = str(error) if isinstance(error, (SyncError, UnsupportedFileError)) else (error.strerror or str(error))

            # If the write may have started, put the original bytes back.
            if entry["state"] in ("writing", "written"):
                try:
                    restore()
                    entry["state"] = "reverted"
                except OSError:
                    pass

            result.failed.append((item, reason))

        _save_journal(journal_path, journal)

    return result


def latest_journal(directory: Path | None = None) -> Path | None:
    files = sorted((directory or journal_dir()).glob("serato-sync-*.json"))
    return files[-1] if files else None


@dataclass
class UndoResult:
    restored: list[str] = field(default_factory=list)
    already_original: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def undo_sync(journal_path: Path, dry_run: bool = False) -> UndoResult:
    """Put back the original cue bytes recorded in a journal."""
    if not dry_run:
        _require_serato_closed()

    try:
        journal = json.loads(Path(journal_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SyncError(f"Could not read the journal {journal_path}.") from None

    result = UndoResult()

    for entry in reversed(journal["entries"]):
        path = Path(entry["path"])

        # Version 1 journals recorded one region per file.
        regions = entry.get("regions") or [
            {"offset": entry["offset"], "length": entry["length"], "old": entry["old"], "new": entry["new"]}
        ]
        regions = [
            (r["offset"], r["length"], bytes.fromhex(r["old"]), bytes.fromhex(r["new"])) for r in regions
        ]

        if not path.is_file():
            result.skipped.append((str(path), "the file no longer exists"))
            continue

        current = [_read_region(path, offset, length) for offset, length, _, _ in regions]

        if all(now == old for now, (_, _, old, _) in zip(current, regions)):
            result.already_original.append(str(path))
            continue

        # A crash mid-write can leave anything; once verified, only our own bytes are safe to replace.
        ours = all(now in (old, new) for now, (_, _, old, new) in zip(current, regions))

        if not ours and entry["state"] == "verified":
            result.skipped.append((str(path), "the tag was changed again since (for example by Serato), so it was left alone"))
            continue

        if not dry_run:
            for offset, _, old, _ in regions:
                _write_region(path, offset, old, entry["mtime_ns"], entry["atime_ns"])

            if any(_read_region(path, offset, length) != old for offset, length, old, _ in regions):
                result.skipped.append((str(path), "the original bytes didn't read back after restoring"))
                continue

            entry["state"] = "undone"

        result.restored.append(str(path))

    if not dry_run:
        _save_journal(Path(journal_path), journal)

    return result


def describe_times(times: tuple[tuple[int, int], ...]) -> str:
    return ", ".join(f"{slot}: {format_time(ms)}" for slot, ms in times) or "no cues"
