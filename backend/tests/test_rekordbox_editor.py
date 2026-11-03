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


class TestWrite:
    def test_default_output_path(self, source):
        plan = editor.build_default_cue_plan(source)

        written = editor.write_plan(plan)

        assert written == source.with_name("rekordbox.with-default-cues.xml")
        assert written.read_bytes() == plan.xml

    def test_custom_output_path(self, source, tmp_path):
        plan = editor.build_default_cue_plan(source)

        written = editor.write_plan(plan, tmp_path / "custom.xml")

        assert written.exists()

    def test_written_file_loads_as_a_library(self, source):
        written = editor.write_plan(editor.build_default_cue_plan(source))

        assert len(tracks_by_id(written.read_bytes())["1001"].cues) == 1

    def test_never_overwrites_an_existing_file(self, source, tmp_path):
        plan = editor.build_default_cue_plan(source)
        existing = tmp_path / "existing.xml"
        existing.write_bytes(b"precious")

        with pytest.raises(OutputPathError, match="already exists"):
            editor.write_plan(plan, existing)

        assert existing.read_bytes() == b"precious"

    def test_second_write_to_default_path_is_refused(self, source):
        plan = editor.build_default_cue_plan(source)
        editor.write_plan(plan)

        with pytest.raises(OutputPathError, match="already exists"):
            editor.write_plan(plan)

    def test_cannot_overwrite_the_source(self, source):
        original = source.read_bytes()
        plan = editor.build_default_cue_plan(source)

        with pytest.raises(OutputPathError, match="original"):
            editor.write_plan(plan, source)

        assert source.read_bytes() == original

    def test_cannot_overwrite_the_source_through_a_symlink(self, source, tmp_path):
        link = tmp_path / "link.xml"
        link.symlink_to(source)
        plan = editor.build_default_cue_plan(source)

        with pytest.raises(OutputPathError, match="original"):
            editor.write_plan(plan, link)

    def test_output_must_be_xml(self, source, tmp_path):
        plan = editor.build_default_cue_plan(source)

        with pytest.raises(OutputPathError, match=".xml"):
            editor.write_plan(plan, tmp_path / "out.txt")

    def test_missing_folder(self, source, tmp_path):
        plan = editor.build_default_cue_plan(source)

        with pytest.raises(OutputPathError, match="does not exist"):
            editor.write_plan(plan, tmp_path / "nope" / "out.xml")
