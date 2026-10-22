"""The add-on: a voice agent for the Home Assistant Voice PE on OpenAI Realtime."""
import asyncio
import logging
import os
import sys
from pathlib import Path

import dotenv
import httpx
from openai import AsyncOpenAI

from app.agent import Agent
from app.event_bus import EventBus
from app.realtime import RealtimeSession, takes_reasoning
from app.session import Home, SessionSetup
from app.config import Settings, device_secret_problem
from app.migrate import migrate_saved_options
from app.tls import CERTIFICATE_FILE
from app.tools import (
    END_CONVERSATION_DEFINITION,
    WEB_SEARCH_DEFINITION,
    Handler,
    HomeAssistantTools,
    Tools,
    end_conversation,
    web_search,
)

# LOG_LEVEL (add-on option) defaults to INFO; DEBUG logs every OpenAI event.
_LOG_LEVEL_NAME = os.environ.get("LOG_LEVEL", "INFO").upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)
logging.basicConfig(level=_LOG_LEVEL, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
# In DEBUG, show the websockets connection lifecycle (close codes, keepalive).
logging.getLogger("websockets").setLevel(logging.INFO if _LOG_LEVEL <= logging.DEBUG else logging.WARNING)
for noisy in ("httpx", "mcp", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

dotenv.load_dotenv()


class Application:
    """The add-on, and what each session reads from Home Assistant.

    Home Assistant's tools and configuration (its language, and where the home
    is) are read for each session, while it connects to OpenAI: a session has
    them as they are when it opens, and Home Assistant needn't be up when the
    add-on starts. A read that fails leaves the last one that worked in place.
    """

    # How long a session waits for Home Assistant before using the last read.
    HOME_ASSISTANT_TIMEOUT_S = 3.0
    # How long to wait for the Supervisor to restart the add-on after moving its settings.
    RESTART_WAIT_S = 60.0

    def __init__(self) -> None:
        self.settings = Settings.from_env()
        self.agent: Agent | None = None
        self._client: AsyncOpenAI | None = None
        # The last of Home Assistant's tools and configuration read: none
        # until a read works.
        self._last_tools: list[tuple[dict, Handler]] = []
        self._last_config: dict = {}

    async def _session_setup(self) -> SessionSetup:
        s = self.settings
        if s.ha_token:
            ha_tools, config = await asyncio.gather(
                self._read_home_assistant_tools(), self._read_home_assistant_config()
            )
        else:
            ha_tools, config = [], {}
        home = Home(
            town=s.home_town,
            country=str(config.get("country") or ""),
            time_zone=str(config.get("time_zone") or ""),
            temperature_unit=str((config.get("unit_system") or {}).get("temperature") or ""),
        )
        return SessionSetup(
            tools=self._tools(ha_tools, home),
            transcription_languages=self._transcription_languages(config),
            home=home,
        )

    def _tools(self, ha_tools: list[tuple[dict, Handler]], home: Home) -> Tools:
        s = self.settings
        tools = Tools()
        if s.enable_end_conversation:
            tools.add(END_CONVERSATION_DEFINITION, end_conversation(lambda: self.agent.end_conversation()))
        if s.enable_web_search and s.engine == "realtime":
            # GPT-Live's backend searches the web with OpenAI's own tool instead.
            tools.add(
                WEB_SEARCH_DEFINITION, web_search(self._client, s.web_search_model, home.web_search_location())
            )
        for definition, handler in ha_tools:
            tools.add(definition, handler)
        return tools

    async def _read_home_assistant_tools(self) -> list[tuple[dict, Handler]]:
        s = self.settings
        try:
            fetched = await asyncio.wait_for(
                HomeAssistantTools(s.ha_mcp_url, s.ha_token).fetch(s.mcp_tool_allowlist),
                self.HOME_ASSISTANT_TIMEOUT_S,
            )
        except Exception as e:
            logger.warning(f"⚠️ Could not read Home Assistant's MCP tools, using the last ones read: {e!r}")
            return self._last_tools
        names = [definition["name"] for definition, _ in fetched]
        if names != [definition["name"] for definition, _ in self._last_tools]:
            logger.info(f"🔧 Home Assistant offers {len(names)} tools: {names}")
        self._last_tools = fetched
        return fetched

    async def _read_home_assistant_config(self) -> dict:
        """Home Assistant's configuration (/api/config), or the last read if it can't be read."""
        s = self.settings
        api_url = self._home_assistant_api_url()
        try:
            async with httpx.AsyncClient(timeout=self.HOME_ASSISTANT_TIMEOUT_S) as http:
                response = await http.get(f"{api_url}/config", headers={"Authorization": f"Bearer {s.ha_token}"})
                response.raise_for_status()
                config = response.json()
        except Exception as e:
            logger.warning(f"⚠️ Could not read Home Assistant's configuration, using the last read: {e!r}")
            return self._last_config
        self._last_config = config
        return config

    def _home_assistant_api_url(self) -> str:
        """Home Assistant's REST API, where its MCP server is."""
        return self.settings.ha_mcp_url.removesuffix("/mcp")

    def _transcription_languages(self, config: dict) -> list[str]:
        """The languages set, or else Home Assistant's own (e.g. "en-GB" → "en"), or English."""
        s = self.settings
        if not s.log_what_you_say or s.engine != "realtime" or s.transcription_language == "auto":
            return []
        if s.transcription_language:
            return [code.strip() for code in s.transcription_language.split(",") if code.strip()]
        return [str(config.get("language") or "en").split("-")[0].lower()]

    async def run(self) -> None:
        s = self.settings
        if s.supervisor_token and await migrate_saved_options(s.supervisor_url, s.supervisor_token):
            await asyncio.sleep(self.RESTART_WAIT_S)
            logger.error("❌ The add-on wasn't restarted after moving its settings: restart it to use them")
        if not s.openai_api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")
        if problem := device_secret_problem(s.device_secret):
            raise ValueError(problem)
        if not s.ha_token:
            logger.warning("⚠️ No Home Assistant token: no Home Assistant tools")
        self._client = AsyncOpenAI(api_key=s.openai_api_key)
        self.agent = Agent(s, self._client, self._session_setup)
        if s.engine == "live":
            tier = "" if s.live_service_tier == "auto" else f", {s.live_service_tier} tier"
            logger.info(
                f"🎚️ GPT-Live {s.live_model}, voice {s.live_voice}; backend {s.live_backend_model} "
                f"(reasoning {s.live_reasoning_effort}{tier})"
            )
        else:
            reasoning = f"reasoning {s.reasoning_effort}" if takes_reasoning(s.model) else "no reasoning"
            logger.info(f"🎚️ {s.model} ({reasoning}), voice {s.voice}, turn detection {s.turn_detection_type}")
        if s.log_what_you_say and s.engine == "realtime":
            language = s.transcription_language or ("Home Assistant's language" if s.ha_token else "en")
            logger.info(f"🗣️ Logging what you say, transcribed by {RealtimeSession.TRANSCRIPTION_MODEL} ({language})")
        logger.info(
            f"🔁 Follow-up window {s.follow_up_ms // 1000} s, mic-open delay {s.follow_up_open_delay_ms} ms, "
            f"wake-open delay {s.wake_open_delay_ms} ms, playback prebuffer {s.playback_prebuffer_ms} ms"
        )
        logger.info(f"💬 Conversation stays open for {s.session_idle_timeout_s} s after its last exchange")
        if not s.ha_token:
            await self.agent.run()
            return
        bus = EventBus(
            self._home_assistant_api_url(),
            s.ha_token,
            self.agent.announce,
            self.agent.start_conversation,
            supervisor_url=s.supervisor_url,
            supervisor_token=s.supervisor_token,
            link={
                "port": s.websocket_port,
                "secret": s.device_secret,
                "certificate": (Path(s.tls_dir) / CERTIFICATE_FILE).read_text(),
            },
        )
        self.agent.on_state = bus.set_state
        self.agent.on_device = bus.device_identified
        listening = asyncio.create_task(bus.run(), name="event_bus")
        listening.add_done_callback(_report_end)
        try:
            await self.agent.run()
        finally:
            listening.cancel()
            await asyncio.gather(listening, return_exceptions=True)


def _report_end(task: asyncio.Task) -> None:
    """A background task that should run until shutdown ended: say so, and why."""
    if task.cancelled():
        return
    error = task.exception()
    logger.error(f"❌ {task.get_name()} stopped: {error!r}" if error else f"❌ {task.get_name()} stopped")


def main() -> None:
    try:
        asyncio.run(Application().run())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()
