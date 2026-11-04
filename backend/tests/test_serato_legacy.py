"""The legacy `Serato Markers_` frame: the codec, and keeping it in step with Markers2."""

import json
from pathlib import Path

import pytest

from cuebridge.parsers import serato_markers as m
from cuebridge.parsers.serato_markers import CueEntry, MarkersError
from cuebridge.parsers.serato_tags import locate_legacy_markers, locate_markers2
from cuebridge.services import serato_sync as sync
from serato_helpers import (
    BPMLOCK,
    COLOR,
    EMPTY_LEGACY,
    REAL_LEGACY,
    REAL_LEGACY_CUES,
    make_mp3,
    make_wav,
    rb_cue,
    rb_track,
)


@pytest.fixture(autouse=True)
def safe_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CUEBRIDGE_HOME", str(tmp_path / "cuebridge-home"))
    monkeypatch.setattr(sync, "is_serato_running", lambda: False)


class TestCodec:
    def test_a_real_frame_decodes_to_its_cues(self):
        assert m.decode_legacy_cues(REAL_LEGACY) == REAL_LEGACY_CUES

    def test_a_real_frame_is_reproduced_byte_for_byte(self):
        assert m.encode_legacy(REAL_LEGACY, REAL_LEGACY_CUES) == REAL_LEGACY

    def test_a_position_is_five_seven_bit_groups(self):
        out = m.encode_legacy(EMPTY_LEGACY, [CueEntry(0, 41, "#cc0000")])

        assert out[6:11] == bytes.fromhex("0000000029")
        assert out[22:26] == bytes.fromhex("06300000")  # the color, 0xcc0000
        assert out[26:28] == b"\x01\x00"  # a cue, unlocked

    def test_a_large_position_round_trips(self):
        cue = CueEntry(2, 199_444, "#0088cc")

        assert m.decode_legacy_cues(m.encode_legacy(EMPTY_LEGACY, [cue])) == [cue]

    def test_slots_without_a_cue_are_emptied(self):
        out = m.encode_legacy(REAL_LEGACY, [CueEntry(1, 5000, "#00cc00")])

        assert m.decode_legacy_cues(out) == [CueEntry(1, 5000, "#00cc00")]

    def test_only_the_first_five_slots_are_written(self):
        out = m.encode_legacy(EMPTY_LEGACY, [CueEntry(0, 100, "#cc0000"), CueEntry(5, 200, "#cc0000")])

        assert [c.index for c in m.decode_legacy_cues(out)] == [0]

    def test_loops_and_the_trailer_are_left_alone(self):
        out = m.encode_legacy(REAL_LEGACY, [])

        assert len(out) == m.LEGACY_SIZE
        assert out[6 + 5 * 22 :] == REAL_LEGACY[6 + 5 * 22 :]
        assert out[:6] == REAL_LEGACY[:6]

    def test_an_empty_slot_with_odd_bytes_is_left_alone(self):
        # Seen in real files: an empty slot carrying a default color or leftover bytes.
        odd = bytearray(EMPTY_LEGACY)
        odd[6 + 22 + 11 : 6 + 22 + 16] = bytes.fromhex("002e43622a")
        odd[6 + 22 + 16 : 6 + 22 + 20] = bytes.fromhex("06300000")

        out = m.encode_legacy(bytes(odd), [CueEntry(0, 100, "#cc0000")])

        assert out[6 + 22 : 6 + 44] == bytes(odd)[6 + 22 : 6 + 44]
        assert m.decode_legacy_cues(out) == [CueEntry(0, 100, "#cc0000")]

    def test_a_slot_that_already_holds_the_cue_keeps_its_bytes(self):
        assert m.encode_legacy(REAL_LEGACY, REAL_LEGACY_CUES[:3])[6 + 66 :] != REAL_LEGACY[6 + 66 :]
        assert m.encode_legacy(REAL_LEGACY, REAL_LEGACY_CUES)[:6 + 66] == REAL_LEGACY[:6 + 66]

    def test_an_unknown_layout_is_refused(self):
        with pytest.raises(MarkersError):
            m.encode_legacy(REAL_LEGACY[:-1], [])

        with pytest.raises(MarkersError):
            m.encode_legacy(b"\x02\x06" + REAL_LEGACY[2:], [])

    def test_a_cue_slot_holding_something_else_is_refused(self):
        odd = bytearray(REAL_LEGACY)
        odd[6 + 21 - 1] = 3  # slot 1's type byte says "loop"

        with pytest.raises(MarkersError):
            m.encode_legacy(bytes(odd), [])


@pytest.fixture(params=["wav", "mp3"])
def tagged(request, tmp_path):
    """A file with an old cue in Markers2 and a legacy frame that has five other cues."""
    old = [COLOR, CueEntry(0, 500, "#cc0000"), BPMLOCK]

    if request.param == "wav":
        return make_wav(tmp_path / "a.wav", old, legacy=REAL_LEGACY)

    return make_mp3(tmp_path / "a.mp3", old, legacy=REAL_LEGACY)


def outside_both(path: Path) -> bytes:
    data = path.read_bytes()
    spans = sorted((l.offset, l.length) for l in (locate_markers2(path), locate_legacy_markers(path)))
    kept, position = b"", 0

    for offset, length in spans:
        kept += data[position:offset]
        position = offset + length

    return kept + data[position:]


def run(path, *cues):
    plan = sync.build_sync_plan([rb_track(path, *cues)])
    return plan, sync.apply_sync_plan(plan)


def legacy_cues(path):
    return m.decode_legacy_cues(locate_legacy_markers(path).data)


class TestLocating:
    def test_the_legacy_frame_is_found(self, tagged):
        assert locate_legacy_markers(tagged).data == REAL_LEGACY

    def test_a_file_without_it_gives_none(self, tmp_path):
        path = make_mp3(tmp_path / "plain.mp3", [COLOR])

        assert locate_legacy_markers(path) is None
        assert locate_markers2(path) is not None


class TestSync:
    def test_both_frames_are_planned_and_written(self, tagged):
        before = outside_both(tagged)

        plan, result = run(tagged, rb_cue(slot=0, ms=1000), rb_cue(slot=2, ms=5000))

        assert [r.description for r in plan.to_change[0].regions] == [m.DESCRIPTION, m.LEGACY_DESCRIPTION]
        assert len(result.verified) == 1 and result.failed == []
        assert [(c.index, c.position_ms > 0) for c in legacy_cues(tagged)] == [(0, True), (2, True)]
        assert outside_both(tagged) == before

    def test_the_two_frames_agree_afterwards(self, tagged):
        run(tagged, rb_cue(slot=0, ms=1000, color="#00cc00"), rb_cue(slot=1, ms=9000))

        legacy = legacy_cues(tagged)
        markers2 = [e for e in m.decode_markers2(locate_markers2(tagged).data) if isinstance(e, CueEntry)]

        assert [(c.index, c.position_ms, c.color) for c in legacy] == [(c.index, c.position_ms, c.color) for c in markers2]

    def test_a_stale_legacy_frame_alone_is_enough_to_change_a_file(self, tmp_path):
        # Markers2 already right, legacy still holding the old single cue: what was seen in Serato.
        right = [COLOR, CueEntry(0, 1000, "#cc0000"), CueEntry(1, 5000, "#cc0000"), BPMLOCK]
        stale = m.encode_legacy(EMPTY_LEGACY, [CueEntry(0, 1000, "#cc0000")])
        path = make_wav(tmp_path / "s.wav", right, legacy=stale)

        plan = sync.build_sync_plan([rb_track(path, rb_cue(slot=0, ms=1000), rb_cue(slot=1, ms=5000))])

        (item,) = plan.to_change
        assert [r.description for r in item.regions] == [m.LEGACY_DESCRIPTION]

        sync.apply_sync_plan(plan)

        assert [(c.index, c.position_ms) for c in legacy_cues(path)] == [(0, 1000), (1, 5000)]

    def test_a_file_that_already_agrees_is_unchanged(self, tagged):
        run(tagged, rb_cue(slot=0, ms=1000))

        again = sync.build_sync_plan([rb_track(tagged, rb_cue(slot=0, ms=1000))])

        assert again.to_change == [] and len(again.with_status("unchanged")) == 1

    def test_cues_beyond_slot_five_only_go_to_markers2(self, tagged):
        run(tagged, rb_cue(slot=0, ms=1000), rb_cue(slot=6, ms=7000))

        assert [c.index for c in legacy_cues(tagged)] == [0]
        assert 6 in [e.index for e in m.decode_markers2(locate_markers2(tagged).data) if isinstance(e, CueEntry)]

    def test_a_file_with_no_legacy_frame_is_still_synced(self, tmp_path):
        path = make_wav(tmp_path / "n.wav", [COLOR, CueEntry(0, 500, "#cc0000")])

        plan, result = run(path, rb_cue(slot=0, ms=1000))

        assert len(result.verified) == 1
        assert [r.description for r in plan.to_change[0].regions] == [m.DESCRIPTION]

    def test_an_unknown_legacy_layout_skips_the_file(self, tmp_path):
        path = make_wav(tmp_path / "u.wav", [COLOR, CueEntry(0, 500, "#cc0000")], legacy=REAL_LEGACY[:-4])

        plan = sync.build_sync_plan([rb_track(path, rb_cue(slot=0, ms=1000))])

        assert plan.to_change == [] and "layout" in plan.items[0].reason

    def test_the_modified_time_is_kept(self, tagged):
        import os

        os.utime(tagged, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))

        run(tagged, rb_cue(slot=0, ms=1000))

        assert tagged.stat().st_mtime_ns == 1_600_000_000_000_000_000


class TestJournalAndUndo:
    def test_the_journal_records_both_regions(self, tagged):
        _, result = run(tagged, rb_cue(slot=0, ms=1000))

        (entry,) = json.loads(result.journal.read_text())["entries"]

        assert [r["description"] for r in entry["regions"]] == [m.DESCRIPTION, m.LEGACY_DESCRIPTION]
        assert bytes.fromhex(entry["regions"][1]["old"]) == REAL_LEGACY

    def test_undo_restores_both_frames_exactly(self, tagged):
        original = tagged.read_bytes()
        _, result = run(tagged, rb_cue(slot=0, ms=1000))

        undone = sync.undo_sync(result.journal)

        assert undone.restored == [str(tagged)]
        assert tagged.read_bytes() == original

    def test_undo_leaves_a_file_alone_if_the_legacy_frame_was_changed_since(self, tagged):
        _, result = run(tagged, rb_cue(slot=0, ms=1000))
        location = locate_legacy_markers(tagged)
        with open(tagged, "r+b") as handle:
            handle.seek(location.offset + 6)
            handle.write(b"\x00\x00\x00\x09\x09")  # Serato (say) moved a cue

        undone = sync.undo_sync(result.journal)

        assert undone.restored == [] and "changed again" in undone.skipped[0][1]

    def test_a_failed_verification_restores_both_frames(self, tagged, monkeypatch):
        original = tagged.read_bytes()
        plan = sync.build_sync_plan([rb_track(tagged, rb_cue(slot=0, ms=1000))])
        real = sync.locate_frame
        # Only the legacy frame fails to read back.
        monkeypatch.setattr(
            sync, "locate_frame", lambda p, d: None if d == m.LEGACY_DESCRIPTION else real(p, d)
        )

        result = sync.apply_sync_plan(plan)

        assert result.failed and "restored" in result.failed[0][1]
        assert tagged.read_bytes() == original

    def test_an_old_single_region_journal_can_still_be_undone(self, tagged):
        original_data = locate_markers2(tagged).data
        location = locate_markers2(tagged)
        run(tagged, rb_cue(slot=0, ms=1000))
        stat = tagged.stat()
        new_data = locate_markers2(tagged).data
        journal = sync.journal_dir() / "serato-sync-old.json"
        journal.write_text(json.dumps({"version": 1, "entries": [{
            "path": str(tagged), "offset": location.offset, "length": location.length,
            "old": original_data.hex(), "new": new_data.hex(),
            "mtime_ns": stat.st_mtime_ns, "atime_ns": stat.st_atime_ns, "size": stat.st_size,
            "state": "verified",
        }]}))

        undone = sync.undo_sync(journal)

        assert undone.restored == [str(tagged)]
        assert locate_markers2(tagged).data == original_data
