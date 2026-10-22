"""Tool calls: the model calls a Home Assistant MCP tool through the add-on."""

import asyncio
import socket

import pytest_asyncio
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from app.agent import Agent
from app.phases import PhaseTracker
from tests.fakes import pcm_tone, wait_until


class FakeHomeAssistant:
    """Home Assistant's API: its MCP server, with two tools that record their calls, and its config."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.delay = 0.0
        self.language = "en"
        self.mcp = FastMCP("Home Assistant", log_level="WARNING", streamable_http_path="/api/mcp")
        self.mcp.tool(name="HassTurnOn", description="Turns on a device")(self._turn_on)
        self.mcp.tool(name="HassTurnOff", description="Turns off a device")(self._turn_off)
        self.mcp.custom_route("/api/config", methods=["GET"])(self._config)
        self.server = None
        self.task = None
        self.port = None

    async def _turn_on(self, name: str) -> str:
        self.calls.append(("HassTurnOn", {"name": name}))
        await asyncio.sleep(self.delay)
        return f"Turned on {name}"

    async def _config(self, request):
        return JSONResponse({"language": self.language})

    async def _turn_off(self, name: str) -> str:
        self.calls.append(("HassTurnOff", {"name": name}))
        return f"Turned off {name}"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/mcp"

    async def start(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        config = uvicorn.Config(
            self.mcp.streamable_http_app(), host="127.0.0.1", port=self.port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.task = asyncio.create_task(self.server.serve())
        await wait_until(lambda: self.server.started, 5.0, "fake Home Assistant MCP server")

    async def stop(self) -> None:
        self.server.should_exit = True
        await asyncio.wait({self.task}, timeout=5)


@pytest_asyncio.fixture
async def home_assistant():
    ha = FakeHomeAssistant()
    await ha.start()
    yield ha
    await ha.stop()


@pytest_asyncio.fixture
async def ha_addon(start_addon, home_assistant):
    return await start_addon(HA_MCP_URL=home_assistant.url, LONGLIVED_TOKEN="ha-token")


async def test_mcp_tools_are_offered_to_the_model(openai, ha_addon, wake):
    device = await ha_addon.connect()
    conn = await wake(device)
    assert sorted(t["name"] for t in conn.session["tools"]) == ["HassTurnOff", "HassTurnOn"]


async def test_the_allowlist_limits_the_tools(openai, start_addon, home_assistant, wake):
    addon = await start_addon(HA_MCP_URL=home_assistant.url, LONGLIVED_TOKEN="ha-token", MCP_TOOL_ALLOWLIST="HassTurnOn")
    device = await addon.connect()
    conn = await wake(device)
    assert [t["name"] for t in conn.session["tools"]] == ["HassTurnOn"]


async def test_a_tool_call_runs_and_its_result_goes_back(openai, home_assistant, ha_addon, wake):
    device = await ha_addon.connect()
    conn = await wake(device)
    await conn.user_turn()
    call_id = await conn.function_call("HassTurnOn", {"name": "kitchen light"})
    output = await conn.wait_for("conversation.item.create")
    assert home_assistant.calls == [("HassTurnOn", {"name": "kitchen light"})]
    assert output["item"]["type"] == "function_call_output"
    assert output["item"]["call_id"] == call_id
    assert output["item"]["output"] == "Turned on kitchen light"
    # And the model is asked to answer with the result.
    await conn.wait_for("response.create")
    await device.close()


async def test_speech_does_not_cancel_a_running_tool(openai, home_assistant, ha_addon, wake):
    home_assistant.delay = 1.0
    device = await ha_addon.connect()
    conn = await wake(device)
    await conn.user_turn()
    await conn.function_call("HassTurnOn", {"name": "kitchen light"})
    await wait_until(lambda: home_assistant.calls, 3.0, "the tool call")
    await conn.user_turn()  # the user carries on talking
    output = await conn.wait_for("conversation.item.create", timeout=4.0)
    assert "Turned on kitchen light" in output["item"]["output"]
    await device.close()


async def test_thinking_outlasts_its_timeout_while_a_tool_runs(openai, home_assistant, ha_addon, monkeypatch, wake):
    monkeypatch.setattr(PhaseTracker, "THINKING_TIMEOUT_S", 0.5)
    home_assistant.delay = 2.0
    device = await ha_addon.connect()
    conn = await wake(device)
    await conn.user_turn()
    await device.wait_for_phase("thinking")
    await conn.function_call("HassTurnOn", {"name": "kitchen light"})
    await device.assert_no_phase("idle", within=1.5)
    await conn.wait_for("conversation.item.create", timeout=3.0)
    await conn.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("replying")
    await device.close()


async def test_an_unreachable_home_assistant_is_reported_to_the_model(openai, home_assistant, ha_addon, wake):
    device = await ha_addon.connect()
    conn = await wake(device)
    await home_assistant.stop()
    await conn.user_turn()
    await conn.function_call("HassTurnOn", {"name": "kitchen light"})
    output = await conn.wait_for("conversation.item.create", timeout=5.0)
    assert "Error calling mcp tool HassTurnOn" in output["item"]["output"]
    await conn.wait_for("response.create")
    await device.close()


async def test_a_running_tool_keeps_the_session_open(openai, start_addon, home_assistant, wake, monkeypatch):
    monkeypatch.setattr(Agent, "IDLE_CHECK_S", 0.05)
    addon = await start_addon(
        HA_MCP_URL=home_assistant.url, LONGLIVED_TOKEN="ha-token", SESSION_REUSE_TIMEOUT_SECONDS="0"
    )
    home_assistant.delay = 1.0
    device = await addon.connect()
    conn = await wake(device)
    await conn.user_turn()
    await conn.function_call("HassTurnOn", {"name": "kitchen light"})
    await conn.wait_for("conversation.item.create", timeout=3.0)
    assert not conn.closed.is_set()
    await device.close()


async def test_tool_calls_are_carried_into_a_new_session(openai, home_assistant, ha_addon, wake):
    device = await ha_addon.connect()
    conn = await wake(device)
    await conn.user_turn()
    await conn.function_call("HassTurnOn", {"name": "kitchen light"})
    output = await conn.wait_for("conversation.item.create")
    # OpenAI adds the tool's result to the conversation.
    await conn.send({"type": "conversation.item.added", "item": {**output["item"], "object": "realtime.item"}})
    await conn.reply(pcm_tone(320, 24000), transcript="The kitchen light is on.")
    await device.wait_for_phase("idle")
    await conn.close(1011)
    second = await wake(device)
    carried = await second.wait_for("conversation.item.create", count=2)
    text = carried["item"]["content"][0]["text"]
    assert 'Assistant called HassTurnOn({"name": "kitchen light"})' in text
    assert "Result:" in text and "Turned on kitchen light" in text
    assert "Assistant: The kitchen light is on.\n</earlier_conversation>" in text
    await device.close()


async def test_transcription_defaults_to_home_assistants_language(openai, start_addon, home_assistant, wake):
    home_assistant.language = "nl-NL"
    addon = await start_addon(HA_MCP_URL=home_assistant.url, LONGLIVED_TOKEN="ha-token", TRANSCRIPTION_LANGUAGE="")
    device = await addon.connect()
    conn = await wake(device)
    assert conn.session["audio"]["input"]["transcription"]["language"] == "nl"
    await device.close()
