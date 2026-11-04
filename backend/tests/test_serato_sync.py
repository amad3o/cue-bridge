import json
import os
from pathlib import Path

import pytest

from cuebridge.parsers.serato_markers import CueEntry, RawEntry
from cuebridge.parsers.serato_tags import locate_markers2, read_cues, read_markers2
from cuebridge.services import serato_sync as sync
from cuebridge.services.serato_sync import SyncError
from serato_helpers import (
    AUDIO_INFO_NO_LAME,
    AUDIO_WITH_LAME,
    BPMLOCK,
    COLOR,
    make_mp3,
    make_wav,
    rb_cue,
    rb_track,
)


@pytest.fixture(autouse=True)
def safe_environment(tmp_path, monkeypatch):
    """Never touch the real journal folder, and never depend on Serato actually running."""
    monkeypatch.setenv("CUEBRIDGE_HOME", str(tmp_path / "cuebridge-home"))
    monkeypatch.setattr(sync, "is_serato_running", lambda: False)


@pytest.fixture(params=["wav", "mp3"])
def audio(request, tmp_path):
    """A file of each type that already carries a Serato tag with one old cue."""
    old = [COLOR, CueEntry(0, 500, "#cc0000"), BPMLOCK]

    if request.param == "wav":
        return make_wav(tmp_path / "a.wav", old)

    return make_mp3(tmp_path / "a.mp3", old, audio=AUDIO_WITH_LAME)


def plan_for(path, *cues, **kwargs):
    return sync.build_sync_plan([rb_track(path, *cues)], **kwargs)


def outside(path: Path) -> bytes:
    """Everything in the file except the cue tag's payload."""
    location = locate_markers2(path)
    data = path.read_bytes()
    return data[: location.offset] + data[location.offset + location.length :]


class TestPlanning:
    def test_a_changed_file_is_planned_with_before_and_after(self, audio):
        plan = plan_for(audio, rb_cue(slot=0, ms=1000), rb_cue(slot=2, ms=5000))

        (item,) = plan.to_change
        assert item.before == ((1, 500),)
        assert [slot for slot, _ in item.after] == [1, 3]
        assert item.location.length == 470 and len(item.new_data) == 470

    def test_planning_never_changes_the_file(self, audio):
        before = audio.read_bytes()

        plan_for(audio, rb_cue(slot=0, ms=1000))

        assert audio.read_bytes() == before

    def test_a_file_that_already_matches_is_unchanged(self, audio):
        plan = plan_for(audio, rb_cue(slot=0, ms=500, color="#cc0000"))

        # Rekordbox says 500 ms; the WAV agrees, the MP3 gets the 5.5 ms correction.
        expected = 500 if audio.suffix == ".wav" else 495
        (item,) = plan.items
        assert item.after == ((1, expected),)
        assert item.status == ("unchanged" if audio.suffix == ".wav" else "change")

    def test_rekordbox_wins_over_what_serato_has(self, audio):
        plan = plan_for(audio, rb_cue(slot=3, ms=9000))

        (item,) = plan.to_change
        # Serato's old cue in slot 1 is gone, because Rekordbox doesn't have it.
        assert [slot for slot, _ in item.after] == [4]

    def test_hot_cues_keep_their_slots_and_memory_cues_take_free_ones(self, audio):
        plan = plan_for(audio, rb_cue("hotcue", 0, 1000), rb_cue("memory", None, 7000), rb_cue("memory", None, 3000))

        (item,) = plan.to_change
        # The hot cue keeps slot 1; the memory cues fill slots 2 and 3 in time order.
        expected = (
            ((1, 1000), (2, 3000), (3, 7000))
            if audio.suffix == ".wav"
            else ((1, 995), (2, 2995), (3, 6995))  # MP3: 5.5 ms earlier, rounded half up
        )
        assert item.after == expected

    def test_memory_cues_can_be_left_out(self, audio):
        plan = plan_for(audio, rb_cue("hotcue", 1, 1000), rb_cue("memory", None, 7000), include_memory_cues=False)

        (item,) = plan.to_change
        assert [slot for slot, _ in item.after] == [2]

    def test_colors_and_names_come_from_rekordbox_with_a_default(self, audio):
        plan = plan_for(audio, rb_cue(slot=0, color="#28E214", name="Drop"), rb_cue(slot=1))

        cues = [e for e in sync.decode_markers2(plan.to_change[0].new_data) if isinstance(e, CueEntry)]
        assert [(c.color, c.name) for c in cues] == [("#28e214", "Drop"), ("#cc0000", "")]

    def test_the_default_color_applies_only_to_a_slot_with_no_color_anywhere(self, audio):
        plan = plan_for(audio, rb_cue(slot=5), default_color="#00cc00")

        cue = next(e for e in sync.decode_markers2(plan.to_change[0].new_data) if isinstance(e, CueEntry))
        assert (cue.index, cue.color) == (5, "#00cc00")

    def test_other_tag_entries_are_kept(self, tmp_path):
        extra = [COLOR, RawEntry("FLIP", b"\x01\x02"), CueEntry(0, 1, "#cc0000"), BPMLOCK]
        path = make_wav(tmp_path / "x.wav", extra)

        entries = sync.decode_markers2(plan_for(path, rb_cue(slot=0, ms=9000)).to_change[0].new_data)

        assert [e.name if isinstance(e, RawEntry) else "CUE" for e in entries] == ["COLOR", "FLIP", "CUE", "BPMLOCK"]

    def test_timing_is_corrected_by_file_type(self, tmp_path):
        wav = make_wav(tmp_path / "a.wav", [COLOR, BPMLOCK])
        mp3 = make_mp3(tmp_path / "a.mp3", [COLOR, BPMLOCK], audio=AUDIO_WITH_LAME)

        assert plan_for(wav, rb_cue(slot=0, ms=30771)).to_change[0].after == ((1, 30771),)
        assert plan_for(mp3, rb_cue(slot=0, ms=30771)).to_change[0].after == ((1, 30766),)

    def test_unverified_timing_is_left_uncorrected_and_flagged(self, tmp_path):
        path = make_mp3(tmp_path / "odd.mp3", [COLOR, BPMLOCK], audio=AUDIO_INFO_NO_LAME)

        plan = plan_for(path, rb_cue(slot=0, ms=30771))

        assert plan.to_change[0].after == ((1, 30771),)
        assert plan.flagged_timing == plan.to_change

    def test_loops_are_counted_and_not_copied(self, audio):
        plan = plan_for(audio, rb_cue("hotcue", 0, 1000), rb_cue("loop", 1, 2000))

        assert plan.loops_ignored == 1
        assert [slot for slot, _ in plan.to_change[0].after] == [1]

    def test_slots_beyond_eight_are_dropped(self, audio):
        plan = plan_for(audio, rb_cue("hotcue", 0, 1000), rb_cue("hotcue", 9, 2000))

        assert [slot for slot, _ in plan.to_change[0].after] == [1]


def tagged(tmp_path, suffix, *cues, audio_bytes=AUDIO_WITH_LAME):
    entries = [COLOR, *cues, BPMLOCK]

    if suffix == "wav":
        return make_wav(tmp_path / "t.wav", entries)

    return make_mp3(tmp_path / "t.mp3", entries, audio=audio_bytes)


def cues_in(plan):
    return [e for e in sync.decode_markers2(plan.to_change[0].new_data) if isinstance(e, CueEntry)]


class TestChangeOnlyWhatDiffers:
    def test_a_cue_that_already_agrees_is_left_exactly_alone(self, tmp_path):
        path = tagged(tmp_path, "wav", CueEntry(0, 5000, "#00cc00"))

        plan = plan_for(path, rb_cue(slot=0, ms=5000))

        assert plan.to_change == [] and plan.items[0].status == "unchanged"

    def test_one_millisecond_of_rounding_noise_is_tolerated_but_two_is_not(self, tmp_path):
        path = tagged(tmp_path, "wav", CueEntry(0, 5000, "#00cc00"))

        assert plan_for(path, rb_cue(slot=0, ms=5001)).to_change == []
        assert plan_for(path, rb_cue(slot=0, ms=4999)).to_change == []
        assert len(plan_for(path, rb_cue(slot=0, ms=5002)).to_change) == 1

    def test_the_mp3_correction_is_applied_before_comparing(self, tmp_path):
        path = tagged(tmp_path, "mp3", CueEntry(0, 30765, "#00cc00"))

        # Rekordbox 30771 -> Serato 30765.5, so Lexicon's 30765 already agrees.
        assert plan_for(path, rb_cue(slot=0, ms=30771)).to_change == []

    def test_an_existing_color_is_kept_when_only_the_position_changes(self, tmp_path):
        path = tagged(tmp_path, "wav", CueEntry(0, 5000, "#00cc00"), CueEntry(1, 9000, "#8800cc"))

        plan = plan_for(path, rb_cue(slot=0, ms=5000), rb_cue(slot=1, ms=9500))

        kept, moved = cues_in(plan)
        assert kept == CueEntry(0, 5000, "#00cc00")
        assert moved == CueEntry(1, 9500, "#8800cc")  # new position, old color

    def test_a_rekordbox_color_wins_over_the_existing_one(self, tmp_path):
        path = tagged(tmp_path, "wav", CueEntry(0, 5000, "#00cc00"))

        plan = plan_for(path, rb_cue(slot=0, ms=5000, color="#28e214"))

        assert cues_in(plan) == [CueEntry(0, 5000, "#28e214")]

    def test_a_rekordbox_name_wins_but_an_empty_one_does_not_erase_a_serato_name(self, tmp_path):
        path = tagged(tmp_path, "wav", CueEntry(0, 5000, "#00cc00", "Intro"))

        assert plan_for(path, rb_cue(slot=0, ms=5000, name=None)).to_change == []
        assert cues_in(plan_for(path, rb_cue(slot=0, ms=5000, name="Drop"))) == [CueEntry(0, 5000, "#00cc00", "Drop")]

    def test_serato_cues_that_rekordbox_lacks_are_removed(self, tmp_path):
        path = tagged(tmp_path, "wav", CueEntry(0, 5000, "#00cc00"), CueEntry(3, 9000, "#cc0000"))

        plan = plan_for(path, rb_cue(slot=0, ms=5000))

        assert cues_in(plan) == [CueEntry(0, 5000, "#00cc00")]

    def test_a_cue_in_a_different_slot_is_not_a_match(self, tmp_path):
        path = tagged(tmp_path, "wav", CueEntry(0, 5000, "#00cc00"))

        plan = plan_for(path, rb_cue(slot=2, ms=5000))

        assert [c.index for c in cues_in(plan)] == [2]

    def test_where_timing_is_unverified_a_cue_within_a_frame_is_trusted(self, tmp_path):
        odd = tagged(tmp_path, "mp3", CueEntry(0, 30797, "#00cc00"), audio_bytes=AUDIO_INFO_NO_LAME)

        # Rekordbox says 30771; the earlier sync put it 26 ms later, which we can't verify but don't undo.
        assert plan_for(odd, rb_cue(slot=0, ms=30771)).to_change == []

    def test_where_timing_is_unverified_a_far_off_cue_is_still_rewritten(self, tmp_path):
        odd = tagged(tmp_path, "mp3", CueEntry(0, 30840, "#00cc00"), audio_bytes=AUDIO_INFO_NO_LAME)

        plan = plan_for(odd, rb_cue(slot=0, ms=30771))

        assert cues_in(plan) == [CueEntry(0, 30771, "#00cc00")]
        assert plan.flagged_timing

    def test_unverified_files_with_nothing_to_change_are_not_flagged_as_changes(self, tmp_path):
        odd = tagged(tmp_path, "mp3", CueEntry(0, 30797, "#00cc00"), audio_bytes=AUDIO_INFO_NO_LAME)

        assert plan_for(odd, rb_cue(slot=0, ms=30771)).flagged_timing == []

    def test_a_new_slot_gets_the_default_color(self, tmp_path):
        path = tagged(tmp_path, "wav", CueEntry(0, 5000, "#00cc00"))

        plan = plan_for(path, rb_cue(slot=0, ms=5000), rb_cue(slot=1, ms=9000))

        assert cues_in(plan) == [CueEntry(0, 5000, "#00cc00"), CueEntry(1, 9000, "#cc0000")]


class TestSkipping:
    def reasons(self, track):
        plan = sync.build_sync_plan([track])
        return plan, [i.reason for i in plan.with_status("skip")]

    def test_tracks_without_rekordbox_cues_are_never_considered(self, audio):
        plan = sync.build_sync_plan([rb_track(audio)])

        assert plan.items == ()

    def test_no_file_path(self):
        _, reasons = self.reasons(rb_track(None, rb_cue()))
        assert reasons == ["the track has no file path"]

    def test_unsupported_format(self, tmp_path):
        _, reasons = self.reasons(rb_track(tmp_path / "a.m4a", rb_cue()))
        assert "aren't supported" in reasons[0]

    def test_missing_file(self, tmp_path):
        _, reasons = self.reasons(rb_track(tmp_path / "gone.mp3", rb_cue()))
        assert "wasn't found" in reasons[0]

    def test_no_serato_tag_yet(self, tmp_path):
        _, reasons = self.reasons(rb_track(make_mp3(tmp_path / "n.mp3", None), rb_cue()))
        assert "no Serato cue tag" in reasons[0]

    def test_a_layout_that_cannot_be_edited_in_place(self, tmp_path):
        path = make_mp3(tmp_path / "u.mp3", [COLOR, BPMLOCK])
        data = bytearray(path.read_bytes())
        data[5] |= 0x80
        path.write_bytes(bytes(data))

        _, reasons = self.reasons(rb_track(path, rb_cue()))

        assert "unsynchronised" in reasons[0]

    def test_only_loops_means_nothing_to_copy(self, audio):
        _, reasons = self.reasons(rb_track(audio, rb_cue("loop", 0, 1000)))
        assert reasons == ["Rekordbox has no hot or memory cues to copy"]

    def test_cues_that_do_not_fit_in_the_tag(self, tmp_path):
        path = make_wav(tmp_path / "big.wav", [COLOR, BPMLOCK])
        cues = [rb_cue(slot=i, ms=1000 * (i + 1), name="n" * 60) for i in range(8)]

        _, reasons = self.reasons(rb_track(path, *cues))

        assert "holds 470" in reasons[0]

    def test_a_skipped_file_is_never_written(self, tmp_path):
        path = make_mp3(tmp_path / "n.mp3", None)
        before = path.read_bytes()

        result = sync.apply_sync_plan(sync.build_sync_plan([rb_track(path, rb_cue())]))

        assert result.journal is None and path.read_bytes() == before


class TestSelection:
    def test_track_ids_limit_the_plan(self, tmp_path):
        a = make_wav(tmp_path / "a.wav", [COLOR, BPMLOCK])
        b = make_wav(tmp_path / "b.wav", [COLOR, BPMLOCK])
        tracks = [rb_track(a, rb_cue(), track_id="A"), rb_track(b, rb_cue(), track_id="B")]

        plan = sync.build_sync_plan(tracks, track_ids=["B"])

        assert [i.track_id for i in plan.items] == ["B"]

    def test_the_file_limit_is_reported(self, tmp_path):
        tracks = [
            rb_track(make_wav(tmp_path / f"{i}.wav", [COLOR, BPMLOCK]), rb_cue(), track_id=str(i))
            for i in range(5)
        ]

        plan = sync.build_sync_plan(tracks, max_files=3)

        assert len(plan.items) == 3 and plan.not_planned == 2


class TestApplying:
    def apply(self, path, *cues, **kwargs):
        plan = plan_for(path, *cues, **kwargs)
        return plan, sync.apply_sync_plan(plan)

    def test_only_the_cue_tag_changes(self, audio):
        before_outside, original = outside(audio), audio.read_bytes()

        _, result = self.apply(audio, rb_cue(slot=0, ms=1000), rb_cue(slot=2, ms=5000))

        assert len(result.verified) == 1 and result.failed == []
        assert audio.read_bytes() != original
        assert outside(audio) == before_outside
        assert audio.stat().st_size == len(original)

    def test_the_new_cues_read_back(self, audio):
        self.apply(audio, rb_cue(slot=0, ms=1000, color="#00cc00"), rb_cue(slot=2, ms=5000))

        cues = read_cues(audio)

        assert [(c.index, c.color) for c in cues] == [(0, "#00cc00"), (2, "#cc0000")]
        assert [c.position_ms for c in cues] == ([1000, 5000] if audio.suffix == ".wav" else [995, 4995])

    def test_other_entries_and_artwork_survive(self, audio):
        self.apply(audio, rb_cue(slot=0, ms=1000))

        entries = read_markers2(audio)

        assert entries[0] == COLOR and entries[-1] == BPMLOCK

    def test_the_modified_time_is_kept(self, audio):
        os.utime(audio, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))

        self.apply(audio, rb_cue(slot=0, ms=1000))

        assert audio.stat().st_mtime_ns == 1_600_000_000_000_000_000

    def test_running_again_finds_nothing_to_do(self, audio):
        self.apply(audio, rb_cue(slot=0, ms=1000))

        again = plan_for(audio, rb_cue(slot=0, ms=1000))

        assert again.to_change == [] and len(again.with_status("unchanged")) == 1

    def test_a_journal_records_the_original_bytes_before_and_after(self, audio):
        original = locate_markers2(audio).data

        plan, result = self.apply(audio, rb_cue(slot=0, ms=1000))

        journal = json.loads(result.journal.read_text())
        (entry,) = journal["entries"]
        (region,) = entry["regions"]
        assert bytes.fromhex(region["old"]) == original
        assert bytes.fromhex(region["new"]) == plan.to_change[0].new_data
        assert entry["state"] == "verified" and entry["path"] == str(audio)
        assert result.journal.parent == Path(os.environ["CUEBRIDGE_HOME"]) / "undo"

    def test_a_journal_is_written_before_any_file_is_touched(self, audio, monkeypatch):
        seen = {}

        def spy(path, offset, data, mtime_ns, atime_ns):
            journal = json.loads(next(sync.journal_dir().glob("*.json")).read_text())
            seen["state_when_writing"] = journal["entries"][0]["state"]
            raise OSError(5, "stop here")

        monkeypatch.setattr(sync, "_write_region", spy)

        _, result = self.apply(audio, rb_cue(slot=0, ms=1000))

        assert seen["state_when_writing"] == "writing"
        assert len(result.failed) == 1

    def test_refuses_while_serato_is_running(self, audio, monkeypatch):
        monkeypatch.setattr(sync, "is_serato_running", lambda: True)
        before = audio.read_bytes()

        with pytest.raises(SyncError, match="Serato DJ Pro is running"):
            self.apply(audio, rb_cue(slot=0, ms=1000))

        assert audio.read_bytes() == before and not sync.journal_dir().exists()

    def test_a_file_changed_since_planning_is_left_alone(self, audio):
        plan = plan_for(audio, rb_cue(slot=0, ms=1000))
        location = locate_markers2(audio)
        theirs = sync.encode_markers2([COLOR, CueEntry(5, 123, "#0000cc"), BPMLOCK])

        with open(audio, "r+b") as handle:
            handle.seek(location.offset)
            handle.write(theirs)

        result = sync.apply_sync_plan(plan)

        assert result.verified == [] and "changed since" in result.failed[0][1]
        assert locate_markers2(audio).data == theirs

    def test_a_failed_verification_restores_the_file(self, audio, monkeypatch):
        original = audio.read_bytes()
        plan = plan_for(audio, rb_cue(slot=0, ms=1000))
        # After the write, pretend the tag can no longer be found where it should be.
        monkeypatch.setattr(sync, "locate_frame", lambda path, description: None)

        result = sync.apply_sync_plan(plan)

        assert result.failed and "restored" in result.failed[0][1]
        assert audio.read_bytes() == original
        assert json.loads(result.journal.read_text())["entries"][0]["state"] == "reverted"

    def test_damage_outside_the_tag_is_caught_and_undone(self, audio, monkeypatch):
        original = audio.read_bytes()
        real = sync._write_region

        def sloppy(path, offset, data, mtime_ns, atime_ns):
            real(path, offset, data, mtime_ns, atime_ns)
            if data != plan.to_change[0].location.data:
                with open(path, "r+b") as handle:  # also corrupt a byte far from the tag
                    handle.seek(offset + 600)
                    handle.write(b"\xaa")

        plan = plan_for(audio, rb_cue(slot=0, ms=1000))
        monkeypatch.setattr(sync, "_write_region", sloppy)

        result = sync.apply_sync_plan(plan)

        assert result.failed and "outside the cue tag" in result.failed[0][1]
        assert locate_markers2(audio).data == plan.to_change[0].location.data

    def test_a_half_finished_write_is_put_back(self, audio, monkeypatch):
        original = locate_markers2(audio).data
        real = sync._write_region
        calls = []

        def flaky(path, offset, data, mtime_ns, atime_ns):
            calls.append(1)
            if len(calls) == 1:
                with open(path, "r+b") as handle:
                    handle.seek(offset)
                    handle.write(data[:100])  # half the bytes, then the disk "fails"
                raise OSError(5, "Input/output error")
            real(path, offset, data, mtime_ns, atime_ns)

        monkeypatch.setattr(sync, "_write_region", flaky)

        result = sync.apply_sync_plan(plan_for(audio, rb_cue(slot=0, ms=1000)))

        assert result.failed and locate_markers2(audio).data == original

    def test_a_read_only_file_fails_cleanly(self, audio):
        before = audio.read_bytes()
        audio.chmod(0o444)

        try:
            _, result = self.apply(audio, rb_cue(slot=0, ms=1000))
        finally:
            audio.chmod(0o644)

        assert result.failed and result.verified == []
        assert audio.read_bytes() == before

    def test_one_failure_does_not_stop_the_others(self, tmp_path):
        good = make_wav(tmp_path / "good.wav", [COLOR, BPMLOCK])
        bad = make_wav(tmp_path / "bad.wav", [COLOR, BPMLOCK])
        plan = sync.build_sync_plan([rb_track(bad, rb_cue(), track_id="bad"), rb_track(good, rb_cue(), track_id="good")])
        bad.chmod(0o444)

        try:
            result = sync.apply_sync_plan(plan)
        finally:
            bad.chmod(0o644)

        assert [i.track_id for i in result.verified] == ["good"]
        assert [i.track_id for i, _ in result.failed] == ["bad"]


class TestUndo:
    def synced(self, path, *cues):
        plan = plan_for(path, *cues)
        return sync.apply_sync_plan(plan)

    def test_undo_restores_the_file_exactly(self, audio):
        original = audio.read_bytes()
        os.utime(audio, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
        original_mtime = audio.stat().st_mtime_ns
        result = self.synced(audio, rb_cue(slot=0, ms=1000))
        assert audio.read_bytes() != original

        undone = sync.undo_sync(result.journal)

        assert undone.restored == [str(audio)]
        assert audio.read_bytes() == original
        assert audio.stat().st_mtime_ns == original_mtime

    def test_a_dry_run_changes_nothing(self, audio):
        result = self.synced(audio, rb_cue(slot=0, ms=1000))
        after_sync = audio.read_bytes()

        preview = sync.undo_sync(result.journal, dry_run=True)

        assert preview.restored == [str(audio)] and audio.read_bytes() == after_sync

    def test_undoing_twice_is_harmless(self, audio):
        result = self.synced(audio, rb_cue(slot=0, ms=1000))
        sync.undo_sync(result.journal)

        again = sync.undo_sync(result.journal)

        assert again.restored == [] and again.already_original == [str(audio)]

    def test_a_tag_changed_again_afterwards_is_left_alone(self, audio):
        result = self.synced(audio, rb_cue(slot=0, ms=1000))
        location = locate_markers2(audio)
        theirs = sync.encode_markers2([COLOR, CueEntry(7, 42, "#0000cc"), BPMLOCK])
        with open(audio, "r+b") as handle:
            handle.seek(location.offset)
            handle.write(theirs)

        undone = sync.undo_sync(result.journal)

        assert undone.restored == [] and "changed again" in undone.skipped[0][1]
        assert locate_markers2(audio).data == theirs

    def test_a_crash_mid_write_is_recovered_from_the_journal(self, audio):
        original = locate_markers2(audio).data
        plan = plan_for(audio, rb_cue(slot=0, ms=1000))
        location = plan.to_change[0].location
        stat = audio.stat()
        sync.journal_dir().mkdir(parents=True)
        journal = sync.journal_dir() / "serato-sync-crash.json"
        journal.write_text(json.dumps({"version": 1, "entries": [{
            "path": str(audio), "offset": location.offset, "length": location.length,
            "old": original.hex(), "new": plan.to_change[0].new_data.hex(),
            "mtime_ns": stat.st_mtime_ns, "atime_ns": stat.st_atime_ns, "size": stat.st_size,
            "state": "writing",
        }]}))
        with open(audio, "r+b") as handle:  # the power went out after 90 bytes
            handle.seek(location.offset)
            handle.write(b"\x99" * 90)

        undone = sync.undo_sync(journal)

        assert undone.restored == [str(audio)] and locate_markers2(audio).data == original

    def test_a_deleted_file_is_reported(self, audio):
        result = self.synced(audio, rb_cue(slot=0, ms=1000))
        audio.unlink()

        undone = sync.undo_sync(result.journal)

        assert "no longer exists" in undone.skipped[0][1]

    def test_refuses_while_serato_is_running(self, audio, monkeypatch):
        result = self.synced(audio, rb_cue(slot=0, ms=1000))
        monkeypatch.setattr(sync, "is_serato_running", lambda: True)

        with pytest.raises(SyncError, match="Serato DJ Pro is running"):
            sync.undo_sync(result.journal)

        sync.undo_sync(result.journal, dry_run=True)  # a preview is fine

    def test_an_unreadable_journal(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json")

        with pytest.raises(SyncError, match="Could not read"):
            sync.undo_sync(bad)

    def test_latest_journal(self, tmp_path, audio):
        assert sync.latest_journal() is None

        first = self.synced(audio, rb_cue(slot=0, ms=1000)).journal
        second = self.synced(audio, rb_cue(slot=1, ms=2000)).journal

        third = self.synced(audio, rb_cue(slot=2, ms=3000)).journal

        assert len({first, second, third}) == 3
        assert sync.latest_journal() == third  # even when the runs share the same second
        assert sorted([third, first, second]) == [first, second, third]
