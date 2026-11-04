import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from apple_helpers import library_bytes, music_track, playlist, write_library
from cuebridge import mcp_server as server
from cuebridge.parsers.apple_music_xml import parse_apple_music_plist
from cuebridge.services import apple_music_service
from cuebridge.services import rekordbox_db as rd


class FakeWriter:
    def __init__(self, backend, path):
        self.backend, self.path = backend, path
        self.state = json.loads(path.read_text())
        self.counter = 0

    def add(self, playlist_id, content_id):
        playlist_ = next(p for p in self.state["playlists"] if p["id"] == playlist_id)
        self.counter += 1
        song_id = f"new-{self.backend.runs}-{self.counter}"
        playlist_["songs"].append([song_id, content_id])
        return song_id

    def remove(self, playlist_id, song_id):
        playlist_ = next(p for p in self.state["playlists"] if p["id"] == playlist_id)
        playlist_["songs"] = [s for s in playlist_["songs"] if s[0] != song_id]

    def commit(self):
        if self.backend.tamper:
            self.state["playlists"][0]["songs"].clear()  # damage something that was not part of the plan

        self.path.write_text(json.dumps(self.state))
        self.backend.runs += 1

        if self.backend.commit_raises:
            raise RuntimeError("disk full")  # after the file was already changed: a half-finished write

    def rollback(self):
        pass

    def close(self):
        pass


class FakeBackend:
    """A 'database' that is a JSON file called master.db, so copies, backups and restores are real."""

    def __init__(self):
        self.commit_raises = False
        self.tamper = False
        self.runs = 0

    def read(self, db_path: Path) -> rd.DbSnapshot:
        state = json.loads(db_path.read_text())
        contents = tuple(rd.DbContent(c["id"], c["title"], c["artist"], c["path"], None) for c in state["contents"])
        playlists = tuple(
            rd.DbPlaylist(
                p["id"], p["name"], None, p.get("folder", False), False, tuple(p.get("folders", ())),
                tuple(s[0] for s in p["songs"]), tuple(s[1] for s in p["songs"]),
            )
            for p in state["playlists"]
        )
        return rd.DbSnapshot(contents, playlists)

    def open_for_write(self, db_path: Path):
        return FakeWriter(self, db_path)


def song(n, content):
    return [f"s{n}", content]


def db_state():
    return {
        "contents": [
            {"id": "c1", "title": "A", "artist": "X", "path": "/Music/a.mp3"},
            {"id": "c2", "title": "B", "artist": "X", "path": "/Music/b.mp3"},
            {"id": "c3", "title": "E", "artist": "X", "path": "/Music/e.mp3"},
            {"id": "c4", "title": "Lost", "artist": "X", "path": "/Music/lost.mp3"},
        ],
        "playlists": [
            {"id": "p1", "name": "Set", "songs": [song(1, "c1"), song(2, "c2")]},
            {"id": "p2", "name": "Warm", "songs": [song(3, "c2")]},
            {"id": "p3", "name": "Extra", "songs": [song(4, "c4"), song(5, "c1")]},
        ],
    }


def apple(playlists=None):
    return parse_apple_music_plist(library_bytes(
        [
            music_track(1, "A", "X", "/Music/a.mp3"), music_track(2, "B", "X", "/Music/b.mp3"),
            music_track(4, "D", "X", "/Music/d.mp3"),  # not in the Rekordbox collection
            music_track(5, "E", "X", "/Music/e.mp3"),
        ],
        playlists if playlists is not None else [playlist("Set", 1, 2), playlist("Warm", 2, 4, 5)],
    ))


@pytest.fixture
def env(tmp_path, monkeypatch):
    folder = tmp_path / "Master"
    folder.mkdir()
    db = folder / "master.db"
    db.write_text(json.dumps(db_state()))
    (folder / "master.db-wal").write_bytes(b"wal-bytes")
    (folder / "masterPlaylists6.xml").write_text("<playlists/>")
    monkeypatch.setenv("CUEBRIDGE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(rd, "rekordbox_running", lambda: False)
    return {"db": db, "backend": FakeBackend(), "home": tmp_path / "home", "folder": folder}


def snapshot(env):
    return rd.read_snapshot(env["db"], env["backend"])


def plan_for(env, music=None, **kwargs):
    return rd.plan_additions(snapshot(env), music or apple(), **kwargs)


class TestPlanning:
    def test_missing_tracks_are_added_at_the_end(self, env):
        plan = plan_for(env)

        assert [(a.playlist_name, a.title, a.position) for a in plan.additions] == [("Warm", "E", 2)]
        assert plan.in_sync == ["Set"]

    def test_a_track_not_in_the_collection_is_reported_not_added(self, env):
        assert plan_for(env).not_in_collection == [("Warm", "D")]

    def test_apple_music_order_is_kept(self, env):
        music = apple([playlist("Warm", 5, 1, 2)])

        plan = plan_for(env, music)

        assert [(a.title, a.position) for a in plan.additions] == [("E", 2), ("A", 3)]

    def test_a_track_already_there_or_listed_twice_is_added_once(self, env):
        music = apple([playlist("Warm", 2, 5, 5)])

        assert [a.title for a in plan_for(env, music).additions] == ["E"]

    def test_a_playlist_rekordbox_does_not_have_is_reported_not_created(self, env):
        plan = plan_for(env, apple([playlist("Brand New", 1)]))

        assert plan.missing_playlists == ["Brand New"] and plan.additions == []

    def test_extras_are_reported_never_removed(self, env):
        plan = plan_for(env, apple([playlist("Extra", 1)]))

        assert plan.extra_in_rekordbox == {"Extra": 1} and plan.additions == []

    def test_playlists_can_be_limited_by_name(self, env):
        music = apple([playlist("Set", 1), playlist("Warm", 2, 5)])

        assert [a.playlist_name for a in plan_for(env, music, only=["warm"]).additions] == ["Warm"]
        assert [a.playlist_name for a in plan_for(env, music, only=["set"]).additions] == []

    def test_two_playlists_with_one_name_are_ambiguous_and_left_alone(self, env):
        state = db_state()
        state["playlists"].append({"id": "p9", "name": "warm", "songs": []})
        env["db"].write_text(json.dumps(state))

        plan = plan_for(env)

        assert plan.ambiguous == ["Warm"] and plan.additions == []

    def test_playlists_inside_folders_are_ignored(self, env):
        state = db_state()
        state["playlists"][1]["folders"] = ["Genres"]
        env["db"].write_text(json.dumps(state))

        plan = plan_for(env)

        assert plan.missing_playlists == ["Warm"] and plan.additions == []


class TestApply:
    def apply(self, env, music=None, **kwargs):
        music = music or apple()
        plan = rd.plan_additions(snapshot(env), music)
        return plan, rd.apply_plan(plan, env["db"], music, env["backend"], **kwargs)

    def test_the_track_is_added_and_nothing_else_changes(self, env):
        before = snapshot(env)

        _, result = self.apply(env)

        after = snapshot(env)
        by_name = {p.name: p.content_ids for p in after.playlists}
        assert by_name["Warm"] == ("c2", "c3") and result.added == 1
        assert {n: c for n, c in by_name.items() if n != "Warm"} == {p.name: p.content_ids for p in before.playlists if p.name != "Warm"}
        assert after.contents == before.contents

    def test_a_backup_of_every_database_file_is_made_first(self, env):
        original = {n: (env["folder"] / n).read_bytes() for n in ("master.db", "master.db-wal", "masterPlaylists6.xml")}

        _, result = self.apply(env)

        for name, data in original.items():
            assert (result.backup / name).read_bytes() == data
        assert result.backup.parent == env["home"] / "db-backups"

    def test_the_journal_records_the_added_row(self, env):
        _, result = self.apply(env)

        journal = json.loads(result.journal.read_text())
        (entry,) = journal["additions"]
        assert journal["state"] == "verified" and entry["playlist"] == "Warm" and entry["contentId"] == "c3"
        assert entry["songId"].startswith("new-") and result.journal.parent == env["home"] / "undo"

    def test_rekordbox_must_be_closed(self, env, monkeypatch):
        plan = plan_for(env)
        before = env["db"].read_bytes()
        monkeypatch.setattr(rd, "rekordbox_running", lambda: True)

        with pytest.raises(rd.DatabaseError, match="Rekordbox is running"):
            rd.apply_plan(plan, env["db"], apple(), env["backend"])

        assert env["db"].read_bytes() == before and not (env["home"] / "db-backups").exists()

    def test_a_plan_that_went_stale_is_refused(self, env):
        plan = plan_for(env)
        state = db_state()
        state["playlists"][1]["songs"].append(song(9, "c3"))  # someone added E meanwhile
        env["db"].write_text(json.dumps(state))
        before = env["db"].read_bytes()

        with pytest.raises(rd.DatabaseError, match="changed since"):
            rd.apply_plan(plan, env["db"], apple(), env["backend"])

        assert env["db"].read_bytes() == before

    def test_nothing_to_add_is_refused(self, env):
        music = apple([playlist("Set", 1, 2)])
        plan = rd.plan_additions(snapshot(env), music)

        with pytest.raises(rd.DatabaseError, match="Nothing to add"):
            rd.apply_plan(plan, env["db"], music, env["backend"])

    def test_a_failed_write_puts_the_backup_back(self, env):
        plan = plan_for(env)
        original = {n: (env["folder"] / n).read_bytes() for n in ("master.db", "master.db-wal", "masterPlaylists6.xml")}
        env["backend"].commit_raises = True

        with pytest.raises(rd.DatabaseError, match="backup was put back"):
            rd.apply_plan(plan, env["db"], apple(), env["backend"])

        assert {n: (env["folder"] / n).read_bytes() for n in original} == original  # the half-written file is gone
        (journal,) = (env["home"] / "undo").glob("*.json")
        assert json.loads(journal.read_text())["state"] == "failed"

    def test_damage_found_afterwards_puts_the_backup_back(self, env):
        plan = plan_for(env)
        original = env["db"].read_bytes()
        env["backend"].tamper = True

        with pytest.raises(rd.DatabaseError, match="Verification failed"):
            rd.apply_plan(plan, env["db"], apple(), env["backend"])

        assert env["db"].read_bytes() == original
        (journal,) = (env["home"] / "undo").glob("*.json")
        assert json.loads(journal.read_text())["state"] == "reverted"

    def test_a_backup_that_does_not_match_stops_everything(self, env, monkeypatch):
        plan = plan_for(env)
        before = env["db"].read_bytes()
        values = iter(range(100))
        monkeypatch.setattr(rd, "_digest", lambda path: next(values))

        with pytest.raises(rd.DatabaseError, match="doesn't match"):
            rd.apply_plan(plan, env["db"], apple(), env["backend"])

        assert env["db"].read_bytes() == before and not (env["home"] / "undo").exists()


class TestUndo:
    def run_sync(self, env):
        music = apple()
        plan = rd.plan_additions(snapshot(env), music)
        return rd.apply_plan(plan, env["db"], music, env["backend"])

    def test_the_added_row_is_removed(self, env):
        before = {p.id: p.content_ids for p in snapshot(env).playlists}
        result = self.run_sync(env)

        undone = rd.undo(result.journal, env["backend"])

        assert undone.removed == ["E in Warm"] and undone.skipped == []
        assert {p.id: p.content_ids for p in snapshot(env).playlists} == before

    def test_a_dry_run_changes_nothing(self, env):
        result = self.run_sync(env)
        added = env["db"].read_bytes()

        undone = rd.undo(result.journal, env["backend"], dry_run=True)

        assert undone.removed == ["E in Warm"] and env["db"].read_bytes() == added

    def test_a_row_that_is_gone_is_skipped(self, env):
        result = self.run_sync(env)
        rd.undo(result.journal, env["backend"])

        again = rd.undo(result.journal, env["backend"])

        assert again.removed == [] and again.skipped == [("E in Warm", "it is no longer there")]

    def test_a_row_that_now_holds_another_track_is_left_alone(self, env):
        result = self.run_sync(env)
        state = json.loads(env["db"].read_text())
        journal = json.loads(result.journal.read_text())
        next(p for p in state["playlists"] if p["name"] == "Warm")["songs"][-1][1] = "c4"
        env["db"].write_text(json.dumps(state))

        undone = rd.undo(result.journal, env["backend"])

        assert undone.removed == [] and "different track" in undone.skipped[0][1]

    def test_rekordbox_must_be_closed_to_undo(self, env, monkeypatch):
        result = self.run_sync(env)
        monkeypatch.setattr(rd, "rekordbox_running", lambda: True)

        with pytest.raises(rd.DatabaseError, match="Rekordbox is running"):
            rd.undo(result.journal, env["backend"])

        assert rd.undo(result.journal, env["backend"], dry_run=True).removed  # a dry run is fine

    def test_the_latest_journal_is_found(self, env):
        assert rd.latest_journal() is None
        result = self.run_sync(env)

        assert rd.latest_journal() == result.journal


class TestFindingTheDatabase:
    def test_it_comes_from_rekordbox_s_settings(self, tmp_path, monkeypatch):
        db = tmp_path / "Master" / "master.db"
        db.parent.mkdir()
        db.write_bytes(b"x")
        settings = tmp_path / "rekordbox3.settings"
        settings.write_text(f'<VALUE name="masterDbDirectory" val="{db.parent}"/>')
        monkeypatch.setattr(rd, "SETTINGS_FILE", settings)

        assert rd.find_database() == db

    def test_a_missing_database_is_a_clear_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rd, "SETTINGS_FILE", tmp_path / "nothing")
        monkeypatch.setattr(Path, "expanduser", lambda self: tmp_path / "nowhere" / self.name)

        with pytest.raises(rd.DatabaseError, match="master.db"):
            rd.find_database()


class TestTools:
    @pytest.fixture(autouse=True)
    def clean(self, env, tmp_path, monkeypatch):
        apple_music_service.clear_cache()
        server.reset_state()
        monkeypatch.setattr(rd, "default_backend", lambda: env["backend"])
        monkeypatch.setattr(rd, "find_database", lambda: env["db"])
        self.xml = write_library(
            tmp_path / "Library.xml",
            [music_track(1, "A", "X", "/Music/a.mp3"), music_track(2, "B", "X", "/Music/b.mp3"),
             music_track(4, "D", "X", "/Music/d.mp3"), music_track(5, "E", "X", "/Music/e.mp3")],
            [playlist("Set", 1, 2), playlist("Warm", 2, 4, 5)],
        )
        yield
        server.reset_state()

    def test_the_apple_music_library_is_needed(self):
        with pytest.raises(ToolError, match="load_apple_music_library"):
            server.sync_playlists_to_rekordbox()

    def test_a_dry_run_lists_what_would_happen(self, env):
        server.load_apple_music_library(str(self.xml))
        before = env["db"].read_bytes()

        result = server.sync_playlists_to_rekordbox()

        assert result["dryRun"] is True and result["additions"] == 1
        assert result["willAdd"] == [{"playlist": "Warm", "track": "E", "artist": "X", "position": 2}]
        assert result["notInTheRekordboxCollection"] == [{"playlist": "Warm", "track": "D"}]
        assert "Nothing has been written" in result["summary"] and env["db"].read_bytes() == before

    def test_applying_needs_the_expected_count(self, env):
        server.load_apple_music_library(str(self.xml))
        before = env["db"].read_bytes()

        for wrong in (None, 2):
            with pytest.raises(ToolError, match="expected_additions"):
                server.sync_playlists_to_rekordbox(dry_run=False, expected_additions=wrong)

        assert env["db"].read_bytes() == before

    def test_apply_then_undo(self, env):
        server.load_apple_music_library(str(self.xml))
        before = {p.id: p.content_ids for p in snapshot(env).playlists}

        done = server.sync_playlists_to_rekordbox(dry_run=False, expected_additions=1)

        assert done["added"] == 1 and "undo_rekordbox_playlist_sync" in done["summary"]
        assert {p.name: p.content_ids for p in snapshot(env).playlists}["Warm"] == ("c2", "c3")
        assert server.sync_playlists_to_rekordbox()["additions"] == 0  # and now it is in sync

        preview = server.undo_rekordbox_playlist_sync(done["journal"])
        assert preview["wouldRemove"] == ["E in Warm"]

        undone = server.undo_rekordbox_playlist_sync(done["journal"], dry_run=False)
        assert undone["removed"] == ["E in Warm"]
        assert {p.id: p.content_ids for p in snapshot(env).playlists} == before

    def test_undo_without_a_journal_is_a_clear_error(self):
        with pytest.raises(ToolError, match="No Rekordbox playlist journal"):
            server.undo_rekordbox_playlist_sync()

    def test_a_running_rekordbox_blocks_writing_but_not_the_dry_run(self, env, monkeypatch):
        server.load_apple_music_library(str(self.xml))
        monkeypatch.setattr(rd, "rekordbox_running", lambda: True)

        assert server.sync_playlists_to_rekordbox()["rekordboxIsRunning"] is True

        with pytest.raises(ToolError, match="Rekordbox is running"):
            server.sync_playlists_to_rekordbox(dry_run=False, expected_additions=1)
