"""The WebSocket server the Voice PE connects to (docs/protocol.md).

The connection is TLS, with the add-on's own certificate (app/tls.py), which
the device trusts and no other. Only a device that presents the shared secret, as `Authorization: Bearer
<secret>` on the upgrade request, is let in; anything else is turned away
before it can take over from the device that is connected.

One device at a time. A Voice PE that reconnects after a Wi-Fi drop leaves its
old connection half-open, its close never answered; so a new connection takes
over at once, the old one is closed in the background, and its ending changes
nothing for the new one.

Everything sent to the device goes through one queue per connection, written
by its own task, so a slow device never holds up the add-on.
"""
import asyncio
import hmac
import json
import logging
import ssl
from http import HTTPStatus
from typing import Optional, Protocol

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

logger = logging.getLogger(__name__)


class DeviceHandler(Protocol):
    async def device_connected(self) -> None: ...
    async def device_disconnected(self) -> None: ...
    async def device_message(self, message_type: str, data: dict) -> None: ...
    async def device_audio(self, pcm: bytes) -> None: ...


class _Connection:
    def __init__(self, websocket: ServerConnection):
        self.websocket = websocket
        self.outbox: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.writer = asyncio.create_task(self._write())

    @property
    def address(self) -> str:
        return str(self.websocket.remote_address)

    async def _write(self) -> None:
        try:
            while True:
                await self.websocket.send(await self.outbox.get())
        except ConnectionClosed:
            pass

    async def close(self) -> None:
        self.writer.cancel()
        await self.websocket.close()


class DeviceServer:
    def __init__(
        self, host: str, port: int, secret: str, tls: ssl.SSLContext, hello: dict, handler: DeviceHandler
    ):
        self._host = host
        self._port = port
        self._tls = tls
        self._authorization = f"Bearer {secret}".encode()
        self._hello = hello
        self._handler = handler
        self._current: Optional[_Connection] = None
        self._closing: set[asyncio.Task] = set()

    @property
    def connected(self) -> bool:
        return self._current is not None

    async def serve(self) -> None:
        """Serve devices until cancelled."""
        async with serve(
            self._serve_connection, self._host, self._port, ssl=self._tls, process_request=self._authorize
        ):
            logger.info(f"✅ Listening for the device on wss://{self._host}:{self._port}/")
            await asyncio.Future()

    def _authorize(self, websocket: ServerConnection, request: Request) -> Optional[Response]:
        """Turn away a connection without the device secret; None lets it in."""
        presented = request.headers.get("Authorization", "").encode()
        if hmac.compare_digest(presented, self._authorization):
            return None
        logger.warning(f"🚫 Refused a device connection from {websocket.remote_address}: wrong or missing secret")
        return websocket.respond(HTTPStatus.UNAUTHORIZED, "Wrong or missing device secret\n")

    def send_json(self, obj: dict) -> None:
        """Send a control message to the device, if one is connected.

        Compact JSON: the device matches substrings such as `"value":"idle"`
        rather than parsing, so `"value": "idle"` would be ignored.
        """
        if self._current is not None:
            self._current.outbox.put_nowait(json.dumps(obj, separators=(",", ":")))

    def send_audio(self, pcm: bytes) -> None:
        if self._current is not None:
            self._current.outbox.put_nowait(pcm)

    def _close_in_background(self, connection: _Connection) -> None:
        task = asyncio.create_task(connection.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _serve_connection(self, websocket: ServerConnection) -> None:
        connection = _Connection(websocket)
        logger.info(f"🔗 Device connected from {connection.address}")
        old, self._current = self._current, connection
        if old is not None:
            logger.warning(f"Device connection from {old.address} superseded by the new one")
            self._close_in_background(old)
        self.send_json({"type": "hello", **self._hello})
        await self._handler.device_connected()
        try:
            async for message in websocket:
                if self._current is not connection:
                    break  # superseded: only the newest connection is heard
                if isinstance(message, bytes):
                    if len(message) % 2:
                        logger.warning(f"⚠️ Dropping audio with an odd byte count: {len(message)}")
                        continue
                    await self._handler.device_audio(message)
                else:
                    await self._handle_text(message)
        except ConnectionClosed as e:
            logger.info(f"Device connection from {connection.address} ended: {e}")
        finally:
            connection.writer.cancel()
            if self._current is connection:
                self._current = None
                await self._handler.device_disconnected()
            logger.info(f"🔌 Device {connection.address} disconnected")

    async def _handle_text(self, text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            return
        if isinstance(data, dict) and isinstance(data.get("type"), str):
            await self._handler.device_message(data["type"], data)
