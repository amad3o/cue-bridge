"""The conversation between the agent and its screen: one JSON object per line.

The agent (Python) owns the model, the tools and the approval rule. The screen (the Ink app in ui/) only shows
things and passes back what the user typed. They talk over the agent's stdin and stdout.

Agent to screen (stdout), each a JSON object with a "type":
  ready             tools, gated                         the server is up; how many tools, how many ask first
  thinking          on                                   the model or a tool is working (true) or done (false)
  tool_start        id, name, arguments                  a tool call began
  tool_end          id, name, seconds, failure           it finished; failure is a reason or null
  approval_request  id, name, arguments                  a call would really change something: waiting for an answer
  reply             text                                 the model's answer (markdown)
  error             message                              the turn failed; the chat goes on

Screen to agent (stdin):
  user_message      text
  approval_response id, approve                          only approve === true (a real boolean) with the matching id goes ahead
  quit

The approval rule itself is in cuebridge.agent.runner. A closed stdin, a wrong id, a malformed line or anything but
`approve: true` all count as a refusal, so a broken or hostile screen can only ever say no.
"""

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from agents import RunHooks

MAX_VALUE_CHARS = 70
ERROR_PREFIX = "Error executing tool"  # how the MCP library words a tool that failed


def short(value: Any, limit: int = MAX_VALUE_CHARS) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_arguments(raw: Any) -> dict[str, Any] | None:
    """A tool call's arguments as a dict (None if they are not a JSON object)."""
    if isinstance(raw, dict):
        return raw

    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return None

    return parsed if isinstance(parsed, dict) else None


def failure_reason(result: Any) -> str | None:
    """Why a tool call failed, or None if it did not. Display only: it goes by how the MCP library words an error."""
    text = result.get("text") if isinstance(result, dict) else result

    if not isinstance(text, str) or not text.startswith(ERROR_PREFIX):
        return None

    return text.split(":", 1)[1].strip() if ":" in text else text


def parse_message(line: str) -> dict[str, Any] | None:
    """One line from the screen as a message, or None if it is not a JSON object with a string "type"."""
    try:
        message = json.loads(line)
    except ValueError:
        return None

    return message if isinstance(message, dict) and isinstance(message.get("type"), str) else None


class ToolLines(RunHooks):
    """Tells the screen when each tool starts and finishes, with how long it took."""

    def __init__(self, ui: "JsonUI") -> None:
        self.ui = ui
        self.started: dict[str, float] = {}

    @staticmethod
    def call_id(context: Any, tool: Any) -> str:
        return getattr(context, "tool_call_id", None) or tool.name

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        call_id = self.call_id(context, tool)
        self.started[call_id] = time.monotonic()
        self.ui.tool_started(call_id, tool.name, parse_arguments(getattr(context, "tool_arguments", None)))

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        call_id = self.call_id(context, tool)
        began = self.started.pop(call_id, None)
        self.ui.tool_finished(call_id, tool.name, None if began is None else time.monotonic() - began, failure_reason(result))


class JsonUI:
    """The agent's side of the conversation: sends events out, and reads the screen's messages from a queue.

    `send` writes one line (a JSON string) to the screen. The queue holds the screen's messages, with None
    standing for a closed stdin; `listen` fills it from a stream.
    """

    def __init__(self, send: Callable[[str], None]) -> None:
        self.send = send
        self.incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.approvals = 0

    def emit(self, type: str, **fields: Any) -> None:
        self.send(json.dumps({"type": type, **fields}, ensure_ascii=False, default=str))

    def listen(self, stream: Any) -> None:
        """Read the screen's lines on a thread, passing each valid one to the queue (and None when the stream ends)."""
        loop = asyncio.get_running_loop()

        def read() -> None:
            for line in stream:
                message = parse_message(line)

                if message is not None:
                    loop.call_soon_threadsafe(self.incoming.put_nowait, message)

            loop.call_soon_threadsafe(self.incoming.put_nowait, None)

        threading.Thread(target=read, daemon=True).start()

    async def next_user_text(self) -> str | None:
        """The next thing the user typed, or None to stop (quit, or the screen went away). Stray approval answers are ignored."""
        while True:
            message = await self.incoming.get()

            if message is None or message["type"] == "quit":
                return None

            if message["type"] == "user_message" and isinstance(message.get("text"), str):
                return message["text"]

    @contextmanager
    def thinking(self) -> Iterator[None]:
        self.emit("thinking", on=True)

        try:
            yield
        finally:
            self.emit("thinking", on=False)

    def hooks(self) -> ToolLines:
        return ToolLines(self)

    def ready(self, tool_count: int, gated_count: int) -> None:
        self.emit("ready", tools=tool_count, gated=gated_count)

    def tool_started(self, call_id: str, name: str, arguments: dict[str, Any] | None) -> None:
        self.emit("tool_start", id=call_id, name=name, arguments=arguments)

    def tool_finished(self, call_id: str, name: str, seconds: float | None, failure: str | None = None) -> None:
        self.emit("tool_end", id=call_id, name=name, seconds=seconds, failure=failure)

    def show_reply(self, text: str | None) -> None:
        self.emit("reply", text=text or "")

    def show_error(self, message: str) -> None:
        self.emit("error", message=message)

    async def ask(self, name: str, arguments: dict[str, Any] | None) -> bool:
        """Ask the screen about one real change. Only a matching approval_response with approve === true goes ahead."""
        self.approvals += 1
        request_id = f"approval-{self.approvals}"
        self.emit("approval_request", id=request_id, name=name, arguments=arguments)

        while True:
            message = await self.incoming.get()

            if message is None:  # the screen went away: that is a no
                return False

            if message["type"] == "approval_response" and message.get("id") == request_id:
                return message.get("approve") is True

            if message["type"] == "quit":
                self.incoming.put_nowait(message)  # so the chat loop also stops
                return False
