"""One connection to the OpenAI Realtime API: one session."""
import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

from openai import AsyncOpenAI
from openai.resources.realtime.realtime import AsyncRealtimeConnection
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class RealtimeSession:
    """A Realtime connection, its events delivered as dicts to `on_event`.

    When the connection ends for any reason but `close`, `on_closed` is called
    with the reason. The SDK's own reconnect stays off: a new connection is a
    new session, which the add-on opens itself.
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        on_event: Callable[["RealtimeSession", dict], Awaitable[None]],
        on_closed: Callable[["RealtimeSession", str], Awaitable[None]],
    ):
        self._client = client
        self._model = model
        self._on_event = on_event
        self._on_closed = on_closed
        self._connection: Optional[AsyncRealtimeConnection] = None
        self._reader: Optional[asyncio.Task] = None
        self._closing = False

    async def connect(self) -> None:
        self._connection = await self._client.realtime.connect(model=self._model).enter()
        self._reader = asyncio.create_task(self._read())

    async def send(self, event: dict) -> None:
        if self._connection is None or self._closing:
            return
        try:
            await self._connection.send_raw(json.dumps(event))
        except ConnectionClosed:
            pass  # the reader reports it

    async def close(self) -> None:
        self._closing = True
        if self._connection is not None:
            await self._connection.close()
        if self._reader is not None and self._reader is not asyncio.current_task():
            await asyncio.gather(self._reader, return_exceptions=True)

    async def _read(self) -> None:
        reason = "OpenAI closed the connection"
        try:
            while True:
                event = json.loads(await self._connection.recv_bytes())
                try:
                    await self._on_event(self, event)
                except Exception:
                    logger.exception(f"❌ handling OpenAI's {event.get('type')} failed")
        except ConnectionClosed as e:
            reason = f"connection closed: {e}"
        if not self._closing:
            await self._on_closed(self, reason)
