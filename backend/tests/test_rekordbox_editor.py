import os
from pathlib import Path

import pytest

from cuebridge.services import rekordbox_editor as editor
from cuebridge.services.rekordbox_editor import OutputPathError
from cuebridge.services.rekordbox_service import (
    InvalidFileError,
    parse_rekordbox_bytes,
)

FIXTURE = Path(__file__).parent / "fixtures" / "rekordbox_sample.xml"


@pytest.fixture
def source(tmp_path) -> Path:
    path = tmp_path / "rekordbox.xml"
    path.write_bytes(FIXTURE.read_bytes())
    return path


def tracks_by_id(xml: bytes) -> dict:
    return {t.trackId: t for t in parse_rekordbox_bytes(xml)}


class TestPlan:
    def test_only_tracks_without_cues_change(self, source):
        plan = editor.build_default_cue_plan(source)

        assert plan.changed_track_ids == ("1001", "4001", "5001")
        assert plan.skipped_with_cues == 3

    def test_added_cue_is_hot_cue_a_at_zero(self, source):
        edited = tracks_by_id(editor.build_default_cue_plan(source).xml)

        for track_id in ("1001", "4001", "5001"):
            (cue,) = edited[track_id].cues
            assert (cue.cueType, cue.cueIndex, cue.timeMs) == ("hotcue", 0, 0)
            assert cue.trackTitle == edited[track_id].trackTitle
            assert cue.filePath == edited[track_id].filePath

    def test_other_tracks_are_unchanged(self, source):
        before = tracks_by_id(source.read_bytes())
        after = tracks_by_id(editor.build_default_cue_plan(source).xml)

        for track_id in ("2001", "3001", "3002"):
            assert after[track_id] == before[track_id]

    def test_track_details_survive(self, source):
        after = tracks_by_id(editor.build_default_cue_plan(source).xml)

        assert after["4001"].trackTitle == "Café del Mar & Friends"
        assert after["4001"].artist == "José González"
        assert after["4001"].filePath == "/Users/dj/Music/Café & More/José.mp3"

    def test_custom_position(self, source):
        plan = editor.build_default_cue_plan(source, position_ms=1500)

        assert tracks_by_id(plan.xml)["1001"].cues[0].timeMs == 1500

    def test_other_xml_sections_are_kept(self, source):
        xml = editor.build_default_cue_plan(source).xml.decode()

        assert 'Version="1.0.0"' in xml
        assert "<PRODUCT" in xml and "<PLAYLISTS>" in xml
        assert xml.count("<TEMPO") == 4  # unchanged: 2 + 1 + 1
        assert xml.count("<POSITION_MARK") == 6 + 3  # 6 originally, 3 added

    def test_running_on_the_result_changes_nothing(self, source, tmp_path):
        first = editor.build_default_cue_plan(source)
        copy = tmp_path / "copy.xml"
        copy.write_bytes(first.xml)

        second = editor.build_default_cue_plan(copy)

        assert second.changed_track_ids == ()
        assert second.skipped_with_cues == 6

    def test_source_file_is_not_modified(self, source):
        original = source.read_bytes()

        editor.build_default_cue_plan(source)

        assert source.read_bytes() == original

    def test_invalid_source_is_rejected(self, tmp_path):
        with pytest.raises(InvalidFileError):
            editor.build_default_cue_plan(tmp_path / "missing.xml")

    def test_fade_and_load_markers_do_not_count_as_cues(self, tmp_path):
        xml = (
            b'<DJ_PLAYLISTS><COLLECTION><TRACK TrackID="1" Name="A">'
            b'<POSITION_MARK Type="1" Start="1.0" Num="-1"/>'
            b'<POSITION_MARK Type="3" Start="0.0" Num="-1"/>'
            b"</TRACK></COLLECTION></DJ_PLAYLISTS>"
        )
        path = tmp_path / "rb.xml"
        path.write_bytes(xml)

        plan = editor.build_default_cue_plan(path)

        assert plan.changed_track_ids == ("1",)
        assert plan.xml.count(b"<POSITION_MARK") == 3  # both originals are kept

    def test_loop_counts_as_a_cue(self, tmp_path):
        xml = (
            b'<DJ_PLAYLISTS><COLLECTION><TRACK TrackID="1" Name="A">'
            b'<POSITION_MARK Type="4" Start="1.0" End="2.0" Num="-1"/>'
            b"</TRACK></COLLECTION></DJ_PLAYLISTS>"
        )
        path = tmp_path / "rb.xml"
        path.write_bytes(xml)

        assert editor.build_default_cue_plan(path).changed_track_ids == ()

    def test_track_without_id_is_still_edited(self, tmp_path):
        path = tmp_path / "rb.xml"
        path.write_bytes(b'<DJ_PLAYLISTS><COLLECTION><TRACK Name="A"/></COLLECTION></DJ_PLAYLISTS>')

        plan = editor.build_default_cue_plan(path)

        assert plan.changed_track_ids == ("idx0",)
        assert len(tracks_by_id(plan.xml)["idx0"].cues) == 1


class TestHotCueDuplication:
    def test_plan_counts(self, source):
        plan = editor.build_hot_cue_duplication_plan(source)

        # Track 2001: memory cues at 0:00 and 1:07 take free slots B and D
        # (A holds a hot cue, C a hot loop). Tracks 3001 and 3002: slot A each.
        assert plan.changed_track_ids == ("2001", "3001", "3002")
        assert plan.cues_added == 4
        assert plan.already_duplicated == 0
        assert plan.overflow == ()

    def test_memory_cues_are_kept_and_hot_cues_added(self, source):
        before = tracks_by_id(source.read_bytes())
        after = tracks_by_id(editor.build_hot_cue_duplication_plan(source).xml)

        for track_id in ("2001", "3001", "3002"):
            assert after[track_id].cues[: len(before[track_id].cues)] == before[track_id].cues

        new = after["2001"].cues[len(before["2001"].cues) :]
        assert sorted((c.cueIndex, c.timeMs, c.cueType) for c in new) == [
            (1, 0, "hotcue"), (3, 67043, "hotcue"),
        ]

    def test_other_tracks_are_unchanged(self, source):
        before = tracks_by_id(source.read_bytes())
        after = tracks_by_id(editor.build_hot_cue_duplication_plan(source).xml)

        for track_id in ("1001", "4001", "5001"):
            assert after[track_id] == before[track_id]

    def test_cue_names_are_copied(self, tmp_path):
        path = tmp_path / "rb.xml"
        path.write_bytes(
            b'<DJ_PLAYLISTS><COLLECTION><TRACK TrackID="1" Name="A">'
            b'<POSITION_MARK Name="Intro" Type="0" Start="1.5" Num="-1"/>'
            b"</TRACK></COLLECTION></DJ_PLAYLISTS>"
        )

        cues = tracks_by_id(editor.build_hot_cue_duplication_plan(path).xml)["1"].cues

        assert [(c.cueType, c.cueName, c.timeMs) for c in cues] == [
            ("memory", "Intro", 1500), ("hotcue", "Intro", 1500),
        ]

    def test_running_on_the_result_changes_nothing(self, source, tmp_path):
        first = editor.build_hot_cue_duplication_plan(source)
        copy = tmp_path / "copy.xml"
        copy.write_bytes(first.xml)

        second = editor.build_hot_cue_duplication_plan(copy)

        assert second.changed_track_ids == () and second.cues_added == 0
        assert second.already_duplicated == 4

    def test_overflow_is_reported_per_track(self, tmp_path):
        marks = "".join(f'<POSITION_MARK Type="0" Start="{i + 1}" Num="-1"/>' for i in range(10))
        path = tmp_path / "rb.xml"
        path.write_bytes(
            f'<DJ_PLAYLISTS><COLLECTION><TRACK TrackID="7" Name="A">{marks}</TRACK>'
            "</COLLECTION></DJ_PLAYLISTS>".encode()
        )

        plan = editor.build_hot_cue_duplication_plan(path)

        assert plan.cues_added == 8 and plan.overflow == (("7", 2),)

    def test_source_file_is_not_modified(self, source):
        original = source.read_bytes()

        editor.build_hot_cue_duplication_plan(source)

        assert source.read_bytes() == original


def backups_of(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.backup-*"))


def cuebridge_files_in(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


class TestInPlace:
    def test_edits_the_file_itself(self, source):
        result = editor.write_plan(editor.build_default_cue_plan(source))

        assert result.path == source.resolve()
        assert len(tracks_by_id(source.read_bytes())["1001"].cues) == 1

    def test_first_edit_of_a_fresh_export_saves_a_verified_backup(self, source):
        original = source.read_bytes()

        result = editor.write_plan(editor.build_default_cue_plan(source))

        assert result.backup is not None
        assert result.backup.read_bytes() == original
        assert result.backup.name.startswith("rekordbox.xml.backup-")
        assert not result.backup.name.endswith(".xml")
        assert backups_of(source) == [result.backup]

    def test_plan_reports_that_a_backup_will_be_made(self, source):
        assert editor.needs_backup(editor.build_default_cue_plan(source)) is True

        editor.write_plan(editor.build_default_cue_plan(source))

        assert editor.needs_backup(editor.build_hot_cue_duplication_plan(source)) is True

    def test_every_edit_backs_up_the_file_as_it_was(self, source):
        original = source.read_bytes()
        editor.write_plan(editor.build_default_cue_plan(source))
        after_first = source.read_bytes()

        second = editor.write_plan(editor.build_hot_cue_duplication_plan(source))

        assert second.backup.read_bytes() == after_first
        assert len(backups_of(source)) == 2
        assert backups_of(source)[0].read_bytes() == original

    def test_a_new_export_from_rekordbox_is_backed_up_again(self, source):
        editor.write_plan(editor.build_default_cue_plan(source))
        source.write_bytes(FIXTURE.read_bytes())  # Rekordbox re-exported over it

        result = editor.write_plan(editor.build_hot_cue_duplication_plan(source))

        assert result.backup is not None
        assert len(backups_of(source)) == 2

    def test_two_backups_in_one_second_do_not_collide(self, source, monkeypatch):
        class Frozen:
            @staticmethod
            def now():
                from datetime import datetime
                return datetime(2026, 1, 2, 3, 4, 5)

        monkeypatch.setattr(editor, "datetime", Frozen)

        editor.write_plan(editor.build_default_cue_plan(source))
        source.write_bytes(FIXTURE.read_bytes())
        editor.write_plan(editor.build_default_cue_plan(source))

        assert [b.name for b in backups_of(source)] == [
            "rekordbox.xml.backup-20260102-030405",
            "rekordbox.xml.backup-20260102-030405-1",
        ]

    def test_edits_add_up_in_the_same_file(self, source):
        editor.write_plan(editor.build_default_cue_plan(source))
        editor.write_plan(editor.build_hot_cue_duplication_plan(source))

        cues = {t: c.cues for t, c in tracks_by_id(source.read_bytes()).items()}

        for track_id in ("1001", "4001", "5001"):
            assert [(c.cueType, c.cueIndex, c.timeMs) for c in cues[track_id]] == [("hotcue", 0, 0)]
        assert sorted(c.cueIndex for c in cues["2001"] if c.cueType == "hotcue") == [0, 1, 3]
        assert sorted(c.cueIndex for c in cues["3001"] if c.cueType == "hotcue") == [0]

    def test_order_does_not_matter(self, source, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        second = other / "rekordbox.xml"
        second.write_bytes(source.read_bytes())

        editor.write_plan(editor.build_default_cue_plan(source))
        editor.write_plan(editor.build_hot_cue_duplication_plan(source))
        editor.write_plan(editor.build_hot_cue_duplication_plan(second))
        editor.write_plan(editor.build_default_cue_plan(second))

        assert tracks_by_id(source.read_bytes()) == tracks_by_id(second.read_bytes())

    def test_repeating_either_edit_changes_nothing(self, source):
        editor.write_plan(editor.build_default_cue_plan(source))
        editor.write_plan(editor.build_hot_cue_duplication_plan(source))

        assert editor.build_default_cue_plan(source).changed_track_ids == ()
        assert editor.build_hot_cue_duplication_plan(source).changed_track_ids == ()

    def test_no_marker_or_comment_is_added(self, source):
        editor.write_plan(editor.build_default_cue_plan(source))
        editor.write_plan(editor.build_hot_cue_duplication_plan(source))

        assert b"<!--" not in source.read_bytes() or b"<!--" in FIXTURE.read_bytes()

    def test_file_permissions_are_kept(self, source):
        source.chmod(0o640)

        editor.write_plan(editor.build_default_cue_plan(source))

        assert source.stat().st_mode & 0o777 == 0o640

    def test_no_temp_files_are_left_behind(self, source):
        editor.write_plan(editor.build_default_cue_plan(source))

        assert [n for n in cuebridge_files_in(source.parent) if n.startswith(".")] == []

    def test_planning_alone_never_touches_the_file_or_makes_a_backup(self, source):
        original = source.read_bytes()
        mtime = source.stat().st_mtime_ns

        editor.build_default_cue_plan(source)
        editor.build_hot_cue_duplication_plan(source)

        assert source.read_bytes() == original and source.stat().st_mtime_ns == mtime
        assert backups_of(source) == []


class TestStalePlans:
    def test_refuses_if_the_file_changed_after_planning(self, source):
        plan = editor.build_default_cue_plan(source)
        changed = source.read_bytes() + b"\n"
        source.write_bytes(changed)

        with pytest.raises(OutputPathError, match="changed since"):
            editor.write_plan(plan)

        assert source.read_bytes() == changed
        assert backups_of(source) == []

    def test_refuses_if_only_the_modification_time_changed(self, source):
        plan = editor.build_default_cue_plan(source)
        os.utime(source, (1, 1))

        with pytest.raises(OutputPathError, match="changed since"):
            editor.write_plan(plan)

    def test_a_plan_cannot_be_applied_twice(self, source):
        plan = editor.build_default_cue_plan(source)
        editor.write_plan(plan)

        with pytest.raises(OutputPathError, match="changed since"):
            editor.write_plan(plan)


class TestSaveElsewhere:
    def test_leaves_the_loaded_file_alone(self, source, tmp_path):
        original = source.read_bytes()

        plan = editor.build_default_cue_plan(source, output_path=tmp_path / "copy.xml")
        result = editor.write_plan(plan)

        assert result.path == (tmp_path / "copy.xml").resolve() and result.backup is None
        assert source.read_bytes() == original
        assert backups_of(source) == []
        assert not plan.target.in_place

    def test_written_copy_loads_as_a_library(self, source, tmp_path):
        editor.write_plan(editor.build_default_cue_plan(source, output_path=tmp_path / "copy.xml"))

        assert len(tracks_by_id((tmp_path / "copy.xml").read_bytes())["1001"].cues) == 1

    def test_refuses_a_file_that_exists(self, source, tmp_path):
        existing = tmp_path / "existing.xml"
        existing.write_bytes(b"precious")

        with pytest.raises(OutputPathError, match="already exists"):
            editor.build_default_cue_plan(source, output_path=existing)

        assert existing.read_bytes() == b"precious"

    def test_never_replaces_a_copy_it_wrote_earlier(self, source, tmp_path):
        target = tmp_path / "copy.xml"
        editor.write_plan(editor.build_default_cue_plan(source, output_path=target))
        written = target.read_bytes()

        with pytest.raises(OutputPathError, match="already exists"):
            editor.build_hot_cue_duplication_plan(source, output_path=target)

        assert target.read_bytes() == written

    def test_refuses_a_file_that_appears_after_planning(self, source, tmp_path):
        target = tmp_path / "copy.xml"
        plan = editor.build_default_cue_plan(source, output_path=target)
        target.write_bytes(b"precious")

        with pytest.raises(OutputPathError, match="already exists"):
            editor.write_plan(plan)

        assert target.read_bytes() == b"precious"

    def test_refuses_a_folder(self, source, tmp_path):
        folder = tmp_path / "folder.xml"
        folder.mkdir()

        with pytest.raises(OutputPathError, match="already exists"):
            editor.build_default_cue_plan(source, output_path=folder)

    def test_output_must_be_xml(self, source, tmp_path):
        with pytest.raises(OutputPathError, match=".xml"):
            editor.build_default_cue_plan(source, output_path=tmp_path / "out.txt")

    def test_missing_folder(self, source, tmp_path):
        with pytest.raises(OutputPathError, match="does not exist"):
            editor.build_default_cue_plan(source, output_path=tmp_path / "nope" / "out.xml")

    def test_naming_the_source_as_the_output_is_the_same_as_in_place(self, source):
        plan = editor.build_default_cue_plan(source, output_path=source)

        assert plan.target.in_place

    def test_naming_the_source_through_a_symlink_is_also_in_place(self, source, tmp_path):
        link = tmp_path / "link.xml"
        link.symlink_to(source)

        assert editor.build_default_cue_plan(source, output_path=link).target.in_place


CRLF_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\r\n\r\n'
    '<DJ_PLAYLISTS Version="1.0.0">\r\n'
    '  <COLLECTION Entries="4">\r\n'
    '    <TRACK TrackID="1" Name="Plain &amp; Simple &gt; Fine" Artist="A"\r\n'
    '           Location="file://localhost/Volumes/X/a.wav">\r\n'
    '      <TEMPO Inizio="0.100" Bpm="120.00" Metro="4/4" Battito="1"/>\r\n'
    '    </TRACK>\r\n'
    '    <TRACK TrackID="2" Name="Empty" Artist="B"/>\r\n'
    '    <TRACK TrackID="3" Name="Has cue" Artist="C">\r\n'
    '      <POSITION_MARK Name="" Type="0" Start="1.000" Num="0" Red="40" Green="226" Blue="20"/>\r\n'
    '    </TRACK>\r\n'
    '    <TRACK TrackID="4" Name="One line" Artist="D"><TEMPO Inizio="0.1" Bpm="100.00" Metro="4/4" Battito="1"/></TRACK>\r\n'
    "  </COLLECTION>\r\n"
    "  <PLAYLISTS>\r\n"
    '    <NODE Type="0" Name="ROOT" Count="1">\r\n'
    '      <NODE Name="p" Type="1" KeyType="0" Entries="1">\r\n'
    '        <TRACK Key="1"/>\r\n'
    "      </NODE>\r\n"
    "    </NODE>\r\n"
    "  </PLAYLISTS>\r\n"
    "</DJ_PLAYLISTS>"
).encode()


class TestTextPatching:
    @pytest.fixture
    def crlf(self, tmp_path) -> Path:
        path = tmp_path / "rb.xml"
        path.write_bytes(CRLF_XML)
        return path

    def test_every_original_line_is_kept_in_order(self, crlf):
        plan = editor.build_default_cue_plan(crlf)

        original = CRLF_XML.split(b"\r\n")
        edited = plan.xml.split(b"\r\n")
        position = 0

        for line in original:
            if line in (b'    <TRACK TrackID="2" Name="Empty" Artist="B"/>',
                        b'    <TRACK TrackID="4" Name="One line" Artist="D"><TEMPO Inizio="0.1" Bpm="100.00" Metro="4/4" Battito="1"/></TRACK>'):
                continue  # these two have to change shape to take a child
            position = edited.index(line, position) + 1

    def test_the_declaration_line_endings_and_escaping_are_untouched(self, crlf):
        edited = editor.build_default_cue_plan(crlf).xml

        assert edited.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\r\n\r\n<DJ_PLAYLISTS')
        assert b"\n" not in edited.replace(b"\r\n", b"")
        assert b'Name="Plain &amp; Simple &gt; Fine"' in edited
        assert b"<!--" not in edited

    def test_added_lines_are_indented_inside_the_track(self, crlf):
        edited = editor.build_default_cue_plan(crlf).xml

        assert (
            b'      <TEMPO Inizio="0.100" Bpm="120.00" Metro="4/4" Battito="1"/>\r\n'
            b'      <POSITION_MARK Name="" Type="0" Start="0.000" Num="0"/>\r\n'
            b"    </TRACK>\r\n"
        ) in edited

    def test_a_self_closing_track_gets_a_closing_tag(self, crlf):
        edited = editor.build_default_cue_plan(crlf).xml

        assert (
            b'    <TRACK TrackID="2" Name="Empty" Artist="B">\r\n'
            b'      <POSITION_MARK Name="" Type="0" Start="0.000" Num="0"/>\r\n'
            b"    </TRACK>\r\n"
        ) in edited

    def test_a_track_on_one_line_is_broken_up(self, crlf):
        plan = editor.build_default_cue_plan(crlf)

        assert len(tracks_by_id(plan.xml)["4"].cues) == 1
        assert b'<TEMPO Inizio="0.1" Bpm="100.00" Metro="4/4" Battito="1"/>\r\n' in plan.xml

    def test_the_playlists_section_is_untouched(self, crlf):
        edited = editor.build_default_cue_plan(crlf).xml

        assert edited[edited.index(b"  <PLAYLISTS>"):] == CRLF_XML[CRLF_XML.index(b"  <PLAYLISTS>"):]

    def test_a_greater_than_sign_inside_an_attribute_is_not_taken_for_the_end_of_the_tag(self, tmp_path):
        xml = b'<DJ_PLAYLISTS><COLLECTION Entries="1"><TRACK TrackID="1" Name="a > b"/></COLLECTION></DJ_PLAYLISTS>'
        path = tmp_path / "rb.xml"
        path.write_bytes(xml)

        edited = editor.build_default_cue_plan(path).xml

        assert b'Name="a > b">' in edited
        assert len(tracks_by_id(edited)["1"].cues) == 1

    def test_cue_names_are_escaped(self, tmp_path):
        xml = (
            b'<DJ_PLAYLISTS><COLLECTION Entries="1"><TRACK TrackID="1" Name="a">\n'
            b'  <POSITION_MARK Name="Drop &amp; &quot;go&quot;" Type="0" Start="5.000" Num="-1"/>\n'
            b"</TRACK></COLLECTION></DJ_PLAYLISTS>"
        )
        path = tmp_path / "rb.xml"
        path.write_bytes(xml)

        plan = editor.build_hot_cue_duplication_plan(path)

        assert b'Name="Drop &amp; &quot;go&quot;" Type="0" Start="5.000" Num="0"' in plan.xml
        assert tracks_by_id(plan.xml)["1"].cues[-1].cueName == 'Drop & "go"'

    def test_a_pending_playlist_note_follows_the_file(self, crlf):
        from cuebridge.services.rekordbox_service import has_pending_playlists, write_pending_note

        patched = CRLF_XML.replace(b"</PLAYLISTS>", b'  <NODE Name="Apple Music" Type="0" Count="0"></NODE>\r\n  </PLAYLISTS>')
        crlf.write_bytes(patched)
        write_pending_note(crlf, "Apple Music", patched)
        assert has_pending_playlists(crlf)

        editor.write_plan(editor.build_default_cue_plan(crlf))

        assert has_pending_playlists(crlf)

    def test_writing_a_plan_that_changes_nothing_makes_no_backup(self, source):
        editor.write_plan(editor.build_default_cue_plan(source))
        plan = editor.build_default_cue_plan(source)

        result = editor.write_plan(plan)

        assert result.backup is None
        assert len(backups_of(source)) == 1
