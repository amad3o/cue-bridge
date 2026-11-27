"""Reading the history for dry runs the user has seen, and describing one for the approval panel."""

import json

import pytest

from cuebridge.agent import previews


def pair(call_id, arguments, output, name="sync_cues_to_serato"):
    return [
        {"type": "function_call", "name": name, "arguments": json.dumps(arguments), "call_id": call_id},
        {"type": "function_call_output", "call_id": call_id, "output": output},
    ]


def mcp(text):
    """How the SDK keeps an MCP tool's result in the history."""
    return [{"type": "input_text", "text": text}]


def covered(history, arguments, name="sync_cues_to_serato"):
    return previews.call_key(name, arguments) in previews.shown_previews(history)


def test_an_mcp_shaped_dry_run_covers_the_same_call():
    history = pair("1", {"track_ids": ["7"]}, mcp('{"summary": "Would change 1 file"}'))

    assert covered(history, {"track_ids": ["7"], "dry_run": False, "expected_files": 1})
    assert not covered(history, {"track_ids": ["8"], "dry_run": False})
    assert not covered(history, {"track_ids": ["7"], "dry_run": False}, name="undo_serato_sync")


def test_null_and_left_out_are_the_same_argument():
    assert covered(pair("1", {"journal": None}, "preview"), {"dry_run": False})


@pytest.mark.parametrize("output", [mcp("Error executing tool sync_cues_to_serato: Serato is running"), "", previews.NOT_PREVIEWED, previews.DECLINED])
def test_a_failed_or_refused_dry_run_covers_nothing(output):
    assert not covered(pair("1", {}, output), {})


def test_a_call_with_no_result_covers_nothing():
    assert not covered(pair("1", {}, "preview")[:1], {})


def test_an_applied_change_uses_up_the_preview_and_a_new_dry_run_renews_it():
    history = pair("1", {}, "preview") + pair("2", {"dry_run": False}, mcp('{"summary": "Done"}'))

    assert not covered(history, {})
    assert covered(history + pair("3", {}, "new preview"), {})


@pytest.mark.parametrize("output", [mcp("Error executing tool x: Rekordbox is running"), previews.DECLINED, previews.NOT_PREVIEWED])
def test_a_change_that_failed_or_was_refused_does_not_use_it_up(output):
    assert covered(pair("1", {}, "preview") + pair("2", {"dry_run": False}, output), {})


@pytest.mark.parametrize("history", [None, "go", [], ["junk", 5]])
def test_no_usable_history_means_no_previews(history):
    assert previews.shown_previews(history) == {}


def test_describe_leads_with_the_whole_summary():
    summary = "Would change many files. " * 12
    lines = previews.describe(json.dumps({"dryRun": True, "files": [{"track": f"t{i}"} for i in range(9)], "summary": summary}))

    assert lines[0] == summary
    assert "files: 9 item(s)" in lines and "  … and 5 more" in lines


def test_describe_is_bounded_and_says_what_it_left_out():
    lines = previews.describe(json.dumps({f"field{i}": "x" * 500 for i in range(40)}))

    assert len(lines) == previews.MAX_LINES
    assert lines[-1].endswith("more line(s) not shown")
    assert all(len(line) <= previews.MAX_LINE_CHARS for line in lines[1:])


def test_describe_plain_text():
    assert previews.describe("two changes\nnothing written") == ["two changes", "nothing written"]
