from cuebridge.models import Cue
from cuebridge.services.cue_rules import plan_memory_to_hot


def cue(kind, time_ms, slot=None, name=None):
    return Cue(
        id=f"{kind}-{slot}-{time_ms}", source="rekordbox", trackTitle="T",
        cueIndex=slot, cueName=name, timeMs=time_ms, cueType=kind,
    )


def plan(*cues, **kw):
    return plan_memory_to_hot(list(cues), **kw)


def pairs(result):
    return [(a.cue.timeMs, a.slot) for a in result.assignments]


def test_memory_cues_take_slots_in_time_order():
    result = plan(cue("memory", 30_000), cue("memory", 10_000), cue("memory", 20_000))

    assert pairs(result) == [(10_000, 0), (20_000, 1), (30_000, 2)]


def test_slots_used_by_hot_cues_and_loops_are_skipped():
    result = plan(
        cue("hotcue", 1_000, slot=0),
        cue("loop", 2_000, slot=2),
        cue("memory", 5_000),
        cue("memory", 6_000),
        cue("memory", 7_000),
    )

    assert pairs(result) == [(5_000, 1), (6_000, 3), (7_000, 4)]


def test_memory_loops_do_not_use_a_slot():
    result = plan(cue("loop", 2_000, slot=None), cue("memory", 5_000))

    assert pairs(result) == [(5_000, 0)]


def test_memory_cue_with_a_hot_cue_at_the_same_time_is_skipped():
    result = plan(cue("hotcue", 5_000, slot=3), cue("memory", 5_000), cue("memory", 6_000))

    assert pairs(result) == [(6_000, 0)]
    assert result.already_duplicated == 1


def test_duplicate_memory_cues_at_one_time_get_one_hot_cue():
    result = plan(cue("memory", 5_000), cue("memory", 5_000))

    assert pairs(result) == [(5_000, 0)]


def test_cues_beyond_eight_slots_are_reported_as_overflow():
    result = plan(*[cue("memory", t * 1000) for t in range(1, 11)])

    assert [a.slot for a in result.assignments] == list(range(8))
    assert [c.timeMs for c in result.overflow] == [9_000, 10_000]


def test_a_full_set_of_hot_cues_leaves_everything_as_overflow():
    result = plan(*[cue("hotcue", t, slot=t) for t in range(8)], cue("memory", 99_000))

    assert result.assignments == ()
    assert len(result.overflow) == 1


def test_tracks_without_memory_cues_have_nothing_to_do():
    result = plan(cue("hotcue", 1_000, slot=0))

    assert result.assignments == () and result.overflow == () and result.already_duplicated == 0


def test_custom_slot_count():
    result = plan(cue("memory", 1_000), cue("memory", 2_000), cue("memory", 3_000), max_slots=2)

    assert len(result.assignments) == 2 and len(result.overflow) == 1
