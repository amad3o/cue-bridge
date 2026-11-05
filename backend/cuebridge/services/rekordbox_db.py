"""Change playlist membership and file locations in Rekordbox's own database.

Rekordbox can only create playlists from an XML import, never update one, so
adding a track to an existing playlist automatically means editing its
database (`master.db`, an encrypted SQLite file). That is the riskiest thing
CueBridge does, so it is deliberately narrow and layered like the Serato sync:

  * it only ever adds a track that is already in the collection to a playlist
    that already exists, at the end. It never touches tracks, files, cues,
    analysis or any other playlist;
  * Rekordbox must be closed, and the live database is never opened while it
    runs: planning reads a copy;
  * before writing, the database files are copied and the copy is checked;
  * the writing is done by pyrekordbox's own methods, which keep Rekordbox's
    row IDs, timestamps and sync counters right;
  * afterwards the database is read again and everything must be exactly as
    planned, otherwise the copy is put back;
  * a journal records the added rows so they can be removed again.

Fixing the location of files Apple Music has renamed or moved follows the same
rules and changes nothing but each track's path and file name.

pyrekordbox and SQLCipher are optional dependencies (see README); they are only
imported when the database is actually read or written.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from cuebridge.models import Track
from cuebridge.parsers.apple_music_xml import MusicLibrary
from cuebridge.parsers.rekordbox_parser import RekordboxPlaylist
from cuebridge.services import drift, library_reports
from cuebridge.services.rekordbox_service import LibraryError

DB_FILES = ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml")
SETTINGS_FILE = Path("~/Library/Application Support/Pioneer/rekordbox6/rekordbox3.settings")
JOURNAL_VERSION = 1


class DatabaseError(LibraryError):
    """Rekordbox's database can't be read or changed."""


# ---------------------------------------------------------------------------
# What is read from the database
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DbContent:
    id: str
    title: str | None
    artist: str | None
    path: str | None
    duration_ms: int | None
    file_name: str | None = None
    org_path: str | None = None  # where the file was when it was first imported


@dataclass(frozen=True)
class DbPlaylist:
    id: str
    name: str
    parent_id: str | None
    is_folder: bool
    is_smart: bool
    folders: tuple[str, ...]  # names of the folders it sits in, outermost first
    song_ids: tuple[str, ...]  # the rows that link it to tracks, in playlist order
    content_ids: tuple[str, ...]  # the tracks, in playlist order

    @property
    def is_plain(self) -> bool:
        return not self.is_folder and not self.is_smart


@dataclass(frozen=True)
class DbSnapshot:
    contents: tuple[DbContent, ...]
    playlists: tuple[DbPlaylist, ...]

    def playlist_contents(self) -> dict[str, tuple[str, ...]]:
        return {p.id: p.content_ids for p in self.playlists}


class Writer(Protocol):
    def add(self, playlist_id: str, content_id: str) -> str: ...
    def remove(self, playlist_id: str, song_id: str) -> None: ...
    def relocate(self, content_id: str, path: str, file_name: str | None, org_path: str | None) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


class Backend(Protocol):
    def read(self, db_path: Path) -> DbSnapshot: ...
    def open_for_write(self, db_path: Path) -> Writer: ...


# ---------------------------------------------------------------------------
# The real backend (pyrekordbox)
# ---------------------------------------------------------------------------


def _load_pyrekordbox():
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from pyrekordbox.masterdb import MasterDatabase
    except Exception as error:  # ImportError, or a SQLCipher problem
        raise DatabaseError(
            "Reading Rekordbox's database needs pyrekordbox and SQLCipher, which aren't set up "
            f"({error}). See the README for the two commands to install them."
        ) from None

    return MasterDatabase


class PyrekordboxWriter:
    def __init__(self, db):
        self._db = db

    def add(self, playlist_id: str, content_id: str) -> str:
        return str(self._db.add_to_playlist(playlist_id, content_id).ID)

    def remove(self, playlist_id: str, song_id: str) -> None:
        self._db.remove_from_playlist(playlist_id, song_id)

    def relocate(self, content_id: str, path: str, file_name: str | None, org_path: str | None) -> None:
        content = self._db.get_content(ID=content_id)

        if content is None:
            raise DatabaseError(f"Track {content_id} is not in the database.")

        content.FolderPath = path
        content.FileNameL = file_name
        content.OrgFolderPath = org_path

    def commit(self) -> None:
        self._db.commit()

    def rollback(self) -> None:
        self._db.rollback()

    def close(self) -> None:
        self._db.close()


class PyrekordboxBackend:
    def _open(self, db_path: Path):
        import logging

        logging.getLogger("pyrekordbox").setLevel(logging.ERROR)

        try:
            return _load_pyrekordbox()(path=db_path)
        except DatabaseError:
            raise
        except Exception as error:
            raise DatabaseError(f"Could not open Rekordbox's database: {error}") from None

    def read(self, db_path: Path) -> DbSnapshot:
        db = self._open(db_path)

        try:
            artists = {str(a.ID): a.Name for a in db.get_artist().all()}
            contents = tuple(
                DbContent(
                    id=str(c.ID), title=c.Title, artist=artists.get(str(c.ArtistID)), path=c.FolderPath or None,
                    duration_ms=int(c.Length) * 1000 if c.Length else None,
                    file_name=c.FileNameL or None, org_path=c.OrgFolderPath or None,
                )
                for c in db.get_content().all()
            )
            rows = db.get_playlist().all()
            names = {str(p.ID): p for p in rows}
            playlists = []

            for p in rows:
                folders, parent = [], names.get(str(p.ParentID))

                while parent is not None:
                    folders.append(parent.Name)
                    parent = names.get(str(parent.ParentID))

                songs = sorted(p.Songs, key=lambda s: s.TrackNo)
                playlists.append(DbPlaylist(
                    id=str(p.ID), name=p.Name, parent_id=str(p.ParentID) if str(p.ParentID) in names else None,
                    is_folder=p.Attribute == 1, is_smart=p.Attribute == 4,
                    folders=tuple(reversed(folders)),
                    song_ids=tuple(str(s.ID) for s in songs), content_ids=tuple(str(s.ContentID) for s in songs),
                ))

            return DbSnapshot(contents, tuple(playlists))
        finally:
            db.close()

    def open_for_write(self, db_path: Path) -> Writer:
        return PyrekordboxWriter(self._open(db_path))


def default_backend() -> Backend:
    return PyrekordboxBackend()


# ---------------------------------------------------------------------------
# Finding, copying and guarding the database
# ---------------------------------------------------------------------------


def find_database() -> Path:
    """The live master.db, from Rekordbox's own settings."""
    try:
        text = SETTINGS_FILE.expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""

    found = re.search(r'name="masterDbDirectory" val="([^"]+)"', text)
    candidates = [Path(found.group(1)) / "master.db"] if found else []
    candidates.append(Path("~/Library/Pioneer/rekordbox/master.db").expanduser())

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise DatabaseError("Couldn't find Rekordbox's master.db. Pass database_path.")


def rekordbox_running() -> bool:
    return subprocess.run(["pgrep", "-x", "rekordbox"], capture_output=True).returncode == 0


def _require_rekordbox_closed() -> None:
    if rekordbox_running():
        raise DatabaseError(
            "Rekordbox is running. It keeps its library open and can overwrite a change, so quit Rekordbox "
            "(Cmd-Q) first."
        )


def cuebridge_home() -> Path:
    return Path(os.environ.get("CUEBRIDGE_HOME") or Path.home() / ".cuebridge")


def copy_database(db_path: Path, destination: Path) -> Path:
    """Copy master.db and its companion files into `destination`; returns the copy of master.db."""
    destination.mkdir(parents=True, exist_ok=True)

    for name in DB_FILES:
        source = db_path.with_name(name)

        if source.is_file():
            shutil.copy2(source, destination / name)

    if not (destination / "master.db").is_file():
        raise DatabaseError(f"Could not copy {db_path}.")

    return destination / "master.db"


def _digest(path: Path) -> str:
    h = hashlib.sha256()

    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            h.update(chunk)

    return h.hexdigest()


def read_snapshot(db_path: Path, backend: Backend) -> DbSnapshot:
    """Read the database through a copy, so the live files are never opened by the reader."""
    with tempfile.TemporaryDirectory(prefix="cuebridge-db-") as folder:
        return backend.read(copy_database(db_path, Path(folder)))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Addition:
    playlist_id: str
    playlist_name: str
    content_id: str
    title: str | None
    artist: str | None
    position: int  # the track number it will get


@dataclass
class DbPlan:
    additions: list[Addition] = field(default_factory=list)
    not_in_collection: list[tuple[str, str]] = field(default_factory=list)  # (playlist, track)
    missing_playlists: list[str] = field(default_factory=list)  # Apple playlists with no playlist in Rekordbox
    ambiguous: list[str] = field(default_factory=list)  # names that match several Rekordbox playlists
    extra_in_rekordbox: dict[str, int] = field(default_factory=dict)  # playlist -> tracks only Rekordbox has
    in_sync: list[str] = field(default_factory=list)


def plan_additions(snapshot: DbSnapshot, music: MusicLibrary, only: list[str] | None = None) -> DbPlan:
    """Which tracks would be added to which existing Rekordbox playlists."""
    contents = {c.id: c for c in snapshot.contents}
    tracks = [
        Track(trackId=c.id, trackTitle=c.title, artist=c.artist, filePath=c.path, durationMs=c.duration_ms, cues=[])
        for c in snapshot.contents
    ]
    top = [p for p in snapshot.playlists if p.is_plain and not p.folders]
    by_name: dict[str, list[DbPlaylist]] = {}

    for playlist in top:
        by_name.setdefault(playlist.name.casefold(), []).append(playlist)

    unique = [group[0] for group in by_name.values() if len(group) == 1]
    result = drift.compare_libraries(
        tracks, [RekordboxPlaylist(p.name, (), p.content_ids) for p in unique], music
    )
    wanted = {n.strip().casefold() for n in only} if only else None
    lengths = {p.id: len(p.content_ids) for p in top}
    plan = DbPlan()

    for item in result.playlists:
        key = item.name.casefold()

        if wanted is not None and key not in wanted:
            continue

        if key in by_name and len(by_name[key]) > 1:
            plan.ambiguous.append(item.name)
            continue

        if item.status == "missing_in_rekordbox":
            plan.missing_playlists.append(item.name)
            continue

        if item.status == "in_sync":
            plan.in_sync.append(item.name)
            continue

        target = by_name[key][0]
        position = lengths[target.id]
        added: set[str] = set()

        for apple in item.apple_only:
            ids = result.rekordbox_for_apple.get(apple.track_id)

            if not ids:
                plan.not_in_collection.append((item.name, apple.title or "(untitled)"))
                continue

            if ids[0] in added or ids[0] in target.content_ids:
                continue

            added.add(ids[0])
            position += 1
            content = contents[ids[0]]
            plan.additions.append(Addition(target.id, item.name, content.id, content.title, content.artist, position))

        if item.rekordbox_only:
            plan.extra_in_rekordbox[item.name] = len(item.rekordbox_only)

    return plan


# ---------------------------------------------------------------------------
# Applying and undoing
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    journal: Path
    backup: Path
    added: int


def _save_journal(path: Path, journal: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")

    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(journal, handle, indent=1)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(temporary, path)


PLAYLIST_JOURNALS = "rekordbox-playlists"
PATH_JOURNALS = "rekordbox-paths"


def _journal_path(directory: Path, prefix: str = PLAYLIST_JOURNALS) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp, counter = f"{datetime.now():%Y%m%d-%H%M%S}", 0
    path = directory / f"{prefix}-{stamp}-{counter:02d}.json"

    while path.exists():
        counter += 1
        path = directory / f"{prefix}-{stamp}-{counter:02d}.json"

    return path


def latest_journal(directory: Path | None = None, prefix: str = PLAYLIST_JOURNALS) -> Path | None:
    files = sorted((directory or cuebridge_home() / "undo").glob(f"{prefix}-*.json"))
    return files[-1] if files else None


def _restore(backup: Path, db_path: Path) -> None:
    for name in DB_FILES:
        source, target = backup / name, db_path.with_name(name)

        if source.is_file():
            shutil.copy2(source, target)
        elif target.is_file() and name != "master.db":
            target.unlink()  # a companion file that didn't exist when the backup was made


def _backup_database(db_path: Path, backend: Backend, before: DbSnapshot) -> Path:
    """Copy the database files to a new backup folder and check the copy is identical and reads back the same."""
    backup = cuebridge_home() / "db-backups" / f"{datetime.now():%Y%m%d-%H%M%S}"
    copy_database(db_path, backup)

    for name in DB_FILES:
        original = db_path.with_name(name)

        if original.is_file() and _digest(original) != _digest(backup / name):
            raise DatabaseError("The backup copy of the database doesn't match the original. Nothing was written.")

    if backend.read(backup / "master.db") != before:
        raise DatabaseError("The backup copy of the database doesn't read back the same. Nothing was written.")

    return backup


def apply_plan(plan: DbPlan, db_path: Path, music: MusicLibrary, backend: Backend, only: list[str] | None = None) -> ApplyResult:
    """Add the planned tracks to their playlists, with backup, journal and verification."""
    _require_rekordbox_closed()

    before = read_snapshot(db_path, backend)
    fresh = plan_additions(before, music, only)

    if [(a.playlist_id, a.content_id) for a in fresh.additions] != [(a.playlist_id, a.content_id) for a in plan.additions]:
        raise DatabaseError("Rekordbox's database changed since this was planned. Nothing was written. Run a dry run again.")

    if not plan.additions:
        raise DatabaseError("Nothing to add.")

    home = cuebridge_home()
    backup = _backup_database(db_path, backend, before)

    journal_path = _journal_path(home / "undo")
    journal = {
        "version": JOURNAL_VERSION, "createdAt": datetime.now().isoformat(timespec="seconds"),
        "database": str(db_path), "backup": str(backup), "state": "pending",
        "additions": [
            {"playlistId": a.playlist_id, "playlist": a.playlist_name, "contentId": a.content_id, "title": a.title, "songId": None}
            for a in plan.additions
        ],
    }
    _save_journal(journal_path, journal)

    writer = backend.open_for_write(db_path)

    try:
        for entry, addition in zip(journal["additions"], plan.additions):
            entry["songId"] = writer.add(addition.playlist_id, addition.content_id)

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

    expected = dict(before.playlist_contents())

    for addition in plan.additions:
        expected[addition.playlist_id] = expected[addition.playlist_id] + (addition.content_id,)

    try:
        after = read_snapshot(db_path, backend)
        problems = []

        if after.playlist_contents() != expected:
            problems.append("the playlists aren't exactly as planned")

        if after.contents != before.contents:
            problems.append("the track list changed")
    except Exception as error:
        problems = [f"the database couldn't be read back ({error})"]

    if problems:
        _restore(backup, db_path)
        journal["state"] = "reverted"
        _save_journal(journal_path, journal)
        raise DatabaseError("Verification failed (" + "; ".join(problems) + ") and the backup was put back.")

    journal["state"] = "verified"
    _save_journal(journal_path, journal)

    return ApplyResult(journal_path, backup, len(plan.additions))


@dataclass
class UndoResult:
    removed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def undo(journal_path: Path, backend: Backend, dry_run: bool = False) -> UndoResult:
    """Remove exactly the rows a run added, if they are still there."""
    try:
        journal = json.loads(Path(journal_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DatabaseError(f"Could not read the journal {journal_path}.") from None

    if not dry_run:
        _require_rekordbox_closed()

    db_path = Path(journal["database"])
    snapshot = read_snapshot(db_path, backend)
    by_id = {p.id: p for p in snapshot.playlists}
    result = UndoResult()
    to_remove = []

    for entry in journal["additions"]:
        label = f"{entry['title']} in {entry['playlist']}"
        playlist = by_id.get(entry["playlistId"])

        if not entry.get("songId"):
            result.skipped.append((label, "it was never written"))
        elif playlist is None or entry["songId"] not in playlist.song_ids:
            result.skipped.append((label, "it is no longer there"))
        elif playlist.content_ids[playlist.song_ids.index(entry["songId"])] != entry["contentId"]:
            result.skipped.append((label, "that row now holds a different track, so it was left alone"))
        else:
            to_remove.append(entry)
            result.removed.append(label)

    if dry_run or not to_remove:
        return result

    writer = backend.open_for_write(db_path)

    try:
        for entry in to_remove:
            writer.remove(entry["playlistId"], entry["songId"])

        writer.commit()
    except Exception as error:
        writer.rollback()
        writer.close()
        raise DatabaseError(f"Removing the tracks failed: {error}") from None

    writer.close()
    journal["state"] = "undone"
    _save_journal(Path(journal_path), journal)

    return result


# ---------------------------------------------------------------------------
# Fixing the location of renamed or moved files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Relocation:
    content_id: str
    title: str | None
    artist: str | None
    old_path: str
    new_path: str
    old_file_name: str | None
    new_file_name: str
    old_org_path: str | None
    new_org_path: str | None


@dataclass
class RelocationPlan:
    relocations: list[Relocation] = field(default_factory=list)
    no_match: list[tuple[str, str]] = field(default_factory=list)  # (title, path) with no file found in Apple Music
    already_in_collection: list[tuple[str, str]] = field(default_factory=list)  # (title, new path) another row has


def plan_relocations(
    snapshot: DbSnapshot, music: MusicLibrary, exists=os.path.exists
) -> RelocationPlan:
    """Which tracks whose file is missing can be pointed at the file Apple Music has."""
    contents = {c.id: c for c in snapshot.contents}
    tracks = [
        Track(trackId=c.id, trackTitle=c.title, artist=c.artist, filePath=c.path, durationMs=c.duration_ms, cues=[])
        for c in snapshot.contents
    ]
    missing = library_reports.find_missing_rekordbox_files(tracks, music, only_with_cues=False, exists=exists)
    in_use = {c.path for c in snapshot.contents if c.path}
    plan = RelocationPlan()
    claimed: set[str] = set()

    for item in missing:
        content = contents[item.track.trackId]
        label = (content.title or "(untitled)", content.path or "")

        if not item.suggested_path:
            plan.no_match.append(label)
            continue

        new_path = item.suggested_path

        if new_path in in_use or new_path in claimed:
            plan.already_in_collection.append((label[0], new_path))
            continue

        claimed.add(new_path)
        # The import location moves along only if it was the same as the current one.
        org_follows = content.org_path is None or content.org_path == content.path
        plan.relocations.append(Relocation(
            content.id, content.title, content.artist, content.path, new_path,
            content.file_name, Path(new_path).name, content.org_path,
            new_path if org_follows else content.org_path,
        ))

    return plan


def _apply_relocation(content: DbContent, relocation: Relocation) -> DbContent:
    return DbContent(
        content.id, content.title, content.artist, relocation.new_path, content.duration_ms,
        relocation.new_file_name, relocation.new_org_path,
    )


def _relocation_key(plan: RelocationPlan) -> list[tuple[str, str]]:
    return [(r.content_id, r.new_path) for r in plan.relocations]


def apply_relocations(plan: RelocationPlan, db_path: Path, music: MusicLibrary, backend: Backend, exists=os.path.exists) -> ApplyResult:
    """Point the planned tracks at their real files, with backup, journal and verification."""
    _require_rekordbox_closed()

    if not plan.relocations:
        raise DatabaseError("Nothing to fix.")

    before = read_snapshot(db_path, backend)

    if _relocation_key(plan_relocations(before, music, exists)) != _relocation_key(plan):
        raise DatabaseError("Rekordbox's database or the files changed since this was planned. Nothing was written. Run a dry run again.")

    backup = _backup_database(db_path, backend, before)
    journal_path = _journal_path(cuebridge_home() / "undo", PATH_JOURNALS)
    journal = {
        "version": JOURNAL_VERSION, "kind": "paths", "createdAt": datetime.now().isoformat(timespec="seconds"),
        "database": str(db_path), "backup": str(backup), "state": "pending",
        "relocations": [
            {
                "contentId": r.content_id, "title": r.title,
                "oldPath": r.old_path, "newPath": r.new_path,
                "oldFileName": r.old_file_name, "newFileName": r.new_file_name,
                "oldOrgPath": r.old_org_path, "newOrgPath": r.new_org_path,
            }
            for r in plan.relocations
        ],
    }
    _save_journal(journal_path, journal)

    writer = backend.open_for_write(db_path)

    try:
        for r in plan.relocations:
            writer.relocate(r.content_id, r.new_path, r.new_file_name, r.new_org_path)

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

    by_id = {r.content_id: r for r in plan.relocations}
    expected = DbSnapshot(
        tuple(_apply_relocation(c, by_id[c.id]) if c.id in by_id else c for c in before.contents),
        before.playlists,
    )

    try:
        problems = [] if read_snapshot(db_path, backend) == expected else ["the database isn't exactly as planned"]
    except Exception as error:
        problems = [f"the database couldn't be read back ({error})"]

    if problems:
        _restore(backup, db_path)
        journal["state"] = "reverted"
        _save_journal(journal_path, journal)
        raise DatabaseError("Verification failed (" + "; ".join(problems) + ") and the backup was put back.")

    journal["state"] = "verified"
    _save_journal(journal_path, journal)

    return ApplyResult(journal_path, backup, len(plan.relocations))


def undo_relocations(journal_path: Path, backend: Backend, dry_run: bool = False) -> UndoResult:
    """Put back the old paths of a run, for the tracks that still hold the path it wrote."""
    try:
        journal = json.loads(Path(journal_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DatabaseError(f"Could not read the journal {journal_path}.") from None

    if "relocations" not in journal:
        raise DatabaseError(f"{journal_path} is not a file-location journal.")

    if not dry_run:
        _require_rekordbox_closed()

    db_path = Path(journal["database"])
    contents = {c.id: c for c in read_snapshot(db_path, backend).contents}
    result = UndoResult()
    to_restore = []

    for entry in journal["relocations"]:
        label = entry["title"] or entry["newPath"]
        content = contents.get(entry["contentId"])

        if content is None:
            result.skipped.append((label, "it is no longer in the database"))
        elif content.path != entry["newPath"]:
            result.skipped.append((label, "its path has changed since, so it was left alone"))
        else:
            to_restore.append(entry)
            result.removed.append(label)

    if dry_run or not to_restore:
        return result

    writer = backend.open_for_write(db_path)

    try:
        for entry in to_restore:
            writer.relocate(entry["contentId"], entry["oldPath"], entry["oldFileName"], entry["oldOrgPath"])

        writer.commit()
    except Exception as error:
        writer.rollback()
        writer.close()
        raise DatabaseError(f"Restoring the paths failed: {error}") from None

    writer.close()
    journal["state"] = "undone"
    _save_journal(Path(journal_path), journal)

    return result
