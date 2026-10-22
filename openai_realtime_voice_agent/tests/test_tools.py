"""Tool calls: the model calls a Home Assistant MCP tool through the add-on."""

import asyncio
import socket

import pytest_asyncio
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.agent import Agent
from app.phases import PhaseTracker
from tests.fakes import pcm_tone, wait_until


class FakeHomeAssistant:
    """Home Assistant's API: its MCP server, with two tools that record their calls, its config,
    and its WebSocket API's event subscriptions."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        # Tool names, as each call finishes.
        self.finished: list[str] = []
        self.delay = 0.0
        self.config_delay = 0.0
        self.language = "en"
        self.added_tools: list[str] = []
        # (websocket, subscription id, event type), for each subscribe_events.
        self.subscriptions: list[tuple[WebSocket, int, str]] = []
        # (event type, data), for each event fired through the REST API.
        self.fired: list[tuple[str, dict]] = []
        self.server = None
        self.task = None
        self.port = None

    async def _turn_on(self, name: str) -> str:
        self.calls.append(("HassTurnOn", {"name": name}))
        await asyncio.sleep(self.delay)
        self.finished.append("HassTurnOn")
        return f"Turned on {name}"

    async def _config(self, request):
        await asyncio.sleep(self.config_delay)
        return JSONResponse({
            "language": self.language,
            "country": "GB",
            "time_zone": "Europe/London",
            "unit_system": {"temperature": "°C", "length": "km"},
        })

    async def _fire_event(self, request):
        assert request.headers["Authorization"] == "Bearer ha-token"
        event_type = request.path_params["event_type"]
        self.fired.append((event_type, await request.json()))
        return JSONResponse({"message": f"Event {event_type} fired."})

    async def _websocket(self, ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json({"type": "auth_required", "ha_version": "2026.10.0"})
        auth = await ws.receive_json()
        if auth != {"type": "auth", "access_token": "ha-token"}:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.10.0"})
        try:
            while True:
                message = await ws.receive_json()
                if message.get("type") == "subscribe_events":
                    self.subscriptions.append((ws, message["id"], message["event_type"]))
                    await ws.send_json({"id": message["id"], "type": "result", "success": True, "result": None})
        except WebSocketDisconnect:
            self.subscriptions = [s for s in self.subscriptions if s[0] is not ws]

    def subscribed(self, event_type: str) -> bool:
        return any(subscribed == event_type for _, _, subscribed in self.subscriptions)

    async def fire(self, event_type: str, data: dict) -> None:
        """Fire an event on the bus, as an automation's event action does."""
        await wait_until(lambda: self.subscribed(event_type), 5.0, f"a subscription to {event_type}")
        for ws, number, subscribed in list(self.subscriptions):
            if subscribed == event_type:
                await ws.send_json({"id": number, "type": "event", "event": {
                    "event_type": event_type, "data": data, "origin": "LOCAL",
                    "time_fired": "2026-10-22T17:00:00+00:00", "context": {"id": "x", "parent_id": None, "user_id": None}}})

    async def _turn_off(self, name: str) -> str:
        self.calls.append(("HassTurnOff", {"name": name}))
        self.finished.append("HassTurnOff")
        return f"Turned off {name}"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/mcp"

    def add_tool(self, name: str) -> None:
        """Expose another tool, as exposing an entity or script in Home Assistant does."""
        self.added_tools.append(name)
        if self.server is not None:
            self._add_tool(self.mcp, name)

    @staticmethod
    def _add_tool(mcp: FastMCP, name: str) -> None:
        async def tool() -> str:
            return name

        mcp.tool(name=name, description=name)(tool)

    def _make_mcp(self) -> FastMCP:
        # A new one for each start: its session manager runs only once.
        mcp = FastMCP("Home Assistant", log_level="WARNING", streamable_http_path="/api/mcp")
        mcp.tool(name="HassTurnOn", description="Turns on a device")(self._turn_on)
        mcp.tool(name="HassTurnOff", description="Turns off a device")(self._turn_off)
        for name in self.added_tools:
            self._add_tool(mcp, name)
        mcp.custom_route("/api/config", methods=["GET"])(self._config)
        mcp.custom_route("/api/events/{event_type}", methods=["POST"])(self._fire_event)
        return mcp

    async def start(self) -> None:
        """Start serving; a restart serves on the same port."""
        if self.port is None:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                self.port = s.getsockname()[1]
        self.mcp = self._make_mcp()
        app = self.mcp.streamable_http_app()
        app.router.routes.append(WebSocketRoute("/api/websocket", self._websocket))
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.task = asyncio.create_task(self.server.serve())
        await wait_until(lambda: self.server.started, 5.0, "fake Home Assistant MCP server")

    async def stop(self) -> None:
        if self.server is None:
            return  # a test stopped it already
        self.server.should_exit = True
        await asyncio.wait({self.task}, timeout=5)
        self.server = None


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


async def test_tool_calls_run_one_at_a_time_in_the_models_order(openai, home_assistant, ha_addon, wake):
    home_assistant.delay = 0.5  # HassTurnOn is slow; HassTurnOff, called second, is not
    device = await ha_addon.connect()
    conn = await wake(device)
    await conn.user_turn()
    first, second = await conn.function_calls(
        [("HassTurnOn", {"name": "kitchen light"}), ("HassTurnOff", {"name": "kitchen light"})]
    )
    await conn.wait_for("response.create")
    assert [call for call, _ in home_assistant.calls] == ["HassTurnOn", "HassTurnOff"]
    assert home_assistant.finished == ["HassTurnOn", "HassTurnOff"]
    outputs = [e["item"]["call_id"] for e in conn.of_type("conversation.item.create")]
    assert outputs == [first, second]
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


async def test_transcription_defaults_to_home_assistants_language(openai, start_addon, home_assistant, wake):
    home_assistant.language = "nl-NL"
    addon = await start_addon(HA_MCP_URL=home_assistant.url, LONGLIVED_TOKEN="ha-token", LOG_WHAT_YOU_SAY="true")
    device = await addon.connect()
    conn = await wake(device)
    assert conn.session["audio"]["input"]["transcription"]["languages"] == ["nl"]
    await device.close()


def tool_names(conn) -> list[str]:
    return sorted(t["name"] for t in conn.session["tools"])


async def test_each_session_has_the_tools_home_assistant_offers_when_it_opens(openai, home_assistant, ha_addon, wake):
    device = await ha_addon.connect()
    first = await wake(device)
    assert tool_names(first) == ["HassTurnOff", "HassTurnOn"]
    home_assistant.add_tool("HassLightSet")
    await first.close(1011)
    second = await wake(device)
    assert tool_names(second) == ["HassLightSet", "HassTurnOff", "HassTurnOn"]
    await device.close()


async def test_home_assistant_need_not_be_up_when_the_addon_starts(openai, home_assistant, start_addon, wake):
    await home_assistant.stop()
    addon = await start_addon(HA_MCP_URL=home_assistant.url, LONGLIVED_TOKEN="ha-token", LOG_WHAT_YOU_SAY="true")
    device = await addon.connect()
    first = await wake(device)
    assert tool_names(first) == []
    assert first.session["audio"]["input"]["transcription"]["languages"] == ["en"]
    home_assistant.language = "nl"
    await home_assistant.start()
    await first.close(1011)
    second = await wake(device)
    assert tool_names(second) == ["HassTurnOff", "HassTurnOn"]
    assert second.session["audio"]["input"]["transcription"]["languages"] == ["nl"]
    await device.close()


async def test_a_session_keeps_the_last_tools_and_language_read_when_home_assistant_is_down(
    openai, home_assistant, start_addon, wake
):
    home_assistant.language = "nl"
    addon = await start_addon(HA_MCP_URL=home_assistant.url, LONGLIVED_TOKEN="ha-token", LOG_WHAT_YOU_SAY="true")
    device = await addon.connect()
    first = await wake(device)
    await home_assistant.stop()
    await first.close(1011)
    second = await wake(device)
    assert tool_names(second) == ["HassTurnOff", "HassTurnOn"]
    assert second.session["audio"]["input"]["transcription"]["languages"] == ["nl"]
    # And its tools still reach Home Assistant once it is back.
    await home_assistant.start()
    await second.user_turn()
    await second.function_call("HassTurnOn", {"name": "kitchen light"})
    output = await second.wait_for("conversation.item.create", timeout=5.0)
    assert output["item"]["output"] == "Turned on kitchen light"
    await device.close()


async def test_a_slow_home_assistant_does_not_hold_the_session_up(openai, home_assistant, ha_addon, wake, monkeypatch):
    from app.main import Application

    monkeypatch.setattr(Application, "HOME_ASSISTANT_TIMEOUT_S", 0.5)
    device = await ha_addon.connect()
    first = await wake(device)
    home_assistant.config_delay = 5.0
    await first.close(1011)
    started = asyncio.get_running_loop().time()
    second = await wake(device)
    assert asyncio.get_running_loop().time() - started < 2.0
    assert tool_names(second) == ["HassTurnOff", "HassTurnOn"]
    await device.close()


# --- ending the conversation -------------------------------------------------------------

async def test_the_model_can_end_the_conversation(openai, start_addon, wake, monkeypatch):
    monkeypatch.setattr(Agent, "IDLE_CHECK_S", 0.05)
    addon = await start_addon(ENABLE_END_CONVERSATION="true", SESSION_REUSE_TIMEOUT_SECONDS="0")
    device = await addon.connect()
    conn = await wake(device)
    assert [t["name"] for t in conn.session["tools"]] == ["end_conversation"]
    await conn.user_turn()
    await conn.function_call("end_conversation", {})
    await wait_until(lambda: {"type": "end"} in device.messages, 3.0, "end sent to the device")
    await conn.wait_for("response.create")
    await conn.reply(pcm_tone(320, 24000), transcript="Goodbye!")
    await device.wait_for_phase("idle")
    # No follow-up window: the conversation is over, and its session closes.
    await asyncio.wait_for(conn.closed.wait(), 3.0)
    await device.close()


async def test_ending_the_conversation_can_be_turned_off(openai, start_addon, wake):
    device = await (await start_addon(ENABLE_END_CONVERSATION="false")).connect()
    conn = await wake(device)
    assert conn.session["tools"] == []
    assert "end_conversation" not in conn.session["instructions"]
    await device.close()


# --- where the home is --------------------------------------------------------------------

async def test_the_model_is_told_where_the_home_is(openai, start_addon, home_assistant, wake):
    addon = await start_addon(HA_MCP_URL=home_assistant.url, LONGLIVED_TOKEN="ha-token", HOME_TOWN="London")
    device = await addon.connect()
    conn = await wake(device)
    assert conn.session["instructions"].endswith(
        "ABOUT THE HOME: The home is in London, GB. Its time zone is Europe/London. Give temperatures in °C. "
        "Unless the user names another place, questions about the weather or local things are about the home."
    )
    await device.close()


async def test_gpt_lives_web_search_knows_where_the_home_is(openai, start_addon, home_assistant):
    addon = await start_addon(
        ENGINE="live", ENABLE_WEB_SEARCH="true", HA_MCP_URL=home_assistant.url, LONGLIVED_TOKEN="ha-token",
        HOME_TOWN="London",
    )
    device = await addon.connect()
    await device.send({"type": "wake"})
    session = (await openai.live_connection()).session
    search = [t for t in session["delegation"]["responses"]["tools"] if t["type"] == "web_search"]
    assert search == [{"type": "web_search", "user_location": {
        "type": "approximate", "city": "London", "country": "GB", "timezone": "Europe/London"}}]
    assert "ABOUT THE HOME" in session["instructions"]
    assert "ABOUT THE HOME" in session["delegation"]["responses"]["instructions"]
    await device.close()


def test_nothing_known_of_the_home_adds_nothing():
    from app.session import Home

    assert Home().describe() == ""
    assert Home().web_search_location() is None


# --- web search -------------------------------------------------------------------------

class _Responses:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)

        class Answer:
            output_text = "Sunny, 14 degrees."

        return Answer()


class _Client:
    def __init__(self):
        self.responses = _Responses()


async def test_web_search_asks_for_a_quick_local_lookup():
    from app.tools import web_search

    client = _Client()
    location = {"type": "approximate", "city": "London", "country": "GB"}
    answer = await web_search(client, "gpt-6-luna", location)({"query": "weather today"})
    assert answer == "Sunny, 14 degrees."
    call = client.responses.calls[0]
    assert call["model"] == "gpt-6-luna"
    assert call["tools"] == [{"type": "web_search", "search_context_size": "low", "user_location": location}]
    assert call["reasoning"] == {"effort": "low"}


async def test_web_search_sends_no_reasoning_effort_to_a_custom_model():
    from app.tools import web_search

    client = _Client()
    await web_search(client, "some-other-model")({"query": "weather today"})
    assert "reasoning" not in client.responses.calls[0]
