"""Edit a Rekordbox XML export.

By default an edit is saved into the file itself. Because that changes a file
the user cares about, the first edit to a file that came straight from
Rekordbox saves a timestamped backup next to it, and every write is atomic:
the new XML is built in memory, checked by parsing it again, written to a
temporary file and swapped in, and only if the file hasn't changed since the
edit was planned.

Files CueBridge has edited carry a marker comment. That marker tells a fresh
Rekordbox export (needs a backup) from a file that was already edited (doesn't),
and it is the only kind of *other* file CueBridge will ever replace when asked
to save a copy elsewhere.
"""

import filecmp
import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cuebridge.parsers.rekordbox_parser import TYPE_CUE, TYPE_LOOP
from cuebridge.services.cue_rules import plan_memory_to_hot
from cuebridge.services.rekordbox_service import (
    LibraryError,
    parse_rekordbox_bytes,
    read_library_bytes,
)

TRACK_INDENT = "\n      "
MARK_TAIL_FALLBACK = "\n    "

EDIT_MARKER = b"<!-- Edited by CueBridge. -->"
MARKER_SEARCH_BYTES = 512


class OutputPathError(LibraryError):
    """The file to save to is not usable, or changed while it was being edited."""


@dataclass(frozen=True)
class EditTarget:
    """The file an edit starts from and the file its result is saved to."""

    source: Path  # the loaded Rekordbox XML; every edit starts from it
    target: Path  # where the result is saved; the source itself unless saving elsewhere
    source_mtime_ns: int  # the source as it was when read, to detect changes before writing
    source_size: int

    @property
    def in_place(self) -> bool:
        return self.target == self.source


@dataclass(frozen=True)
class WriteResult:
    path: Path
    # Set when a backup of the previous file was saved first.
    backup: Path | None


@dataclass(frozen=True)
class DefaultCuePlan:
    """What adding default cues would do, with the edited XML ready to write."""

    target: EditTarget
    position_ms: int
    changed_track_ids: tuple[str, ...]
    skipped_with_cues: int
    xml: bytes


@dataclass(frozen=True)
class HotCueDuplicationPlan:
    """What copying memory cues to hot cues would do, with the edited XML ready."""

    target: EditTarget
    changed_track_ids: tuple[str, ...]
    cues_added: int
    # Memory cues skipped because a hot cue already sits at the same time.
    already_duplicated: int
    # (track ID, number of memory cues that found no free slot)
    overflow: tuple[tuple[str, int], ...]
    xml: bytes


Plan = DefaultCuePlan | HotCueDuplicationPlan


def _is_cuebridge_file(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return EDIT_MARKER in handle.read(MARKER_SEARCH_BYTES)
    except OSError:
        return False


def needs_backup(plan: Plan) -> bool:
    """True if saving this plan first backs up the file (a fresh Rekordbox export)."""
    return plan.target.in_place and not _is_cuebridge_file(plan.target.source)


def _read_source(source: str | Path) -> tuple[Path, bytes, os.stat_result]:
    """Read the source, returning the bytes with the file's state when it was read."""
    source_path = Path(source).expanduser().resolve()

    try:
        first = source_path.stat()
    except OSError:
        first = None

    data = read_library_bytes(source_path)  # raises a clear error if unusable
    second = source_path.stat()

    if first is None or (first.st_mtime_ns, first.st_size) != (second.st_mtime_ns, second.st_size):
        raise OutputPathError(f"{source_path} changed while it was being read. Try again.")

    return source_path, data, second


def resolve_target(source_path: Path, stat: os.stat_result, output_path: str | Path | None) -> EditTarget:
    """Decide where an edit is saved, and check the destination is usable."""
    target = Path(output_path).expanduser() if output_path else source_path

    if target.suffix.lower() != ".xml":
        raise OutputPathError("The output file must end in .xml.")

    if not target.parent.is_dir():
        raise OutputPathError(f"The folder {target.parent} does not exist.")

    resolved = target.resolve()

    if resolved != source_path and resolved.exists():
        if not resolved.is_file() or not _is_cuebridge_file(resolved):
            raise OutputPathError(
                f"{target} already exists and wasn't written by CueBridge, so it won't be "
                "overwritten. Choose a different output_path or move it."
            )

    return EditTarget(source_path, resolved, stat.st_mtime_ns, stat.st_size)


def _has_cue(track: ET.Element) -> bool:
    """True if the track already has a cue or loop (fade and load markers don't count)."""
    return any(m.attrib.get("Type") in (TYPE_CUE, TYPE_LOOP) for m in track.findall("POSITION_MARK"))


def _append_mark(track: ET.Element, position_ms: int, slot: int = 0, name: str = "") -> None:
    mark = ET.Element(
        "POSITION_MARK",
        {"Name": name, "Type": TYPE_CUE, "Start": f"{position_ms / 1000:.3f}", "Num": str(slot)},
    )

    children = list(track)

    if children:
        # Keep the closing tag's indentation after the new element.
        mark.tail = children[-1].tail or MARK_TAIL_FALLBACK
        children[-1].tail = TRACK_INDENT
    else:
        # A self-closing <TRACK/> gains a child, so it needs its own whitespace.
        track.text = TRACK_INDENT
        mark.tail = MARK_TAIL_FALLBACK

    track.append(mark)


def _serialize(root: ET.Element) -> bytes:
    """Serialize the XML with the working-copy marker right after the declaration."""
    xml = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    declaration, _, rest = xml.partition(b"\n")

    return declaration + b"\n" + EDIT_MARKER + b"\n" + rest


def build_default_cue_plan(
    source: str | Path,
    position_ms: int = 0,
    output_path: str | Path | None = None,
) -> DefaultCuePlan:
    """Plan a hot cue A at `position_ms` for every track that has no cue or loop."""
    source_path, original, stat = _read_source(source)
    target = resolve_target(source_path, stat, output_path)

    # Validates the file with the same errors as every other entry point.
    before = parse_rekordbox_bytes(original)

    root = ET.fromstring(original)
    collection = root.find("COLLECTION")

    changed: list[str] = []
    skipped = 0

    for position, track in enumerate(collection.findall("TRACK")):
        if _has_cue(track):
            skipped += 1
            continue

        _append_mark(track, position_ms)
        changed.append(track.attrib.get("TrackID") or f"idx{position}")

    edited = _serialize(root)

    _verify(before, edited, set(changed), position_ms)

    return DefaultCuePlan(
        target=target,
        position_ms=position_ms,
        changed_track_ids=tuple(changed),
        skipped_with_cues=skipped,
        xml=edited,
    )


def _verify(before, edited: bytes, changed_ids: set[str], position_ms: int) -> None:
    """Re-parse the edited XML and check it differs only where intended."""
    after = parse_rekordbox_bytes(edited)

    if [t.trackId for t in after] != [t.trackId for t in before]:
        raise AssertionError("Edited XML has a different list of tracks.")

    for old, new in zip(before, after):
        if old.trackId in changed_ids:
            added = [c for c in new.cues if c.cueType == "hotcue" and c.cueIndex == 0]

            if len(new.cues) != 1 or len(added) != 1 or added[0].timeMs != position_ms:
                raise AssertionError(f"Track {old.trackId} did not get exactly one default cue.")
        elif new.cues != old.cues:
            raise AssertionError(f"Track {old.trackId} was changed unexpectedly.")


def build_hot_cue_duplication_plan(
    source: str | Path,
    output_path: str | Path | None = None,
) -> HotCueDuplicationPlan:
    """Plan a hot cue duplicate for every memory cue, in the lowest free slots."""
    source_path, original, stat = _read_source(source)
    target = resolve_target(source_path, stat, output_path)

    # Validates the file with the same errors as every other entry point.
    before = parse_rekordbox_bytes(original)

    root = ET.fromstring(original)
    elements = root.find("COLLECTION").findall("TRACK")

    if len(elements) != len(before):
        raise AssertionError("Parsed tracks don't line up with the XML tracks.")

    changed: list[str] = []
    added: dict[str, set[tuple[int, int]]] = {}
    overflow: list[tuple[str, int]] = []
    already = 0
    cues_added = 0

    for element, track in zip(elements, before):
        result = plan_memory_to_hot(track.cues)
        already += result.already_duplicated

        if result.overflow:
            overflow.append((track.trackId, len(result.overflow)))

        if not result.assignments:
            continue

        for item in result.assignments:
            _append_mark(element, item.cue.timeMs, slot=item.slot, name=item.cue.cueName or "")

        changed.append(track.trackId)
        added[track.trackId] = {(a.slot, a.cue.timeMs) for a in result.assignments}
        cues_added += len(result.assignments)

    edited = _serialize(root)

    _verify_duplication(before, edited, added)

    return HotCueDuplicationPlan(
        target=target,
        changed_track_ids=tuple(changed),
        cues_added=cues_added,
        already_duplicated=already,
        overflow=tuple(overflow),
        xml=edited,
    )


def _verify_duplication(before, edited: bytes, added: dict[str, set[tuple[int, int]]]) -> None:
    """Check each track gained exactly its planned hot cues and nothing else."""
    after = parse_rekordbox_bytes(edited)

    if [t.trackId for t in after] != [t.trackId for t in before]:
        raise AssertionError("Edited XML has a different list of tracks.")

    for old, new in zip(before, after):
        if old.trackId not in added:
            if new.cues != old.cues:
                raise AssertionError(f"Track {old.trackId} was changed unexpectedly.")
            continue

        if new.cues[: len(old.cues)] != old.cues:
            raise AssertionError(f"Track {old.trackId} lost or changed an existing cue.")

        new_hot = {
            (c.cueIndex, c.timeMs)
            for c in new.cues[len(old.cues) :]
            if c.cueType == "hotcue"
        }

        if new_hot != added[old.trackId] or len(new.cues) != len(old.cues) + len(new_hot):
            raise AssertionError(f"Track {old.trackId} did not get exactly the planned hot cues.")


def _make_backup(path: Path) -> Path:
    """Copy `path` to a timestamped sibling, verify it, and return the copy's path."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.backup-{stamp}")
    counter = 1

    while backup.exists():
        backup = path.with_name(f"{path.name}.backup-{stamp}-{counter}")
        counter += 1

    shutil.copy2(path, backup)

    if not filecmp.cmp(path, backup, shallow=False):
        backup.unlink(missing_ok=True)
        raise OutputPathError(f"The backup of {path.name} didn't match the original, so nothing was changed.")

    return backup


def _replace_atomically(target: Path, data: bytes, mode_from: Path | None = None) -> None:
    temporary = target.with_name(f".{target.name}.cuebridge-tmp")

    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        if mode_from is not None:
            shutil.copymode(mode_from, temporary)

        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def write_plan(plan: Plan) -> WriteResult:
    """Save the planned XML.

    In place: refuses if the file changed since it was planned, saves a backup
    first if the file is a fresh Rekordbox export, then swaps the new XML in
    atomically. Elsewhere: creates a new file, or replaces an existing file only
    if CueBridge wrote it.
    """
    t = plan.target

    try:
        if t.in_place:
            current = t.source.stat()

            if (current.st_mtime_ns, current.st_size) != (t.source_mtime_ns, t.source_size):
                raise OutputPathError(
                    f"{t.source.name} changed since this edit was planned (was it re-exported?). "
                    "Nothing was written. Run the tool again."
                )

            backup = _make_backup(t.source) if not _is_cuebridge_file(t.source) else None
            _replace_atomically(t.source, plan.xml, mode_from=t.source)
            return WriteResult(t.source, backup)

        if not t.target.exists():
            # "x" fails if the file appeared since planning, so nothing is clobbered.
            with open(t.target, "xb") as handle:
                handle.write(plan.xml)
            return WriteResult(t.target, None)

        if not t.target.is_file() or not _is_cuebridge_file(t.target):
            raise OutputPathError(
                f"{t.target} already exists and wasn't written by CueBridge, so it won't be overwritten."
            )

        _replace_atomically(t.target, plan.xml)
        return WriteResult(t.target, None)

    except FileExistsError:
        raise OutputPathError(f"{t.target} already exists and wasn't written by CueBridge.") from None
    except OSError as error:
        raise OutputPathError(f"Could not write {t.target}: {error.strerror}") from None
