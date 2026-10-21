"""Pipecat's WebSocket server transport, with a new device connection taking over at once.

Pipecat 0.0.97 serves one client at a time and, when a second connects, awaits
the first one's close handshake before serving the second. A Voice PE that
reconnects after a Wi-Fi drop leaves its old connection half-open: the close
is never answered, so the reconnected device would get no `hello`, and none of
what it sends would be read, until the 10 s close timeout. And whichever
connection's handler finished last cleared the transport's connection, even
when that was the old one finishing after the new one had taken over.

Here the newest connection takes over immediately; the old one is closed in
the background, and its ending only clears the transport's connection if it
is still the current one.
"""
import asyncio
import logging

from pipecat.frames.frames import InputAudioRawFrame
from pipecat.transports.websocket.server import (
    WebsocketServerInputTransport,
    WebsocketServerOutputTransport,
    WebsocketServerTransport,
)

logger = logging.getLogger(__name__)


class DeviceServerInputTransport(WebsocketServerInputTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Closes of superseded connections still running in the background.
        self._closing: set[asyncio.Task] = set()

    def _close_in_background(self, websocket) -> None:
        task = asyncio.create_task(websocket.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _client_handler(self, websocket):  # type: ignore[override]
        logger.info(f"New device connection from {websocket.remote_address}")
        old = self._websocket
        self._websocket = websocket
        if old is not None:
            logger.warning(f"Device connection from {old.remote_address} superseded by the new one")
            self._close_in_background(old)

        await self._callbacks.on_client_connected(websocket)

        if not self._monitor_task and self._params.session_timeout:
            self._monitor_task = self.create_task(
                self._monitor_websocket(websocket, self._params.session_timeout)
            )

        try:
            async for message in websocket:
                if not self._params.serializer:
                    continue
                frame = await self._params.serializer.deserialize(message)
                if not frame:
                    continue
                if isinstance(frame, InputAudioRawFrame):
                    await self.push_audio_frame(frame)
                else:
                    await self.push_frame(frame)
        except Exception as e:
            logger.info(f"Device connection from {websocket.remote_address} ended: {e.__class__.__name__} ({e})")

        if self._websocket is websocket:
            self._websocket = None
        await self._callbacks.on_client_disconnected(websocket)
        self._close_in_background(websocket)
        logger.info(f"Device {websocket.remote_address} disconnected")


class DeviceServerOutputTransport(WebsocketServerOutputTransport):
    async def set_client_connection(self, websocket):  # type: ignore[override]
        # The input side closes superseded connections; never wait on one here.
        self._websocket = websocket


class DeviceServerTransport(WebsocketServerTransport):
    def input(self) -> DeviceServerInputTransport:
        if not self._input:
            self._input = DeviceServerInputTransport(
                self, self._host, self._port, self._params, self._callbacks, name=self._input_name
            )
        return self._input

    def output(self) -> DeviceServerOutputTransport:
        if not self._output:
            self._output = DeviceServerOutputTransport(self, self._params, name=self._output_name)
        return self._output

    async def _on_client_disconnected(self, websocket):
        if self._output is None:
            logger.error("A WebsocketServerTransport output is missing in the pipeline")
            return
        # A superseded connection ending must not unset the one that replaced it.
        if self._output._websocket is websocket:
            await self._output.set_client_connection(None)
        await self._call_event_handler("on_client_disconnected", websocket)
