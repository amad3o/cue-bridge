from cuebridge.services.cue_timing import MP3_OFFSET_MS, timing_offset, to_serato_ms
from serato_helpers import AUDIO_INFO_NO_LAME, AUDIO_PLAIN, AUDIO_WITH_LAME, make_mp3, make_wav


def test_wav_needs_no_correction(tmp_path):
    offset = timing_offset(make_wav(tmp_path / "a.wav"))

    assert offset.offset_ms == 0 and not offset.flagged


def test_mp3_with_a_lame_header_is_corrected(tmp_path):
    offset = timing_offset(make_mp3(tmp_path / "a.mp3", None, audio=AUDIO_WITH_LAME))

    assert offset.offset_ms == MP3_OFFSET_MS and not offset.flagged
    assert "LAME" in offset.note


def test_mp3_without_any_info_frame_is_corrected(tmp_path):
    offset = timing_offset(make_mp3(tmp_path / "a.mp3", None, audio=AUDIO_PLAIN))

    assert offset.offset_ms == MP3_OFFSET_MS and not offset.flagged


def test_mp3_with_an_info_frame_but_no_lame_tag_is_flagged_and_not_corrected(tmp_path):
    offset = timing_offset(make_mp3(tmp_path / "a.mp3", None, audio=AUDIO_INFO_NO_LAME))

    assert offset.offset_ms == 0 and offset.flagged
    assert "26 ms" in offset.note


def test_mp3_without_an_id3_tag(tmp_path):
    path = tmp_path / "bare.mp3"
    path.write_bytes(AUDIO_WITH_LAME)

    assert timing_offset(path).offset_ms == MP3_OFFSET_MS


def test_unreadable_or_frameless_mp3_is_flagged(tmp_path):
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")

    assert timing_offset(empty).flagged
    assert timing_offset(tmp_path / "missing.mp3").flagged


def test_other_formats_are_flagged(tmp_path):
    assert timing_offset(tmp_path / "a.m4a").flagged


def test_conversion_rounds_half_up_and_never_goes_below_zero(tmp_path):
    offset = timing_offset(make_mp3(tmp_path / "a.mp3", None, audio=AUDIO_WITH_LAME))

    assert to_serato_ms(30771, offset) == 30766  # 30765.5 rounds up, not to the even number
    assert to_serato_ms(51, offset) == 46 and to_serato_ms(52, offset) == 47
    assert to_serato_ms(10, offset) == 5 and to_serato_ms(3, offset) == 0
    assert to_serato_ms(0, offset) == 0


def test_a_zero_offset_changes_nothing(tmp_path):
    assert to_serato_ms(12345, timing_offset(make_wav(tmp_path / "a.wav"))) == 12345
