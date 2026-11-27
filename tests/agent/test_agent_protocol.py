"""The JSON-lines conversation between the agent and its screen."""

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from cuebridge.agent.protocol import JsonUI, ToolLines, failure_reason, parse_arguments, parse_message, short


class TestParseArguments:
    def test_reads_a_json_object_or_a_dict(self):
        assert parse_arguments('{"a": 1}') == {"a": 1}
        assert parse_arguments({"a": 1}) == {"a": 1}

    @pytest.mark.parametrize("raw", [None, ""])
    def test_nothing_is_an_empty_dict(self, raw):
        assert parse_arguments(raw) == {}

    @pytest.mark.parametrize("raw", ["not json", "[1, 2]", "5", 5])
    def test_anything_else_is_none(self, raw):
        assert parse_arguments(raw) is None


class TestParseMessage:
    def test_a_json_object_with_a_type(self):
        assert parse_message('{"type": "quit"}') == {"type": "quit"}

    @pytest.mark.parametrize("line", ["", "nope", "[1]", '"quit"', "{}", '{"type": 5}'])
    def test_anything_else_is_ignored(self, line):
        assert parse_message(line) is None


class TestShort:
    def test_long_values_are_cut(self):
        assert len(short("x" * 500)) == 70

    def test_short_values_are_kept(self):
        assert short(3) == "3"


class TestFailedTools:
    def test_a_failure_is_recognised_in_a_dict_or_a_string(self):
        assert failure_reason("Error executing tool x: boom") == "boom"
        assert failure_reason({"type": "text", "text": "Error executing tool x: boom"}) == "boom"

    @pytest.mark.parametrize("result", ["fine", None, {"text": 5}, 7])
    def test_anything_else_is_not_a_failure(self, result):
        assert failure_reason(result) is None


@pytest.fixture
def sent():
    return []


@pytest.fixture
def ui(sent):
    async def make():
        return JsonUI(sent.append)

    return asyncio.run(make())


def events(sent):
    return [json.loads(line) for line in sent]


class TestEvents:
    def test_each_event_is_one_json_line(self, sent):
        async def go():
            ui = JsonUI(sent.append)
            ui.ready(27, 14)
            ui.show_reply("hello [world] — ünïcode")
            ui.show_error("bad")

        asyncio.run(go())

        assert all("\n" not in line for line in sent)
        assert events(sent) == [
            {"type": "ready", "tools": 27, "gated": 14},
            {"type": "reply", "text": "hello [world] — ünïcode"},
            {"type": "error", "message": "bad"},
        ]

    def test_no_reply_is_an_empty_string(self, sent):
        async def go():
            JsonUI(sent.append).show_reply(None)

        asyncio.run(go())
        assert events(sent) == [{"type": "reply", "text": ""}]

    def test_thinking_wraps_work_even_when_it_fails(self, sent):
        async def go():
            ui = JsonUI(sent.append)

            with pytest.raises(RuntimeError), ui.thinking():
                raise RuntimeError

        asyncio.run(go())
        assert events(sent) == [{"type": "thinking", "on": True}, {"type": "thinking", "on": False}]


class TestToolLines:
    def tool(self, name="compare_libraries"):
        return SimpleNamespace(name=name)

    def test_a_started_and_a_finished_event(self, sent):
        async def go():
            hooks = JsonUI(sent.append).hooks()
            context = SimpleNamespace(tool_call_id="c1", tool_arguments='{"limit": 5}')
            await hooks.on_tool_start(context, None, self.tool())
            await hooks.on_tool_end(context, None, self.tool(), "ok")

        asyncio.run(go())
        start, end = events(sent)

        assert start == {"type": "tool_start", "id": "c1", "name": "compare_libraries", "arguments": {"limit": 5}}
        assert end["type"] == "tool_end" and end["id"] == "c1" and end["failure"] is None and end["seconds"] >= 0

    def test_a_finish_without_a_start_still_reports(self, sent):
        async def go():
            await ToolLines(JsonUI(sent.append)).on_tool_end(SimpleNamespace(tool_call_id="c9"), None, self.tool(), "ok")

        asyncio.run(go())
        assert events(sent)[0]["seconds"] is None

    def test_a_failure_carries_its_reason(self, sent):
        async def go():
            hooks = JsonUI(sent.append).hooks()
            await hooks.on_tool_end(SimpleNamespace(tool_call_id="c1"), None, self.tool(), "Error executing tool x: no file")

        asyncio.run(go())
        assert events(sent)[0]["failure"] == "no file"


class TestApproval:
    def ask(self, *incoming):
        sent = []

        async def go():
            ui = JsonUI(sent.append)

            for message in incoming:
                ui.incoming.put_nowait(message)

            return await ui.ask("sync_cues_to_serato", {"dry_run": False}, ["would change 2 files"]), ui

        return (*asyncio.run(go()), events(sent))

    def test_the_request_names_the_tool_and_arguments(self):
        _, _, sent = self.ask(None)

        assert sent == [{"type": "approval_request", "id": "approval-1", "name": "sync_cues_to_serato", "arguments": {"dry_run": False},
                         "preview": ["would change 2 files"]}]

    def test_only_a_real_true_with_the_right_id_is_a_yes(self):
        assert self.ask({"type": "approval_response", "id": "approval-1", "approve": True})[0] is True
        assert self.ask({"type": "approval_response", "id": "approval-1", "approve": False})[0] is False
        assert self.ask({"type": "approval_response", "id": "approval-1", "approve": "true"})[0] is False
        assert self.ask({"type": "approval_response", "id": "approval-1"})[0] is False

    def test_a_stale_or_unrelated_message_is_not_an_answer(self):
        answered, *_ = self.ask(
            {"type": "approval_response", "id": "approval-0", "approve": True},
            {"type": "user_message", "text": "yes"},
            {"type": "approval_response", "id": "approval-1", "approve": False},
        )

        assert answered is False

    def test_a_closed_screen_or_a_quit_is_a_no(self):
        assert self.ask(None)[0] is False
        assert self.ask({"type": "quit"})[0] is False

    def test_quit_during_a_question_also_stops_the_chat(self):
        _, ui, _ = self.ask({"type": "quit"})

        assert asyncio.run(self.next_text(ui)) is None

    @staticmethod
    async def next_text(ui):
        return await ui.next_user_text()

    def test_each_request_gets_a_new_id(self):
        sent = []

        async def go():
            ui = JsonUI(sent.append)
            ui.incoming.put_nowait(None)
            ui.incoming.put_nowait(None)
            await ui.ask("a", {}, [])
            await ui.ask("b", {}, [])

        asyncio.run(go())
        assert [e["id"] for e in events(sent)] == ["approval-1", "approval-2"]


class TestListening:
    def test_valid_lines_are_queued_and_the_end_is_none(self):
        async def go():
            ui = JsonUI(lambda line: None)
            ui.listen(io.StringIO('{"type": "user_message", "text": "hi"}\nnot json\n\n{"type": "quit"}\n'))
            return [await ui.incoming.get() for _ in range(3)]

        assert asyncio.run(go()) == [{"type": "user_message", "text": "hi"}, {"type": "quit"}, None]

    def test_the_next_text_skips_stray_messages_and_stops_on_quit(self):
        async def go():
            ui = JsonUI(lambda line: None)

            stray = [{"type": "approval_response", "id": "x"}, {"type": "user_message", "text": 5}]

            for message in [*stray, {"type": "user_message", "text": "hi"}, {"type": "quit"}]:
                ui.incoming.put_nowait(message)

            return [await ui.next_user_text(), await ui.next_user_text()]

        assert asyncio.run(go()) == ["hi", None]
