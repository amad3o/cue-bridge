"""The safe-write layer for Rekordbox XML files (used by the playlist export).

The edits here are a harmless comment slipped into the collection; what is tested is how the
file is written: backups, atomic swap, stale plans, and never clobbering a copy.
"""

import os
from pathlib import Path

import pytest

from cuebridge.services import rekordbox_editor as editor
from cuebridge.services.rekordbox_editor import OutputPathError

FIXTURE = Path(__file__).parent / "fixtures" / "rekordbox_sample.xml"


@pytest.fixture
def source(tmp_path) -> Path:
    path = tmp_path / "rekordbox.xml"
    path.write_bytes(FIXTURE.read_bytes())
    return path


def plan_edit(source: Path, output_path=None, edit: bytes = b"<!-- edited -->") -> editor.EditPlan:
    path, data, stat = editor._read_source(source)
    target = editor.resolve_target(path, stat, output_path)

    return editor.EditPlan(target, data.replace(b"</COLLECTION>", edit + b"</COLLECTION>", 1))


def backups_of(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.backup-*"))


class TestInPlace:
    def test_edits_the_file_itself(self, source):
        result = editor.write_plan(plan_edit(source))

        assert result.path == source.resolve()
        assert b"<!-- edited -->" in source.read_bytes()

    def test_the_first_edit_saves_a_verified_backup(self, source):
        original = source.read_bytes()

        result = editor.write_plan(plan_edit(source))

        assert result.backup is not None
        assert result.backup.read_bytes() == original
        assert result.backup.name.startswith("rekordbox.xml.backup-")
        assert not result.backup.name.endswith(".xml")
        assert backups_of(source) == [result.backup]

    def test_plan_reports_that_a_backup_will_be_made(self, source):
        assert editor.needs_backup(plan_edit(source)) is True
        assert editor.needs_backup(plan_edit(source, output_path=source.parent / "copy.xml")) is False

    def test_every_edit_backs_up_the_file_as_it_was(self, source):
        original = source.read_bytes()
        editor.write_plan(plan_edit(source))
        after_first = source.read_bytes()

        second = editor.write_plan(plan_edit(source, edit=b"<!-- second -->"))

        assert second.backup.read_bytes() == after_first
        assert len(backups_of(source)) == 2
        assert backups_of(source)[0].read_bytes() == original

    def test_a_new_export_from_rekordbox_is_backed_up_again(self, source):
        editor.write_plan(plan_edit(source))
        source.write_bytes(FIXTURE.read_bytes())  # Rekordbox re-exported over it

        result = editor.write_plan(plan_edit(source))

        assert result.backup is not None
        assert len(backups_of(source)) == 2

    def test_two_backups_in_one_second_do_not_collide(self, source, monkeypatch):
        class Frozen:
            @staticmethod
            def now():
                from datetime import datetime
                return datetime(2026, 1, 2, 3, 4, 5)

        monkeypatch.setattr(editor, "datetime", Frozen)

        editor.write_plan(plan_edit(source))
        source.write_bytes(FIXTURE.read_bytes())
        editor.write_plan(plan_edit(source))

        assert [b.name for b in backups_of(source)] == [
            "rekordbox.xml.backup-20260102-030405",
            "rekordbox.xml.backup-20260102-030405-1",
        ]

    def test_file_permissions_are_kept(self, source):
        source.chmod(0o640)

        editor.write_plan(plan_edit(source))

        assert source.stat().st_mode & 0o777 == 0o640

    def test_no_temp_files_are_left_behind(self, source):
        editor.write_plan(plan_edit(source))

        assert [p.name for p in source.parent.iterdir() if p.name.startswith(".")] == []

    def test_planning_alone_never_touches_the_file_or_makes_a_backup(self, source):
        original = source.read_bytes()
        mtime = source.stat().st_mtime_ns

        plan_edit(source)

        assert source.read_bytes() == original and source.stat().st_mtime_ns == mtime
        assert backups_of(source) == []

    def test_writing_a_plan_that_changes_nothing_makes_no_backup(self, source):
        path, data, stat = editor._read_source(source)
        plan = editor.EditPlan(editor.resolve_target(path, stat, None), data)

        result = editor.write_plan(plan)

        assert result.backup is None and backups_of(source) == []

    def test_a_pending_playlist_note_follows_the_file(self, source):
        from cuebridge.services.rekordbox_service import has_pending_playlists, write_pending_note

        patched = source.read_bytes().replace(b"</PLAYLISTS>", b'  <NODE Name="Apple Music" Type="0" Count="0"></NODE>\n  </PLAYLISTS>')
        source.write_bytes(patched)
        write_pending_note(source, "Apple Music", patched)
        assert has_pending_playlists(source)

        editor.write_plan(plan_edit(source))

        assert has_pending_playlists(source)


class TestStalePlans:
    def test_refuses_if_the_file_changed_after_planning(self, source):
        plan = plan_edit(source)
        changed = source.read_bytes() + b"\n"
        source.write_bytes(changed)

        with pytest.raises(OutputPathError, match="changed since"):
            editor.write_plan(plan)

        assert source.read_bytes() == changed
        assert backups_of(source) == []

    def test_refuses_if_only_the_modification_time_changed(self, source):
        plan = plan_edit(source)
        os.utime(source, (1, 1))

        with pytest.raises(OutputPathError, match="changed since"):
            editor.write_plan(plan)

    def test_a_plan_cannot_be_applied_twice(self, source):
        plan = plan_edit(source)
        editor.write_plan(plan)

        with pytest.raises(OutputPathError, match="changed since"):
            editor.write_plan(plan)


class TestSaveElsewhere:
    def test_leaves_the_loaded_file_alone(self, source, tmp_path):
        original = source.read_bytes()

        plan = plan_edit(source, output_path=tmp_path / "copy.xml")
        result = editor.write_plan(plan)

        assert result.path == (tmp_path / "copy.xml").resolve() and result.backup is None
        assert source.read_bytes() == original
        assert backups_of(source) == []
        assert not plan.target.in_place
        assert b"<!-- edited -->" in (tmp_path / "copy.xml").read_bytes()

    def test_refuses_a_file_that_exists(self, source, tmp_path):
        existing = tmp_path / "existing.xml"
        existing.write_bytes(b"precious")

        with pytest.raises(OutputPathError, match="already exists"):
            plan_edit(source, output_path=existing)

        assert existing.read_bytes() == b"precious"

    def test_never_replaces_a_copy_it_wrote_earlier(self, source, tmp_path):
        target = tmp_path / "copy.xml"
        editor.write_plan(plan_edit(source, output_path=target))
        written = target.read_bytes()

        with pytest.raises(OutputPathError, match="already exists"):
            plan_edit(source, output_path=target)

        assert target.read_bytes() == written

    def test_refuses_a_file_that_appears_after_planning(self, source, tmp_path):
        target = tmp_path / "copy.xml"
        plan = plan_edit(source, output_path=target)
        target.write_bytes(b"precious")

        with pytest.raises(OutputPathError, match="already exists"):
            editor.write_plan(plan)

        assert target.read_bytes() == b"precious"

    def test_refuses_a_folder(self, source, tmp_path):
        folder = tmp_path / "folder.xml"
        folder.mkdir()

        with pytest.raises(OutputPathError, match="already exists"):
            plan_edit(source, output_path=folder)

    def test_output_must_be_xml(self, source, tmp_path):
        with pytest.raises(OutputPathError, match=".xml"):
            plan_edit(source, output_path=tmp_path / "out.txt")

    def test_missing_folder(self, source, tmp_path):
        with pytest.raises(OutputPathError, match="does not exist"):
            plan_edit(source, output_path=tmp_path / "nope" / "out.xml")

    def test_naming_the_source_as_the_output_is_the_same_as_in_place(self, source):
        assert plan_edit(source, output_path=source).target.in_place

    def test_naming_the_source_through_a_symlink_is_also_in_place(self, source, tmp_path):
        link = tmp_path / "link.xml"
        link.symlink_to(source)

        assert plan_edit(source, output_path=link).target.in_place
