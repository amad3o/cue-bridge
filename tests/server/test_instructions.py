"""One copy of the guidance, used by both clients, and kept in step with the tools."""

import asyncio
import re

from cuebridge import mcp_server as server
from cuebridge.agent import runner
from cuebridge.instructions import GUIDANCE


def tool_names() -> set[str]:
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_the_server_sends_the_shared_guidance():
    assert server.mcp.instructions == GUIDANCE


def test_the_agent_prompt_is_the_shared_guidance_plus_its_own_rules():
    assert runner.INSTRUCTIONS.startswith(GUIDANCE)
    assert "asks the user to confirm" in runner.INSTRUCTIONS[len(GUIDANCE) :]


def test_every_tool_the_guidance_names_exists():
    named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", GUIDANCE)) - {"dry_run"}

    assert named, "the pattern found no names"
    assert named - tool_names() == set()


def test_the_tool_said_to_have_no_undo_is_the_xml_export():
    assert "except export_playlists_for_rekordbox, which has none" in GUIDANCE
    assert not any(name.startswith("undo_") and "export" in name for name in tool_names())
