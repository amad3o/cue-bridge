"""Edit a Rekordbox XML export.

The edits are made to the file's text: the new cue lines are slotted in and every
other byte stays exactly as Rekordbox wrote it. The file is not parsed and
written back, because Rekordbox rejects an XML export that went through an XML
library (different declaration, line endings, escaping and spacing).

By default an edit is saved into the file itself. Because that changes a file
the user cares about, every in-place edit saves a timestamped backup next to
it first, and every write is atomic: the new XML is built in memory, checked by
parsing it again, written to a temporary file and swapped in, and only if the
file hasn't changed since the edit was planned. A copy saved elsewhere is only
ever created, never overwriting a file that exists.
"""

import filecmp
import os
import shutil
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cuebridge.parsers.rekordbox_parser import TYPE_CUE
from cuebridge.services.cue_rules import plan_memory_to_hot
from cuebridge.services.rekordbox_service import (
    LibraryError,
    parse_rekordbox_bytes,
    pending_folder,
    read_library_bytes,
    write_pending_note,
)

_COLLECTION = re.compile(r"<COLLECTION\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*?(?<!/)>.*?</COLLECTION>", re.S)
_TRACK_OPEN = re.compile(r"""<TRACK\b(?:[^>"']|"[^"]*"|'[^']*')*>""")
CLOSE_TAG = "</TRACK>"
INDENT_STEP = "  "


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


def needs_backup(plan: Plan) -> bool:
    """True if saving this plan first backs up the file (every in-place edit does)."""
    return plan.target.in_place


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
        raise OutputPathError(f"{target} already exists, so it won't be overwritten. Choose a different output_path or move it.")

    return EditTarget(source_path, resolved, stat.st_mtime_ns, stat.st_size)


def _attr(value: str) -> str:
    """Escape an attribute value the way Rekordbox writes it."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")
    )


@dataclass(frozen=True)
class NewMark:
    """A cue to add to a track: a hot cue in `slot` (0 is A), or a memory cue with slot -1."""

    position_ms: int
    slot: int = 0
    name: str = ""

    def line(self) -> str:
        return (
            f'<POSITION_MARK Name="{_attr(self.name)}" Type="{TYPE_CUE}" '
            f'Start="{self.position_ms / 1000:.3f}" Num="{self.slot}"/>'
        )


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _line_indent(text: str, index: int) -> str:
    """The whitespace at the start of the line holding `index`."""
    start = text.rfind("\n", 0, index) + 1
    match = re.match(r"[ \t]*", text[start:])
    return match.group(0)


def patch_marks(original: bytes, marks: dict[int, list[NewMark]]) -> bytes:
    """Add cues to collection tracks (by position in the collection) as text, leaving every other byte alone."""
    text = original.decode("utf-8")
    newline = _newline(text)
    collection = _COLLECTION.search(text)

    if collection is None:
        if marks:
            raise AssertionError("The XML has no collection to add cues to.")
        return original

    opens = [m for m in _TRACK_OPEN.finditer(text, collection.start(), collection.end())]

    for position in sorted(marks, reverse=True):
        opening = opens[position]
        indent = _line_indent(text, opening.start())
        inner = indent + INDENT_STEP
        lines = [f"{inner}{mark.line()}" for mark in marks[position]]

        if opening.group(0).endswith("/>"):
            # <TRACK .../> gains children, so it needs a closing tag.
            head = opening.group(0)[:-2].rstrip()
            replacement = head + ">" + newline + newline.join(lines) + newline + indent + CLOSE_TAG
            text = text[: opening.start()] + replacement + text[opening.end() :]
            continue

        close = text.find(CLOSE_TAG, opening.end())

        if close == -1 or close > collection.end():
            raise AssertionError("A track in the XML has no closing tag.")

        line_start = text.rfind("\n", 0, close) + 1

        if line_start > opening.end() and text[line_start:close].strip() == "":
            # The closing tag has its own line: put the cues on lines before it.
            text = text[:line_start] + "".join(line + newline for line in lines) + text[line_start:]
        else:
            # Everything is on one line: break it up around the new cues.
            text = text[:close] + newline + newline.join(lines) + newline + indent + text[close:]

    return text.encode("utf-8")


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

    marks: dict[int, list[NewMark]] = {}
    changed: list[str] = []
    skipped = 0

    for position, track in enumerate(before):
        if track.cues:
            skipped += 1
            continue

        marks[position] = [NewMark(position_ms)]
        changed.append(track.trackId)

    edited = patch_marks(original, marks)

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

    marks: dict[int, list[NewMark]] = {}
    changed: list[str] = []
    added: dict[str, set[tuple[int, int]]] = {}
    overflow: list[tuple[str, int]] = []
    already = 0
    cues_added = 0

    for position, track in enumerate(before):
        result = plan_memory_to_hot(track.cues)
        already += result.already_duplicated

        if result.overflow:
            overflow.append((track.trackId, len(result.overflow)))

        if not result.assignments:
            continue

        marks[position] = [NewMark(a.cue.timeMs, a.slot, a.cue.cueName or "") for a in result.assignments]
        changed.append(track.trackId)
        added[track.trackId] = {(a.slot, a.cue.timeMs) for a in result.assignments}
        cues_added += len(result.assignments)

    edited = patch_marks(original, marks)

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
    first, then swaps the new XML in
    atomically. Elsewhere: creates a new file and never replaces one.
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

            current_bytes = t.source.read_bytes()

            if plan.xml == current_bytes:
                return WriteResult(t.source, None)  # nothing would change, so no backup either

            pending = pending_folder(t.source, current_bytes)
            backup = _make_backup(t.source)
            _replace_atomically(t.source, plan.xml, mode_from=t.source)

            if pending:
                # The note about playlists waiting to be imported is tied to the file's contents.
                write_pending_note(t.source, pending, plan.xml)

            return WriteResult(t.source, backup)

        # "x" fails if the file appeared since planning, so nothing is clobbered.
        with open(t.target, "xb") as handle:
            handle.write(plan.xml)
        return WriteResult(t.target, None)

    except FileExistsError:
        raise OutputPathError(f"{t.target} already exists, so it won't be overwritten.") from None
    except OSError as error:
        raise OutputPathError(f"Could not write {t.target}: {error.strerror}") from None
