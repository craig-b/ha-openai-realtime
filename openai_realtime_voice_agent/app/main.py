"""The add-on: a voice agent for the Home Assistant Voice PE on OpenAI Realtime."""
import asyncio
import logging
import os
import sys

import dotenv
import httpx
from openai import AsyncOpenAI

from app.agent import Agent
from app.config import Settings, device_secret_problem
from app.tools import (
    DISCONNECT_DEFINITION,
    WEB_SEARCH_DEFINITION,
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
    def __init__(self) -> None:
        self.settings = Settings.from_env()
        self.agent: Agent | None = None

    async def _tools(self, client: AsyncOpenAI) -> Tools:
        s = self.settings
        tools = Tools()
        if s.enable_disconnect_tool:
            tools.add(DISCONNECT_DEFINITION, disconnect(lambda: self.agent.device.close_device()))
        if s.enable_web_search:
            tools.add(WEB_SEARCH_DEFINITION, web_search(client, s.web_search_model))
        if s.ha_token:
            try:
                await HomeAssistantTools(s.ha_mcp_url, s.ha_token).add_to(tools, s.mcp_tool_allowlist)
            except Exception as e:
                logger.warning(f"⚠️ Could not load Home Assistant's MCP tools: {e!r}")
        else:
            logger.warning("⚠️ No Home Assistant token: no Home Assistant tools")
        logger.info(f"🔧 {len(tools.names)} tools: {tools.names}")
        return tools

    async def _home_assistant_language(self) -> str:
        """Home Assistant's own language (e.g. "en-GB" → "en"), or English if it can't be read."""
        s = self.settings
        api_url = s.ha_mcp_url.removesuffix("/mcp")
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                response = await http.get(
                    f"{api_url}/config", headers={"Authorization": f"Bearer {s.ha_token}"}
                )
                response.raise_for_status()
                language = str(response.json()["language"])
        except Exception as e:
            logger.warning(f"⚠️ Could not read Home Assistant's language, using English: {e!r}")
            return "en"
        return language.split("-")[0].lower()

    async def run(self) -> None:
        s = self.settings
        if not s.openai_api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")
        if problem := device_secret_problem(s.device_secret):
            raise ValueError(problem)
        client = AsyncOpenAI(api_key=s.openai_api_key)
        if not s.transcription_language:
            s.transcription_language = await self._home_assistant_language() if s.ha_token else "en"
        self.agent = Agent(s, client, await self._tools(client))
        logger.info(
            f"🎚️ {s.model}, voice {s.voice}, turn detection {s.turn_detection_type}, "
            f"transcription {s.transcription_model} ({s.transcription_language})"
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
