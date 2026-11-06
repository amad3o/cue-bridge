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

    def delete_content(self, content_id):
        content = next(c for c in self.state["contents"] if c["id"] == content_id)
        cues = [c for c in self.state.get("cues", []) if c[0] == content_id]
        songs = [[p["id"], s[0], s[1]] for p in self.state["playlists"] for s in p["songs"] if s[1] == content_id]
        self.state["contents"].remove(content)
        self.state["cues"] = [c for c in self.state.get("cues", []) if c[0] != content_id]

        for p in self.state["playlists"]:
            p["songs"] = [s for s in p["songs"] if s[1] != content_id]

        return {"content": content, "cues": cues, "songs": songs}

    def restore_content(self, payload):
        self.state["contents"].append(payload["content"])
        self.state.setdefault("cues", []).extend(payload["cues"])
        existing = {p["id"]: p for p in self.state["playlists"]}

        for playlist_id, song_id, content_id in payload["songs"]:
            if playlist_id in existing:
                existing[playlist_id]["songs"].append([song_id, content_id])

    def copy_cues(self, from_id, to_id, cue_ids):
        made = []

        for cue_id in cue_ids:
            source = next(c for c in self.state["cues"] if c[3] == cue_id and c[0] == from_id)
            self.counter += 1
            made.append(f"copy-{self.counter}")
            self.state["cues"].append([to_id, source[1], source[2], made[-1]])

        return {"cueIds": made}

    def remove_copied_cues(self, to_id, token):
        self.state["cues"] = [c for c in self.state["cues"] if not (c[0] == to_id and c[3] in token["cueIds"])]

    def create_playlist(self, name):
        self.counter += 1
        playlist_id = f"new-playlist-{self.backend.runs}-{self.counter}"
        self.state["playlists"].append({"id": playlist_id, "name": name, "songs": []})
        return playlist_id

    def delete_playlist(self, playlist_id):
        self.state["playlists"] = [p for p in self.state["playlists"] if p["id"] != playlist_id]

    def relocate(self, content_id, path, file_name, org_path):
        content = next(c for c in self.state["contents"] if c["id"] == content_id)
        content.update(path=path, file_name=file_name, org_path=org_path)

    def commit(self):
        if self.backend.tamper:
            self.state["playlists"][0]["songs"].clear()  # damage something that was not part of the plan

        if getattr(self.backend, "tamper_cues", False):
            self.state.setdefault("cues", []).append(["c1", 1, 123, "stray"])  # a cue nobody planned

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
        contents = tuple(rd.DbContent(c["id"], c["title"], c["artist"], c["path"], None, c.get("file_name"), c.get("org_path")) for c in state["contents"])
        playlists = tuple(
            rd.DbPlaylist(
                p["id"], p["name"], None, p.get("folder", False), False, tuple(p.get("folders", ())),
                tuple(s[0] for s in p["songs"]), tuple(s[1] for s in p["songs"]),
            )
            for p in state["playlists"]
        )
        cues = tuple(rd.DbCue(c[0], c[1], c[2], c[3] if len(c) > 3 else "") for c in state.get("cues", []))
        return rd.DbSnapshot(contents, playlists, cues)

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


@pytest.fixture
def files(tmp_path, env):
    music = tmp_path / "music"
    music.mkdir()
    real = {name: music / name for name in ("1-01 Old.mp3", "1-02 Twice.mp3", "03 Same.mp3")}

    for path in real.values():
        path.write_bytes(b"audio")

    state = {
        "contents": [
            {"id": "c1", "title": "Old", "artist": "X", "path": str(music / "01 Old.mp3"),
             "file_name": "01 Old.mp3", "org_path": str(music / "01 Old.mp3")},
            # Rekordbox already has a row for the real file of this one.
            {"id": "c2", "title": "Twice", "artist": "X", "path": str(music / "02 Twice.mp3"),
             "file_name": "02 Twice.mp3", "org_path": str(music / "02 Twice.mp3")},
            {"id": "c3", "title": "Twice", "artist": "X", "path": str(real["1-02 Twice.mp3"]),
             "file_name": "1-02 Twice.mp3", "org_path": str(real["1-02 Twice.mp3"])},
            {"id": "c4", "title": "Nowhere", "artist": "X", "path": str(music / "gone.mp3"),
             "file_name": "gone.mp3", "org_path": str(music / "gone.mp3")},
            # Imported from another folder, then moved: the import location stays as it was.
            {"id": "c5", "title": "Same", "artist": "X", "path": str(music / "old-03.mp3"),
             "file_name": "old-03.mp3", "org_path": "/Users/me/Downloads/03.mp3"},
            {"id": "c6", "title": "Fine", "artist": "X", "path": str(real["03 Same.mp3"]),
             "file_name": "03 Same.mp3", "org_path": str(real["03 Same.mp3"])},
        ],
        "playlists": [{"id": "p1", "name": "Set", "songs": [song(1, "c1"), song(2, "c2"), song(3, "c5")]}],
    }
    env["db"].write_text(json.dumps(state))
    tracks = [
        music_track(1, "Old", "X", real["1-01 Old.mp3"]),
        music_track(2, "Twice", "X", real["1-02 Twice.mp3"]),
        music_track(5, "Same", "X", real["03 Same.mp3"]),
    ]
    # c5's real file is the one c6 already points at, so c5 is a duplicate, not a fix.
    return {"music": parse_apple_music_plist(library_bytes(tracks, [playlist("Set", 1, 2, 5)])), "real": real}



class TestRelocation:
    """Tracks whose file was renamed, pointed at the file Apple Music has."""

    def plan(self, env, files):
        return rd.plan_relocations(snapshot(env), files["music"])

    def test_a_renamed_file_is_pointed_at_the_real_one(self, env, files):
        plan = self.plan(env, files)

        (fix,) = plan.relocations
        assert fix.content_id == "c1" and fix.new_path == str(files["real"]["1-01 Old.mp3"])
        assert fix.new_file_name == "1-01 Old.mp3"
        assert fix.new_org_path == fix.new_path  # the import location was the old path, so it follows

    def test_a_real_file_another_row_already_has_is_not_pointed_at_twice(self, env, files):
        plan = self.plan(env, files)

        assert sorted(t for t, _ in plan.already_in_collection) == ["Same", "Twice"]

    def test_a_row_for_the_same_file_spelled_with_other_case_or_accents_counts_too(self, env, files):
        real = str(files["real"]["1-01 Old.mp3"])
        state = json.loads(env["db"].read_text())
        # Rekordbox has the real file under a differently cased folder, and the name composed the other way.
        state["contents"].append({"id": "c9", "title": "Old", "artist": "X", "path": real.upper(),
                                  "file_name": "1-01 OLD.MP3", "org_path": real.upper()})
        env["db"].write_text(json.dumps(state))

        plan = self.plan(env, files)

        assert plan.relocations == []
        assert "Old" in [t for t, _ in plan.already_in_collection]

    def test_two_missing_rows_for_one_real_file_are_not_both_pointed_at_it(self, env, files):
        state = json.loads(env["db"].read_text())
        state["contents"].append({"id": "c8", "title": "Old", "artist": "X", "path": "/gone/01 Old.mp3",
                                  "file_name": "01 Old.mp3", "org_path": "/gone/01 Old.mp3"})
        env["db"].write_text(json.dumps(state))

        plan = self.plan(env, files)

        assert [r.content_id for r in plan.relocations] == ["c1"]

    def test_a_track_apple_music_has_no_file_for_is_reported(self, env, files):
        assert [t for t, _ in self.plan(env, files).no_match] == ["Nowhere"]

    def test_the_import_location_stays_when_it_was_somewhere_else(self, tmp_path, env, files):
        state = json.loads(env["db"].read_text())
        state["contents"] = [c for c in state["contents"] if c["id"] != "c6"]
        env["db"].write_text(json.dumps(state))

        plan = self.plan(env, files)

        fix = next(r for r in plan.relocations if r.content_id == "c5")
        assert fix.new_path == str(files["real"]["03 Same.mp3"])
        assert fix.new_org_path == "/Users/me/Downloads/03.mp3"

    def apply(self, env, files):
        return rd.apply_relocations(self.plan(env, files), env["db"], files["music"], env["backend"])

    def test_only_the_path_and_file_name_change(self, env, files):
        before = snapshot(env)

        self.apply(env, files)

        after = snapshot(env)
        changed = [(b, a) for b, a in zip(before.contents, after.contents) if b != a]
        assert [b.id for b, _ in changed] == ["c1"]
        assert changed[0][1].path == str(files["real"]["1-01 Old.mp3"])
        assert changed[0][1].file_name == "1-01 Old.mp3"
        assert (changed[0][1].title, changed[0][1].artist) == (changed[0][0].title, changed[0][0].artist)
        assert after.playlists == before.playlists

    def test_a_backup_and_journal_are_made(self, env, files):
        result = self.apply(env, files)

        assert (result.backup / "master.db").is_file()
        journal = json.loads(result.journal.read_text())
        assert journal["state"] == "verified" and journal["relocations"][0]["oldFileName"] == "01 Old.mp3"
        assert result.journal.name.startswith("rekordbox-paths-")

    def test_rekordbox_must_be_closed(self, env, files, monkeypatch):
        monkeypatch.setattr(rd, "rekordbox_running", lambda: True)
        before = env["db"].read_bytes()

        with pytest.raises(rd.DatabaseError, match="running|open"):
            self.apply(env, files)

        assert env["db"].read_bytes() == before

    def test_a_plan_that_went_stale_is_refused(self, env, files):
        plan = self.plan(env, files)
        files["real"]["1-01 Old.mp3"].unlink()

        with pytest.raises(rd.DatabaseError, match="changed since"):
            rd.apply_relocations(plan, env["db"], files["music"], env["backend"])

    def test_nothing_to_fix_is_refused(self, env, files):
        with pytest.raises(rd.DatabaseError, match="Nothing to fix"):
            rd.apply_relocations(rd.RelocationPlan(), env["db"], files["music"], env["backend"])

    def test_a_failed_write_puts_the_backup_back(self, env, files):
        before = env["db"].read_bytes()
        env["backend"].commit_raises = True

        with pytest.raises(rd.DatabaseError, match="backup was put back"):
            self.apply(env, files)

        assert env["db"].read_bytes() == before

    def test_damage_found_afterwards_puts_the_backup_back(self, env, files):
        before = env["db"].read_bytes()
        env["backend"].tamper = True

        with pytest.raises(rd.DatabaseError, match="Verification failed"):
            self.apply(env, files)

        assert env["db"].read_bytes() == before

    def test_undo_puts_the_old_path_back(self, env, files):
        original = snapshot(env)
        result = self.apply(env, files)

        outcome = rd.undo_relocations(result.journal, env["backend"])

        assert outcome.removed == ["Old"]
        assert snapshot(env) == original

    def test_undo_leaves_a_track_whose_path_changed_since(self, env, files):
        result = self.apply(env, files)
        state = json.loads(env["db"].read_text())
        state["contents"][0]["path"] = "/somewhere/else.mp3"
        env["db"].write_text(json.dumps(state))

        outcome = rd.undo_relocations(result.journal, env["backend"])

        assert outcome.removed == [] and outcome.skipped[0][0] == "Old"
        assert snapshot(env).contents[0].path == "/somewhere/else.mp3"

    def test_undo_dry_run_changes_nothing(self, env, files):
        result = self.apply(env, files)
        fixed = env["db"].read_bytes()

        outcome = rd.undo_relocations(result.journal, env["backend"], dry_run=True)

        assert outcome.removed == ["Old"] and env["db"].read_bytes() == fixed

    def test_a_playlist_journal_is_not_a_location_journal(self, env, tmp_path):
        journal = tmp_path / "playlists.json"
        journal.write_text(json.dumps({"database": str(env["db"]), "additions": []}))

        with pytest.raises(rd.DatabaseError, match="not a file-location journal"):
            rd.undo_relocations(journal, env["backend"])


class TestStaleDuplicates:
    def pairs(self, env, files, cues=(), extra_playlists=()):
        state = json.loads(env["db"].read_text())
        state["cues"] = list(cues)
        state["playlists"] += list(extra_playlists)
        env["db"].write_text(json.dumps(state))
        snap = snapshot(env)

        return {p.broken.content_id: p for p in rd.describe_duplicates(rd.plan_relocations(snap, files["music"]), snap)}

    def test_a_broken_row_with_nothing_the_good_row_lacks_is_safe_to_remove(self, env, files):
        pairs = self.pairs(
            env, files, cues=[("c2", 1, 5000), ("c3", 1, 5010)],
            extra_playlists=[{"id": "p9", "name": "Set", "songs": [song(9, "c3")]}],
        )

        assert sorted(pairs) == ["c2", "c5"]
        assert pairs["c2"].verdict == "safe_to_remove" and pairs["c2"].good.content_id == "c3"

    def test_a_cue_only_the_broken_row_has_needs_a_merge(self, env, files):
        pairs = self.pairs(env, files, cues=[("c2", 1, 5000), ("c2", 0, 9000), ("c3", 1, 5000)])

        assert pairs["c2"].cues_only_on_broken == 1 and pairs["c2"].verdict == "needs_merge"
        assert (pairs["c2"].broken.hot_cues, pairs["c2"].broken.memory_cues) == (1, 1)

    def test_the_same_position_in_another_slot_is_not_the_same_cue(self, env, files):
        pairs = self.pairs(env, files, cues=[("c2", 1, 5000), ("c3", 2, 5000)])

        assert pairs["c2"].cues_only_on_broken == 1

    def test_a_playlist_only_the_broken_row_is_in_needs_a_merge(self, env, files):
        pairs = self.pairs(env, files)

        # c2 sits in "Set" and c3 does not, so removing c2 would drop it from the playlist.
        assert pairs["c2"].playlists_only_on_broken == ("Set",)
        assert pairs["c2"].verdict == "needs_merge"

    def test_a_playlist_both_rows_are_in_is_fine(self, env, files):
        pairs = self.pairs(env, files, extra_playlists=[{"id": "p9", "name": "Set", "songs": [song(9, "c3")]}])

        assert pairs["c2"].playlists_only_on_broken == ()

    def test_the_tool_reports_without_changing_anything(self, env, files, tmp_path, monkeypatch):
        apple_music_service.clear_cache()
        server.reset_state()
        monkeypatch.setattr(rd, "default_backend", lambda: env["backend"])
        monkeypatch.setattr(rd, "find_database", lambda: env["db"])
        server.load_apple_music_library(str(write_library(tmp_path / "Lib.xml", [
            music_track(2, "Twice", "X", files["real"]["1-02 Twice.mp3"]),
        ], [playlist("Set", 2)])))
        before = env["db"].read_bytes()

        result = server.report_stale_duplicates()

        server.reset_state()
        assert result["pairs"] == 1 and result["details"][0]["track"] == "Twice"
        assert result["details"][0]["goodRow"]["id"] == "c3"
        assert env["db"].read_bytes() == before


class TestReviewPlaylists:
    @pytest.fixture
    def music(self, files):
        return files["music"]

    def plan(self, env, music):
        return rd.plan_review_playlists(snapshot(env), music)

    def apply(self, env, music):
        return rd.apply_review_playlists(self.plan(env, music), env["db"], music, env["backend"])

    def by_name(self, env):
        return {p.name: p for p in snapshot(env).playlists}

    def test_broken_rows_with_a_twin_are_listed_by_what_removing_them_would_lose(self, env, music):
        plan = self.plan(env, music)

        # c2 is in the playlist "Set" and its twin is not, so it needs a look; c5 is in "Set" too.
        assert [r[0] for r in plan.safe] == []
        assert sorted(r[0] for r in plan.merge) == ["c2", "c5"]

    def test_a_row_that_loses_nothing_is_in_the_safe_playlist(self, env, music):
        state = json.loads(env["db"].read_text())
        state["playlists"][0]["songs"] = []  # neither broken row is in any playlist now
        env["db"].write_text(json.dumps(state))

        plan = self.plan(env, music)

        assert sorted(r[0] for r in plan.safe) == ["c2", "c5"] and plan.merge == []

    def test_only_two_playlists_are_added_and_nothing_else_changes(self, env, music):
        before = snapshot(env)

        self.apply(env, music)

        after = snapshot(env)
        assert after.contents == before.contents and after.cues == before.cues
        assert [p for p in after.playlists if p.name not in (rd.SAFE_PLAYLIST, rd.MERGE_PLAYLIST)] == list(before.playlists)
        assert set(self.by_name(env)) - {p.name for p in before.playlists} == {rd.MERGE_PLAYLIST}
        assert sorted(self.by_name(env)[rd.MERGE_PLAYLIST].content_ids) == ["c2", "c5"]

    def test_existing_review_playlists_stop_a_new_run(self, env, music):
        self.apply(env, music)

        with pytest.raises(rd.DatabaseError, match="already has a playlist"):
            self.plan(env, music)

    def test_rekordbox_must_be_closed(self, env, music, monkeypatch):
        plan = self.plan(env, music)
        monkeypatch.setattr(rd, "rekordbox_running", lambda: True)
        before = env["db"].read_bytes()

        with pytest.raises(rd.DatabaseError):
            rd.apply_review_playlists(plan, env["db"], music, env["backend"])

        assert env["db"].read_bytes() == before

    def test_a_failed_write_puts_the_backup_back(self, env, music):
        before = env["db"].read_bytes()
        env["backend"].commit_raises = True

        with pytest.raises(rd.DatabaseError, match="backup was put back"):
            self.apply(env, music)

        assert env["db"].read_bytes() == before

    def test_damage_found_afterwards_puts_the_backup_back(self, env, music):
        before = env["db"].read_bytes()
        env["backend"].tamper = True

        with pytest.raises(rd.DatabaseError, match="Verification failed"):
            self.apply(env, music)

        assert env["db"].read_bytes() == before

    def test_a_stale_plan_is_refused(self, env, music):
        plan = self.plan(env, music)
        state = json.loads(env["db"].read_text())
        state["contents"] = [c for c in state["contents"] if c["id"] != "c2"]
        env["db"].write_text(json.dumps(state))

        with pytest.raises(rd.DatabaseError, match="changed since"):
            rd.apply_review_playlists(plan, env["db"], music, env["backend"])

    def test_undo_removes_the_playlists_and_no_tracks(self, env, music):
        original = snapshot(env)
        result = self.apply(env, music)

        outcome = rd.undo_review_playlists(result.journal, env["backend"])

        assert outcome.removed == [rd.MERGE_PLAYLIST]
        assert snapshot(env) == original

    def test_undo_works_after_rekordbox_removed_the_tracks(self, env, music):
        result = self.apply(env, music)
        state = json.loads(env["db"].read_text())
        state["contents"] = [c for c in state["contents"] if c["id"] not in ("c2", "c5")]
        for playlist_ in state["playlists"]:
            playlist_["songs"] = [x for x in playlist_["songs"] if x[1] not in ("c2", "c5")]
        env["db"].write_text(json.dumps(state))

        outcome = rd.undo_review_playlists(result.journal, env["backend"])

        assert outcome.removed == [rd.MERGE_PLAYLIST]
        assert rd.MERGE_PLAYLIST not in self.by_name(env)

    def test_undo_leaves_a_playlist_that_gained_other_tracks(self, env, music):
        result = self.apply(env, music)
        state = json.loads(env["db"].read_text())
        next(p for p in state["playlists"] if p["name"] == rd.MERGE_PLAYLIST)["songs"].append(["x", "c1"])
        env["db"].write_text(json.dumps(state))

        outcome = rd.undo_review_playlists(result.journal, env["backend"])

        assert outcome.removed == [] and outcome.skipped[0][0] == rd.MERGE_PLAYLIST
        assert rd.MERGE_PLAYLIST in self.by_name(env)

    def test_undo_leaves_a_renamed_playlist(self, env, music):
        result = self.apply(env, music)
        state = json.loads(env["db"].read_text())
        next(p for p in state["playlists"] if p["name"] == rd.MERGE_PLAYLIST)["name"] = "My set"
        env["db"].write_text(json.dumps(state))

        outcome = rd.undo_review_playlists(result.journal, env["backend"])

        assert outcome.removed == [] and "My set" in self.by_name(env)

    def test_a_location_journal_is_not_a_review_journal(self, env, tmp_path):
        journal = tmp_path / "x.json"
        journal.write_text(json.dumps({"database": str(env["db"]), "relocations": []}))

        with pytest.raises(rd.DatabaseError, match="not a review-playlist journal"):
            rd.undo_review_playlists(journal, env["backend"])


@pytest.fixture
def cleanup_env(env, files):
    """c2 and c5 are broken rows with twins c3 and c6. c2 holds nothing its twin lacks; c5 has cues c6 lacks."""
    state = json.loads(env["db"].read_text())
    state["playlists"] = [
        {"id": "p1", "name": "Set", "songs": [song(1, "c1"), song(3, "c6")]},
        {"id": "pr", "name": rd.SAFE_PLAYLIST, "songs": [["r1", "c2"], ["r2", "c5"]]},
    ]
    state["cues"] = [["c5", 1, 1000, "k1"], ["c5", 0, 1000, "k2"], ["c3", 1, 7000, "k3"], ["c2", 1, 7010, "k4"]]
    env["db"].write_text(json.dumps(state))
    return files["music"]


def key(snap):
    return (
        sorted((c.id, c.path) for c in snap.contents),
        sorted((c.content_id, c.kind, c.position_ms) for c in snap.cues),
        sorted((p.id, p.name, tuple(sorted(zip(p.song_ids, p.content_ids)))) for p in snap.playlists),
    )


class TestCleanup:
    def plan(self, env, music):
        return rd.plan_cleanup(snapshot(env), music)

    def apply(self, env, music):
        return rd.apply_cleanup(self.plan(env, music), env["db"], music, env["backend"])

    def test_rows_that_lose_nothing_are_removed_and_rows_with_cues_get_them_copied(self, env, cleanup_env):
        plan = self.plan(env, cleanup_env)

        assert [r.broken_id for r in plan.removals] == ["c2"]
        assert [(m.broken_id, m.good_id, m.cue_ids) for m in plan.merges] == [("c5", "c6", ("k1", "k2"))]
        assert plan.skipped == []

    def test_a_cue_the_twin_already_has_is_not_copied(self, env, cleanup_env):
        state = json.loads(env["db"].read_text())
        state["cues"].append(["c6", 1, 1010, "k5"])  # same slot, 10 ms apart
        env["db"].write_text(json.dumps(state))

        (merge,) = self.plan(env, cleanup_env).merges

        assert merge.cue_ids == ("k2",)

    def test_a_row_in_a_real_playlist_its_twin_is_not_in_is_left_alone(self, env, cleanup_env):
        state = json.loads(env["db"].read_text())
        state["playlists"][0]["songs"].append(["x", "c2"])
        env["db"].write_text(json.dumps(state))

        plan = self.plan(env, cleanup_env)

        assert plan.removals == [] and plan.skipped[0][0] == "Twice"

    def test_the_cues_are_copied_the_row_is_removed_and_nothing_else_changes(self, env, cleanup_env):
        before = snapshot(env)

        self.apply(env, cleanup_env)

        after = snapshot(env)
        assert [c.id for c in after.contents] == [c.id for c in before.contents if c.id != "c2"]
        assert sorted((c.content_id, c.kind, c.position_ms) for c in after.cues if c.content_id == "c6") == [("c6", 0, 1000), ("c6", 1, 1000)]
        assert [c for c in after.cues if c.content_id == "c5"] == [c for c in before.cues if c.content_id == "c5"]
        assert not any(c.content_id == "c2" for c in after.cues)
        review = next(p for p in after.playlists if p.name == rd.SAFE_PLAYLIST)
        assert review.content_ids == ("c5",)
        assert next(p for p in after.playlists if p.name == "Set").content_ids == ("c1", "c6")

    def test_a_backup_and_a_journal_holding_the_removed_row_are_made(self, env, cleanup_env):
        result = self.apply(env, cleanup_env)

        journal = json.loads(result.journal.read_text())
        assert journal["state"] == "verified" and result.journal.name.startswith("rekordbox-cleanup-")
        assert journal["removals"][0]["payload"]["content"]["id"] == "c2"
        assert (result.backup / "master.db").is_file()

    def test_rekordbox_must_be_closed(self, env, cleanup_env, monkeypatch):
        plan = self.plan(env, cleanup_env)
        monkeypatch.setattr(rd, "rekordbox_running", lambda: True)
        before = env["db"].read_bytes()

        with pytest.raises(rd.DatabaseError):
            rd.apply_cleanup(plan, env["db"], cleanup_env, env["backend"])

        assert env["db"].read_bytes() == before

    def test_a_stale_plan_is_refused(self, env, cleanup_env):
        plan = self.plan(env, cleanup_env)
        state = json.loads(env["db"].read_text())
        state["cues"].append(["c2", 1, 99999, "k9"])  # now the broken row has a cue its twin lacks
        env["db"].write_text(json.dumps(state))

        with pytest.raises(rd.DatabaseError, match="changed since"):
            rd.apply_cleanup(plan, env["db"], cleanup_env, env["backend"])

    def test_nothing_to_clean_up_is_refused(self, env, cleanup_env):
        with pytest.raises(rd.DatabaseError, match="Nothing to clean up"):
            rd.apply_cleanup(rd.CleanupPlan(), env["db"], cleanup_env, env["backend"])

    def test_a_failed_write_puts_the_backup_back(self, env, cleanup_env):
        before = env["db"].read_bytes()
        env["backend"].commit_raises = True

        with pytest.raises(rd.DatabaseError, match="backup was put back"):
            self.apply(env, cleanup_env)

        assert env["db"].read_bytes() == before

    def test_damage_found_afterwards_puts_the_backup_back(self, env, cleanup_env):
        before = env["db"].read_bytes()
        env["backend"].tamper = True

        with pytest.raises(rd.DatabaseError, match="Verification failed"):
            self.apply(env, cleanup_env)

        assert env["db"].read_bytes() == before

    def test_an_unplanned_cue_found_afterwards_puts_the_backup_back(self, env, cleanup_env):
        before = env["db"].read_bytes()
        env["backend"].tamper_cues = True

        with pytest.raises(rd.DatabaseError, match="cues aren't exactly as planned"):
            self.apply(env, cleanup_env)

        assert env["db"].read_bytes() == before

    def test_undo_puts_everything_back(self, env, cleanup_env):
        original = key(snapshot(env))
        result = self.apply(env, cleanup_env)

        outcome = rd.undo_cleanup(result.journal, env["backend"])

        assert len(outcome.removed) == 2
        assert key(snapshot(env)) == original

    def test_undo_dry_run_changes_nothing(self, env, cleanup_env):
        result = self.apply(env, cleanup_env)
        done = env["db"].read_bytes()

        outcome = rd.undo_cleanup(result.journal, env["backend"], dry_run=True)

        assert len(outcome.removed) == 2 and env["db"].read_bytes() == done

    def test_undo_does_not_restore_a_row_whose_id_is_taken(self, env, cleanup_env):
        result = self.apply(env, cleanup_env)
        state = json.loads(env["db"].read_text())
        state["contents"].append({"id": "c2", "title": "New", "artist": "Y", "path": "/new.mp3"})
        env["db"].write_text(json.dumps(state))

        outcome = rd.undo_cleanup(result.journal, env["backend"])

        assert any("already there" in why for _, why in outcome.skipped)
        assert next(c for c in snapshot(env).contents if c.id == "c2").title == "New"

    def test_undo_skips_playlist_entries_of_a_playlist_deleted_since(self, env, cleanup_env):
        result = self.apply(env, cleanup_env)
        state = json.loads(env["db"].read_text())
        state["playlists"] = [p for p in state["playlists"] if p["id"] != "pr"]
        env["db"].write_text(json.dumps(state))

        rd.undo_cleanup(result.journal, env["backend"])

        assert any(c.id == "c2" for c in snapshot(env).contents)
        assert all(p.id != "pr" for p in snapshot(env).playlists)

    def test_other_journals_are_not_cleanup_journals(self, env, tmp_path):
        journal = tmp_path / "x.json"
        journal.write_text(json.dumps({"database": str(env["db"]), "kind": "review"}))

        with pytest.raises(rd.DatabaseError, match="not a cleanup journal"):
            rd.undo_cleanup(journal, env["backend"])


class TestCleanupTools:
    @pytest.fixture(autouse=True)
    def clean(self, env, cleanup_env, tmp_path, files, monkeypatch):
        apple_music_service.clear_cache()
        server.reset_state()
        monkeypatch.setattr(rd, "default_backend", lambda: env["backend"])
        monkeypatch.setattr(rd, "find_database", lambda: env["db"])
        tracks = [
            music_track(2, "Twice", "X", files["real"]["1-02 Twice.mp3"]),
            music_track(5, "Same", "X", files["real"]["03 Same.mp3"]),
        ]
        server.load_apple_music_library(str(write_library(tmp_path / "Lib.xml", tracks, [playlist("Set", 2, 5)])))
        yield
        server.reset_state()

    def test_a_dry_run_lists_what_would_happen(self, env):
        before = env["db"].read_bytes()

        result = server.clean_up_stale_duplicates()

        assert (result["removals"], result["cueCopies"]) == (1, 1)
        assert result["willCopyCues"] == [{"track": "Same", "cues": 2}]
        assert env["db"].read_bytes() == before

    def test_applying_needs_both_counts(self):
        with pytest.raises(ToolError, match="expected_removals"):
            server.clean_up_stale_duplicates(dry_run=False, expected_removals=1)

    def test_apply_then_undo(self, env):
        original = key(snapshot(env))

        done = server.clean_up_stale_duplicates(dry_run=False, expected_removals=1, expected_cue_copies=1)
        preview = server.undo_stale_duplicate_cleanup(done["journal"])
        server.undo_stale_duplicate_cleanup(done["journal"], dry_run=False)

        assert "undo_stale_duplicate_cleanup" in done["summary"] and len(preview["wouldUndo"]) == 2
        assert key(snapshot(env)) == original

    def test_undo_without_a_journal_is_a_clear_error(self):
        with pytest.raises(ToolError, match="No cleanup journal"):
            server.undo_stale_duplicate_cleanup()


class TestRelocationTools:
    @pytest.fixture(autouse=True)
    def clean(self, env, files, monkeypatch):
        apple_music_service.clear_cache()
        server.reset_state()
        monkeypatch.setattr(rd, "default_backend", lambda: env["backend"])
        monkeypatch.setattr(rd, "find_database", lambda: env["db"])
        monkeypatch.setattr(server, "_apple_path", None, raising=False)
        self.files = files
        yield
        server.reset_state()

    @pytest.fixture
    def loaded(self, files, tmp_path):
        tracks = [
            music_track(1, "Old", "X", files["real"]["1-01 Old.mp3"]),
            music_track(2, "Twice", "X", files["real"]["1-02 Twice.mp3"]),
        ]
        path = write_library(tmp_path / "Lib.xml", tracks, [playlist("Set", 1, 2)])
        server.load_apple_music_library(str(path))

    def test_a_dry_run_lists_the_fixes_and_the_rest(self, loaded):
        result = server.fix_moved_files_in_rekordbox()

        assert result["dryRun"] is True and result["fixes"] == 1
        assert result["willFix"][0]["track"] == "Old"
        assert result["alreadyInTheCollection"] == 1 and result["noFileFound"] == 2

    def test_applying_needs_the_expected_count(self, loaded):
        with pytest.raises(ToolError, match="expected_fixes"):
            server.fix_moved_files_in_rekordbox(dry_run=False, expected_fixes=5)

    def test_apply_then_undo(self, loaded, env):
        original = env["db"].read_bytes()

        done = server.fix_moved_files_in_rekordbox(dry_run=False, expected_fixes=1)
        preview = server.undo_rekordbox_file_fix(done["journal"])
        undone = server.undo_rekordbox_file_fix(done["journal"], dry_run=False)

        assert done["fixed"] == 1 and "undo_rekordbox_file_fix" in done["summary"]
        assert preview["wouldRestore"] == ["Old"]
        assert undone["restored"] == ["Old"]
        assert json.loads(env["db"].read_text()) == json.loads(original)

    def test_undo_without_a_journal_is_a_clear_error(self):
        with pytest.raises(ToolError, match="No file-location journal"):
            server.undo_rekordbox_file_fix()
