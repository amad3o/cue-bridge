"""The terminal look: what is printed, and that nothing a library can contain is read as formatting."""

import asyncio
from types import SimpleNamespace

import pytest
from rich.console import Console

from cuebridge import agent_ui
from cuebridge.agent_ui import TerminalUI


@pytest.fixture
def ui():
    return TerminalUI(Console(record=True, width=100, force_terminal=False))


def printed(ui):
    return ui.console.export_text()


class TestParseArguments:
    def test_reads_a_json_object_or_a_dict(self):
        assert agent_ui.parse_arguments('{"a": 1}') == {"a": 1}
        assert agent_ui.parse_arguments({"a": 1}) == {"a": 1}

    @pytest.mark.parametrize("raw", [None, ""])
    def test_nothing_is_an_empty_dict(self, raw):
        assert agent_ui.parse_arguments(raw) == {}

    @pytest.mark.parametrize("raw", ["not json", "[1]", "5", 5])
    def test_anything_else_is_none(self, raw):
        assert agent_ui.parse_arguments(raw) is None


class TestSummaries:
    def test_long_values_are_cut(self):
        text = agent_ui.short("x" * 500, 30)

        assert len(text) == 30
        assert text.endswith("…")

    def test_short_values_are_kept(self):
        assert agent_ui.short("abc") == "'abc'"

    def test_the_summary_lists_each_argument(self):
        assert agent_ui.argument_summary({"dry_run": False, "limit": 5}) == "dry_run=False, limit=5"
        assert agent_ui.argument_summary({}) == ""

    def test_unreadable_arguments_are_said_so(self):
        assert "could not be read" in agent_ui.argument_summary(None)


class TestTheApprovalPanel:
    def test_it_names_the_tool_and_every_argument(self, ui):
        ui.console.print(ui.approval_panel("sync_cues_to_serato", {"dry_run": False, "expected_files": 3}))
        out = printed(ui)

        assert "Approval needed" in out
        assert "This will really change something." in out
        assert "sync_cues_to_serato" in out
        assert "expected_files" in out
        assert "3" in out

    def test_unreadable_arguments_are_flagged(self, ui):
        ui.console.print(ui.approval_panel("sync_cues_to_serato", None))

        assert "could not be read" in printed(ui)

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [("y", True), ("Y", True), ("yes", True), (" YES ", True), ("", False), ("n", False), ("yes please", False), ("ok", False)],
    )
    def test_only_y_or_yes_goes_ahead(self, ui, monkeypatch, answer, expected):
        monkeypatch.setattr("builtins.input", lambda *a: answer)

        assert asyncio.run(ui.ask("set_default_cue", {"dry_run": False})) is expected


class TestNothingIsReadAsMarkup:
    """Track titles and paths come from the user's library, so brackets in them must print as they are."""

    TITLE = "[bold red]Remix[/] [link=http://x]click[/link]"

    def test_in_the_approval_panel(self, ui):
        ui.console.print(ui.approval_panel("set_default_cue", {"title": self.TITLE}))

        assert "[bold red]Remix[/]" in printed(ui)

    def test_in_a_tool_line(self, ui):
        ui.tool_started("search_tracks", {"query": self.TITLE})

        assert "[bold red]Remix[/]" in printed(ui)

    def test_in_a_tool_name_or_error(self, ui):
        ui.tool_finished("[red]weird[/]", 0.2)
        ui.show_error("failed on [bold]x[/]")
        out = printed(ui)

        assert "[red]weird[/]" in out
        assert "[bold]x[/]" in out


class TestToolLines:
    def tool(self, name="compare_libraries"):
        return SimpleNamespace(name=name)

    def test_a_started_and_a_finished_line(self, ui):
        hooks = ui.hooks()
        context = SimpleNamespace(tool_call_id="c1", tool_arguments='{"limit": 5}')

        asyncio.run(hooks.on_tool_start(context, None, self.tool()))
        asyncio.run(hooks.on_tool_end(context, None, self.tool(), "result"))
        out = printed(ui)

        assert "→ compare_libraries(limit=5)" in out
        assert "✓ compare_libraries" in out
        assert hooks.started == {}

    def test_a_finish_without_a_start_still_prints(self, ui):
        asyncio.run(ui.hooks().on_tool_end(SimpleNamespace(tool_call_id="x"), None, self.tool(), "r"))

        assert "✓ compare_libraries" in printed(ui)

    def test_two_calls_at_once_are_timed_separately(self, ui):
        hooks = ui.hooks()
        first, second = SimpleNamespace(tool_call_id="a", tool_arguments="{}"), SimpleNamespace(tool_call_id="b", tool_arguments="{}")

        asyncio.run(hooks.on_tool_start(first, None, self.tool("one")))
        asyncio.run(hooks.on_tool_start(second, None, self.tool("two")))
        assert set(hooks.started) == {"a", "b"}

        asyncio.run(hooks.on_tool_end(first, None, self.tool("one"), "r"))
        assert set(hooks.started) == {"b"}


class TestFailedTools:
    ERROR = {"type": "text", "text": "Error executing tool get_track_cues: No library is loaded. Load one first."}

    def test_a_failure_is_recognised_in_a_dict_or_a_string(self):
        assert agent_ui.failure_reason(self.ERROR) == "No library is loaded. Load one first."
        assert agent_ui.failure_reason(self.ERROR["text"]) == "No library is loaded. Load one first."

    @pytest.mark.parametrize("result", ["fine", {"type": "text", "text": "ok"}, None, 5, {"type": "image"}])
    def test_anything_else_is_not_a_failure(self, result):
        assert agent_ui.failure_reason(result) is None

    def test_a_failed_tool_line_shows_the_reason(self, ui):
        hooks = ui.hooks()
        context = SimpleNamespace(tool_call_id="c1", tool_arguments="{}")
        tool = SimpleNamespace(name="get_track_cues")

        asyncio.run(hooks.on_tool_start(context, None, tool))
        asyncio.run(hooks.on_tool_end(context, None, tool, self.ERROR))
        out = printed(ui)

        assert "✗ get_track_cues" in out
        assert "No library is loaded" in out
        assert "✓" not in out


class TestReplies:
    def test_markdown_is_rendered(self, ui):
        ui.show_reply("**Done.** 3 tracks changed.\n\n- one\n- two")
        out = printed(ui)

        assert "**" not in out
        assert "Done." in out
        assert "one" in out

    def test_no_reply_is_said_so(self, ui):
        ui.show_reply(None)

        assert "(no reply)" in printed(ui)


class TestTheBanner:
    def test_it_counts_the_tools_and_the_gated_ones(self, ui):
        ui.banner(27, 14)
        out = printed(ui)

        assert "27 tools" in out
        assert "14 of them ask you first" in out


class TestThinking:
    def test_the_spinner_wraps_work_and_lets_lines_through(self, ui):
        with ui.thinking():
            ui.tool_started("x", {})

        assert "→ x()" in printed(ui)
