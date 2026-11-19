"""Add hot cues to tracks in Rekordbox's database (the replacement for the XML cue editors).

Two plans, one apply and one undo:
  * `plan_default_cues`: a hot cue A at 0:00 for every playlisted track with no cue at all.
  * `plan_memory_to_hot_cues`: a hot cue duplicate for each memory cue that has none, in the
    lowest free slot (same rules as `cue_rules.plan_memory_to_hot`).

Only playlisted tracks are touched by default: the collection holds thousands of sample-pack
files nobody plays from a playlist (and Pioneer's one-shots would each get a cue too).
A track that has a cue in a slot CueBridge can't read (kind 4, or 10 and up) is skipped and
reported, since its free slots can't be worked out.

Writing follows the other database tools: Rekordbox closed, a verified backup, a journal,
read-back verification with an immediate restore on any difference, and undo.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cuebridge.services import cue_rules, db_library
from cuebridge.services.rekordbox_db import (
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

    @property
    def cue_count(self) -> int:
        return sum(len(a.cues) for a in self.additions)


def _playlisted_ids(snapshot: DbSnapshot) -> set[str]:
    """Tracks that are in at least one real playlist (folders and smart playlists don't count)."""
    return {c for p in snapshot.playlists if p.is_plain for c in p.content_ids}


def _scope(snapshot: DbSnapshot, playlisted_only: bool) -> tuple[list, int]:
    """The tracks to consider, and how many were left out for being in no playlist."""
    if not playlisted_only:
        return list(snapshot.contents), 0

    playlisted = _playlisted_ids(snapshot)
    chosen = [c for c in snapshot.contents if c.id in playlisted]

    return chosen, len(snapshot.contents) - len(chosen)


def plan_default_cues(snapshot: DbSnapshot, playlisted_only: bool = True) -> CuePlan:
    """A hot cue A at 0:00 for each track that has no cue or loop at all."""
    plan = CuePlan("default cue")
    contents, plan.not_playlisted = _scope(snapshot, playlisted_only)
    with_cues = {c.content_id for c in snapshot.cues}

    for content in contents:
        if content.id in with_cues:
            plan.already_done += 1
        else:
            plan.additions.append(CueAddition(content.id, content.title, content.artist, ((SLOT_KINDS[0], 0),)))

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
