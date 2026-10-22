"""Fixtures that run the real add-on between a fake device and a fake OpenAI."""

import asyncio
import socket
import ssl
from pathlib import Path

import pytest
import pytest_asyncio

from tests.fakes import FakeDevice, FakeRealtimeServer, wait_until


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def openai(monkeypatch):
    """A fake OpenAI Realtime server that the add-on's OpenAI client connects to."""
    server = FakeRealtimeServer()
    await server.start()
    monkeypatch.setenv("OPENAI_BASE_URL", server.base_url)
    yield server
    await server.stop()


class Addon:
    """The add-on's Application running in this event loop."""

    def __init__(self, port: int, secret: str | None, tls_dir: str):
        self.port = port
        self.secret = secret
        self.tls_dir = Path(tls_dir)
        self.app = None
        self.task = None

    @property
    def url(self) -> str:
        return f"wss://127.0.0.1:{self.port}/"

    def trusting_context(self) -> ssl.SSLContext:
        """A client context that trusts the add-on's certificate and nothing else, as the firmware does."""
        context = ssl.create_default_context(cafile=self.tls_dir / "certificate.pem")
        context.check_hostname = False
        return context

    def device(self, secret: str | None = ..., tls: ssl.SSLContext | None = None) -> FakeDevice:
        """A device presenting the add-on's secret, or `secret` if one is given."""
        return FakeDevice(self.url, self.secret if secret is ... else secret, tls or self.trusting_context())

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
def addon_env(monkeypatch, tmp_path):
    """Environment for the add-on, as run.sh would export it. Tests may override."""
    port = _free_port()
    env = {
        "OPENAI_API_KEY": "test-key",
        "DEVICE_SECRET": "test-device-secret",
        "TLS_DIR": str(tmp_path / "tls"),
        "WEBSOCKET_HOST": "127.0.0.1",
        "WEBSOCKET_PORT": str(port),
        "ENABLE_WEB_SEARCH": "false",
        # Tests of the tool turn it on.
        "ENABLE_END_CONVERSATION": "false",
        # Short debounce keeps tests fast; tests of the debounce itself set it.
        "PHASE_IDLE_DEBOUNCE_MS": "300",
    }
    for name in ("SUPERVISOR_TOKEN", "LONGLIVED_TOKEN", "HA_MCP_URL"):
        monkeypatch.delenv(name, raising=False)
    return env


@pytest.fixture
def running() -> list:
    """The add-ons started in this test."""
    return []


@pytest_asyncio.fixture
async def start_addon(openai, addon_env, monkeypatch, running):
    """Start the add-on with `addon_env` plus any overrides; stop it after the test."""
    from app.main import Application

    started = running

    async def start(**overrides) -> Addon:
        env = {**addon_env, **overrides}
        for name, value in env.items():
            if value is None:  # an override of None unsets the variable
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        addon = Addon(int(env["WEBSOCKET_PORT"]), env.get("DEVICE_SECRET"), env["TLS_DIR"])
        addon.app = Application()
        addon.task = asyncio.create_task(addon.app.run())
        started.append(addon)
        await addon.wait_listening()
        return addon

    yield start

    for addon in started:
        addon.task.cancel()
        await asyncio.wait({addon.task}, timeout=5.0)
        assert addon.task.done(), "the add-on did not stop"


@pytest_asyncio.fixture
async def addon(start_addon):
    return await start_addon()


@pytest_asyncio.fixture
async def device(addon):
    """A device connected to the add-on (it has sent `start`, as the firmware does)."""
    dev = await addon.connect()
    yield dev
    await dev.close()


@pytest_asyncio.fixture
async def wake(openai, running):
    """Wake a device, as the wake word does; return the OpenAI session that opens."""

    async def wake(dev: FakeDevice):
        index = len(openai.connections)
        await dev.send({"type": "wake"})
        conn = await openai.connection(index)
        # OpenAI has the session's settings; wait for the add-on to have its
        # answer too, so what the test sends next reaches an open session.
        await wait_until(
            lambda: any(a.app.agent is not None and a.app.agent._state.name == "OPEN" for a in running),
            3.0,
            "the add-on's session open",
        )
        return conn

    return wake


@pytest_asyncio.fixture
async def session(device, wake):
    """The OpenAI session the connected device's wake opened."""
    return await wake(device)
