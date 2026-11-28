"""Add hot cues to tracks in Rekordbox's database (the replacement for the XML cue editors).

Three plans, one apply and one undo:
  * `plan_default_cues`: a hot cue A at 0:00 for every playlisted track with no cue at all.
  * `plan_memory_to_hot_cues`: a hot cue duplicate for each memory cue that has none, in the
    lowest free slot (same rules as `cue_rules.plan_memory_to_hot`).
  * `plan_serato_cues`: Serato's hot cues, in the same slots, for tracks with no Rekordbox cue or loop.

Only playlisted tracks are touched by default: the collection holds thousands of sample-pack
files nobody plays from a playlist (and Pioneer's one-shots would each get a cue too).
The default cue also skips a track unless its file is confirmed to hold no Serato cues: once a track has a
Rekordbox cue, the Serato sync replaces the file's cues with Rekordbox's.

A track that has a cue in a slot CueBridge can't read (kind 4, or 10 and up) is skipped and
reported, since its free slots can't be worked out.

Writing follows the other database tools: Rekordbox closed, a verified backup, a journal,
read-back verification with an immediate restore on any difference, and undo.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cuebridge.services.cues import cue_rules
from cuebridge.services.cues.cue_timing import refine_with_beat_grids, timing_offset, to_rekordbox_ms
from cuebridge.services.rekordbox import db_library
from cuebridge.services.rekordbox.db import (
    JOURNAL_VERSION,
    ApplyResult,
    Backend,
    DatabaseError,
    DbCue,
    DbSnapshot,
    UndoResult,
    _backup_database,
    _journal_path,
    _require_rekordbox_closed,
    _restore,
    _save_journal,
    cuebridge_home,
    read_snapshot,
)
from cuebridge.services.rekordbox.service import LibraryError
from cuebridge.services.serato import sync as serato_sync
from cuebridge.services.serato import tags as serato_tags

CUE_JOURNALS = "rekordbox-cues"
# Hot cue slot (0 is A) -> the kind Rekordbox stores it under; the reverse of db_library.HOT_CUE_SLOTS.
SLOT_KINDS = {slot: kind for kind, slot in db_library.HOT_CUE_SLOTS.items()}


@dataclass(frozen=True)
class CueAddition:
    content_id: str
    title: str | None
    artist: str | None
    cues: tuple[tuple[int, int], ...]  # (kind, position in ms), in the order they are added


@dataclass
class CuePlan:
    what: str  # "default cue" or "memory cues to hot cues": shown in summaries and kept in the journal
    additions: list[CueAddition] = field(default_factory=list)
    already_done: int = 0  # tracks (default) or memory cues (duplication) that need nothing
    no_free_slot: list[tuple[str, int]] = field(default_factory=list)  # (title, memory cues left over)
    skipped_unreadable: list[str] = field(default_factory=list)  # tracks with cues in slots nobody has checked
    not_playlisted: int = 0  # tracks left out because they are in no playlist
    has_serato_cues: list[str] = field(default_factory=list)  # titles left out to protect their Serato cues
    serato_unchecked: Counter = field(default_factory=Counter)  # reason -> tracks left out because it can't be ruled out

    # only for copying Serato's cues
    no_serato_cues: int = 0
    timing_unsure: list[str] = field(default_factory=list)  # titles left out: no correction, or only an estimate not asked for
    timing_could_be_estimated: int = 0  # how many of those include_estimated_timing would bring in
    timing_estimated: int = 0  # tracks in the plan whose timing is an estimate from the beat grids
    sync_would_differ: list[str] = field(default_factory=list)  # titles left out: a sync back would change the file
    unsupported_slot: list[str] = field(default_factory=list)  # titles with a Serato cue in a slot Rekordbox can't hold

    @property
    def cue_count(self) -> int:
        return sum(len(a.cues) for a in self.additions)


def _playlisted_ids(snapshot: DbSnapshot) -> set[str]:
    """Tracks that are in at least one real playlist (folders and smart playlists don't count)."""
    return {c for p in snapshot.playlists if p.is_plain for c in p.content_ids}


def _scope(snapshot: DbSnapshot, playlisted_only: bool, track_ids: list[str] | None = None) -> tuple[list, int]:
    """The tracks to consider, and how many were left out for being in no playlist.

    `track_ids` limits them to those tracks. An empty list means no tracks, never all of them.
    """
    contents = list(snapshot.contents)

    if track_ids is not None:
        wanted = {str(i) for i in track_ids}
        unknown = wanted - {c.id for c in contents}

        if unknown:
            raise DatabaseError("These track IDs aren't in Rekordbox's database: " + ", ".join(sorted(unknown)[:10]))

        contents = [c for c in contents if c.id in wanted]

    if not playlisted_only:
        return contents, 0

    playlisted = _playlisted_ids(snapshot)
    chosen = [c for c in contents if c.id in playlisted]

    return chosen, len(contents) - len(chosen)


def plan_default_cues(snapshot: DbSnapshot, playlisted_only: bool = True, track_ids: list[str] | None = None) -> CuePlan:
    """A hot cue A at 0:00 for each track that has no cue or loop at all, in Rekordbox or (checked) in Serato."""
    plan = CuePlan("default cue")
    contents, plan.not_playlisted = _scope(snapshot, playlisted_only, track_ids)
    with_cues = {c.content_id for c in snapshot.cues}

    for content in contents:
        if content.id in with_cues:
            plan.already_done += 1
            continue

        doubt = serato_tags.why_it_may_have_cues(content.path)

        if doubt == serato_tags.HAS_CUES:
            plan.has_serato_cues.append(content.title or content.id)
        elif doubt:
            plan.serato_unchecked[doubt] += 1
        else:
            plan.additions.append(CueAddition(content.id, content.title, content.artist, ((SLOT_KINDS[0], 0),)))

    return plan


def _sync_would_change_nothing(content, cues: tuple[tuple[int, int], ...], first_beat_ms: int | None) -> bool:
    """Once the track has these cues in Rekordbox, would the Serato sync leave its file exactly as it is?

    Asks the sync's own planner, reading the cues the way it will read them from the database.
    """
    added = DbSnapshot((content,), (), tuple(DbCue(content.id, kind, ms) for kind, ms in cues))
    track = db_library.tracks_from_snapshot(added, first_beat=lambda _: first_beat_ms).tracks[0]
    items = serato_sync.build_sync_plan([track]).items

    return len(items) == 1 and items[0].status == "unchanged"


def plan_serato_cues(
    snapshot: DbSnapshot,
    playlisted_only: bool = True,
    track_ids: list[str] | None = None,
    include_estimated_timing: bool = False,
) -> CuePlan:
    """Serato's hot cues, in the same slots, for each track that has no Rekordbox cue or loop.

    It only ever fills an empty track, so there is nothing to merge. Left out: a track whose timing correction isn't
    measured (an estimate from the beat grids is used only if asked for, and no correction at all never is), and one
    whose file a sync back to Serato would then change. Reads the audio files; never writes them. Colours aren't
    copied: Rekordbox's default doesn't override Serato's in a sync, so Serato keeps its own.
    """
    plan = CuePlan("Serato cues")
    contents, plan.not_playlisted = _scope(snapshot, playlisted_only, track_ids)
    with_cues = {c.content_id for c in snapshot.cues}

    for content in contents:
        title = content.title or content.id

        if content.id in with_cues:
            plan.already_done += 1
            continue

        doubt = serato_tags.why_it_may_have_cues(content.path)

        if doubt is None:
            plan.no_serato_cues += 1
            continue

        if doubt != serato_tags.HAS_CUES:
            plan.serato_unchecked[doubt] += 1
            continue

        try:
            found = sorted(serato_tags.read_cues(content.path), key=lambda c: c.index)
            beat = db_library.read_first_beat_ms(content.analysis_path)
            offset = refine_with_beat_grids(timing_offset(content.path), content.path, beat)
        except (OSError, LibraryError):
            plan.serato_unchecked["its tags couldn't be read"] += 1
            continue

        if any(c.index not in SLOT_KINDS for c in found):
            plan.unsupported_slot.append(title)
            continue

        estimated = offset.confidence == "estimated"

        if offset.flagged and not (estimated and include_estimated_timing):
            plan.timing_unsure.append(title)
            plan.timing_could_be_estimated += estimated
            continue

        cues = tuple((SLOT_KINDS[c.index], to_rekordbox_ms(c.position_ms, offset)) for c in found)

        if not _sync_would_change_nothing(content, cues, beat):
            plan.sync_would_differ.append(title)
            continue

        plan.timing_estimated += estimated
        plan.additions.append(CueAddition(content.id, content.title, content.artist, cues))

    return plan


def plan_memory_to_hot_cues(snapshot: DbSnapshot, playlisted_only: bool = True) -> CuePlan:
    """A hot cue for each memory cue that has none, using `cue_rules.plan_memory_to_hot`."""
    plan = CuePlan("memory cues to hot cues")
    contents, plan.not_playlisted = _scope(snapshot, playlisted_only)
    wanted = {c.id for c in contents}
    library = db_library.tracks_from_snapshot(snapshot, first_beat=lambda path: None)
    unreadable = {content_id for content_id, kind in _unreadable_kinds(snapshot)}

    for track in library.tracks:
        if track.trackId not in wanted:
            continue

        if track.trackId in unreadable:
            plan.skipped_unreadable.append(track.trackTitle or track.trackId)
            continue

        result = cue_rules.plan_memory_to_hot(track.cues)
        plan.already_done += result.already_duplicated

        if result.overflow:
            plan.no_free_slot.append((track.trackTitle or track.trackId, len(result.overflow)))

        if result.assignments:
            plan.additions.append(CueAddition(
                track.trackId, track.trackTitle, track.artist,
                tuple((SLOT_KINDS[a.slot], a.cue.timeMs) for a in result.assignments),
            ))

    return plan


def _unreadable_kinds(snapshot: DbSnapshot) -> set[tuple[str, int]]:
    """(track, kind) for each cue in a kind CueBridge can't place in a slot."""
    return {
        (c.content_id, c.kind) for c in snapshot.cues
        if c.kind > 0 and c.kind not in db_library.HOT_CUE_SLOTS
    }


def _plan_key(plan: CuePlan) -> list[tuple]:
    return [(a.content_id, a.cues) for a in plan.additions]


def _cue_key(cues) -> list[tuple[str, int, int]]:
    return sorted((c.content_id, c.kind, c.position_ms) for c in cues)


def apply_cue_plan(
    plan: CuePlan,
    db_path: Path,
    backend: Backend,
    replan,
) -> ApplyResult:
    """Add the planned cues, verified, journaled, undoable.

    `replan` takes a fresh snapshot and returns the plan it would make now: if that differs
    from `plan`, the database changed since the dry run and nothing is written.
    """
    _require_rekordbox_closed()

    if not plan.additions:
        raise DatabaseError("There are no cues to add.")

    before = read_snapshot(db_path, backend)

    if _plan_key(replan(before)) != _plan_key(plan):
        raise DatabaseError("Rekordbox's database changed since this was planned. Nothing was written. Run a dry run again.")

    backup = _backup_database(db_path, backend, before)
    journal_path = _journal_path(cuebridge_home() / "undo", CUE_JOURNALS)
    journal = {
        "version": JOURNAL_VERSION, "kind": "cues", "what": plan.what, "createdAt": datetime.now().isoformat(timespec="seconds"),
        "database": str(db_path), "backup": str(backup), "state": "pending",
        "additions": [
            {"contentId": a.content_id, "title": a.title, "cues": [list(c) for c in a.cues], "token": None}
            for a in plan.additions
        ],
    }
    _save_journal(journal_path, journal)

    writer = backend.open_for_write(db_path)

    try:
        for entry in journal["additions"]:
            entry["token"] = writer.add_cues(entry["contentId"], [tuple(c) for c in entry["cues"]])

        journal["state"] = "writing"
        _save_journal(journal_path, journal)
        writer.commit()
    except Exception as error:
        try:
            writer.rollback()
        except Exception:
            pass
        writer.close()
        _restore(backup, db_path)
        journal["state"] = "failed"
        _save_journal(journal_path, journal)
        raise DatabaseError(f"Writing to the database failed and the backup was put back: {error}") from None

    writer.close()
    journal["state"] = "written"
    _save_journal(journal_path, journal)

    expected = list(before.cues) + [DbCue(a.content_id, kind, ms) for a in plan.additions for kind, ms in a.cues]

    try:
        after = read_snapshot(db_path, backend)
        problems = []

        if after.contents != before.contents:
            problems.append("the track list changed")

        if _cue_key(after.cues) != _cue_key(expected):
            problems.append("the cues aren't exactly as planned")

        if after.playlists != before.playlists:
            problems.append("the playlists changed")
    except Exception as error:
        problems = [f"the database couldn't be read back ({error})"]

    if problems:
        _restore(backup, db_path)
        journal["state"] = "reverted"
        _save_journal(journal_path, journal)
        raise DatabaseError("Verification failed (" + "; ".join(problems) + ") and the backup was put back.")

    journal["state"] = "verified"
    _save_journal(journal_path, journal)

    return ApplyResult(journal_path, backup, plan.cue_count)


def undo_cue_additions(journal_path: Path, backend: Backend, dry_run: bool = False) -> UndoResult:
    """Take the added cues off again. A track whose added cues are gone already is skipped."""
    try:
        journal = json.loads(Path(journal_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DatabaseError(f"Could not read the journal {journal_path}.") from None

    if journal.get("kind") != "cues":
        raise DatabaseError(f"{journal_path} is not a cue journal.")

    if not dry_run:
        _require_rekordbox_closed()

    db_path = Path(journal["database"])
    snapshot = read_snapshot(db_path, backend)
    present = {c.id for c in snapshot.contents}
    cue_ids = {c.id for c in snapshot.cues}
    result = UndoResult()
    remove = []

    for entry in journal["additions"]:
        label = f"{len(entry['cues'])} cue(s) on {entry['title'] or entry['contentId']}"

        if entry["contentId"] not in present or not entry.get("token"):
            result.skipped.append((label, "the track is gone or nothing was written"))
        elif not set(entry["token"]["cueIds"]) & cue_ids:
            result.skipped.append((label, "they are no longer there"))
        else:
            remove.append(entry)
            result.removed.append(label)

    if dry_run or not remove:
        return result

    writer = backend.open_for_write(db_path)

    try:
        # Newest first, so a track's contentCue goes back through each state it had.
        for entry in reversed(remove):
            writer.remove_copied_cues(entry["contentId"], entry["token"])

        writer.commit()
    except Exception as error:
        writer.rollback()
        writer.close()
        raise DatabaseError(f"Undoing failed: {error}") from None

    writer.close()
    journal["state"] = "undone"
    _save_journal(Path(journal_path), journal)

    return result
