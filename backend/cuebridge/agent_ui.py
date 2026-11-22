"""How the agent looks in the terminal: a spinner while it works, a line per tool call, markdown replies, and the approval panel.

Nothing here decides anything. The approval rule lives in cuebridge.agent; this module only shows the question and reads the answer.
Everything is built from rich Text objects, never markup strings, so a track title with [brackets] in it cannot be read as formatting.
"""

import asyncio
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from agents import RunHooks
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

MAX_VALUE_CHARS = 70
YES = ("y", "yes")
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


def argument_summary(arguments: dict[str, Any] | None) -> str:
    if arguments is None:
        return "(arguments could not be read)"

    return ", ".join(f"{key}={short(value, 40)}" for key, value in arguments.items())


def failure_reason(result: Any) -> str | None:
    """Why a tool call failed, or None if it did not. Display only: it goes by how the MCP library words an error."""
    text = result.get("text") if isinstance(result, dict) else result

    if not isinstance(text, str) or not text.startswith(ERROR_PREFIX):
        return None

    return text.split(":", 1)[1].strip() if ":" in text else text


class ToolLines(RunHooks):
    """Prints a line when each tool starts and when it finishes, with how long it took."""

    def __init__(self, ui: "TerminalUI") -> None:
        self.ui = ui
        self.started: dict[str, float] = {}

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        call_id = getattr(context, "tool_call_id", None) or tool.name
        self.started[call_id] = time.monotonic()
        arguments = parse_arguments(getattr(context, "tool_arguments", None))
        self.ui.tool_started(tool.name, arguments)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        call_id = getattr(context, "tool_call_id", None) or tool.name
        began = self.started.pop(call_id, None)
        self.ui.tool_finished(tool.name, None if began is None else time.monotonic() - began, failure_reason(result))


class TerminalUI:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    @contextmanager
    def thinking(self, label: str = "Working…") -> Iterator[None]:
        """A spinner while the model and the tools work. Lines printed meanwhile appear above it."""
        with self.console.status(Text(label, style="dim"), spinner="dots"):
            yield

    def hooks(self) -> ToolLines:
        return ToolLines(self)

    def tool_started(self, name: str, arguments: dict[str, Any] | None) -> None:
        self.console.print(Text.assemble(("  → ", "dim"), (name, "bold"), (f"({argument_summary(arguments)})", "dim")))

    def tool_finished(self, name: str, seconds: float | None, failure: str | None = None) -> None:
        took = "" if seconds is None else f" {seconds:.1f}s"

        if failure is None:
            self.console.print(Text.assemble(("  ✓ ", "green"), (name, ""), (took, "dim")))
        else:
            self.console.print(Text.assemble(("  ✗ ", "red"), (name, ""), (took, "dim"), (f" {short(failure, 110)}", "red")))

    def banner(self, tool_count: int, gated_count: int) -> None:
        self.console.print(
            Panel(
                Text.assemble(
                    ("CueBridge agent\n", "bold"),
                    (f"{tool_count} tools. {gated_count} of them ask you first before a real change; dry runs and reads do not.\n", ""),
                    ("An empty line or Ctrl-D quits.", "dim"),
                ),
                border_style="cyan",
            )
        )

    def read_line(self) -> str:
        """Blocking; run it in a thread. Raises EOFError on Ctrl-D."""
        return self.console.input(Text("\nyou> ", style="bold cyan")).strip()

    def show_reply(self, text: str | None) -> None:
        self.console.print()
        self.console.print(Markdown(text) if text else Text("(no reply)", style="dim"))

    def show_error(self, message: str) -> None:
        self.console.print(Panel(Text(message), title="Something went wrong", border_style="red"))

    def approval_panel(self, name: str, arguments: dict[str, Any] | None) -> Panel:
        table = Table.grid(padding=(0, 2))

        if arguments is None:
            table.add_row(Text("(the arguments could not be read)", style="yellow"), "")
        else:
            for key, value in arguments.items():
                table.add_row(Text(key, style="dim"), Text(short(value, 100)))

        return Panel(
            Group(
                Text.assemble(("This will really change something.\n", "bold red"), ("Tool: ", "dim"), (name, "bold")),
                Text(),
                table,
            ),
            title="Approval needed",
            border_style="red",
        )

    async def ask(self, name: str, arguments: dict[str, Any] | None) -> bool:
        """Show the panel and wait for an answer. Only y or yes goes ahead."""
        self.console.print()
        self.console.print(self.approval_panel(name, arguments))
        answer = await asyncio.to_thread(self.console.input, Text("Go ahead? [y/N] ", style="bold"))
        return answer.strip().lower() in YES
