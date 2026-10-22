"""Settings saved under an old name are moved to their new place (app/migrate.py)."""

import asyncio
import socket

import pytest_asyncio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.migrate import migrated
from app.shipped_prompts import SHIPPED_PROMPTS
from tests.fakes import wait_until

DEFAULTS = {
    "openai_api_key": "sk-test",
    "house_rules": "Be cheerful.",
    "realtime": {"model": "gpt-realtime-2.1", "voice": "marin", "speed": 1.0},
    "enable_web_search": True,
}
# The 0.7.0 defaults, as Home Assistant saved them.
SHIPPED_REALTIME = next(p for p in SHIPPED_PROMPTS if "voice assistant of Home Assistant" in p and "ENDING" in p)
SHIPPED_LIVE = next(p for p in SHIPPED_PROMPTS if "voice of Home Assistant" in p and "ENDING" in p)


class FakeSupervisor:
    """The Supervisor's API for the add-on itself: its options, and restarting it."""

    def __init__(self, options: dict):
        self.options = options
        self.saved: list[dict] = []
        self.restarts = 0
        self.port = None
        self.app = Starlette(routes=[
            Route("/addons/self/info", self._info),
            Route("/addons/self/options", self._save, methods=["POST"]),
            Route("/addons/self/restart", self._restart, methods=["POST"]),
        ])

    async def _info(self, request: Request):
        assert request.headers["Authorization"] == "Bearer supervisor-token"
        return JSONResponse({"result": "ok", "data": {"options": self.options}})

    async def _save(self, request: Request):
        self.saved.append((await request.json())["options"])
        return JSONResponse({"result": "ok", "data": {}})

    async def _restart(self, request: Request):
        self.restarts += 1
        return JSONResponse({"result": "ok", "data": {}})

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.task = asyncio.create_task(self.server.serve())
        await wait_until(lambda: self.server.started, 5.0, "fake Supervisor")

    async def stop(self) -> None:
        self.server.should_exit = True
        await asyncio.wait({self.task}, timeout=5)


@pytest_asyncio.fixture
async def supervisor():
    started = []

    async def start(options: dict) -> FakeSupervisor:
        fake = FakeSupervisor(options)
        await fake.start()
        started.append(fake)
        return fake

    yield start
    for fake in started:
        await fake.stop()


def test_options_under_old_names_move_into_their_section():
    old = {**DEFAULTS, "openai_model": "gpt-realtime-1.5", "openai_voice": "cedar", "vad_threshold": 0.6}
    assert migrated(old) == {
        "openai_api_key": "sk-test",
        "house_rules": "Be cheerful.",
        "realtime": {"model": "gpt-realtime-1.5", "voice": "cedar", "speed": 1.0, "vad_threshold": 0.6},
        "enable_web_search": True,
    }


def test_a_retired_model_moves_to_its_replacement():
    assert migrated({**DEFAULTS, "openai_model": "gpt-realtime-mini"})["realtime"]["model"] == "gpt-realtime-2.1-mini"
    assert migrated({**DEFAULTS, "openai_model": "gpt-realtime"})["realtime"]["model"] == "gpt-realtime-2.1"


def test_an_old_search_model_moves_to_the_default():
    assert migrated({**DEFAULTS, "web_search_model": "gpt-5.5"})["realtime"]["web_search_model"] == "gpt-6-luna"
    assert migrated({**DEFAULTS, "web_search_model": "custom"})["realtime"]["web_search_model"] == "custom"


def test_a_shipped_prompt_gives_way_to_the_built_in_rules():
    # Saved with the line breaks of an edit in YAML: they don't count.
    saved = {
        **DEFAULTS,
        "realtime": {**DEFAULTS["realtime"], "instructions": SHIPPED_REALTIME.replace(". ", ".\n")},
        "live": {"voice": "marin", "instructions": SHIPPED_LIVE},
    }
    assert migrated(saved) == {**DEFAULTS, "live": {"voice": "marin"}}


def test_an_edited_prompt_becomes_the_house_rules():
    saved = {**DEFAULTS, "realtime": {**DEFAULTS["realtime"], "instructions": "  Speak only Dutch.  "}}
    assert migrated(saved) == {**DEFAULTS, "house_rules": "Speak only Dutch."}


def test_every_edited_prompt_is_kept_once():
    saved = {
        **DEFAULTS,
        "realtime": {**DEFAULTS["realtime"], "instructions": "Speak only Dutch."},
        "live": {"instructions": "Speak only Dutch.", "backend_instructions": "Call the lounge the lounge."},
    }
    assert migrated(saved)["house_rules"] == "Speak only Dutch.\n\nCall the lounge the lounge."


def test_a_prompt_under_its_oldest_name_moves_all_the_way():
    assert migrated({**DEFAULTS, "instructions": "Speak only Dutch."}) == {**DEFAULTS, "house_rules": "Speak only Dutch."}
    assert migrated({**DEFAULTS, "instructions": SHIPPED_REALTIME}) == DEFAULTS


def test_options_under_new_names_are_left_alone():
    assert migrated(DEFAULTS) is None


async def test_the_addon_saves_moved_settings_and_restarts(openai, supervisor, addon_env, monkeypatch):
    from app.main import Application

    fake = await supervisor({**DEFAULTS, "openai_model": "gpt-realtime-1.5"})
    monkeypatch.setattr(Application, "RESTART_WAIT_S", 30.0)
    for name, value in {**addon_env, "SUPERVISOR_URL": fake.url, "SUPERVISOR_TOKEN": "supervisor-token"}.items():
        monkeypatch.setenv(name, value)
    app = Application()
    task = asyncio.create_task(app.run())
    try:
        await wait_until(lambda: fake.restarts, 5.0, "a restart")
        assert fake.saved == [migrated({**DEFAULTS, "openai_model": "gpt-realtime-1.5"})]
        await asyncio.sleep(0.3)
        assert not task.done()  # it waits to be restarted, rather than serving old settings
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=5)


async def test_the_addon_starts_as_usual_with_nothing_to_move(openai, supervisor, start_addon):
    fake = await supervisor(DEFAULTS)
    addon = await start_addon(SUPERVISOR_URL=fake.url, SUPERVISOR_TOKEN="supervisor-token")
    device = await addon.connect()
    await device.close()
    assert fake.saved == [] and fake.restarts == 0


async def test_the_addon_starts_when_the_supervisor_cannot_be_reached(openai, start_addon):
    addon = await start_addon(SUPERVISOR_URL="http://127.0.0.1:9", SUPERVISOR_TOKEN="supervisor-token")
    device = await addon.connect()
    await device.close()
