import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from cuebridge import mcp_server as server
from cuebridge.services import rekordbox_cue_writes as cw, rekordbox_db as rd
from test_rekordbox_db import FakeBackend, db_state

# Kinds as Rekordbox stores them: 0 memory, 1-3 A-C, 5-9 D-H; 4 and 10+ are unknown to CueBridge.


def state_with(cues):
    state = db_state()  # c1, c2 and c4 are in playlists; c3 ("E") is in none
    state["cues"] = [[c, k, ms, f"cue{n}"] for n, (c, k, ms) in enumerate(cues)]
    return state


@pytest.fixture
def env(tmp_path, monkeypatch):
    folder = tmp_path / "Master"
    folder.mkdir()
    db = folder / "master.db"
    (folder / "master.db-wal").write_bytes(b"wal-bytes")
    (folder / "masterPlaylists6.xml").write_text("<playlists/>")
    monkeypatch.setenv("CUEBRIDGE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(rd, "rekordbox_running", lambda: False)
    monkeypatch.setattr(rd, "default_backend", lambda: backend)
    monkeypatch.setattr(rd, "find_database", lambda: db)
    backend = FakeBackend()

    def write(cues):
        db.write_text(json.dumps(state_with(cues)))

    write([("c1", 1, 0)])
    return {"db": db, "backend": backend, "home": tmp_path / "home", "write": write}


def snap(env):
    return rd.read_snapshot(env["db"], env["backend"])


def cues_of(env, content_id):
    return sorted((c.kind, c.position_ms) for c in snap(env).cues if c.content_id == content_id)


class TestDefaultCuePlan:
    def test_playlisted_tracks_without_any_cue_get_hot_cue_a_at_zero(self, env):
        plan = cw.plan_default_cues(snap(env))

        assert [(a.content_id, a.cues) for a in plan.additions] == [("c2", ((1, 0),)), ("c4", ((1, 0),))]
        assert plan.already_done == 1  # c1 has a cue

    def test_a_track_in_no_playlist_is_left_out_unless_asked(self, env):
        assert cw.plan_default_cues(snap(env)).not_playlisted == 1
        assert "c3" in [a.content_id for a in cw.plan_default_cues(snap(env), playlisted_only=False).additions]

    def test_any_kind_of_cue_counts_as_having_one(self, env):
        env["write"]([("c1", 0, 500), ("c2", 4, 100), ("c4", 10, 100)])

        assert cw.plan_default_cues(snap(env)).additions == []

    def test_a_folder_or_smart_playlist_does_not_count_as_a_playlist(self, env):
        state = state_with([])
        state["playlists"] = [{"id": "f", "name": "Folder", "folder": True, "songs": [["s1", "c1"]]}]
        env["db"].write_text(json.dumps(state))

        plan = cw.plan_default_cues(snap(env))

        assert plan.additions == [] and plan.not_playlisted == 4


class TestMemoryToHotPlan:
    def test_a_memory_cue_gets_the_lowest_free_slot(self, env):
        env["write"]([("c1", 0, 1000), ("c1", 0, 5000), ("c1", 1, 1000)])  # A is at 1000 already

        plan = cw.plan_memory_to_hot_cues(snap(env))

        assert [(a.content_id, a.cues) for a in plan.additions] == [("c1", ((2, 5000),))]  # kind 2 is slot B
        assert plan.already_done == 1

    def test_slots_follow_the_kind_numbering(self, env):
        env["write"]([("c1", 0, t) for t in (1000, 2000, 3000, 4000)] + [("c1", 1, 9000), ("c1", 2, 9500), ("c1", 3, 9900)])

        (addition,) = cw.plan_memory_to_hot_cues(snap(env)).additions

        assert addition.cues == ((5, 1000), (6, 2000), (7, 3000), (8, 4000))  # D, E, F, G

    def test_a_memory_cue_with_no_free_slot_is_reported(self, env):
        hot = [("c1", kind, 100 * kind) for kind in (1, 2, 3, 5, 6, 7, 8, 9)]
        env["write"](hot + [("c1", 0, 7777)])

        plan = cw.plan_memory_to_hot_cues(snap(env))

        assert plan.additions == [] and plan.no_free_slot == [("A", 1)]

    def test_a_track_with_a_cue_in_an_unknown_slot_is_skipped_and_reported(self, env):
        env["write"]([("c1", 0, 1000), ("c1", 4, 2000), ("c2", 0, 1000)])

        plan = cw.plan_memory_to_hot_cues(snap(env))

        assert plan.skipped_unreadable == ["A"] and [a.content_id for a in plan.additions] == ["c2"]

    def test_a_loop_is_not_copied(self, env):
        state = state_with([("c1", 0, 1000)])
        state["cues"].append(["c2", 0, 1000, "loop"])
        env["db"].write_text(json.dumps(state))
        original = env["backend"].read

        def read(path):
            snapshot = original(path)
            cues = tuple(rd.DbCue(c.content_id, c.kind, c.position_ms, c.id, 5000 if c.id == "loop" else None) for c in snapshot.cues)
            return rd.DbSnapshot(snapshot.contents, snapshot.playlists, cues)

        env["backend"].read = read

        assert [a.content_id for a in cw.plan_memory_to_hot_cues(snap(env)).additions] == ["c1"]

    def test_unplaylisted_tracks_are_left_out_unless_asked(self, env):
        env["write"]([("c3", 0, 1000)])

        assert cw.plan_memory_to_hot_cues(snap(env)).additions == []
        assert [a.content_id for a in cw.plan_memory_to_hot_cues(snap(env), playlisted_only=False).additions] == ["c3"]


def apply(env, plan_function=cw.plan_default_cues, **kwargs):
    plan = plan_function(snap(env), **kwargs)
    return plan, cw.apply_cue_plan(plan, env["db"], env["backend"], lambda s: plan_function(s, **kwargs))


class TestApply:
    def test_the_cues_are_added_and_nothing_else_changes(self, env):
        before = snap(env)

        plan, result = apply(env)
        after = snap(env)

        assert result.added == 2 and cues_of(env, "c2") == [(1, 0)] and cues_of(env, "c4") == [(1, 0)]
        assert after.contents == before.contents and after.playlists == before.playlists
        assert cues_of(env, "c1") == [(1, 0)]

    def test_running_it_again_has_nothing_to_add(self, env):
        apply(env)

        assert cw.plan_default_cues(snap(env)).additions == []
        with pytest.raises(rd.DatabaseError, match="no cues"):
            apply(env)

    def test_memory_cues_become_hot_cues_and_stay(self, env):
        env["write"]([("c1", 0, 1000), ("c1", 0, 5000)])

        apply(env, cw.plan_memory_to_hot_cues)

        assert cues_of(env, "c1") == [(0, 1000), (0, 5000), (1, 1000), (2, 5000)]

    def test_a_journal_and_a_backup_are_made_and_the_journal_ends_verified(self, env):
        _, result = apply(env)

        journal = json.loads(result.journal.read_text())

        assert journal["kind"] == "cues" and journal["state"] == "verified"
        assert [a["contentId"] for a in journal["additions"]] == ["c2", "c4"]
        assert (result.backup / "master.db").is_file()

    def test_rekordbox_must_be_closed(self, env, monkeypatch):
        monkeypatch.setattr(rd, "rekordbox_running", lambda: True)
        before = env["db"].read_bytes()

        with pytest.raises(rd.DatabaseError, match="closed|running|quit"):
            apply(env)

        assert env["db"].read_bytes() == before

    def test_a_database_that_changed_since_planning_is_refused(self, env):
        plan = cw.plan_default_cues(snap(env))
        env["write"]([("c1", 1, 0), ("c2", 1, 0)])  # someone added a cue meanwhile
        before = env["db"].read_bytes()

        with pytest.raises(rd.DatabaseError, match="changed since"):
            cw.apply_cue_plan(plan, env["db"], env["backend"], cw.plan_default_cues)

        assert env["db"].read_bytes() == before

    def test_a_failed_write_puts_the_backup_back(self, env):
        env["backend"].commit_raises = True
        before = env["db"].read_bytes()

        with pytest.raises(rd.DatabaseError, match="backup was put back"):
            apply(env)

        assert env["db"].read_bytes() == before

    def test_a_write_that_does_something_else_too_is_caught_and_reverted(self, env):
        env["backend"].tamper_cues = True
        before = env["db"].read_bytes()

        with pytest.raises(rd.DatabaseError, match="Verification failed"):
            apply(env)

        assert env["db"].read_bytes() == before

    def test_a_write_that_damages_a_playlist_is_caught_and_reverted(self, env):
        env["backend"].tamper = True  # the fake empties a playlist when it commits
        before = env["db"].read_bytes()

        with pytest.raises(rd.DatabaseError, match="playlists"):
            apply(env)

        assert env["db"].read_bytes() == before

    def test_a_write_that_loses_a_track_is_caught_and_reverted(self, env):
        env["backend"].tamper_contents = True
        before = env["db"].read_bytes()

        with pytest.raises(rd.DatabaseError, match="track list"):
            apply(env)

        assert env["db"].read_bytes() == before


class TestUndo:
    def test_the_added_cues_come_off_again(self, env):
        original = env["db"].read_bytes()
        _, result = apply(env)

        outcome = cw.undo_cue_additions(result.journal, env["backend"])

        assert len(outcome.removed) == 2 and outcome.skipped == []
        assert cues_of(env, "c2") == [] and cues_of(env, "c1") == [(1, 0)]
        assert json.loads(env["db"].read_text())["cues"] == json.loads(original)["cues"]

    def test_a_dry_run_changes_nothing(self, env):
        _, result = apply(env)
        before = env["db"].read_bytes()

        outcome = cw.undo_cue_additions(result.journal, env["backend"], dry_run=True)

        assert len(outcome.removed) == 2 and env["db"].read_bytes() == before

    def test_undoing_twice_skips_what_is_already_gone(self, env):
        _, result = apply(env)
        cw.undo_cue_additions(result.journal, env["backend"])

        outcome = cw.undo_cue_additions(result.journal, env["backend"])

        assert outcome.removed == [] and len(outcome.skipped) == 2

    def test_a_cue_the_user_removed_in_rekordbox_is_not_a_problem(self, env):
        _, result = apply(env)
        state = json.loads(env["db"].read_text())
        state["cues"] = [c for c in state["cues"] if c[0] != "c2"]
        env["db"].write_text(json.dumps(state))

        outcome = cw.undo_cue_additions(result.journal, env["backend"])

        assert [s[0] for s in outcome.skipped] == ["1 cue(s) on B"] and cues_of(env, "c4") == []

    def test_rekordbox_must_be_closed(self, env, monkeypatch):
        _, result = apply(env)
        monkeypatch.setattr(rd, "rekordbox_running", lambda: True)

        with pytest.raises(rd.DatabaseError):
            cw.undo_cue_additions(result.journal, env["backend"])

    def test_another_kind_of_journal_is_refused(self, env, tmp_path):
        other = tmp_path / "other.json"
        other.write_text(json.dumps({"kind": "paths"}))

        with pytest.raises(rd.DatabaseError, match="not a cue journal"):
            cw.undo_cue_additions(other, env["backend"])


class TestTools:
    def test_dry_run_is_the_default_and_writes_nothing(self, env):
        before = env["db"].read_bytes()

        result = server.set_default_cue()

        assert result["dryRun"] is True and result["tracksToChange"] == 2 and result["cuesToAdd"] == 2
        assert result["tracksSkippedForBeingInNoPlaylist"] == 1
        assert result["examples"][0]["cues"] == [{"slot": "A", "positionMs": 0}]
        assert "Nothing has been written" in result["summary"] and env["db"].read_bytes() == before

    def test_applying_needs_the_count_from_the_dry_run(self, env):
        with pytest.raises(ToolError, match="expected_cues"):
            server.set_default_cue(dry_run=False)

        with pytest.raises(ToolError, match="expected_cues"):
            server.set_default_cue(dry_run=False, expected_cues=3)

        assert cues_of(env, "c2") == []

    def test_applying_with_the_right_count_writes_and_names_the_undo_tool(self, env):
        result = server.set_default_cue(dry_run=False, expected_cues=2)

        assert cues_of(env, "c2") == [(1, 0)]
        assert "undo_rekordbox_cue_additions" in result["summary"] and result["journal"]

    def test_a_second_run_has_nothing_to_do(self, env):
        server.set_default_cue(dry_run=False, expected_cues=2)

        result = server.set_default_cue()

        assert result["tracksToChange"] == 0 and "Nothing to do" in result["summary"]

    def test_duplicate_memory_cues_reports_overflow_and_applies(self, env):
        env["write"]([("c1", 0, 1000), ("c1", 0, 5000), ("c2", 0, 700)])

        dry = server.duplicate_memory_cues_to_hot_cues()
        done = server.duplicate_memory_cues_to_hot_cues(dry_run=False, expected_cues=dry["cuesToAdd"])

        assert dry["cuesToAdd"] == 3 and dry["tracksToChange"] == 2
        assert dry["examples"][0]["cues"] == [{"slot": "A", "positionMs": 1000}, {"slot": "B", "positionMs": 5000}]
        assert cues_of(env, "c2") == [(0, 700), (1, 700)] and "undo" in done["summary"]

    def test_a_running_rekordbox_blocks_writing_but_not_the_dry_run(self, env, monkeypatch):
        monkeypatch.setattr(rd, "rekordbox_running", lambda: True)

        dry = server.set_default_cue()

        assert dry["rekordboxIsRunning"] is True and "Quit Rekordbox" in dry["summary"]
        with pytest.raises(ToolError):
            server.set_default_cue(dry_run=False, expected_cues=2)

    def test_the_undo_tool_uses_the_latest_cue_journal(self, env):
        server.set_default_cue(dry_run=False, expected_cues=2)

        preview = server.undo_rekordbox_cue_additions()
        done = server.undo_rekordbox_cue_additions(dry_run=False)

        assert preview["dryRun"] is True and len(preview["wouldUndo"]) == 2
        assert len(done["undone"]) == 2 and cues_of(env, "c2") == []

    def test_the_undo_tool_without_a_journal_says_so(self, env):
        with pytest.raises(ToolError, match="No cue journal"):
            server.undo_rekordbox_cue_additions()
