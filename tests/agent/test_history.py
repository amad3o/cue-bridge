"""Shortening old tool results, and what it must leave alone for the preview rule."""

import json

import pytest

from cuebridge.agent import history, previews

WRITERS = frozenset({"sync_cues_to_serato"})
LONG = json.dumps({"summary": "Would change 3 files.", "files": "x" * 4000})


def pair(call_id, arguments, output, name="sync_cues_to_serato"):
    return [
        {"type": "function_call", "name": name, "arguments": json.dumps(arguments), "call_id": call_id},
        {"type": "function_call_output", "call_id": call_id, "output": output},
    ]


def turns(*contents):
    """A history with one turn per argument: a user message, that turn's items and a reply."""
    out = []

    for number, items in enumerate(contents):
        out += [{"role": "user", "content": f"message {number}"}, *items, {"role": "assistant", "content": "reply"}]

    return out


def aged(*first):
    """A history whose first turn holds these items and is old enough to be shortened."""
    return turns([item for group in first for item in group], [], [])


def outputs(items):
    return {i["call_id"]: previews.output_text(i["output"]) for i in items if i.get("type") == "function_call_output"}


def read(call_id="r1", output=LONG):
    return pair(call_id, {}, output, name="compare_cues")


def test_a_long_old_result_becomes_a_note_that_keeps_its_summary():
    note = outputs(history.trim(aged(read()), WRITERS))["r1"]

    assert note.startswith(previews.TRIMMED) and "Would change 3 files." in note
    assert f"{len(LONG):,} characters" in note and len(note) < 300


def test_the_mcp_shape_is_read_too():
    mcp = [{"type": "input_text", "text": LONG}]

    assert outputs(history.trim(aged(read(output=mcp)), WRITERS))["r1"].startswith(previews.TRIMMED)


def test_a_result_without_a_summary_just_says_it_was_shortened():
    note = outputs(history.trim(aged(read(output="y" * 3000)), WRITERS))["r1"]

    assert note.startswith(previews.TRIMMED) and "summary" not in note


def test_a_very_long_summary_is_cut():
    long_summary = json.dumps({"summary": "s" * 5000})

    assert len(outputs(history.trim(aged(read(output=long_summary)), WRITERS))["r1"]) < history.MAX_SUMMARY_CHARS + 250


def test_the_latest_turns_are_left_whole():
    chat = turns(read("old"), read("previous"), read("latest"))
    after = outputs(history.trim(chat, WRITERS))

    assert after["old"].startswith(previews.TRIMMED)
    assert after["previous"] == LONG and after["latest"] == LONG


def test_a_short_conversation_is_untouched():
    chat = turns(read("a"), read("b"))

    assert history.trim(chat, WRITERS) == chat


def test_a_short_result_is_left_alone():
    assert outputs(history.trim(aged(read(output="z" * history.MAX_KEPT_CHARS)), WRITERS))["r1"] == "z" * history.MAX_KEPT_CHARS


def test_the_original_history_is_not_changed_and_nothing_is_dropped():
    chat = aged(read())
    copy = json.loads(json.dumps(chat))
    after = history.trim(chat, WRITERS)

    assert chat == copy and len(after) == len(chat)


def test_trimming_again_changes_nothing_more():
    once = history.trim(aged(read()), WRITERS)

    assert history.trim(once, WRITERS) == once


@pytest.mark.parametrize("arguments", [{}, {"dry_run": True}])
def test_an_unused_dry_run_of_a_writing_tool_stays_whole(arguments):
    after = history.trim(aged(pair("p", arguments, LONG)), WRITERS)

    assert outputs(after)["p"] == LONG
    assert previews.shown_previews(after) == previews.shown_previews(aged(pair("p", arguments, LONG)))


def test_only_the_latest_dry_run_of_the_same_change_stays_whole():
    after = outputs(history.trim(aged(pair("first", {}, LONG), pair("second", {}, LONG)), WRITERS))

    assert after["first"].startswith(previews.TRIMMED) and after["second"] == LONG


def test_dry_runs_of_different_changes_each_stay_whole():
    after = outputs(history.trim(aged(pair("a", {"track_ids": ["1"]}, LONG), pair("b", {"track_ids": ["2"]}, LONG)), WRITERS))

    assert after == {"a": LONG, "b": LONG}


def test_a_used_up_dry_run_and_the_applied_result_are_both_shortened_and_cover_nothing():
    after = history.trim(aged(pair("p", {}, LONG), pair("done", {"dry_run": False}, LONG)), WRITERS)

    assert all(text.startswith(previews.TRIMMED) for text in outputs(after).values())
    assert previews.shown_previews(after) == {}


@pytest.mark.parametrize("untouchable", ["Error executing tool x: " + "e" * 4000, previews.NOT_PREVIEWED, previews.DECLINED])
def test_a_failed_or_refused_call_is_never_shortened_so_its_dry_run_still_counts(untouchable):
    chat = aged(pair("p", {}, LONG), pair("failed", {"dry_run": False}, untouchable))
    after = history.trim(chat, WRITERS)

    assert outputs(after)["failed"] == untouchable and outputs(after)["p"] == LONG
    assert previews.shown_previews(after) == previews.shown_previews(chat) != {}


def test_a_shortened_dry_run_never_counts_as_a_preview():
    note = history.shortened(LONG)

    assert previews.shown_previews(pair("p", {}, note)) == {}
    # and a real call whose result was shortened still counts as applied
    assert previews.shown_previews(pair("p", {}, "preview") + pair("done", {"dry_run": False}, note)) == {}
