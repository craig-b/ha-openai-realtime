"""Fake peers for the add-on: an OpenAI Realtime server and a Voice PE device.

Both speak the real wire protocols over real WebSockets, so the add-on under
test runs unmodified. Each records what it receives and offers waits with
timeouts so tests can assert on order and timing.
"""

import asyncio
import base64
import json
import ssl
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
        return (await self.function_calls([(name, arguments)]))[0]

    async def function_calls(self, calls: list[tuple[str, dict]]) -> list[str]:
        """A response that only calls tools, in this order; returns the call ids."""
        response = FakeResponse(self)
        await self.send({"type": "response.created", "response": response.body("in_progress")})
        output = []
        for index, (name, arguments) in enumerate(calls):
            item = {
                "id": _id("item"),
                "object": "realtime.item",
                "type": "function_call",
                "status": "in_progress",
                "call_id": _id("call"),
                "name": name,
                "arguments": "",
            }
            await self.send({"type": "conversation.item.added", "item": item})
            await self.send({
                "type": "response.function_call_arguments.done",
                "response_id": response.id,
                "item_id": item["id"],
                "output_index": index,
                "call_id": item["call_id"],
                "arguments": json.dumps(arguments),
            })
            output.append({**item, "status": "completed", "arguments": json.dumps(arguments)})
        await self.send({"type": "response.done", "response": response.body("completed", output=output)})
        return [item["call_id"] for item in output]

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
        # An error to answer each session.update with, as OpenAI does when it refuses a setting.
        self.refuse_sessions_with: Optional[dict] = None
        # GPT-Live sessions (/v1/live/sessions), and an error to answer session.start with.
        self.live_connections: list[FakeLiveConnection] = []
        self.refuse_live_with: Optional[dict] = None

    @property
    def base_url(self) -> str:
        """The API base URL; the client connects to its /realtime."""
        return f"http://127.0.0.1:{self.port}/v1"

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
        if websocket.request.path.startswith("/v1/live/sessions"):
            await self._handle_live(websocket)
            return
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
                if event.get("type") == "session.update" and self.refuse_sessions_with:
                    await conn.send({"type": "error", "error": self.refuse_sessions_with})
                elif event.get("type") == "session.update" and self.configure_sessions:
                    if self.session_updated_delay:
                        await asyncio.sleep(self.session_updated_delay)
                    await conn.send({"type": "session.updated", "session": event["session"]})
        except websockets.ConnectionClosed:
            pass
        finally:
            conn.closed.set()

    async def _handle_live(self, websocket) -> None:
        conn = FakeLiveConnection(websocket, websocket.request.headers.get("Authorization", ""))
        self.live_connections.append(conn)
        try:
            async for message in websocket:
                event = json.loads(message)
                conn.events.append(event)
                if event.get("type") == "session.start":
                    if self.refuse_live_with:
                        await conn.send({"type": "error", "error": self.refuse_live_with})
                    else:
                        conn.started_at = time.monotonic()
                        await conn.send({"type": "session.started", "session": {"id": _id("live"), **event["session"]}})
                elif event.get("type") == "session.close":
                    await conn.send({"type": "session.closed", "reason": "close_requested", "usage": {"seconds": 12.0}})
                    await websocket.close()
        except websockets.ConnectionClosed:
            pass
        finally:
            conn.closed.set()

    async def live_connection(self, index: int = 0, timeout: float = 5.0) -> "FakeLiveConnection":
        """Wait for the `index`-th GPT-Live connection (0-based) and for its session.start."""
        await wait_until(lambda: len(self.live_connections) > index, timeout, f"GPT-Live connection #{index}")
        conn = self.live_connections[index]
        await conn.wait_for("session.start", timeout=timeout)
        return conn

    async def connection(self, index: int = 0, timeout: float = 5.0) -> FakeRealtimeConnection:
        """Wait for the `index`-th connection (0-based) and for its session to be configured."""
        await wait_until(lambda: len(self.connections) > index, timeout, f"OpenAI connection #{index}")
        conn = self.connections[index]
        await conn.wait_for("session.update", timeout=timeout)
        return conn

    @property
    def live(self) -> list[FakeRealtimeConnection]:
        return [c for c in self.connections if not c.closed.is_set()]


class FakeLiveConnection(FakeRealtimeConnection):
    """One client connection to the fake GPT-Live (/v1/live/sessions)."""

    def __init__(self, websocket, authorization: str):
        super().__init__(websocket, "/v1/live/sessions")
        self.authorization = authorization
        self.started_at = time.monotonic()

    @property
    def session(self) -> dict:
        """The session config from session.start."""
        return self.of_type("session.start")[-1]["session"]

    def appended_audio(self) -> bytes:
        return b"".join(base64.b64decode(e["audio"]) for e in self.of_type("session.input_audio.append"))

    @property
    def ms(self) -> int:
        """Now, on the session's timeline: milliseconds since it started."""
        return int((time.monotonic() - self.started_at) * 1000)

    async def heard(self, text: str, start_ms: Optional[int] = None) -> None:
        """GPT-Live hears the user say `text`: now, or at `start_ms` (transcribed late)."""
        ms = self.ms if start_ms is None else start_ms
        for word in text.split(" "):
            await self.send({"type": "session.input_transcript.delta", "delta": word + " ",
                             "start_ms": ms, "end_ms": ms + 200})
            ms += 200

    async def says(self, text: str, audio: bytes) -> None:
        """GPT-Live speaks: its words, and the audio of them, in 100 ms chunks."""
        await self.send({"type": "session.output_transcript.delta", "delta": text,
                         "start_ms": self.ms, "end_ms": self.ms + 200})
        chunk = 24000 * 2 // 10
        for i in range(0, len(audio), chunk):
            await self.send({"type": "session.output_audio.delta",
                             "delta": base64.b64encode(audio[i:i + chunk]).decode()})

    async def delegates(self) -> None:
        await self.send({"type": "session.delegation.created",
                         "delegation": {"id": _id("dlg"), "target": "responses", "offset_ms": self.ms}})

    async def backend(self, event: dict) -> None:
        await self.send({"type": "response.event", "event": event})

    async def function_call(self, name: str, arguments: dict) -> str:
        """The backend's response calls a tool, and completes; returns the call id."""
        call_id, response_id = _id("call"), _id("resp")
        await self.backend({"type": "response.created", "response": {"id": response_id}})
        await self.backend({"type": "response.output_item.done", "item": {
            "type": "function_call", "id": _id("fc"), "call_id": call_id, "name": name,
            "arguments": json.dumps(arguments)}})
        await self.backend({"type": "response.completed", "response": {"id": response_id}})
        return call_id


class FakeDevice:
    """A WebSocket client speaking the va_client side of docs/protocol.md."""

    def __init__(self, url: str, secret: Optional[str], tls: ssl.SSLContext):
        self.url = url
        # Sent as the firmware does; None sends no Authorization header.
        self.secret = secret
        self.tls = tls
        self.websocket = None
        self.raw_text: list[str] = []
        self.messages: list[dict] = []
        self.audio = bytearray()
        self.closed = asyncio.Event()
        self._reader = None

    async def connect(self, send_start: bool = True) -> "FakeDevice":
        headers = {"Authorization": f"Bearer {self.secret}"} if self.secret is not None else {}
        self.websocket = await websockets.connect(self.url, additional_headers=headers, ssl=self.tls)
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
