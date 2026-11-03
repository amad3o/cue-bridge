"""Create an edited copy of a Rekordbox XML export.

Nothing here modifies the source file. The edited XML is built in memory,
checked by parsing it again, and only then written to a new file that must not
already exist.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from cuebridge.parsers.rekordbox_parser import TYPE_CUE, TYPE_LOOP
from cuebridge.services.rekordbox_service import (
    LibraryError,
    parse_rekordbox_bytes,
    read_library_bytes,
)

TRACK_INDENT = "\n      "
MARK_TAIL_FALLBACK = "\n    "


class OutputPathError(LibraryError):
    """The place the edited copy should be written is not usable."""


@dataclass(frozen=True)
class DefaultCuePlan:
    """What adding default cues would do, with the edited XML ready to write."""

    source: Path
    position_ms: int
    changed_track_ids: tuple[str, ...]
    skipped_with_cues: int
    xml: bytes


def default_output_path(source: Path) -> Path:
    return source.with_name(f"{source.stem}.with-default-cues.xml")


def _has_cue(track: ET.Element) -> bool:
    """True if the track already has a cue or loop (fade and load markers don't count)."""
    return any(m.attrib.get("Type") in (TYPE_CUE, TYPE_LOOP) for m in track.findall("POSITION_MARK"))


def _append_mark(track: ET.Element, position_ms: int) -> None:
    mark = ET.Element(
        "POSITION_MARK",
        {"Name": "", "Type": TYPE_CUE, "Start": f"{position_ms / 1000:.3f}", "Num": "0"},
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


def build_default_cue_plan(source: str | Path, position_ms: int = 0) -> DefaultCuePlan:
    """Plan a hot cue A at `position_ms` for every track that has no cue or loop."""
    source_path = Path(source).expanduser().resolve()
    original = read_library_bytes(source_path)

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

    edited = ET.tostring(root, encoding="UTF-8", xml_declaration=True)

    _verify(before, edited, set(changed), position_ms)

    return DefaultCuePlan(
        source=source_path,
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


def write_plan(plan: DefaultCuePlan, output_path: str | Path | None = None) -> Path:
    """Write the planned XML to a new file and return its path.

    Refuses to overwrite anything, including the source file.
    """
    output = Path(output_path).expanduser() if output_path else default_output_path(plan.source)

    if output.suffix.lower() != ".xml":
        raise OutputPathError("The output file must end in .xml.")

    if not output.parent.is_dir():
        raise OutputPathError(f"The folder {output.parent} does not exist.")

    if output.resolve() == plan.source:
        raise OutputPathError("The output file can't be the original library.")

    try:
        # "x" fails if the file exists, so an existing file is never overwritten.
        with open(output, "xb") as handle:
            handle.write(plan.xml)
    except FileExistsError:
        raise OutputPathError(
            f"{output} already exists. Choose a different output_path or delete it."
        ) from None
    except OSError as error:
        raise OutputPathError(f"Could not write {output}: {error.strerror}") from None

    return output
