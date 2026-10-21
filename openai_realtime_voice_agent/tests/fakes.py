"""Fake peers for the add-on: an OpenAI Realtime server and a Voice PE device.

Both speak the real wire protocols over real WebSockets, so the add-on under
test runs unmodified. Each records what it receives and offers waits with
timeouts so tests can assert on order and timing.
"""

import asyncio
import base64
import json
import time
import uuid
from typing import Any, Callable, Optional

import websockets
from websockets.asyncio.server import serve


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def wait_until(predicate: Callable[[], Any], timeout: float, what) -> Any:
    """Poll `predicate` until it returns something truthy, or fail after `timeout`.

    `what` describes the wait in the failure message; a callable is evaluated
    at failure time, so it can report the state reached.
    """
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what() if callable(what) else what}")
        await asyncio.sleep(0.01)


class FakeRealtimeConnection:
    """One client connection to the fake OpenAI Realtime server."""

    def __init__(self, websocket, uri: str):
        self.websocket = websocket
        self.uri = uri
        self.events: list[dict] = []
        self.closed = asyncio.Event()

    # --- what the add-on sent ------------------------------------------------

    def index_of(self, event_type: str) -> int:
        """Position of the first event of `event_type` among those received."""
        return next(i for i, e in enumerate(self.events) if e.get("type") == event_type)

    def of_type(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == event_type]

    def count(self, event_type: str) -> int:
        return len(self.of_type(event_type))

    async def wait_for(self, event_type: str, count: int = 1, timeout: float = 3.0) -> dict:
        """Wait until at least `count` events of `event_type` arrived; return the last."""
        await wait_until(
            lambda: self.count(event_type) >= count,
            timeout,
            lambda: f"{count} x {event_type} (got {self.count(event_type)})",
        )
        return self.of_type(event_type)[count - 1]

    async def assert_none(self, event_type: str, within: float, after: int = 0) -> None:
        """Assert no more than `after` events of `event_type` arrive within `within` s."""
        await asyncio.sleep(within)
        assert self.count(event_type) <= after, (
            f"unexpected {event_type}: {self.of_type(event_type)[after:]}"
        )

    def appended_audio(self) -> bytes:
        return b"".join(base64.b64decode(e["audio"]) for e in self.of_type("input_audio_buffer.append"))

    @property
    def session(self) -> dict:
        """The session config from the most recent session.update."""
        return self.of_type("session.update")[-1]["session"]

    # --- server events -------------------------------------------------------

    async def send(self, event: dict) -> None:
        event.setdefault("event_id", _id("event"))
        await self.websocket.send(json.dumps(event))

    async def speech_started(self) -> str:
        item_id = _id("item")
        await self.send({"type": "input_audio_buffer.speech_started", "audio_start_ms": 0, "item_id": item_id})
        return item_id

    async def speech_stopped(self, item_id: Optional[str] = None) -> str:
        """Turn detection ends the user's turn; OpenAI commits it as a user item."""
        item_id = item_id or _id("item")
        await self.send({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 1000, "item_id": item_id})
        await self.send({"type": "input_audio_buffer.committed", "item_id": item_id})
        await self.send({
            "type": "conversation.item.added",
            "item": {
                "id": item_id,
                "object": "realtime.item",
                "type": "message",
                "status": "completed",
                "role": "user",
                "content": [{"type": "input_audio", "transcript": None}],
            },
        })
        return item_id

    async def user_turn(self) -> None:
        """The server's turn detection hears the user start and stop speaking."""
        item_id = await self.speech_started()
        await self.speech_stopped(item_id)

    async def user_said(self, text: str) -> None:
        """A user turn, and its transcript (sent when transcription is on)."""
        item_id = await self.speech_started()
        await self.speech_stopped(item_id)
        await self.transcript(item_id, text)

    async def transcript(self, item_id: str, text: str) -> None:
        """Transcription of a user item completes."""
        await self.send({
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": item_id,
            "content_index": 0,
            "transcript": text,
        })

    def created_items(self) -> list[dict]:
        """The conversation items the add-on created in this session."""
        return [e["item"] for e in self.of_type("conversation.item.create")]

    async def start_response(self) -> "FakeResponse":
        """Begin an assistant response: response.created + the assistant item."""
        response = FakeResponse(self)
        await self.send({
            "type": "response.created",
            "response": response.body("in_progress"),
        })
        await self.send({
            "type": "conversation.item.added",
            "item": {
                "id": response.item_id,
                "object": "realtime.item",
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        })
        return response

    async def reply(self, audio: bytes, chunk: int = 4800, transcript: str = "") -> "FakeResponse":
        """A whole spoken assistant response, burst faster than real time."""
        response = await self.start_response()
        await response.audio(audio, chunk=chunk)
        if transcript:
            await response.transcript(transcript)
        await response.finish()
        return response

    async def function_call(self, name: str, arguments: dict) -> str:
        """A response that only calls a tool; returns the call id."""
        response = FakeResponse(self)
        call_id = _id("call")
        item = {
            "id": response.item_id,
            "object": "realtime.item",
            "type": "function_call",
            "status": "in_progress",
            "call_id": call_id,
            "name": name,
            "arguments": "",
        }
        await self.send({"type": "response.created", "response": response.body("in_progress")})
        await self.send({"type": "conversation.item.added", "item": item})
        await self.send({
            "type": "response.function_call_arguments.done",
            "response_id": response.id,
            "item_id": response.item_id,
            "output_index": 0,
            "call_id": call_id,
            "arguments": json.dumps(arguments),
        })
        await self.send({
            "type": "response.done",
            "response": response.body("completed", output=[{**item, "status": "completed",
                                                             "arguments": json.dumps(arguments)}]),
        })
        return call_id

    async def error(self, code: str, message: str = "", error_type: str = "invalid_request_error") -> None:
        await self.send({
            "type": "error",
            "error": {"type": error_type, "code": code, "message": message or code},
        })

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self.websocket.close(code, reason)


class FakeResponse:
    """An assistant response being streamed by the fake server."""

    def __init__(self, conn: FakeRealtimeConnection):
        self.conn = conn
        self.id = _id("resp")
        self.item_id = _id("item")

    def body(self, status: str, output: Optional[list] = None) -> dict:
        return {
            "id": self.id,
            "object": "realtime.response",
            "status": status,
            "status_details": None,
            "output": output or [],
            "usage": {
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "input_token_details": {},
                "output_token_details": {},
            },
        }

    async def audio(self, audio: bytes, chunk: int = 4800) -> None:
        for i in range(0, len(audio), chunk):
            await self.conn.send({
                "type": "response.output_audio.delta",
                "response_id": self.id,
                "item_id": self.item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": base64.b64encode(audio[i:i + chunk]).decode(),
            })

    async def transcript(self, text: str) -> None:
        await self.conn.send({
            "type": "response.output_audio_transcript.delta",
            "response_id": self.id,
            "item_id": self.item_id,
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        })

    async def finish(self, status: str = "completed") -> None:
        await self.conn.send({
            "type": "response.output_audio.done",
            "response_id": self.id,
            "item_id": self.item_id,
            "output_index": 0,
            "content_index": 0,
        })
        await self.conn.send({"type": "response.done", "response": self.body(status)})


class FakeRealtimeServer:
    """A WebSocket server speaking enough of the OpenAI Realtime protocol.

    On connect it sends session.created; it answers every session.update with
    session.updated. Everything else is recorded and driven by the test.
    """

    def __init__(self):
        self.connections: list[FakeRealtimeConnection] = []
        self._server = None
        self.port: Optional[int] = None
        # Knobs for failure tests: refuse connections with this HTTP status;
        # delay, or never send, session.updated.
        self.reject_status: Optional[int] = None
        self.rejected = 0
        self.session_updated_delay = 0.0
        self.configure_sessions = True

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/realtime"

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0, process_request=self._process_request)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def _process_request(self, connection, request):
        if self.reject_status is not None:
            self.rejected += 1
            return connection.respond(self.reject_status, "rejected by the test\n")
        return None

    async def _handle(self, websocket) -> None:
        conn = FakeRealtimeConnection(websocket, websocket.request.path)
        self.connections.append(conn)
        try:
            await conn.send({
                "type": "session.created",
                "session": {"type": "realtime", "object": "realtime.session", "id": _id("sess")},
            })
            async for message in websocket:
                event = json.loads(message)
                conn.events.append(event)
                if event.get("type") == "session.update" and self.configure_sessions:
                    if self.session_updated_delay:
                        await asyncio.sleep(self.session_updated_delay)
                    await conn.send({"type": "session.updated", "session": event["session"]})
        except websockets.ConnectionClosed:
            pass
        finally:
            conn.closed.set()

    async def connection(self, index: int = 0, timeout: float = 5.0) -> FakeRealtimeConnection:
        """Wait for the `index`-th connection (0-based) and for its session to be configured."""
        await wait_until(lambda: len(self.connections) > index, timeout, f"OpenAI connection #{index}")
        conn = self.connections[index]
        await conn.wait_for("session.update", timeout=timeout)
        return conn

    @property
    def live(self) -> list[FakeRealtimeConnection]:
        return [c for c in self.connections if not c.closed.is_set()]


class FakeDevice:
    """A WebSocket client speaking the va_client side of docs/protocol.md."""

    def __init__(self, url: str):
        self.url = url
        self.websocket = None
        self.raw_text: list[str] = []
        self.messages: list[dict] = []
        self.audio = bytearray()
        self.closed = asyncio.Event()
        self._reader = None

    async def connect(self, send_start: bool = True) -> "FakeDevice":
        self.websocket = await websockets.connect(self.url)
        self._reader = asyncio.create_task(self._read())
        if send_start:
            await self.send({"type": "start"})
        return self

    async def _read(self) -> None:
        try:
            async for message in self.websocket:
                if isinstance(message, bytes):
                    self.audio.extend(message)
                else:
                    self.raw_text.append(message)
                    self.messages.append(json.loads(message))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.closed.set()

    async def send(self, obj: dict) -> None:
        # The firmware writes its control messages compactly.
        await self.websocket.send(json.dumps(obj, separators=(",", ":")))

    async def send_audio(self, pcm: bytes) -> None:
        await self.websocket.send(pcm)

    async def close(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()
        if self._reader is not None:
            await self._reader

    @property
    def errors(self) -> list[dict]:
        return [m for m in self.messages if m.get("type") == "error"]

    @property
    def phases(self) -> list[str]:
        return [m["value"] for m in self.messages if m.get("type") == "phase"]

    async def wait_for_phase(self, value: str, count: int = 1, timeout: float = 3.0) -> None:
        await wait_until(
            lambda: self.phases.count(value) >= count,
            timeout,
            lambda: f"{count} x phase {value} (phases so far: {self.phases})",
        )

    async def assert_no_phase(self, value: str, within: float, after: int = 0) -> None:
        await asyncio.sleep(within)
        assert self.phases.count(value) <= after, f"unexpected phase {value}: {self.phases}"

    async def wait_for_audio(self, size: int, timeout: float = 5.0) -> None:
        await wait_until(
            lambda: len(self.audio) >= size,
            timeout,
            lambda: f"{size} bytes of reply audio (got {len(self.audio)})",
        )


def pcm_silence(ms: int, rate: int) -> bytes:
    """`ms` of 16-bit mono silence at `rate` Hz."""
    return b"\x00\x00" * (rate * ms // 1000)


# The add-on sends reply audio to the device in chunks of this many ms.
OUTPUT_CHUNK_MS = 40


def pcm_tone(ms: int, rate: int, amplitude: int = 8000) -> bytes:
    """`ms` of a 16-bit mono square wave at `rate` Hz (non-silent, cheap to make)."""
    samples = rate * ms // 1000
    period = max(2, rate // 440)
    out = bytearray()
    for i in range(samples):
        value = amplitude if (i // (period // 2)) % 2 == 0 else -amplitude
        out += int(value).to_bytes(2, "little", signed=True)
    return bytes(out)
