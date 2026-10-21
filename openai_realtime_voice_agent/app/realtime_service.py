"""The add-on's OpenAI Realtime service: Pipecat's, with its failure modes patched.

It also decides when the OpenAI session is open: one session per conversation.
Pipecat connects when the pipeline starts and stays connected; here a session
opens when the device wakes (or mic audio arrives with none open), holding the
mic audio until it is configured, and closes once nothing has happened for the
idle timeout. A connection that dies ends the session; the next wake opens a
new one.
"""
import asyncio
import base64
import enum
import logging
import time
from typing import Awaitable, Callable, Optional

from pipecat.frames.frames import StartFrame
from pipecat.services.llm_service import LLMService
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from app.phase_emitter import TURN_LIVENESS

logger = logging.getLogger(__name__)

# OpenAI Realtime's input audio: 24 kHz, 16-bit mono.
_INPUT_BYTES_PER_S = 24000 * 2


class SessionState(enum.Enum):
    CLOSED = "closed"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"


class SafeRealtimeLLMService(OpenAIRealtimeLLMService):
    """OpenAIRealtimeLLMService with audio-truncation-on-interruption disabled.

    pipecat's `_truncate_current_audio_response()` (called by `_handle_interruption`
    on EVERY interruption — both our device "stop" AND pipecat's own server-VAD
    barge-in when the user wakes/speaks mid-reply) sends a
    `conversation.item.truncate` with `audio_end_ms = wall-clock ms since audio
    start`. But OpenAI BURSTS the reply faster than real-time, so that elapsed
    value massively overshoots the audio that actually exists, and OpenAI rejects
    it with `invalid_request_error("Audio content of N ms is already shorter than
    M ms")`. That errored truncate wedges the realtime session, so the user's very
    next turn gets NO response — the recurring "interrupt, then immediately ask
    again → silence" bug (confirmed in logs: session goes quiet right after
    `_truncate_current_audio_response`).

    The device stops playback authoritatively on its own, so server-side
    truncation buys us nothing. No-op it. (Cost: OpenAI's conversation history
    keeps the full assistant text the user may not have fully heard — purely
    cosmetic for context.)

    See the module docstring for the session lifecycle.
    """

    # How long a new session may take to connect and be configured.
    OPEN_TIMEOUT_S = 10.0
    # Mic audio held while a session opens; older audio is dropped beyond this.
    BUFFER_LIMIT_S = 10.0
    IDLE_CHECK_S = 1.0

    def __init__(self, *, idle_timeout_s: float = 300.0, **kwargs):
        super().__init__(**kwargs)
        self._idle_timeout_s = max(0.0, idle_timeout_s)
        self._state = SessionState.CLOSED
        self._session_ready = asyncio.Event()
        self._opened_at = 0.0
        self._last_activity = 0.0
        self._buffered_audio: list[bytes] = []
        self._buffered_bytes = 0
        self._wake_while_closing = False
        self._open_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._on_session_failed: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_session_lost: Optional[Callable[[str], Awaitable[None]]] = None

    def set_session_handlers(
        self,
        on_failed: Callable[[str], Awaitable[None]],
        on_lost: Callable[[str], Awaitable[None]],
    ) -> None:
        """Wire what happens when a session can't be opened, or dies while open."""
        self._on_session_failed = on_failed
        self._on_session_lost = on_lost

    @property
    def session_state(self) -> SessionState:
        return self._state

    async def start(self, frame: StartFrame):  # type: ignore[override]
        # Skip OpenAIRealtimeLLMService.start, which connects: sessions open on demand.
        await LLMService.start(self, frame)

    async def stop(self, frame):  # type: ignore[override]
        await self._cancel_session_tasks()
        await super().stop(frame)

    async def cancel(self, frame):  # type: ignore[override]
        await self._cancel_session_tasks()
        await super().cancel(frame)

    def note_activity(self) -> None:
        """Something happened in the conversation; the idle timeout restarts."""
        self._last_activity = time.monotonic()

    def on_wake(self) -> None:
        """The device woke: the user is about to speak. Open a session if none is."""
        self.note_activity()
        if self._state is SessionState.CLOSED:
            self._begin_open("wake")
        elif self._state is SessionState.CLOSING:
            self._wake_while_closing = True

    async def clear_input_audio(self) -> None:
        """Drop the user's uncommitted audio: OpenAI's input buffer, or ours."""
        if self._state is SessionState.OPEN:
            await self.send_client_event(events.InputAudioBufferClearEvent())
        else:
            self._drop_buffered_audio()

    def session_lost(self, reason: str) -> None:
        """OpenAI's connection died. End the session; the next wake opens another."""
        if self._state is SessionState.OPENING:
            self.create_task(self._abort_open(reason), "abort_open")
        elif self._state is SessionState.OPEN:
            self._state = SessionState.CLOSING
            self.create_task(self._lose(reason), "session_lost")

    async def _abort_open(self, reason: str) -> None:
        if self._open_task is not None and not self._open_task.done():
            await self.cancel_task(self._open_task)
        await self._disconnect()
        await self._fail_open(reason)

    async def _lose(self, reason: str) -> None:
        await self._close(f"lost: {reason[:90]}")
        if self._on_session_lost is not None:
            await self._on_session_lost(reason)

    # --- opening ---------------------------------------------------------------

    def _begin_open(self, why: str) -> None:
        self._state = SessionState.OPENING
        self._open_task = self.create_task(self._open(why), "open_session")

    async def _open(self, why: str) -> None:
        started = time.monotonic()
        logger.info(f"🔌 opening an OpenAI session ({why})")
        self._session_ready.clear()
        # Nothing from an earlier session may act on this one: a tool result
        # that arrived after it closed, or a response it was waiting to create.
        self._run_llm_when_api_session_ready = False
        if self._context is not None:
            await self._process_completed_function_calls(send_new_results=False)

        await self._connect()
        if self._websocket is None:
            await self._fail_open("could not connect to OpenAI")
            return
        try:
            await asyncio.wait_for(self._session_ready.wait(), self.OPEN_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self._disconnect()
            await self._fail_open(f"OpenAI did not configure the session within {self.OPEN_TIMEOUT_S:.0f} s")
            return

        self._state = SessionState.OPEN
        self._opened_at = time.monotonic()
        buffered_ms = self._buffered_bytes * 1000 // _INPUT_BYTES_PER_S
        for audio in self._buffered_audio:
            await self.send_client_event(
                events.InputAudioBufferAppendEvent(audio=_b64(audio))
            )
        self._drop_buffered_audio()
        self.note_activity()
        self._idle_task = self.create_task(self._close_when_idle(), "close_when_idle")
        logger.info(
            f"✅ OpenAI session open in {(self._opened_at - started) * 1000:.0f} ms "
            f"({buffered_ms} ms of audio held meanwhile)"
        )

    async def _fail_open(self, reason: str) -> None:
        if self._state is not SessionState.OPENING:
            return  # already failed by the other path
        logger.error(f"❌ OpenAI session failed to open: {reason}")
        self._state = SessionState.CLOSED
        self._drop_buffered_audio()
        if self._on_session_failed is not None:
            await self._on_session_failed(reason)

    async def _handle_evt_session_updated(self, evt):  # type: ignore[override]
        await super()._handle_evt_session_updated(evt)
        self._session_ready.set()

    async def _send_user_audio(self, frame):  # type: ignore[override]
        self.note_activity()
        if self._state is SessionState.OPEN:
            await super()._send_user_audio(frame)
            return
        # Opening, closing or closed: hold it for the session that opens next.
        self._buffered_audio.append(frame.audio)
        self._buffered_bytes += len(frame.audio)
        limit = int(self.BUFFER_LIMIT_S * _INPUT_BYTES_PER_S)
        while self._buffered_bytes > limit and len(self._buffered_audio) > 1:
            self._buffered_bytes -= len(self._buffered_audio.pop(0))
        if self._state is SessionState.CLOSED:
            self._begin_open("mic audio")

    def _drop_buffered_audio(self) -> None:
        self._buffered_audio = []
        self._buffered_bytes = 0

    # --- closing ---------------------------------------------------------------

    async def _close_when_idle(self) -> None:
        while self._state is SessionState.OPEN:
            await asyncio.sleep(self.IDLE_CHECK_S)
            if self._state is not SessionState.OPEN:
                return
            if self._current_assistant_response is not None or TURN_LIVENESS.in_flight > 0:
                continue
            quiet = time.monotonic() - max(self._last_activity, TURN_LIVENESS.last_activity)
            if quiet >= self._idle_timeout_s:
                self._state = SessionState.CLOSING
                await self._close(f"idle for {quiet:.0f} s")
                return

    async def _close(self, reason: str) -> None:
        """Close the session; the conversation ends with it."""
        self._state = SessionState.CLOSING
        self._drop_buffered_audio()
        self._wake_while_closing = False
        current = asyncio.current_task()
        for task in (self._open_task, self._idle_task):
            if task is not None and task is not current and not task.done():
                await self.cancel_task(task)
        self._open_task = self._idle_task = None
        await self._disconnect()
        self._current_assistant_response = None
        self._current_audio_response = None
        self._pending_function_calls = {}
        self._messages_added_manually = {}
        if self._context is not None:
            self._context.set_messages([])
        self._state = SessionState.CLOSED
        age = time.monotonic() - self._opened_at if self._opened_at else 0.0
        logger.info(f"🔌 OpenAI session closed after {age:.0f} s ({reason})")
        # The user woke the device or spoke while it closed: they want a session.
        if self._wake_while_closing or self._buffered_audio:
            self._wake_while_closing = False
            self._begin_open("woken while closing")

    async def _cancel_session_tasks(self) -> None:
        for task in (self._open_task, self._idle_task):
            if task is not None and not task.done():
                await self.cancel_task(task)
        self._open_task = self._idle_task = None

    # Error codes that must NOT kill the realtime session. pipecat 0.0.97's
    # _receive_task_handler does `_handle_evt_error(evt); return` on EVERY
    # error event — the reader task dies, the in-flight reply cuts off
    # mid-sentence and the session is deaf until the next connection death.
    # Observed live (2026-06-10): semantic_vad split one utterance into two
    # turns, the server's auto-created second response collided with the
    # first → conversation_already_has_active_response → the playing reply
    # stopped at 4.4 s and the session wedged. These codes are harmless
    # protocol races; the right move is to keep reading.
    BENIGN_ERROR_CODES = {
        # The server auto-created a response while one was still active
        # (VAD split a sentence into two turns). The active response keeps
        # streaming — nothing is broken.
        "conversation_already_has_active_response",
        # response.cancel landed after the response already finished (device
        # "stop" / the post-interrupt racing-response kill) — nothing to
        # cancel, nothing broken.
        "response_cancel_not_active",
        # input_audio_buffer.commit raced our input_audio_buffer.clear (device
        # "stop"): an empty commit is exactly the outcome we wanted.
        "input_audio_buffer_commit_empty",
    }

    async def _maybe_handle_evt_retrieve_conversation_item_error(self, evt):  # type: ignore[override]
        """Generic benign-error filter, hooked into pipecat's receive loop.

        pipecat's `_receive_task_handler` treats a True return from this
        method as "error handled — keep the receive loop alive"; every other
        error event kills the reader task (`_handle_evt_error` + `return`).
        It is the ONLY surviving path, so besides the original retrieve-item
        case (super()), we declare our benign protocol races handled here
        instead of letting them cut off live audio and wedge the session.
        """
        if await super()._maybe_handle_evt_retrieve_conversation_item_error(evt):
            return True
        code = getattr(getattr(evt, "error", None), "code", None)
        if code in self.BENIGN_ERROR_CODES:
            logger.warning(
                f"⚠️ benign realtime error ignored (session stays alive): {code}"
            )
            return True
        return False

    def register_function(self, function_name, handler, start_callback=None, *,
                          cancel_on_interruption: bool = True):  # type: ignore[override]
        """Force cancel_on_interruption=False for every tool registration.

        pipecat cancels in-flight function-call tasks on EVERY user-speech
        interruption — and semantic_vad fires one per utterance fragment, so
        merely continuing your own sentence kills the tool call your previous
        fragment started. By then the HTTP request to Home Assistant has
        usually already been SENT: the action executes, but its result never
        reaches the model, which then tells the user it failed (observed
        live: the lights turned ON while the assistant claimed they
        wouldn't). Our tools are all short-lived (HA service calls, one web
        search), so letting them finish and report the truth always beats
        killing them halfway. This single override covers every registration
        path (MCP tools via pipecat's MCPClient, web_search, disconnect).

        The handler is also wrapped to tick TURN_LIVENESS around its run, so
        the PhaseEmitter's thinking-watchdog knows a tool is in flight and a
        slow tool (web search: 10-20 s of pipeline silence) is never mistaken
        for a dead turn. All our handlers use the single-param
        FunctionCallParams signature, so the wrapper does too (pipecat
        inspects the signature to pick the calling convention).
        """
        async def liveness_tracked(params):
            TURN_LIVENESS.tool_started()
            try:
                return await handler(params)
            finally:
                TURN_LIVENESS.tool_finished()

        super().register_function(
            function_name, liveness_tracked, start_callback, cancel_on_interruption=False
        )

    async def _receive_task_handler(self):  # type: ignore[override]
        """Surface OpenAI reader death as an ErrorFrame, which ends the session.

        pipecat's receive loop can end without producing ANY ErrorFrame: a
        silent server-side close ends the `async for` normally, and a network
        drop raises ConnectionClosed, which the task manager merely LOGS
        ("unexpected exception"). Nothing would notice, and the session would
        sit deaf until the next user utterance hit the dead socket, losing it
        (observed live twice). Wrap the loop and report its end;
        SessionErrorWatch ends the session on it.
        """
        try:
            await super()._receive_task_handler()
        except asyncio.CancelledError:
            raise  # our own disconnect tearing the task down — not a death
        except Exception as e:
            await self.push_error(error_msg=f"realtime receive loop died: {e!r}")
            return
        # Loop ended without an exception: a clean server-side close, or the
        # fatal-error path (which already pushed its own ErrorFrame; the
        # session ends on whichever arrives first).
        await self.push_error(error_msg="realtime receive loop ended — connection closed")


def _b64(audio: bytes) -> str:
    return base64.b64encode(audio).decode("utf-8")
