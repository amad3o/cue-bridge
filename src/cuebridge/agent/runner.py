"""CueBridge's own agent: calls the CueBridge tools through the OpenAI Agents SDK, with a screen of its own.

Run it with:  make agent      (the Ink screen in src/ui/ starts this module as a child process)

This process has no screen. It reads the user's messages from stdin and writes events to stdout, one JSON
object per line (see cuebridge.agent.protocol). It starts the MCP server (`python -m cuebridge.mcp_server`) as
a subprocess and gets all its tools. Reading and dry-run calls go straight through. Any call that would really
change something (a write tool with dry_run=false) is refused unless its dry run happened before the user's latest
message (see cuebridge.agent.previews), and then stops and asks the screen. Both rules are enforced here, not by the
model and not by the screen: the model cannot skip them, and the screen can only say no.
"""

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from agents import Agent, Runner, set_tracing_disabled
from agents.mcp import MCPServerStdio
from dotenv import load_dotenv

from cuebridge.agent.previews import DECLINED, NOT_PREVIEWED, call_key, describe, is_real_change, shown_previews
from cuebridge.agent.protocol import JsonUI, parse_arguments
from cuebridge.instructions import GUIDANCE

SRC_DIR = Path(__file__).resolve().parents[2]  # src/, which holds the package
PROJECT_DIR = SRC_DIR.parent  # the repository root, which holds .env, tests and the virtualenv
MAX_TURNS = 40
# Some tools are slow (reading the open Music app takes about half a minute; a Serato sync touches many files).
TOOL_TIMEOUT_SECONDS = 600

# The shared rules, plus what is only true of this agent.
INSTRUCTIONS = (
    GUIDANCE
    + """\
- This program refuses a real change unless the same call's dry run came before the user's latest message, so end
  your reply after a dry run. It then asks the user to confirm. If they decline, don't retry it.
- Keep answers short. Report what the tools returned, including failures, exactly.
"""
)

# Asks the user about one call that would really change something, given its dry run as a few lines; True to go ahead.
Ask = Callable[[str, dict[str, Any], list[str]], Awaitable[bool]]


def server_params() -> dict[str, Any]:
    """How to start the CueBridge MCP server: the same command the README gives, with src/ on the path."""
    return {
        "command": sys.executable,
        "args": ["-m", "cuebridge.mcp_server"],
        # The server logs every failed tool call at INFO, which would print over the chat.
        "env": {"PYTHONPATH": str(SRC_DIR), "CUEBRIDGE_LOG_LEVEL": "WARNING"},
    }


def tool_needs_approval(tool: Any) -> bool:
    """True for any tool not marked read-only. A tool with no annotations is treated as one that writes."""
    annotations = getattr(tool, "annotations", None)
    # mcp 2.x calls the field read_only_hint; older releases call it readOnlyHint.
    read_only = getattr(annotations, "read_only_hint", None) or getattr(annotations, "readOnlyHint", None)
    return read_only is not True


async def needs_approval(_context: Any, _agent: Any, tool: Any) -> bool:
    """The SDK's approval policy for the server's tools. It sees the tool but not its arguments."""
    return tool_needs_approval(tool)


def call_arguments(item: Any) -> dict[str, Any] | None:
    """The arguments of a pending tool call, as a dict (None if they cannot be read)."""
    raw = item.raw_item
    return parse_arguments(raw.get("arguments") if isinstance(raw, dict) else getattr(raw, "arguments", None))


async def run_turn(agent: Agent, conversation: Any, ask: Ask, ui: JsonUI | None = None) -> Any:
    """Run the agent until it answers, stopping for approval on each call that would really change something.

    `conversation` is the list of messages so far (it may end with the user's new message). Dry runs and
    read-only calls are approved here without asking. A real change is refused without asking unless
    `conversation` already holds its dry run; otherwise the user is asked. A refused call is reported to
    the model as refused. With a `ui`, the screen is told when the model and tools are working, and about
    each tool call.
    """
    previews = shown_previews(conversation)  # taken before the run, so this turn's dry runs don't count
    thinking = ui.thinking if ui else nullcontext
    hooks = ui.hooks() if ui else None

    with thinking():
        result = await Runner.run(agent, conversation, max_turns=MAX_TURNS, hooks=hooks)

    while result.interruptions:
        state = result.to_state()

        for item in result.interruptions:
            name = item.tool_name or "(unknown tool)"
            arguments = call_arguments(item)

            if not is_real_change(arguments):
                state.approve(item)
                continue

            key = None if arguments is None else call_key(name, arguments)

            if key not in previews:
                state.reject(item, rejection_message=NOT_PREVIEWED)
            elif await ask(name, arguments, describe(previews[key])):
                del previews[key]  # one dry run covers one real change
                state.approve(item)
            else:
                state.reject(item, rejection_message=DECLINED)

        with thinking():
            result = await Runner.run(agent, state, max_turns=MAX_TURNS, hooks=hooks)

    return result


def build_agent(server: MCPServerStdio) -> Agent:
    return Agent(
        name="CueBridge",
        instructions=INSTRUCTIONS,
        mcp_servers=[server],
        model=os.environ.get("CUEBRIDGE_MODEL") or None,
    )


async def chat(ui: JsonUI, stdin: Any) -> None:
    # The SDK sends traces to OpenAI by default; the tool results hold the user's library, so keep them local.
    set_tracing_disabled(True)
    ui.listen(stdin)

    async with MCPServerStdio(
        params=server_params(),
        cache_tools_list=True,
        client_session_timeout_seconds=TOOL_TIMEOUT_SECONDS,
        require_approval=needs_approval,
    ) as server:
        agent = build_agent(server)
        tools = await server.list_tools()
        ui.ready(len(tools), sum(tool_needs_approval(t) for t in tools))
        conversation: list[Any] = []

        while (line := await ui.next_user_text()) is not None:
            if not line.strip():
                continue

            try:
                result = await run_turn(agent, [*conversation, {"role": "user", "content": line}], ui.ask, ui)
            except Exception as error:  # a failed model call or a turn limit should not end the chat; the question can be retried
                ui.show_error(f"{type(error).__name__}: {error}")
                continue

            conversation = result.to_input_list()
            ui.show_reply(result.final_output)


def main() -> None:
    load_dotenv(PROJECT_DIR / ".env")

    # stdout belongs to the protocol. Keep a private copy of it, then point fd 1 at stderr so that anything
    # else that prints (a library warning, a stray print) cannot corrupt the stream the screen is reading.
    protocol_out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    ui = JsonUI(lambda line: (protocol_out.write(line + "\n"), protocol_out.flush()))

    if not os.environ.get("OPENAI_API_KEY"):
        ui.show_error(
            "Put OPENAI_API_KEY in .env (or the environment) first. "
            "Note that your tool results (track names, file paths, playlists) are sent to OpenAI."
        )
        sys.exit(1)

    try:
        asyncio.run(chat(ui, sys.stdin))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
