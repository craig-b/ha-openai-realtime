"""The GPT-Live engine: one GPT-Live session (gpt-live-1, /v1/live/sessions).

GPT-Live is a voice that listens and speaks at once, with no turns: it hands
anything that needs doing to a backend model (OpenAI's Responses API), which
calls the tools. Its events are fragments of what it hears and says, its audio,
and the backend's events wrapped in `response.event`.

So the phases the device shows are the real states, read from those:

    words heard from the user            -> listening
    work handed to the backend           -> thinking (the user has finished)
    its voice audible                    -> replying
    its voice silent for SPEECH_HANG_S   -> the reply has ended

GPT-Live's audio may run on through silence; only audible audio is played, so
a reply starts and ends when the voice does.

A session bills every minute it is open, so the agent closes it when the
conversation on the device ends (live → session_reuse_timeout_seconds).
"""
import asyncio
import base64
import json
import logging
import time
from typing import Optional

import numpy as np
import websockets
from openai import AsyncOpenAI
from websockets.exceptions import ConnectionClosed

from app.audio import OPENAI_RATE
from app.config import Settings
from app import prompts
from app.session import SessionEvents, SessionFailed, SessionSetup

logger = logging.getLogger(__name__)


# Sessions closing in the background, kept until GPT-Live confirms.
_closing_sessions: set[asyncio.Task] = set()


class LiveSession:
    """A GPT-Live connection: the session's settings, the user's audio in, GPT-Live's events out."""

    ROLLS_OVER = False
    # Audio quieter than this (peak, of 32767) is silence.
    SILENCE_PEAK = 300
    # The voice is taken to have stopped after this much silence.
    SPEECH_HANG_S = 0.6
    # How long to wait for GPT-Live to confirm the session closed.
    CLOSE_TIMEOUT_S = 3.0
    # A backend response with no news for this long is taken to have ended:
    # its end can't be relied on to arrive, and while it runs the session
    # (billed by the minute) can't close.
    BACKEND_SILENCE_S = 30.0

    def __init__(self, settings: Settings, client: AsyncOpenAI, events: SessionEvents):
        self.settings = settings
        self._client = client
        self._events = events
        self._ws: Optional[websockets.ClientConnection] = None
        self._reader: Optional[asyncio.Task] = None
        self._closing = False
        self._started = asyncio.Event()
        self._started_at = 0.0
        self._closed = asyncio.Event()
        # Why GPT-Live refused the session's settings, if it did while it started.
        self._refused: Optional[str] = None
        self._usage_seconds: Optional[float] = None
        # Backend responses running, and when the backend last said anything.
        self._responses = 0
        self._last_backend_event = 0.0
        self._backend_watch: Optional[asyncio.Task] = None
        self._user_speaking = False
        self._user_text = ""
        self._speaking = False
        self._speech_started_ms = 0.0
        self._silent_s = 0.0
        self._said_text = ""
        # After a device "stop": GPT-Live's voice is dropped until the device wakes again.
        self._muted = False
        self._hang_task: Optional[asyncio.Task] = None

    @property
    def busy(self) -> bool:
        """Whether the voice is speaking (the backend's work counts through work_started)."""
        return self._speaking

    # --- opening and closing ---------------------------------------------------------

    def _url(self) -> str:
        base = str(self._client.base_url).rstrip("/")
        scheme, rest = base.split("://", 1)
        return f"{'wss' if scheme == 'https' else 'ws'}://{rest}/live/sessions"

    async def connect(self, timeout: float) -> None:
        self._ws = await asyncio.wait_for(
            websockets.connect(
                self._url(), additional_headers={"Authorization": f"Bearer {self._client.api_key}"}, max_size=None
            ),
            timeout,
        )
        self._reader = asyncio.create_task(self._read())

    async def configure(self, setup: SessionSetup, timeout: float) -> None:
        """Start the session; raises SessionFailed if GPT-Live doesn't take the settings."""
        await self._send({"type": "session.start", "session": self._session_config(setup)})
        try:
            await asyncio.wait_for(self._started.wait(), timeout)
        except asyncio.TimeoutError:
            raise SessionFailed(f"GPT-Live did not start the session within {timeout:.0f} s") from None
        if self._refused is not None:
            raise SessionFailed(self._refused)

    def _session_config(self, setup: SessionSetup) -> dict:
        s = self.settings
        tools = list(setup.tools.definitions)
        if s.enable_web_search:
            search = {"type": "web_search"}
            if location := setup.home.web_search_location():
                search["user_location"] = location
            tools.append(search)
        backend = {
            "model": s.live_backend_model,
            "instructions": prompts.live_backend(s, setup.home),
            "tools": tools,
            "tool_choice": "auto",
            "reasoning": {"effort": s.live_reasoning_effort},
        }
        if s.live_service_tier != "auto":
            backend["service_tier"] = s.live_service_tier
        return {
            "model": s.live_model,
            "instructions": prompts.live_voice(s, setup.home),
            "audio": {"format": {"type": "audio/pcm", "rate": OPENAI_RATE}, "output": {"voice": s.live_voice}},
            "delegation": {"type": "responses", "responses": backend},
        }

    async def close(self) -> None:
        """End the session. GPT-Live's confirmation, with the final usage, is
        awaited in the background, so the next session can open meanwhile."""
        if self._closing:
            return
        self._closing = True
        self._cancel_hang()
        if self._backend_watch is not None:
            self._backend_watch.cancel()
        if self._reader is asyncio.current_task():
            await self._finish_closing()  # GPT-Live ended it: nothing to wait for
            return
        task = asyncio.create_task(self._finish_closing())
        _closing_sessions.add(task)
        task.add_done_callback(_closing_sessions.discard)

    async def _finish_closing(self) -> None:
        if self._ws is not None and self._started.is_set() and not self._closed.is_set():
            try:
                await self._ws.send(json.dumps({"type": "session.close"}))
                await asyncio.wait_for(self._closed.wait(), self.CLOSE_TIMEOUT_S)
            except (ConnectionClosed, asyncio.TimeoutError):
                pass
        if self._usage_seconds is not None:
            logger.info(f"🧾 GPT-Live session used {self._usage_seconds:.0f} s of voice")
        if self._ws is not None:
            await self._ws.close()
        if self._reader is not None and self._reader is not asyncio.current_task():
            await asyncio.gather(self._reader, return_exceptions=True)

    # --- what the agent sends ----------------------------------------------------------

    async def send_audio(self, audio: bytes) -> None:
        await self._send({"type": "session.input_audio.append", "audio": base64.b64encode(audio).decode("ascii")})

    async def clear_input(self) -> None:
        pass  # GPT-Live takes audio as it streams: there is no buffer to clear

    async def interrupt(self) -> None:
        """The device's "stop": silence GPT-Live, and drop its voice until the device wakes."""
        self._muted = True
        self._end_speech()
        await self._send({
            "type": "session.instructions.append",
            "delegation_id": None,
            "content": "The user said stop. Stop speaking now, and wait.",
        })

    def woke(self) -> None:
        self._muted = False

    def real_speech(self) -> None:
        pass  # no turns

    async def speak(self, instruction: str) -> None:
        """Tell the voice, which speaks on its own when it has something to say.

        Its voice is let through again, if a "stop" had dropped it.
        """
        self._muted = False
        await self._send({"type": "session.instructions.append", "delegation_id": None, "content": instruction})

    async def send_tool_results(self, results: list[tuple[str, str]]) -> None:
        """Each call's result, then continue the backend's work."""
        for call_id, output in results:
            await self._send({
                "type": "response.item.create",
                "item": {"type": "function_call_output", "call_id": call_id, "output": output},
            })
        await self._send({"type": "response.create"})

    async def _send(self, event: dict) -> None:
        if self._ws is None or self._closing:
            return
        try:
            await self._ws.send(json.dumps(event))
        except ConnectionClosed:
            pass  # the reader reports it

    # --- GPT-Live's events ------------------------------------------------------------

    async def _read(self) -> None:
        reason = "GPT-Live closed the connection"
        try:
            async for raw in self._ws:
                event = json.loads(raw)
                try:
                    reason = await self._on_event(event) or reason
                except Exception:
                    logger.exception(f"❌ handling GPT-Live's {event.get('type')} failed")
        except ConnectionClosed as e:
            reason = f"connection closed: {e}"
        self._closed.set()
        self._cancel_hang()
        self._finish_backend_work()
        if not self._closing:
            await self._events.session_lost(self, reason)

    async def _on_event(self, event: dict) -> Optional[str]:
        """Handle one event; returns why the session ended, if it did."""
        if self._closing and event.get("type") != "session.closed":
            return None  # the agent has moved on from this session
        kind = event.get("type", "")
        if kind != "session.output_audio.delta":
            logger.debug(f"⬅️ {kind}")
        if kind == "session.started":
            self._started_at = time.monotonic()
            self._started.set()
        elif kind == "session.input_transcript.delta":
            self._heard(event.get("delta", ""), event.get("start_ms"))
        elif kind == "session.output_transcript.delta":
            self._said_text += event.get("delta", "")
        elif kind == "session.output_audio.delta":
            self._voice(base64.b64decode(event.get("delta", "")))
        elif kind == "session.delegation.created":
            self._user_finished()
            logger.info("🧠 GPT-Live handed work to its backend")
        elif kind == "response.event":
            await self._on_response_event(event.get("event") or {})
        elif kind == "session.usage.updated":
            self._usage_seconds = (event.get("usage") or {}).get("seconds", self._usage_seconds)
        elif kind == "session.closed":
            self._usage_seconds = (event.get("usage") or {}).get("seconds", self._usage_seconds)
            self._closed.set()
            return f"GPT-Live ended the session ({event.get('reason', 'no reason given')})"
        elif kind == "error":
            self._on_error(event.get("error") or {})
        return None

    def _heard(self, delta: str, start_ms: Optional[int]) -> None:
        """Words heard from the user."""
        if self._speaking and start_ms is not None and start_ms < self._speech_started_ms:
            # Said before the voice began, and only now transcribed: the end of
            # what it is answering, not the user talking over it.
            logger.debug(f"🗣️ late words from before the reply: {delta!r}")
            return
        if not self._user_speaking:
            self._user_speaking = True
            self._muted = False
            self._events.user_started()
        self._user_text += delta

    def _user_finished(self) -> None:
        if self._user_speaking:
            self._user_speaking = False
            self._events.user_stopped()
        if self._user_text.strip():
            logger.info(f"🗣️ user: {self._user_text.strip()}")
        self._user_text = ""

    def _voice(self, audio: bytes) -> None:
        """GPT-Live's audio: played while it is audible, and through its short pauses."""
        if self._muted or not audio:
            return
        samples = np.frombuffer(audio[: len(audio) // 2 * 2], dtype=np.int16)
        audible = samples.size > 0 and int(np.abs(samples.astype(np.int32)).max()) > self.SILENCE_PEAK
        if audible:
            if not self._speaking:
                self._speaking = True
                # On GPT-Live's timeline (milliseconds from session start), as its transcripts are.
                self._speech_started_ms = (time.monotonic() - self._started_at) * 1000
                self._user_finished()
            self._silent_s = 0.0
        elif not self._speaking:
            return  # silence between replies
        else:
            self._silent_s += samples.size / OPENAI_RATE
        self._events.reply_audio(audio)
        if self._silent_s >= self.SPEECH_HANG_S:
            self._end_speech()
        else:
            self._arm_hang()

    def _arm_hang(self) -> None:
        """End the reply if no more audio comes at all."""
        self._cancel_hang()
        self._hang_task = asyncio.create_task(self._hang())

    async def _hang(self) -> None:
        await asyncio.sleep(self.SPEECH_HANG_S)
        self._hang_task = None
        self._end_speech()

    def _cancel_hang(self) -> None:
        if self._hang_task is not None and self._hang_task is not asyncio.current_task():
            self._hang_task.cancel()
        self._hang_task = None

    def _end_speech(self) -> None:
        self._cancel_hang()
        if not self._speaking:
            return
        self._speaking = False
        self._silent_s = 0.0
        self._events.reply_ended()
        if self._said_text.strip():
            logger.info(f"🤖 assistant: {self._said_text.strip()}")
        self._said_text = ""

    async def _on_response_event(self, inner: dict) -> None:
        """The backend's work: its responses and tool calls."""
        kind = inner.get("type", "")
        logger.debug(f"⬅️ response.event {kind}")
        self._last_backend_event = time.monotonic()
        if kind == "response.created":
            self._responses += 1
            self._events.work_started()
            if self._backend_watch is None or self._backend_watch.done():
                self._backend_watch = asyncio.create_task(self._watch_backend())
        elif kind == "response.output_item.done":
            item = inner.get("item") or {}
            if item.get("type") == "function_call":
                self._events.tool_call(item.get("call_id", ""), item.get("name", ""), item.get("arguments") or "")
        elif kind in ("response.completed", "response.failed", "response.incomplete"):
            if self._responses:
                self._responses -= 1
                self._events.work_finished()
            if kind == "response.failed":
                error = ((inner.get("response") or {}).get("error") or {}).get("message", "")
                self._events.turn_failed(f"the backend's response failed: {error}")
            self._events.response_done(self)

    async def _watch_backend(self) -> None:
        """End backend work that has gone silent (BACKEND_SILENCE_S)."""
        while self._responses:
            await asyncio.sleep(min(1.0, self.BACKEND_SILENCE_S))
            silent = time.monotonic() - self._last_backend_event
            if self._responses and silent >= self.BACKEND_SILENCE_S:
                logger.warning(f"⚠️ GPT-Live's backend said nothing for {silent:.0f} s: taking its work as ended")
                self._finish_backend_work()

    def _finish_backend_work(self) -> None:
        while self._responses:
            self._responses -= 1
            self._events.work_finished()

    def _on_error(self, error: dict) -> None:
        code = error.get("code") or ""
        if not self._started.is_set():
            # The session's settings were refused: say which setting to change.
            param = error.get("param") or ""
            setting = setting_for(param)
            advice = f"; check the setting {setting}" if setting else ""
            self._refused = f"GPT-Live refused the session's settings: {error.get('message', code)} ({param or code}){advice}"
            self._started.set()
            return
        logger.error(f"❌ GPT-Live error: {error}")
        self._events.error(f"{code}: {error.get('message', '')}")


# The setting behind each part of the session GPT-Live may reject, by the
# error's `param`, most specific first.
SETTING_FOR_PARAM = (
    ("session.delegation.responses.model", "live → backend_model"),
    ("session.delegation.responses.reasoning", "live → reasoning_effort"),
    ("session.delegation.responses.service_tier", "live → service_tier"),
    ("session.delegation.responses.instructions", "house_rules"),
    ("session.delegation.responses.tools", "the Home Assistant tools (mcp_tool_allowlist), or enable_web_search"),
    ("session.audio.output.voice", "live → voice"),
    ("session.instructions", "house_rules"),
    ("session.model", "live → model"),
)


def setting_for(param: str) -> str | None:
    """The add-on setting behind the session field `param`, if one is."""
    for prefix, setting in SETTING_FOR_PARAM:
        if param == prefix or param.startswith(prefix + ".") or param.startswith(prefix + "["):
            return setting
    return None
