"""The add-on's link to Home Assistant's event bus.

Automations ask the device to speak by firing an event:

    openai_realtime_voice_agent_announce             {"message": "The washing is done."}
    openai_realtime_voice_agent_start_conversation   {"prompt": "Ask whether to turn the heating up."}

An announcement is said word for word, and the device doesn't listen after it.
Starting a conversation has the model speak first and then the device listens
for the answer: it says `message` word for word if there is one, or else
something of its own about `prompt`; `prompt` is the conversation's context
either way. It needs at least one of them.

When it is over, the add-on fires `openai_realtime_voice_agent_done`, with the
request's `request_id` (any value the automation sent, to tell its own answer
apart) and how it went:

    {"request_id": "...", "request": "announce", "result": "spoken"}

`result` is "spoken" (the reply has played), "stopped" (a stop cut it off),
"dropped" (the device wasn't free in time) or "failed". An automation can wait
for it, matching its request_id.

The add-on subscribes to the events over Home Assistant's WebSocket API, with
the token it reads Home Assistant with: nothing to set up, no port to open, and
only what can fire events in Home Assistant can ask. The subscription is kept
open, and made again whenever the connection drops. It fires its own events
through Home Assistant's REST API, one at a time and in order.
"""
import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

INTEGRATION = "openai_realtime_voice_agent"

ANNOUNCE = f"{INTEGRATION}_announce"
START_CONVERSATION = f"{INTEGRATION}_start_conversation"
DONE = f"{INTEGRATION}_done"

SUBSCRIBED = (ANNOUNCE, START_CONVERSATION)

# Results of a request to speak, in DONE.
FAILED = "failed"


class EventBus:
    # Waits between attempts to connect, the last repeating.
    RETRY_S = (1.0, 2.0, 5.0, 10.0, 30.0)
    OPEN_TIMEOUT_S = 10.0

    def __init__(
        self,
        api_url: str,
        token: str,
        announce: Callable[[str], Awaitable[str]],
        start_conversation: Callable[[Optional[str], Optional[str]], Awaitable[str]],
    ):
        """`api_url` is Home Assistant's REST API, e.g. http://supervisor/core/api.

        Each handler says the text, and returns how it went (`result` in DONE).
        """
        self._api_url = api_url
        self.url = api_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/websocket"
        self._token = token
        self._announce = announce
        self._start_conversation = start_conversation
        self._outbox: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        """Listen for the events until cancelled; requests still waiting are dropped then."""
        sender = asyncio.create_task(self._send_events(), name="event_bus_sender")
        try:
            await self._listen_and_reconnect()
        finally:
            sender.cancel()
            for task in self._tasks:
                task.cancel()

    # --- firing events ---------------------------------------------------------------------

    def fire(self, event_type: str, data: dict) -> None:
        """Fire an event on Home Assistant's bus, after those fired before it."""
        self._outbox.put_nowait((event_type, data))

    async def _send_events(self) -> None:
        async with httpx.AsyncClient(timeout=self.OPEN_TIMEOUT_S) as http:
            while True:
                event_type, data = await self._outbox.get()
                try:
                    response = await http.post(
                        f"{self._api_url}/events/{event_type}",
                        json=data,
                        headers={"Authorization": f"Bearer {self._token}"},
                    )
                    response.raise_for_status()
                except Exception as e:
                    logger.warning(f"⚠️ Could not fire {event_type} {data}: {e!r}")

    # --- listening ---------------------------------------------------------------------------

    async def _listen_and_reconnect(self) -> None:
        failures = 0
        while True:
            try:
                async with connect(self.url, open_timeout=self.OPEN_TIMEOUT_S, max_size=None) as ws:
                    await self._subscribe(ws)
                    if failures:
                        logger.info("📣 Listening for automations' requests again")
                    else:
                        logger.info(f"📣 Listening for automations' requests ({ANNOUNCE}, {START_CONVERSATION})")
                    failures = 0
                    await self._listen(ws)
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionClosed, asyncio.TimeoutError, _Refused) as e:
                if failures == 0:
                    logger.warning(f"⚠️ Not listening for automations' requests: {e!r}; retrying")
            delay = self.RETRY_S[min(failures, len(self.RETRY_S) - 1)]
            failures += 1
            await asyncio.sleep(delay)

    async def _subscribe(self, ws) -> None:
        message = json.loads(await ws.recv())
        if message.get("type") != "auth_required":
            raise _Refused(f"expected auth_required, got {message.get('type')!r}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        message = json.loads(await ws.recv())
        if message.get("type") != "auth_ok":
            raise _Refused(f"Home Assistant refused the token: {message.get('message', message.get('type'))}")
        for number, event_type in enumerate(SUBSCRIBED, start=1):
            result = await self._command(ws, number, {"type": "subscribe_events", "event_type": event_type})
            if not result.get("success"):
                raise _Refused(f"could not subscribe to {event_type}: {result.get('error')}")

    async def _command(self, ws, number: int, command: dict) -> dict:
        """Send a command and return its result; events that come meanwhile are handled."""
        await ws.send(json.dumps({"id": number, **command}))
        while True:
            message = json.loads(await ws.recv())
            if message.get("id") == number and message.get("type") == "result":
                return message
            self._handle(message)

    async def _listen(self, ws) -> None:
        async for raw in ws:
            self._handle(json.loads(raw))

    def _handle(self, message: dict) -> None:
        if message.get("type") == "event":
            event = message.get("event") or {}
            self._request(event.get("event_type", ""), event.get("data") or {})

    def _request(self, event_type: str, data: dict) -> None:
        request_id = data.get("request_id")
        if event_type == ANNOUNCE:
            message = _text(data, "message")
            if message is None:
                self._refuse(event_type, request_id, "announce", 'its data needs "message": "<text>"')
                return
            # Each on its own, so the next event is read meanwhile.
            self._spawn(self._carry_out(event_type, request_id, "announce", self._announce(message)))
        elif event_type == START_CONVERSATION:
            message, prompt = _text(data, "message"), _text(data, "prompt")
            if message is None and prompt is None:
                self._refuse(event_type, request_id, "start_conversation", 'its data needs "message" or "prompt"')
                return
            self._spawn(
                self._carry_out(event_type, request_id, "start_conversation", self._start_conversation(message, prompt))
            )

    def _refuse(self, event_type: str, request_id: Any, name: str, why: str) -> None:
        logger.warning(f"⚠️ {event_type} without its text: {why}")
        self.fire(DONE, {"request_id": request_id, "request": name, "result": FAILED})

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _carry_out(self, event_type: str, request_id: Any, name: str, saying: Awaitable[str]) -> None:
        try:
            result = await saying
        except Exception:
            logger.exception(f"❌ {event_type} failed")
            result = FAILED
        self.fire(DONE, {"request_id": request_id, "request": name, "result": result})


def _text(data: dict, field: str) -> Optional[str]:
    """The non-blank text in `field`, or None."""
    value = data.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


class _Refused(Exception):
    """Home Assistant refused the connection or the subscription."""
