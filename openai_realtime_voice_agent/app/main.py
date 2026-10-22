"""The add-on: a voice agent for the Home Assistant Voice PE on OpenAI Realtime."""
import asyncio
import logging
import os
import sys

import dotenv
import httpx
from openai import AsyncOpenAI

from app.agent import Agent, SessionSetup
from app.config import Settings, device_secret_problem
from app.tools import (
    DISCONNECT_DEFINITION,
    WEB_SEARCH_DEFINITION,
    Handler,
    HomeAssistantTools,
    Tools,
    disconnect,
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

    Home Assistant's tools and language are read for each session, while it
    connects to OpenAI: a session has them as they are when it opens, and Home
    Assistant needn't be up when the add-on starts. A read that fails leaves
    the last one that worked in place.
    """

    # How long a session waits for Home Assistant before using the last read.
    HOME_ASSISTANT_TIMEOUT_S = 3.0

    def __init__(self) -> None:
        self.settings = Settings.from_env()
        self.agent: Agent | None = None
        self._client: AsyncOpenAI | None = None
        # The last of Home Assistant's tools and language read: none, and
        # English, until a read works.
        self._last_tools: list[tuple[dict, Handler]] = []
        self._last_language = "en"

    async def _session_setup(self) -> SessionSetup:
        tools, language = await asyncio.gather(self._tools(), self._transcription_language())
        return SessionSetup(tools=tools, transcription_language=language)

    async def _tools(self) -> Tools:
        s = self.settings
        tools = Tools()
        if s.enable_disconnect_tool:
            tools.add(DISCONNECT_DEFINITION, disconnect(lambda: self.agent.device.close_device()))
        if s.enable_web_search:
            tools.add(WEB_SEARCH_DEFINITION, web_search(self._client, s.web_search_model))
        if s.ha_token:
            for definition, handler in await self._read_home_assistant_tools():
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

    async def _transcription_language(self) -> str:
        """The language set, or else Home Assistant's own (e.g. "en-GB" → "en")."""
        s = self.settings
        if s.transcription_language or not s.ha_token:
            return s.transcription_language or "en"
        api_url = s.ha_mcp_url.removesuffix("/mcp")
        try:
            async with httpx.AsyncClient(timeout=self.HOME_ASSISTANT_TIMEOUT_S) as http:
                response = await http.get(
                    f"{api_url}/config", headers={"Authorization": f"Bearer {s.ha_token}"}
                )
                response.raise_for_status()
                language = str(response.json()["language"]).split("-")[0].lower()
        except Exception as e:
            logger.warning(
                f"⚠️ Could not read Home Assistant's language, using {self._last_language!r}: {e!r}"
            )
            return self._last_language
        self._last_language = language
        return language

    async def run(self) -> None:
        s = self.settings
        if not s.openai_api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")
        if problem := device_secret_problem(s.device_secret):
            raise ValueError(problem)
        if not s.ha_token:
            logger.warning("⚠️ No Home Assistant token: no Home Assistant tools")
        self._client = AsyncOpenAI(api_key=s.openai_api_key)
        self.agent = Agent(s, self._client, self._session_setup)
        language = s.transcription_language or ("Home Assistant's language" if s.ha_token else "en")
        logger.info(
            f"🎚️ {s.model}, voice {s.voice}, turn detection {s.turn_detection_type}, "
            f"transcription {s.transcription_model} ({language})"
        )
        logger.info(
            f"🔁 Follow-up window {s.follow_up_ms // 1000} s, mic-open delay {s.follow_up_open_delay_ms} ms, "
            f"wake-open delay {s.wake_open_delay_ms} ms, playback prebuffer {s.playback_prebuffer_ms} ms"
        )
        logger.info(f"💬 Conversation stays open for {s.session_idle_timeout_s} s after its last exchange")
        await self.agent.run()


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
