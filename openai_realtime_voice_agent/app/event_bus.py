"""Requests from Home Assistant's automations, as events on its event bus.

An automation fires an event, and the device speaks:

    openai_realtime_voice_agent_announce             {"message": "The washing is done."}
    openai_realtime_voice_agent_start_conversation   {"prompt": "Ask whether to turn the heating up."}

An announcement is said word for word, and the device doesn't listen after it.
Starting a conversation has the model speak first about the prompt, in its own
words, and then the device listens for the answer.

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
open, and made again whenever the connection drops.
"""
import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

ANNOUNCE = "openai_realtime_voice_agent_announce"
START_CONVERSATION = "openai_realtime_voice_agent_start_conversation"
DONE = "openai_realtime_voice_agent_done"

# (event type, the field of its data that holds the text, the request's name in DONE)
_REQUESTS = ((ANNOUNCE, "message", "announce"), (START_CONVERSATION, "prompt", "start_conversation"))


class EventBus:
    # Waits between attempts to connect, the last repeating.
    RETRY_S = (1.0, 2.0, 5.0, 10.0, 30.0)
    OPEN_TIMEOUT_S = 10.0

    def __init__(
        self,
        api_url: str,
        token: str,
        announce: Callable[[str], Awaitable[str]],
        start_conversation: Callable[[str], Awaitable[str]],
    ):
        """`api_url` is Home Assistant's REST API, e.g. http://supervisor/core/api.

        Each handler says the text, and returns how it went (`result` in DONE).
        """
        self._api_url = api_url
        self.url = api_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/websocket"
        self._token = token
        self._handlers = {ANNOUNCE: announce, START_CONVERSATION: start_conversation}
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        """Listen for the events until cancelled; requests still waiting are dropped then."""
        try:
            await self._listen_and_reconnect()
        finally:
            for task in self._tasks:
                task.cancel()

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
        for number, (event_type, _, _) in enumerate(_REQUESTS, start=1):
            await ws.send(json.dumps({"id": number, "type": "subscribe_events", "event_type": event_type}))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == number and message.get("type") == "result":
                    break
            if not message.get("success"):
                raise _Refused(f"could not subscribe to {event_type}: {message.get('error')}")

    async def _listen(self, ws) -> None:
        async for raw in ws:
            message = json.loads(raw)
            if message.get("type") != "event":
                continue
            event = message.get("event") or {}
            self._request(event.get("event_type", ""), event.get("data") or {})

    def _request(self, event_type: str, data: dict) -> None:
        known = {request[0]: request for request in _REQUESTS}
        if event_type not in known:
            return
        _, field, name = known[event_type]
        request_id = data.get("request_id")
        text = data.get(field)
        if not isinstance(text, str) or not text.strip():
            logger.warning(f"⚠️ {event_type} without a {field}: its data needs \"{field}\": \"<text>\"")
            self._spawn(self._done(request_id, name, "failed"), DONE)
            return
        # Each on its own, so the next event is read meanwhile.
        self._spawn(self._carry_out(event_type, text.strip(), request_id, name), event_type)

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _carry_out(self, event_type: str, text: str, request_id: Any, name: str) -> None:
        try:
            result = await self._handlers[event_type](text)
        except Exception:
            logger.exception(f"❌ {event_type} failed")
            result = "failed"
        await self._done(request_id, name, result)

    async def _done(self, request_id: Any, name: str, result: str) -> None:
        """Fire DONE on Home Assistant's bus."""
        data = {"request_id": request_id, "request": name, "result": result}
        try:
            async with httpx.AsyncClient(timeout=self.OPEN_TIMEOUT_S) as http:
                response = await http.post(
                    f"{self._api_url}/events/{DONE}", json=data, headers={"Authorization": f"Bearer {self._token}"}
                )
                response.raise_for_status()
        except Exception as e:
            logger.warning(f"⚠️ Could not tell Home Assistant how the {name} went ({result}): {e!r}")


class _Refused(Exception):
    """Home Assistant refused the connection or the subscription."""
