"""The tools the model can call: Home Assistant's MCP tools, web search, disconnect."""
import contextlib
import logging
from typing import AsyncIterator, Awaitable, Callable

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[str]]


class Tools:
    """Tool definitions for the OpenAI session, and the handlers that run them."""

    def __init__(self) -> None:
        self.definitions: list[dict] = []
        self._handlers: dict[str, Handler] = {}

    def add(self, definition: dict, handler: Handler) -> None:
        self.definitions.append(definition)
        self._handlers[definition["name"]] = handler

    @property
    def names(self) -> list[str]:
        return [d["name"] for d in self.definitions]

    async def call(self, name: str, arguments: dict) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return f"Unknown tool {name}"
        return await handler(arguments)


class HomeAssistantTools:
    """Home Assistant's MCP server, over streamable HTTP; a session per request."""

    def __init__(self, url: str, token: str):
        self._url = url
        self._headers = {"Authorization": f"Bearer {token}"}

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        async with create_mcp_http_client(headers=self._headers) as http:
            async with streamable_http_client(self._url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def add_to(self, tools: Tools, allowlist: list[str]) -> None:
        """Add the server's tools (those in `allowlist`, if one is given)."""
        async with self._session() as session:
            listed = (await session.list_tools()).tools
        exposed = [t for t in listed if not allowlist or t.name in allowlist]
        for tool in exposed:
            schema = tool.inputSchema or {}
            tools.add(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": {
                        "type": "object",
                        "properties": schema.get("properties", {}),
                        "required": schema.get("required", []),
                    },
                },
                self._handler(tool.name),
            )
        logger.info(f"✅ Fetched {len(listed)} MCP tools, exposing {len(exposed)}")

    def _handler(self, name: str) -> Handler:
        async def call(arguments: dict) -> str:
            return await self._call(name, arguments)

        return call

    async def _call(self, name: str, arguments: dict) -> str:
        logger.info(f"🔧 {name}({arguments})")
        try:
            async with self._session() as session:
                result = await session.call_tool(name, arguments=arguments)
        except Exception as e:
            logger.error(f"❌ {name} failed: {e!r}")
            return f"Error calling mcp tool {name}: {e}"
        text = "".join(c.text for c in result.content if getattr(c, "text", None))
        return text or "Sorry, could not call the mcp tool"


WEB_SEARCH_DEFINITION = {
    "type": "function",
    "name": "web_search",
    "description": (
        "Search the public internet for current, real-time, or factual "
        "information the assistant does not already know — for example the "
        "weather, news, sports scores, opening hours, prices, travel info, or "
        "recent events. Do NOT use this for controlling the smart home — use "
        "the Hass* tools for lights, switches, climate, etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The search query, phrased as a clear natural-language "
                    "question in the user's language."
                ),
            }
        },
        "required": ["query"],
    },
}


def web_search(client: AsyncOpenAI, model: str) -> Handler:
    """The Realtime API has no web search: run one through the Responses API."""

    async def search(arguments: dict) -> str:
        query = str(arguments.get("query", "")).strip()
        logger.info(f"🔎 web_search: {query!r} (model={model})")
        if not query:
            return "Geen zoekopdracht ontvangen."
        try:
            response = await client.responses.create(
                model=model,
                tools=[{"type": "web_search"}],
                input=(
                    "Answer in at most 2 short sentences suitable for being read "
                    "aloud, in the same language as the question. Do not include "
                    "URLs, citations, or markdown. Question: " + query
                ),
            )
        except Exception as e:
            logger.error(f"❌ web_search failed: {e!r}")
            return "Het zoeken op internet lukte even niet."
        answer = (response.output_text or "").strip()
        logger.info(f"🔎 web_search answer: {answer[:200]}")
        return answer or "Ik kon hier online niets over vinden."

    return search


DISCONNECT_DEFINITION = {
    "type": "function",
    "name": "disconnect_client",
    "description": (
        "Disconnect the voice assistant session when the user says goodbye, farewell, stop, "
        "or only thank you without additional questions and wants to end the conversation. "
        "Use this when the user explicitly wants to end the conversation or says phrases like "
        "'Auf Wiedersehen', 'Tschüss', 'Stop', 'Beenden', 'Ende', etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "The reason for disconnecting (e.g., 'user_said_goodbye', 'user_requested_stop')",
                "enum": ["user_requested_stop", "conversation_ended"],
            }
        },
        "required": ["reason"],
    },
}


def disconnect(close_device: Callable[[], Awaitable[None]]) -> Handler:
    async def call(arguments: dict) -> str:
        logger.info(f"🔌 disconnect_client: {arguments.get('reason', 'unknown')}")
        await close_device()
        return "Disconnected successfully"

    return call
