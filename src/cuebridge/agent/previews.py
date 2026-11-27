"""Which real changes have had their dry run shown to the user, and what that dry run said.

The history given to a turn holds only completed turns, each of which ended with a reply the user saw and then answered.
So a successful dry run found there is one the user has had the chance to see. A dry run made in the current turn is not
in it, which is the point: the agent can't preview and then ask to apply without the user getting a word in. (That the
reply described the preview well is not something code can check, so the approval panel shows the dry run's own result.)

A dry run covers one real call of the same tool with the same arguments, ignoring dry_run and the expected_* guards.
"""

import json
from typing import Any

from cuebridge.agent.protocol import ERROR_PREFIX, parse_arguments, short

DECLINED = "The user declined this change. Nothing was changed."
NOT_PREVIEWED = (
    "Refused, and nothing was changed: the user hasn't seen a dry run of this exact call. Call this tool with dry_run=true "
    "and the same other arguments, tell the user what it would change, and stop. Apply it only after the user replies."
)
MAX_LINES = 18
MAX_LINE_CHARS = 110
MAX_ITEMS = 4


def is_real_change(arguments: dict[str, Any] | None) -> bool:
    """Whether a call to a writing tool would really change something.

    Every writing tool takes dry_run and defaults to true, so the call is a preview unless dry_run is
    explicitly false. Anything that is not clearly a preview (a malformed value, or arguments that could
    not be read at all) counts as a real change.
    """
    if arguments is None:
        return True

    return arguments.get("dry_run", True) is not True


def call_key(name: str, arguments: dict[str, Any]) -> str:
    """What makes two calls the same change. An argument left out and one given as null are the same."""
    relevant = {k: v for k, v in arguments.items() if k != "dry_run" and not k.startswith("expected_") and v is not None}
    return json.dumps([name, relevant], sort_keys=True, default=str)


def output_text(output: Any) -> str:
    """A tool result as text: a plain string, or the list of text parts that MCP results are kept as."""
    if isinstance(output, str):
        return output

    if isinstance(output, list):
        return "\n".join(part["text"] for part in output if isinstance(part, dict) and isinstance(part.get("text"), str))

    return ""


def shown_previews(history: Any) -> dict[str, str]:
    """The dry runs in `history` that still cover a real call: call key -> the dry run's result."""
    if not isinstance(history, list):
        return {}

    items = [item for item in history if isinstance(item, dict)]
    outputs = {item.get("call_id"): output_text(item.get("output")) for item in items if item.get("type") == "function_call_output"}
    previews: dict[str, str] = {}

    for item in items:
        if item.get("type") != "function_call":
            continue

        arguments = parse_arguments(item.get("arguments"))
        result = outputs.get(item.get("call_id"))

        if arguments is None or not result or result.startswith(ERROR_PREFIX) or result in (DECLINED, NOT_PREVIEWED):
            continue  # unreadable, unfinished, failed or refused: it neither previews nor uses up a preview

        key = call_key(str(item.get("name")), arguments)

        if is_real_change(arguments):
            previews.pop(key, None)  # applied: another run needs a new dry run
        else:
            previews[key] = result

    return previews


def describe(result: str) -> list[str]:
    """A dry run's result as a few short lines for the approval panel: its summary first, then the rest."""
    try:
        parsed = json.loads(result)
    except ValueError:
        parsed = None

    if not isinstance(parsed, dict):
        lines = result.splitlines()
    else:
        lines = [str(parsed["summary"])] if parsed.get("summary") else []

        for name, value in parsed.items():
            if name == "summary":
                continue

            if isinstance(value, list):
                lines.append(f"{name}: {len(value)} item(s)")
                lines += [f"  {short(item, MAX_LINE_CHARS)}" for item in value[:MAX_ITEMS]]

                if len(value) > MAX_ITEMS:
                    lines.append(f"  … and {len(value) - MAX_ITEMS} more")
            else:
                lines.append(f"{name}: {short(value, MAX_LINE_CHARS)}")

    # The summary is kept whole (the screen wraps it); the other lines are cut, and so is their number.
    cut = lines[:1] + [line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 1] + "…" for line in lines[1:]]

    return cut if len(cut) <= MAX_LINES else [*cut[: MAX_LINES - 1], f"… {len(cut) - MAX_LINES + 1} more line(s) not shown"]
