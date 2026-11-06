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
from cuebridge.services.track_matcher import normalize_path

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
class DbCue:
    content_id: str
    kind: int  # 0 is a memory cue, 1 and up are hot cues (1 is A)
    position_ms: int
    id: str = ""


@dataclass(frozen=True)
class DbSnapshot:
    contents: tuple[DbContent, ...]
    playlists: tuple[DbPlaylist, ...]
    cues: tuple[DbCue, ...] = ()

    def playlist_contents(self) -> dict[str, tuple[str, ...]]:
        return {p.id: p.content_ids for p in self.playlists}


class Writer(Protocol):
    def add(self, playlist_id: str, content_id: str) -> str: ...
    def remove(self, playlist_id: str, song_id: str) -> None: ...
    def relocate(self, content_id: str, path: str, file_name: str | None, org_path: str | None) -> None: ...
    def create_playlist(self, name: str) -> str: ...
    def delete_playlist(self, playlist_id: str) -> None: ...
    def delete_content(self, content_id: str) -> dict: ...
    def restore_content(self, payload: dict) -> None: ...
    def copy_cues(self, from_content_id: str, to_content_id: str, cue_ids: list[str]) -> dict: ...
    def remove_copied_cues(self, to_content_id: str, token: dict) -> None: ...
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

    # Everything that hangs off a track, besides the track itself. None of it has foreign keys or triggers.
    _CONTENT_TABLES = ("djmdCue", "contentCue", "contentFile", "djmdMixerParam", "djmdSongPlaylist")
    _CUE_JSON_FIELDS = (
        "ContentID", "UUID", "ContentUUID", "InMsec", "InFrame", "InMpegFrame", "InMpegAbs", "OutMpegAbs", "OutMsec",
        "OutFrame", "OutMpegFrame", "Kind", "ActiveLoop", "BeatLoopSize", "Color", "ColorTableIndex", "Comment",
        "created_at", "updated_at", "ID",
    )

    def _sql(self, statement: str, **params):
        from sqlalchemy import text

        return self._db.session.execute(text(statement), params)

    def _rows(self, table: str, column: str, value: str) -> list[dict]:
        return [dict(r) for r in self._sql(f'select * from "{table}" where "{column}" = :v', v=value).mappings().all()]

    def _insert(self, table: str, row: dict) -> None:
        columns = ", ".join(f'"{c}"' for c in row)
        marks = ", ".join(f":{i}" for i in range(len(row)))
        self._sql(f'insert into "{table}" ({columns}) values ({marks})', **{str(i): v for i, v in enumerate(row.values())})

    def delete_content(self, content_id: str) -> dict:
        payload = {"djmdContent": self._rows("djmdContent", "ID", content_id)}

        if len(payload["djmdContent"]) != 1:
            raise DatabaseError(f"Track {content_id} is not in the database.")

        for table in self._CONTENT_TABLES:
            payload[table] = self._rows(table, "ContentID", content_id)
            self._sql(f'delete from "{table}" where "ContentID" = :v', v=content_id)

        self._sql('delete from "djmdContent" where "ID" = :v', v=content_id)

        return payload

    def restore_content(self, payload: dict) -> None:
        playlists = {str(r["ID"]) for r in self._sql('select "ID" from "djmdPlaylist"').mappings().all()}

        for table in ("djmdContent", *self._CONTENT_TABLES):
            for row in payload.get(table, []):
                if table == "djmdSongPlaylist" and str(row["PlaylistID"]) not in playlists:
                    continue  # that playlist has been deleted since

                self._insert(table, row)

    def copy_cues(self, from_content_id: str, to_content_id: str, cue_ids: list[str]) -> dict:
        import json
        import uuid
        from datetime import datetime

        target = self._rows("djmdContent", "ID", to_content_id)

        if len(target) != 1:
            raise DatabaseError(f"Track {to_content_id} is not in the database.")

        stamp = datetime.now().astimezone()
        db_stamp = stamp.strftime("%Y-%m-%d %H:%M:%S.") + f"{stamp.microsecond // 1000:03d} " + stamp.strftime("%z")[:3] + ":" + stamp.strftime("%z")[3:]
        json_stamp = stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}" + stamp.strftime("%z")[:3] + ":" + stamp.strftime("%z")[3:]
        used = {r["ID"] for r in self._sql('select "ID" from "djmdCue"').mappings().all()}
        new_uuids: dict[str, str] = {}
        created, entries = [], []

        for cue_id in cue_ids:
            (source,) = self._rows("djmdCue", "ID", cue_id)

            if str(source["ContentID"]) != str(from_content_id):
                raise DatabaseError(f"Cue {cue_id} doesn't belong to track {from_content_id}.")

            new_id = str(uuid.uuid4().int % 4_000_000_000)

            while new_id in used:
                new_id = str(uuid.uuid4().int % 4_000_000_000)

            used.add(new_id)
            # A hot cue and its memory cue share one UUID in Rekordbox, so their copies do too.
            new_uuid = new_uuids.setdefault(source["UUID"], str(uuid.uuid4()))
            row = {
                **source, "ID": new_id, "ContentID": to_content_id, "ContentUUID": target[0]["UUID"], "UUID": new_uuid,
                "created_at": db_stamp, "updated_at": db_stamp,
            }
            self._insert("djmdCue", row)
            created.append(new_id)
            entries.append({k: (json_stamp if k in ("created_at", "updated_at") else row.get(k)) for k in self._CUE_JSON_FIELDS})

        existing = self._rows("contentCue", "ContentID", to_content_id)

        if existing:
            previous = existing[0]["Cues"]
            merged = json.loads(previous or "[]") + entries
            self._sql('update "contentCue" set "Cues" = :c where "ID" = :i', c=json.dumps(merged, separators=(",", ":")), i=existing[0]["ID"])
            token = {"cueIds": created, "contentCue": {"previous": previous, "id": existing[0]["ID"]}}
        else:
            row_id = str(uuid.uuid4())
            self._insert("contentCue", {
                "ID": row_id, "ContentID": to_content_id, "Cues": json.dumps(entries, separators=(",", ":")),
                "rb_cue_count": None, "UUID": str(uuid.uuid4()), "rb_data_status": 0, "rb_local_data_status": 0,
                "rb_local_deleted": 0, "rb_local_synced": 0, "usn": None, "rb_local_usn": None,
                "created_at": db_stamp, "updated_at": db_stamp,
            })
            token = {"cueIds": created, "contentCue": {"created": row_id}}

        return token

    def remove_copied_cues(self, to_content_id: str, token: dict) -> None:
        for cue_id in token["cueIds"]:
            self._sql('delete from "djmdCue" where "ID" = :i and "ContentID" = :c', i=cue_id, c=to_content_id)

        info = token["contentCue"]

        if "created" in info:
            self._sql('delete from "contentCue" where "ID" = :i', i=info["created"])
        else:
            self._sql('update "contentCue" set "Cues" = :c where "ID" = :i', c=info["previous"], i=info["id"])

    def create_playlist(self, name: str) -> str:
        return str(self._db.create_playlist(name).ID)

    def delete_playlist(self, playlist_id: str) -> None:
        self._db.delete_playlist(playlist_id)

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

            cues = tuple(
                DbCue(str(c.ContentID), int(c.Kind or 0), int(c.InMsec or 0), str(c.ID)) for c in db.get_cue().all()
            )

            return DbSnapshot(contents, tuple(playlists), cues)
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
REVIEW_JOURNALS = "rekordbox-review"
CLEANUP_JOURNALS = "rekordbox-cleanup"


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
    duplicates: list[tuple[str, str]] = field(default_factory=list)  # (broken row ID, the row that has the real file)


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
    # The drive ignores case and Unicode form, so two spellings of one path are the same file.
    in_use = {normalize_path(c.path): c.id for c in snapshot.contents if c.path}
    plan = RelocationPlan()
    claimed: set[str] = set()

    for item in missing:
        content = contents[item.track.trackId]
        label = (content.title or "(untitled)", content.path or "")

        if not item.suggested_path:
            plan.no_match.append(label)
            continue

        new_path = item.suggested_path
        key = normalize_path(new_path)

        if key in in_use or key in claimed:
            plan.already_in_collection.append((label[0], new_path))

            if key in in_use:
                plan.duplicates.append((content.id, in_use[key]))

            continue

        claimed.add(key)
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
        before.cues,
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


# ---------------------------------------------------------------------------
# Stale duplicate rows
# ---------------------------------------------------------------------------

CUE_TOLERANCE_MS = 25


@dataclass(frozen=True)
class RowSummary:
    content_id: str
    path: str | None
    hot_cues: int
    memory_cues: int
    playlists: tuple[str, ...]


@dataclass(frozen=True)
class DuplicatePair:
    title: str | None
    artist: str | None
    broken: RowSummary
    good: RowSummary
    cues_only_on_broken: int
    playlists_only_on_broken: tuple[str, ...]

    @property
    def verdict(self) -> str:
        """"safe_to_remove" if the broken row holds nothing the good one lacks, otherwise "needs_merge"."""
        if self.cues_only_on_broken or self.playlists_only_on_broken:
            return "needs_merge"

        return "safe_to_remove"


def _covered(cue: DbCue, others: list[DbCue]) -> bool:
    return any(o.kind == cue.kind and abs(o.position_ms - cue.position_ms) <= CUE_TOLERANCE_MS for o in others)


def describe_duplicates(plan: RelocationPlan, snapshot: DbSnapshot) -> list[DuplicatePair]:
    """For each broken row that has a twin holding the real file: what each row has (cues, playlists)."""
    contents = {c.id: c for c in snapshot.contents}
    cues: dict[str, list[DbCue]] = {}
    playlists: dict[str, list[str]] = {}

    for cue in snapshot.cues:
        cues.setdefault(cue.content_id, []).append(cue)

    for playlist in snapshot.playlists:
        # CueBridge's own review playlists say nothing about whether a row is used.
        if playlist.is_plain and playlist.name not in (SAFE_PLAYLIST, MERGE_PLAYLIST):
            for content_id in set(playlist.content_ids):
                playlists.setdefault(content_id, []).append("/".join((*playlist.folders, playlist.name)))

    def summary(content_id: str) -> RowSummary:
        own = cues.get(content_id, [])

        return RowSummary(
            content_id, contents[content_id].path,
            sum(1 for c in own if c.kind > 0), sum(1 for c in own if c.kind == 0),
            tuple(sorted(playlists.get(content_id, []))),
        )

    pairs = []

    for broken_id, good_id in plan.duplicates:
        broken, good = summary(broken_id), summary(good_id)
        missing = sum(1 for c in cues.get(broken_id, []) if not _covered(c, cues.get(good_id, [])))
        pairs.append(DuplicatePair(
            contents[broken_id].title, contents[broken_id].artist, broken, good, missing,
            tuple(p for p in broken.playlists if p not in good.playlists),
        ))

    return pairs


# ---------------------------------------------------------------------------
# Review playlists: let Rekordbox remove the stale rows itself
# ---------------------------------------------------------------------------

SAFE_PLAYLIST = "CueBridge: remove (safe)"
MERGE_PLAYLIST = "CueBridge: check cues first"


@dataclass
class ReviewPlan:
    safe: list[tuple[str, str | None, str | None]] = field(default_factory=list)  # (content ID, title, artist)
    merge: list[tuple[str, str | None, str | None]] = field(default_factory=list)


def plan_review_playlists(snapshot: DbSnapshot, music: MusicLibrary, exists=os.path.exists) -> ReviewPlan:
    """The broken rows that have a twin, split by whether removing them loses anything."""
    taken = {p.name for p in snapshot.playlists} & {SAFE_PLAYLIST, MERGE_PLAYLIST}

    if taken:
        raise DatabaseError(
            f"Rekordbox already has a playlist called {sorted(taken)[0]!r}. Remove it (or run "
            "undo_rekordbox_review_playlists) before making new review playlists."
        )

    pairs = describe_duplicates(plan_relocations(snapshot, music, exists), snapshot)
    plan = ReviewPlan()

    for pair in sorted(pairs, key=lambda p: (p.title or "").casefold()):
        row = (pair.broken.content_id, pair.title, pair.artist)
        (plan.safe if pair.verdict == "safe_to_remove" else plan.merge).append(row)

    return plan


def _review_key(plan: ReviewPlan) -> tuple:
    return (tuple(r[0] for r in plan.safe), tuple(r[0] for r in plan.merge))


def apply_review_playlists(plan: ReviewPlan, db_path: Path, music: MusicLibrary, backend: Backend, exists=os.path.exists) -> ApplyResult:
    """Create the two review playlists, with backup, journal and verification."""
    _require_rekordbox_closed()

    if not plan.safe and not plan.merge:
        raise DatabaseError("Nothing to review.")

    before = read_snapshot(db_path, backend)

    if _review_key(plan_review_playlists(before, music, exists)) != _review_key(plan):
        raise DatabaseError("Rekordbox's database or the files changed since this was planned. Nothing was written. Run a dry run again.")

    wanted = [(name, rows) for name, rows in ((SAFE_PLAYLIST, plan.safe), (MERGE_PLAYLIST, plan.merge)) if rows]
    backup = _backup_database(db_path, backend, before)
    journal_path = _journal_path(cuebridge_home() / "undo", REVIEW_JOURNALS)
    journal = {
        "version": JOURNAL_VERSION, "kind": "review", "createdAt": datetime.now().isoformat(timespec="seconds"),
        "database": str(db_path), "backup": str(backup), "state": "pending",
        "playlists": [{"name": name, "playlistId": None, "contentIds": [r[0] for r in rows]} for name, rows in wanted],
    }
    _save_journal(journal_path, journal)

    writer = backend.open_for_write(db_path)

    try:
        for entry in journal["playlists"]:
            entry["playlistId"] = writer.create_playlist(entry["name"])

            for content_id in entry["contentIds"]:
                writer.add(entry["playlistId"], content_id)

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

    try:
        after = read_snapshot(db_path, backend)
        old_ids = {p.id for p in before.playlists}
        old = [p for p in after.playlists if p.id in old_ids]
        new = {p.name: p.content_ids for p in after.playlists if p.id not in old_ids}
        problems = []

        if old != list(before.playlists) or after.contents != before.contents or after.cues != before.cues:
            problems.append("something other than the new playlists changed")

        if new != {name: tuple(r[0] for r in rows) for name, rows in wanted}:
            problems.append("the new playlists aren't exactly as planned")
    except Exception as error:
        problems = [f"the database couldn't be read back ({error})"]

    if problems:
        _restore(backup, db_path)
        journal["state"] = "reverted"
        _save_journal(journal_path, journal)
        raise DatabaseError("Verification failed (" + "; ".join(problems) + ") and the backup was put back.")

    journal["state"] = "verified"
    _save_journal(journal_path, journal)

    return ApplyResult(journal_path, backup, len(plan.safe) + len(plan.merge))


def undo_review_playlists(journal_path: Path, backend: Backend, dry_run: bool = False) -> UndoResult:
    """Delete the review playlists a run made (never their tracks), if they are still the ones it made."""
    try:
        journal = json.loads(Path(journal_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DatabaseError(f"Could not read the journal {journal_path}.") from None

    if journal.get("kind") != "review":
        raise DatabaseError(f"{journal_path} is not a review-playlist journal.")

    if not dry_run:
        _require_rekordbox_closed()

    db_path = Path(journal["database"])
    by_id = {p.id: p for p in read_snapshot(db_path, backend).playlists}
    result = UndoResult()
    to_delete = []

    for entry in journal["playlists"]:
        playlist = by_id.get(entry["playlistId"])

        if playlist is None:
            result.skipped.append((entry["name"], "it is no longer there"))
        elif playlist.name != entry["name"]:
            result.skipped.append((entry["name"], "that playlist was renamed, so it was left alone"))
        elif not set(playlist.content_ids) <= set(entry["contentIds"]):
            result.skipped.append((entry["name"], "tracks were added to it since, so it was left alone"))
        else:
            to_delete.append(entry)
            result.removed.append(entry["name"])

    if dry_run or not to_delete:
        return result

    writer = backend.open_for_write(db_path)

    try:
        for entry in to_delete:
            writer.delete_playlist(entry["playlistId"])

        writer.commit()
    except Exception as error:
        writer.rollback()
        writer.close()
        raise DatabaseError(f"Removing the playlists failed: {error}") from None

    writer.close()
    journal["state"] = "undone"
    _save_journal(Path(journal_path), journal)

    return result


# ---------------------------------------------------------------------------
# Cleaning up stale duplicate rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RowRemoval:
    broken_id: str
    good_id: str
    title: str | None
    artist: str | None
    path: str | None


@dataclass(frozen=True)
class CueMerge:
    broken_id: str
    good_id: str
    title: str | None
    cue_ids: tuple[str, ...]  # the cues on the broken row that the good row lacks
    cues: tuple[tuple[int, int], ...]  # (kind, position in ms), for showing


@dataclass
class CleanupPlan:
    removals: list[RowRemoval] = field(default_factory=list)  # broken rows that hold nothing the twin lacks
    merges: list[CueMerge] = field(default_factory=list)  # cues to copy to the twin; the broken row is kept
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (title, why)


def plan_cleanup(snapshot: DbSnapshot, music: MusicLibrary, exists=os.path.exists) -> CleanupPlan:
    """Rows to remove, cues to copy, and rows left alone, for the broken rows that have a twin."""
    pairs = describe_duplicates(plan_relocations(snapshot, music, exists), snapshot)
    cues: dict[str, list[DbCue]] = {}

    for cue in snapshot.cues:
        cues.setdefault(cue.content_id, []).append(cue)

    plan = CleanupPlan()

    for pair in sorted(pairs, key=lambda p: ((p.title or "").casefold(), p.broken.content_id)):
        broken, good = pair.broken.content_id, pair.good.content_id

        if pair.verdict == "safe_to_remove":
            plan.removals.append(RowRemoval(broken, good, pair.title, pair.artist, pair.broken.path))
        elif pair.playlists_only_on_broken:
            plan.skipped.append((pair.title or broken, "it is in " + ", ".join(pair.playlists_only_on_broken) + " and its twin isn't"))
        else:
            lacking = [c for c in cues.get(broken, []) if not _covered(c, cues.get(good, []))]

            if any(not c.id for c in lacking):
                raise DatabaseError("The database reader didn't return cue IDs, so cues can't be copied.")

            plan.merges.append(CueMerge(
                broken, good, pair.title, tuple(c.id for c in lacking), tuple((c.kind, c.position_ms) for c in lacking),
            ))

    return plan


def _cleanup_key(plan: CleanupPlan) -> tuple:
    return (
        tuple((r.broken_id, r.good_id) for r in plan.removals),
        tuple((m.broken_id, m.good_id, m.cue_ids) for m in plan.merges),
    )


def _cue_key(cues) -> list[tuple[str, int, int]]:
    return sorted((c.content_id, c.kind, c.position_ms) for c in cues)


def apply_cleanup(plan: CleanupPlan, db_path: Path, music: MusicLibrary, backend: Backend, exists=os.path.exists) -> ApplyResult:
    """Copy the planned cues, then remove the planned rows with everything attached, verified, journaled, undoable."""
    _require_rekordbox_closed()

    if not plan.removals and not plan.merges:
        raise DatabaseError("Nothing to clean up.")

    before = read_snapshot(db_path, backend)

    if _cleanup_key(plan_cleanup(before, music, exists)) != _cleanup_key(plan):
        raise DatabaseError("Rekordbox's database or the files changed since this was planned. Nothing was written. Run a dry run again.")

    backup = _backup_database(db_path, backend, before)
    journal_path = _journal_path(cuebridge_home() / "undo", CLEANUP_JOURNALS)
    journal = {
        "version": JOURNAL_VERSION, "kind": "cleanup", "createdAt": datetime.now().isoformat(timespec="seconds"),
        "database": str(db_path), "backup": str(backup), "state": "pending",
        "merges": [{"brokenId": m.broken_id, "goodId": m.good_id, "title": m.title, "cueIds": list(m.cue_ids), "token": None} for m in plan.merges],
        "removals": [{"contentId": r.broken_id, "goodId": r.good_id, "title": r.title, "payload": None} for r in plan.removals],
    }
    _save_journal(journal_path, journal)

    writer = backend.open_for_write(db_path)

    try:
        for entry in journal["merges"]:
            entry["token"] = writer.copy_cues(entry["brokenId"], entry["goodId"], entry["cueIds"])

        for entry in journal["removals"]:
            entry["payload"] = writer.delete_content(entry["contentId"])

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

    removed = {r.broken_id for r in plan.removals}
    expected_playlists = []

    for p in before.playlists:
        kept = [(song, content) for song, content in zip(p.song_ids, p.content_ids) if content not in removed]
        expected_playlists.append((p.id, p.name, tuple(k[0] for k in kept), tuple(k[1] for k in kept)))

    expected_cues = [c for c in before.cues if c.content_id not in removed]
    expected_cues += [DbCue(m.good_id, kind, ms) for m in plan.merges for kind, ms in m.cues]

    try:
        after = read_snapshot(db_path, backend)
        problems = []

        if after.contents != tuple(c for c in before.contents if c.id not in removed):
            problems.append("the track list isn't exactly as planned")

        if _cue_key(after.cues) != _cue_key(expected_cues):
            problems.append("the cues aren't exactly as planned")

        if [(p.id, p.name, p.song_ids, p.content_ids) for p in after.playlists] != expected_playlists:
            problems.append("the playlists aren't exactly as planned")
    except Exception as error:
        problems = [f"the database couldn't be read back ({error})"]

    if problems:
        _restore(backup, db_path)
        journal["state"] = "reverted"
        _save_journal(journal_path, journal)
        raise DatabaseError("Verification failed (" + "; ".join(problems) + ") and the backup was put back.")

    journal["state"] = "verified"
    _save_journal(journal_path, journal)

    return ApplyResult(journal_path, backup, len(plan.removals) + len(plan.merges))


def undo_cleanup(journal_path: Path, backend: Backend, dry_run: bool = False) -> UndoResult:
    """Take the copied cues off again and put the removed rows back, each only if it is safe to."""
    try:
        journal = json.loads(Path(journal_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DatabaseError(f"Could not read the journal {journal_path}.") from None

    if journal.get("kind") != "cleanup":
        raise DatabaseError(f"{journal_path} is not a cleanup journal.")

    if not dry_run:
        _require_rekordbox_closed()

    db_path = Path(journal["database"])
    snapshot = read_snapshot(db_path, backend)
    present = {c.id for c in snapshot.contents}
    cue_ids = {c.id for c in snapshot.cues}
    result = UndoResult()
    uncopy, restore = [], []

    for entry in journal["merges"]:
        label = f"cues copied to {entry['title'] or entry['goodId']}"

        if entry["goodId"] not in present or not entry.get("token"):
            result.skipped.append((label, "the track is gone or nothing was written"))
        elif not set(entry["token"]["cueIds"]) & cue_ids:
            result.skipped.append((label, "they are no longer there"))
        else:
            uncopy.append(entry)
            result.removed.append(label)

    for entry in journal["removals"]:
        label = f"row of {entry['title'] or entry['contentId']}"

        if not entry.get("payload"):
            result.skipped.append((label, "it was never removed"))
        elif entry["contentId"] in present:
            result.skipped.append((label, "a track with that ID is already there"))
        else:
            restore.append(entry)
            result.removed.append(label)

    if dry_run or not (uncopy or restore):
        return result

    writer = backend.open_for_write(db_path)

    try:
        for entry in uncopy:
            writer.remove_copied_cues(entry["goodId"], entry["token"])

        for entry in restore:
            writer.restore_content(entry["payload"])

        writer.commit()
    except Exception as error:
        writer.rollback()
        writer.close()
        raise DatabaseError(f"Undoing failed: {error}") from None

    writer.close()
    journal["state"] = "undone"
    _save_journal(Path(journal_path), journal)

    return result
