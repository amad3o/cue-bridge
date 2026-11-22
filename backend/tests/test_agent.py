"""The agent's approval gate, tested with a scripted model: no network, no API key, no real library."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents import Agent, ModelResponse, Usage, function_tool, set_tracing_disabled
from agents.mcp import MCPServerStdio
from agents.models.interface import Model
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from cuebridge import agent as agent_module

set_tracing_disabled(True)


def call(name, arguments, call_id):
    return ResponseFunctionToolCall(name=name, arguments=json.dumps(arguments), call_id=call_id, type="function_call")


def say(text):
    return ResponseOutputMessage(
        id="msg", role="assistant", status="completed", type="message",
        content=[ResponseOutputText(text=text, type="output_text", annotations=[])],
    )


class ScriptedModel(Model):
    """Replies with the next scripted list of output items each time it is called, and keeps what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.inputs = []

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kwargs):
        self.inputs.append(input)
        return ModelResponse(output=self.replies.pop(0), usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class World:
    """A pretend library: a tool that writes (and so needs approval) and one that only reads, both recording their calls."""

    def __init__(self):
        self.writes = []
        self.reads = 0
        self.asked = []
        self.answer = True

        @function_tool(name_override="sync_thing", needs_approval=True)
        def sync_thing(dry_run: bool = True, note: str = "") -> str:
            self.writes.append(dry_run)
            return "dry run: 2 changes" if dry_run else "applied: 2 changes"

        @function_tool(name_override="look_at_thing")
        def look_at_thing() -> str:
            self.reads += 1
            return "a thing"

        self.tools = [sync_thing, look_at_thing]

    async def ask(self, name, arguments):
        self.asked.append((name, arguments))
        return self.answer

    def run(self, *replies):
        self.model = ScriptedModel(*replies)
        agent = Agent(name="test", model=self.model, tools=self.tools)
        return asyncio.run(agent_module.run_turn(agent, [{"role": "user", "content": "go"}], ask=self.ask))


@pytest.fixture
def world():
    return World()


class TestTheApprovalGate:
    def test_a_dry_run_goes_through_without_asking(self, world):
        result = world.run([call("sync_thing", {"dry_run": True}, "c1")], [say("done")])

        assert world.writes == [True]
        assert world.asked == []
        assert result.final_output == "done"

    def test_leaving_dry_run_out_is_a_dry_run(self, world):
        world.run([call("sync_thing", {}, "c1")], [say("done")])

        assert world.writes == [True]
        assert world.asked == []

    def test_a_real_change_asks_first_and_runs_when_approved(self, world):
        result = world.run([call("sync_thing", {"dry_run": False, "note": "x"}, "c1")], [say("done")])

        assert world.asked == [("sync_thing", {"dry_run": False, "note": "x"})]
        assert world.writes == [False]
        assert result.final_output == "done"

    def test_a_refused_change_does_not_run_and_the_model_is_told(self, world):
        world.answer = False
        result = world.run([call("sync_thing", {"dry_run": False}, "c1")], [say("ok, nothing changed")])

        assert world.asked == [("sync_thing", {"dry_run": False})]
        assert world.writes == []
        assert result.final_output == "ok, nothing changed"
        # the model's second turn saw the refusal, not a result
        assert "The user declined this change" in json.dumps(world.model.inputs[-1], default=str)

    def test_a_read_only_tool_never_asks(self, world):
        world.run([call("look_at_thing", {}, "c1")], [say("done")])

        assert world.reads == 1
        assert world.asked == []

    def test_only_the_real_change_in_a_mixed_batch_is_asked_about(self, world):
        world.run(
            [
                call("sync_thing", {"dry_run": True}, "c1"),
                call("sync_thing", {"dry_run": False}, "c2"),
                call("look_at_thing", {}, "c3"),
            ],
            [say("done")],
        )

        assert world.asked == [("sync_thing", {"dry_run": False})]
        assert sorted(world.writes) == [False, True]
        assert world.reads == 1

    def test_a_dry_run_then_the_real_change_in_two_steps(self, world):
        world.run(
            [call("sync_thing", {"dry_run": True}, "c1")],
            [call("sync_thing", {"dry_run": False}, "c2")],
            [say("applied")],
        )

        assert world.writes == [True, False]
        assert world.asked == [("sync_thing", {"dry_run": False})]

    def test_a_refusal_in_one_step_does_not_block_a_later_approved_one(self, world):
        answers = iter([False, True])

        async def ask(name, arguments):
            world.asked.append((name, arguments))
            return next(answers)

        world.model = ScriptedModel(
            [call("sync_thing", {"dry_run": False, "note": "first"}, "c1")],
            [call("sync_thing", {"dry_run": False, "note": "second"}, "c2")],
            [say("done")],
        )
        agent = Agent(name="test", model=world.model, tools=world.tools)
        asyncio.run(agent_module.run_turn(agent, [{"role": "user", "content": "go"}], ask=ask))

        assert [a["note"] for _, a in world.asked] == ["first", "second"]
        assert world.writes == [False]


class TestIsRealChange:
    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            ({}, False),
            (None, True),  # the arguments could not be read
            ({"dry_run": True}, False),
            ({"dry_run": False}, True),
            ({"dry_run": "false"}, True),  # anything that is not clearly a preview is asked about
            ({"dry_run": None}, True),
            ({"dry_run": 0}, True),
        ],
    )
    def test_only_a_clear_preview_skips_the_question(self, arguments, expected):
        assert agent_module.is_real_change(arguments) is expected


class TestCallArguments:
    def item(self, arguments):
        return SimpleNamespace(raw_item=SimpleNamespace(arguments=arguments))

    def test_reads_a_json_string(self):
        assert agent_module.call_arguments(self.item('{"dry_run": false}')) == {"dry_run": False}

    def test_reads_a_dict_raw_item(self):
        assert agent_module.call_arguments(SimpleNamespace(raw_item={"arguments": {"dry_run": True}})) == {"dry_run": True}

    @pytest.mark.parametrize("arguments", ["not json", "[1, 2]", 5])
    def test_anything_unreadable_is_none(self, arguments):
        assert agent_module.call_arguments(self.item(arguments)) is None

    @pytest.mark.parametrize("arguments", [None, ""])
    def test_no_arguments_at_all_is_an_empty_dict(self, arguments):
        assert agent_module.call_arguments(self.item(arguments)) == {}

    def test_unreadable_arguments_are_asked_about(self):
        assert agent_module.is_real_change(agent_module.call_arguments(self.item("not json"))) is True


class TestToolNeedsApproval:
    def tool(self, annotations):
        return SimpleNamespace(annotations=annotations)

    def test_read_only_tools_are_free(self):
        assert agent_module.tool_needs_approval(self.tool(SimpleNamespace(read_only_hint=True))) is False
        assert agent_module.tool_needs_approval(self.tool(SimpleNamespace(readOnlyHint=True))) is False

    def test_writing_tools_need_approval(self):
        assert agent_module.tool_needs_approval(self.tool(SimpleNamespace(read_only_hint=False))) is True
        assert agent_module.tool_needs_approval(self.tool(SimpleNamespace(readOnlyHint=False))) is True

    def test_a_tool_with_no_annotations_is_treated_as_writing(self):
        assert agent_module.tool_needs_approval(self.tool(None)) is True
        assert agent_module.tool_needs_approval(self.tool(SimpleNamespace())) is True
        assert agent_module.tool_needs_approval(SimpleNamespace()) is True

    def test_the_sdk_policy_agrees(self):
        needs = asyncio.run(agent_module.needs_approval(None, None, self.tool(SimpleNamespace(read_only_hint=False))))
        assert needs is True


class TestMalformedCalls:
    def test_a_call_whose_arguments_cannot_be_read_is_asked_about(self, world):
        world.run([ResponseFunctionToolCall(name="sync_thing", arguments="{not json", call_id="c1", type="function_call")], [say("done")])

        assert [name for name, _ in world.asked] == ["sync_thing"]
        assert world.asked[0][1] is None


class TestDescribeCall:
    def test_lists_the_tool_and_every_argument(self):
        text = agent_module.describe_call("sync_cues_to_serato", {"dry_run": False, "expected_files": 3})

        assert "sync_cues_to_serato" in text
        assert "dry_run = False" in text
        assert "expected_files = 3" in text

    def test_says_so_when_the_arguments_are_unreadable(self):
        assert "could not be read" in agent_module.describe_call("sync_cues_to_serato", None)


@pytest.fixture(scope="module")
def server_tools():
    async def fetch():
        async with MCPServerStdio(params=agent_module.server_params(), client_session_timeout_seconds=60) as server:
            return await server.list_tools()

    return asyncio.run(fetch())


def properties_of(tool):
    # mcp 2.x calls it input_schema; older releases call it inputSchema.
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
    return schema.get("properties", {})


class TestTheRealServer:
    """The gate is only as good as the server's tool list, so check that list: every writer must be gated, and be a preview by default."""

    def test_the_server_starts_and_has_tools(self, server_tools):
        assert len(server_tools) > 20

    def test_every_tool_that_writes_takes_dry_run_and_defaults_to_a_preview(self, server_tools):
        writers = [t for t in server_tools if agent_module.tool_needs_approval(t)]

        assert writers
        for tool in writers:
            assert properties_of(tool).get("dry_run", {}).get("default") is True, f"{tool.name} must default to a dry run"

    def test_every_tool_with_dry_run_is_gated(self, server_tools):
        for tool in server_tools:
            if "dry_run" in properties_of(tool):
                assert agent_module.tool_needs_approval(tool), f"{tool.name} takes dry_run but is not gated"

    def test_the_read_only_tools_are_not_gated(self, server_tools):
        assert any(not agent_module.tool_needs_approval(t) for t in server_tools)


class TestServerParams:
    def test_it_starts_the_same_server_as_the_readme_with_backend_on_the_path(self):
        params = agent_module.server_params()

        assert params["args"] == ["-m", "cuebridge.mcp_server"]
        assert params["env"]["PYTHONPATH"] == str(agent_module.BACKEND_DIR)
        assert (agent_module.BACKEND_DIR / "cuebridge" / "mcp_server.py").exists()
