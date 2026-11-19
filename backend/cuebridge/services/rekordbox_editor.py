"""Write an edited Rekordbox XML export safely.

Used by the playlist export (`playlist_export.py`), which builds the new XML by patching
the file's text. The file is never parsed and written back, because Rekordbox rejects an
XML export that went through an XML library (different declaration, line endings,
escaping and spacing). The cue editors that used to live here now work on the database
(`rekordbox_cue_writes.py`).

By default an edit is saved into the file itself. Because that changes a file
the user cares about, every in-place edit saves a timestamped backup next to
it first, and every write is atomic: the new XML is written to a temporary file and
swapped in, and only if the file hasn't changed since the edit was planned. A copy saved
elsewhere is only ever created, never overwriting a file that exists.
"""

import filecmp
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from cuebridge.services.rekordbox_service import (
    LibraryError,
    pending_folder,
    read_library_bytes,
    write_pending_note,
)


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
class EditPlan:
    """An edited XML, ready to write: where it goes and its bytes."""

    target: EditTarget
    xml: bytes



class Plan(Protocol):
    """Anything with a target and the XML to write there (an `EditPlan`, or the playlist export's plan)."""

    target: EditTarget
    xml: bytes


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
