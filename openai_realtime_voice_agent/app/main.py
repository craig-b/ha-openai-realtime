"""The add-on: a voice agent for the Home Assistant Voice PE on OpenAI Realtime."""
import asyncio
import logging
import os
import sys

import dotenv
import httpx
from openai import AsyncOpenAI

from app.agent import Agent
from app.realtime import RealtimeSession, takes_reasoning
from app.session import SessionSetup
from app.config import Settings, device_secret_problem
from app.migrate import migrate_saved_options
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

    Home Assistant's tools and language are read for each session, while it
    connects to OpenAI: a session has them as they are when it opens, and Home
    Assistant needn't be up when the add-on starts. A read that fails leaves
    the last one that worked in place.
    """

    # How long a session waits for Home Assistant before using the last read.
    HOME_ASSISTANT_TIMEOUT_S = 3.0
    # How long to wait for the Supervisor to restart the add-on after moving its settings.
    RESTART_WAIT_S = 60.0

    def __init__(self) -> None:
        self.settings = Settings.from_env()
        self.agent: Agent | None = None
        self._client: AsyncOpenAI | None = None
        # The last of Home Assistant's tools and language read: none, and
        # English, until a read works.
        self._last_tools: list[tuple[dict, Handler]] = []
        self._last_language = "en"

    async def _session_setup(self) -> SessionSetup:
        tools, languages = await asyncio.gather(self._tools(), self._transcription_languages())
        return SessionSetup(tools=tools, transcription_languages=languages)

    async def _tools(self) -> Tools:
        s = self.settings
        tools = Tools()
        if s.enable_end_conversation:
            tools.add(END_CONVERSATION_DEFINITION, end_conversation(lambda: self.agent.end_conversation()))
        if s.enable_web_search and s.engine == "realtime":
            # GPT-Live's backend searches the web with OpenAI's own tool instead.
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

    async def _transcription_languages(self) -> list[str]:
        """The languages set, or else Home Assistant's own (e.g. "en-GB" → "en")."""
        s = self.settings
        if not s.log_what_you_say or s.engine != "realtime" or s.transcription_language == "auto":
            return []
        if s.transcription_language:
            return [code.strip() for code in s.transcription_language.split(",") if code.strip()]
        if not s.ha_token:
            return ["en"]
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
            return [self._last_language]
        self._last_language = language
        return [language]

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
