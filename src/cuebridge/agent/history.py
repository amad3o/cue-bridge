"""Keeps the conversation from growing without bound: old, large tool results are shortened.

The whole history is sent with every model call, so each large result is paid for again and again. After a turn,
the results from before the last KEEP_TURNS turns that are longer than MAX_KEPT_CHARS are replaced by a short note,
which keeps the tool's own summary if it has one. The model can call the tool again if it needs the rest.

Two kinds of result are never shortened, because the preview rule (cuebridge.agent.previews) reads the history:
  * a writing tool's dry run that still covers a real change: the approval panel shows what it said;
  * a failed or refused call: that it failed is why it didn't use up its dry run.
"""

import json
from typing import Any

from cuebridge.agent.previews import TRIMMED, is_untouchable, output_text, unused_preview_calls

KEEP_TURNS = 2  # the latest turns keep every result whole
MAX_KEPT_CHARS = 1500  # older results no longer than this are left alone
MAX_SUMMARY_CHARS = 600


def shortened(result: str) -> str:
    """The note that takes the place of a long result."""
    note = f"{TRIMMED} to save tokens ({len(result):,} characters). Call the tool again if you need the details."

    try:
        parsed = json.loads(result)
    except ValueError:
        return note

    summary = parsed.get("summary") if isinstance(parsed, dict) else None

    if not isinstance(summary, str) or not summary:
        return note

    return f"{note} Its summary was: {summary[:MAX_SUMMARY_CHARS]}"


def trim(history: list[Any], writers: frozenset[str]) -> list[Any]:
    """A copy of `history` with old, large tool results shortened. `writers` names the tools that can change something.

    A turn starts at each message from the user. Nothing in `history` itself is changed.
    """
    starts = [i for i, item in enumerate(history) if isinstance(item, dict) and item.get("role") == "user"]

    if len(starts) <= KEEP_TURNS:
        return list(history)

    recent = starts[-KEEP_TURNS]
    keep = unused_preview_calls(history, writers)
    trimmed = []

    for index, item in enumerate(history):
        if index < recent and isinstance(item, dict) and item.get("type") == "function_call_output" and item.get("call_id") not in keep:
            result = output_text(item.get("output"))

            if len(result) > MAX_KEPT_CHARS and not is_untouchable(result):
                item = {**item, "output": shortened(result)}

        trimmed.append(item)

    return trimmed
