"""Fixtures that run the real add-on between a fake device and a fake OpenAI."""

import asyncio
import socket

import pytest
import pytest_asyncio

from pipecat.services.openai.realtime import llm as realtime_llm

from tests.fakes import FakeDevice, FakeRealtimeServer, wait_until


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def openai(monkeypatch):
    """A fake OpenAI Realtime server that the add-on's realtime client connects to."""
    server = FakeRealtimeServer()
    await server.start()
    real_connect = realtime_llm.websocket_connect

    def connect(uri, **kwargs):
        # Keep the path and query (the model) so tests can see what was asked for.
        query = uri.split("?", 1)[1] if "?" in uri else ""
        return real_connect(f"{server.url}?{query}", **kwargs)

    monkeypatch.setattr(realtime_llm, "websocket_connect", connect)
    yield server
    await server.stop()


class Addon:
    """The add-on's Application running in this event loop."""

    def __init__(self, port: int):
        self.port = port
        self.app = None
        self.task = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/"

    def device(self) -> FakeDevice:
        return FakeDevice(self.url)

    async def connect(self, send_start: bool = True) -> FakeDevice:
        """Connect a device and wait for its hello, so the add-on has taken it on."""
        dev = await self.device().connect(send_start=send_start)
        await wait_until(lambda: dev.messages, 3.0, "hello")
        return dev

    async def wait_listening(self, timeout: float = 10.0) -> None:
        def accepting():
            if self.task.done():
                self.task.result()  # surface a startup crash
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return True
            except OSError:
                return False

        await wait_until(accepting, timeout, f"add-on listening on port {self.port}")


@pytest.fixture
def addon_env(monkeypatch):
    """Environment for the add-on, as run.sh would export it. Tests may override."""
    port = _free_port()
    env = {
        "OPENAI_API_KEY": "test-key",
        "WEBSOCKET_HOST": "127.0.0.1",
        "WEBSOCKET_PORT": str(port),
        "ENABLE_WEB_SEARCH": "false",
        # Short debounce keeps tests fast; tests of the debounce itself set it.
        "PHASE_IDLE_DEBOUNCE_MS": "300",
    }
    for name in ("SUPERVISOR_TOKEN", "LONGLIVED_TOKEN", "HA_MCP_URL"):
        monkeypatch.delenv(name, raising=False)
    return env


@pytest_asyncio.fixture
async def start_addon(openai, addon_env, monkeypatch):
    """Start the add-on with `addon_env` plus any overrides; stop it after the test."""
    from app.main import Application

    started: list[Addon] = []

    async def start(**overrides) -> Addon:
        env = {**addon_env, **overrides}
        for name, value in env.items():
            if value is None:  # an override of None unsets the variable
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        addon = Addon(int(env["WEBSOCKET_PORT"]))
        addon.app = Application()
        addon.task = asyncio.create_task(addon.app.run())
        started.append(addon)
        await addon.wait_listening()
        return addon

    yield start

    for addon in started:
        # Cancelling the pipeline is what a signal does; run() then returns.
        await addon.app.runner.cancel()
        await asyncio.wait_for(addon.task, timeout=5.0)


@pytest_asyncio.fixture
async def addon(start_addon):
    return await start_addon()


@pytest_asyncio.fixture
async def device(addon):
    """A device connected to the add-on (it has sent `start`, as the firmware does)."""
    dev = await addon.connect()
    yield dev
    await dev.close()
