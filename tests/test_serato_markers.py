import base64
import struct

import pytest

from cuebridge.parsers import serato_markers as m
from cuebridge.parsers.serato_markers import CueEntry, MarkersError, RawEntry

# The Markers2 payload of a real MP3 written by Serato DJ Pro (two hot cues).
REAL_FRAME = bytes.fromhex(
    "01014151464454307850556741414141414541502f2f2f304e56525141414141414e4141414141414472414d774141414141"
    "41454e56525141414141414e4141454141414a464141444d0a4141414141454a515455785051307341414141414151414100"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000"
)

COLOR = RawEntry("COLOR", bytes.fromhex("00ffffff"))
BPMLOCK = RawEntry("BPMLOCK", b"\x00")


def cue(index=0, position_ms=1000, color="#cc0000", name=""):
    return CueEntry(index, position_ms, color, name)


def entries_with(*cues):
    return [COLOR, *cues, BPMLOCK]


class TestRealTag:
    def test_decodes_to_the_cues_serato_wrote(self):
        assert m.decode_markers2(REAL_FRAME) == [
            COLOR,
            CueEntry(0, 235, "#cc0000", ""),
            CueEntry(1, 581, "#00cc00", ""),
            BPMLOCK,
        ]

    def test_encodes_back_to_the_same_bytes(self):
        assert m.encode_markers2(m.decode_markers2(REAL_FRAME)) == REAL_FRAME

    def test_has_the_documented_layout(self):
        assert len(REAL_FRAME) == m.FRAME_SIZE == 470
        assert REAL_FRAME[:2] == b"\x01\x01"
        assert set(REAL_FRAME[2 + 97 :]) == {0}  # NUL padding after the base64 text


class TestEncoding:
    def test_output_is_always_the_fixed_size(self):
        for count in range(9):
            cues = [cue(i, 1000 * (i + 1)) for i in range(count)]

            assert len(m.encode_markers2(entries_with(*cues))) == m.FRAME_SIZE

    def test_base64_is_wrapped_at_72_characters_without_padding(self):
        data = m.encode_markers2(entries_with(*[cue(i, 1000 * i) for i in range(8)]))
        text = data[2:].split(b"\x00")[0]

        assert b"=" not in text
        assert all(len(line) <= 72 for line in text.split(b"\n"))
        assert text.count(b"\n") >= 1

    def test_start_of_the_decoded_data(self):
        data = m.encode_markers2(entries_with(cue()))
        raw = base64.b64decode(data[2:].split(b"\x00")[0].replace(b"\n", b"") + b"==")

        assert raw[:2] == b"\x01\x01"
        assert raw.startswith(b"\x01\x01COLOR\x00")
        assert raw.endswith(b"BPMLOCK\x00" + struct.pack(">I", 1) + b"\x00\x00")

    @pytest.mark.parametrize("count", range(9))
    def test_round_trip_for_any_number_of_cues(self, count):
        entries = entries_with(*[cue(i, 12_345 * (i + 1), "#00cc00") for i in range(count)])

        assert m.decode_markers2(m.encode_markers2(entries)) == entries

    def test_names_survive_including_unicode(self):
        entries = entries_with(cue(0, 5000, "#cc8800", "Intro"), cue(1, 9000, "#00cccc", "Dröp ♪"))

        assert m.decode_markers2(m.encode_markers2(entries)) == entries

    def test_unknown_entries_are_kept_exactly_in_order(self):
        entries = [COLOR, RawEntry("LOOP", b"\x01\x02\x03"), cue(), RawEntry("FLIP", b""), BPMLOCK]

        assert m.decode_markers2(m.encode_markers2(entries)) == entries

    def test_too_many_cues_for_the_tag_are_refused(self):
        entries = entries_with(*[cue(i, 1000, name="x" * 60) for i in range(8)])

        with pytest.raises(MarkersError, match="holds 470"):
            m.encode_markers2(entries)


class TestCuePayload:
    def test_payload_layout(self):
        payload = m._cue_payload(CueEntry(2, 0x01020304, "#112233", "ab"))

        assert payload == b"\x00\x02" + b"\x01\x02\x03\x04" + b"\x00" + b"\x11\x22\x33" + b"\x00\x00" + b"ab\x00"

    def test_non_canonical_cue_is_kept_verbatim(self):
        # A reserved byte is non-zero, so re-encoding would change it.
        odd = b"\x07\x00" + struct.pack(">I", 99) + b"\x00\xcc\x00\x00\x00\x00\x00\x00"
        data = m.encode_markers2([COLOR, RawEntry("CUE", odd), BPMLOCK])

        decoded = m.decode_markers2(data)

        assert decoded[1] == RawEntry("CUE", odd)
        assert m.encode_markers2(decoded) == data

    @pytest.mark.parametrize("payload", [b"", b"\x00" * 5, b"\x00" * 12 + b"x"])
    def test_truncated_or_unterminated_cue_is_kept_verbatim(self, payload):
        assert m._parse_cue(payload) == RawEntry("CUE", payload)

    def test_invalid_utf8_name_is_kept_verbatim(self):
        payload = b"\x00\x00" + struct.pack(">I", 1) + b"\x00\xcc\x00\x00\x00\x00\xff\xfe\x00"

        assert isinstance(m._parse_cue(payload), RawEntry)

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(index=-1), dict(index=256), dict(position_ms=-1), dict(position_ms=2**32),
            dict(color="red"), dict(color="#12345"), dict(color="#gggggg"), dict(name="a\x00b"),
        ],
    )
    def test_invalid_cue_values_are_rejected(self, kwargs):
        with pytest.raises(MarkersError):
            cue(**kwargs)


class TestDecodingErrors:
    def test_missing_prefix(self):
        with pytest.raises(MarkersError, match="01 01"):
            m.decode_markers2(b"hello")

    def test_not_base64(self):
        with pytest.raises(MarkersError, match="base64"):
            m.decode_markers2(b"\x01\x01!!!not base64!!!")

    def test_decoded_prefix_missing(self):
        with pytest.raises(MarkersError, match="Decoded"):
            m.decode_markers2(b"\x01\x01" + base64.b64encode(b"xx"))

    def test_entry_shorter_than_its_length(self):
        raw = b"\x01\x01COLOR\x00" + struct.pack(">I", 99) + b"\x00"

        with pytest.raises(MarkersError, match="shorter"):
            m.decode_markers2(b"\x01\x01" + base64.b64encode(raw))

    def test_data_ending_inside_an_entry_header(self):
        raw = b"\x01\x01COLOR\x00\x00"

        with pytest.raises(MarkersError, match="entry header"):
            m.decode_markers2(b"\x01\x01" + base64.b64encode(raw))

    def test_empty_tag_has_no_entries(self):
        assert m.decode_markers2(m.encode_markers2([])) == []


class TestReplaceCues:
    def test_replaces_cues_in_place_and_keeps_the_rest(self):
        old = entries_with(cue(0, 100), cue(1, 200))
        new = [cue(0, 111, "#00cc00"), cue(1, 222, "#00cc00"), cue(2, 333, "#00cc00")]

        assert m.replace_cues(old, new) == entries_with(*new)

    def test_adds_cues_after_color_when_there_were_none(self):
        assert m.replace_cues([COLOR, BPMLOCK], [cue()]) == [COLOR, cue(), BPMLOCK]

    def test_adds_cues_first_when_there_is_no_color(self):
        assert m.replace_cues([BPMLOCK], [cue()]) == [cue(), BPMLOCK]

    def test_no_cues_removes_them_all(self):
        assert m.replace_cues(entries_with(cue(0), cue(1)), []) == [COLOR, BPMLOCK]

    def test_new_cues_are_ordered_by_slot(self):
        result = m.replace_cues([COLOR, BPMLOCK], [cue(3, 30), cue(0, 10), cue(1, 20)])

        assert [c.index for c in result if isinstance(c, CueEntry)] == [0, 1, 3]

    def test_other_entries_keep_their_order(self):
        old = [COLOR, RawEntry("LOOP", b"\x01"), cue(0), RawEntry("FLIP", b"\x02"), BPMLOCK]

        result = m.replace_cues(old, [cue(0, 5)])

        assert [e.name if isinstance(e, RawEntry) else "CUE" for e in result] == [
            "COLOR", "LOOP", "CUE", "FLIP", "BPMLOCK",
        ]

    def test_replaces_a_non_canonical_cue_too(self):
        odd = RawEntry("CUE", b"\x07" * 13)

        assert m.replace_cues([COLOR, odd, BPMLOCK], [cue()]) == [COLOR, cue(), BPMLOCK]

    def test_duplicate_slots_are_refused(self):
        with pytest.raises(MarkersError, match="same slot"):
            m.replace_cues([COLOR], [cue(0), cue(0)])

    def test_does_not_change_the_input(self):
        old = entries_with(cue(0))
        snapshot = list(old)

        m.replace_cues(old, [cue(1)])

        assert old == snapshot
