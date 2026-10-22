"""The voice agent: the device's turns, carried out by an OpenAI Realtime session.

One OpenAI session per conversation. A session opens when the device wakes (or
mic audio arrives with none open), holding the mic audio until OpenAI has
configured it. What the session is configured with that comes from Home
Assistant, its tools and language, is read while it connects to OpenAI, so
each session has Home Assistant as it is then. Once the conversation on the
device has ended, and nothing has happened since for the idle timeout, the
session closes. A connection that dies ends the session; the next wake opens a
new one.

The conversation on the device runs from a wake (or mic audio) until it sends
`flush` (a follow-up window closed without speech), `interrupt`, or
disconnects, or until `idle` when no follow-up window will open. As a
fallback, once `idle` has been sent, it ends when its follow-up window would
have.

Within a session, OpenAI bills the whole conversation on every reply. Once it
passes `max_context_tokens`, OpenAI drops its oldest items down to
RETENTION_RATIO of that limit in one go: dropping a little on every turn would
change the start of the conversation each time, and with it lose the prompt
cache for everything after.

The conversation outlives its session in two cases: at a wake once the session
nears OpenAI's 60-minute cap, the add-on rolls over to a new session; and after
a lost connection, a wake within the idle timeout opens one. Either way the new
session starts with a transcript of the conversation so far.
"""
import asyncio
import base64
import enum
import json
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from openai import AsyncOpenAI

from app.audio import OPENAI_RATE, InputResampler, ReplyAudio, SessionRecording
from app.config import Settings
from app.conversation import Conversation, new_item_id
from app.device import DeviceServer
from app.phases import PhaseTracker, TurnLiveness
from app.realtime import RealtimeSession
from app.tls import server_context
from app.tools import Tools

logger = logging.getLogger(__name__)

_INPUT_BYTES_PER_S = OPENAI_RATE * 2


@dataclass
class SessionSetup:
    """What a session is configured with that is read anew for each one."""

    tools: Tools
    # The language hint for transcribing the user: an ISO 639-1 code, or "auto".
    transcription_language: str


class SessionState(enum.Enum):
    CLOSED = "closed"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"


class Agent:
    # How long a new session may take to connect and be configured.
    OPEN_TIMEOUT_S = 10.0
    # A wake rolls a session this old over to a new one, ahead of the 60-min cap.
    ROLLOVER_AGE_S = 50 * 60
    # Mic audio held while a session opens; older audio is dropped beyond this.
    BUFFER_LIMIT_S = 10.0
    IDLE_CHECK_S = 1.0
    # After a device "stop", any response OpenAI starts within this window is
    # cancelled: the stop word's audio may already have been committed as a turn.
    INTERRUPT_KILL_WINDOW_S = 1.5
    IDLE_UNSTICK_COOLDOWN_S = 2.0
    # Share of max_context_tokens kept when OpenAI truncates the conversation.
    RETENTION_RATIO = 0.5
    # A rough size of a token in English text, to bound a carried transcript.
    CHARS_PER_TOKEN = 4
    # The device holds a follow-up window for up to this long while its reply
    # audio drains, then opens it; this much more covers the network.
    PLAYBACK_DRAIN_FALLBACK_S = 3.0
    FOLLOW_UP_MARGIN_S = 1.0
    # OpenAI errors that are harmless protocol races, not a broken session.
    BENIGN_ERROR_CODES = {
        # Turn detection split an utterance; its second response collided with the first.
        "conversation_already_has_active_response",
        # A response.cancel landed after the response had finished.
        "response_cancel_not_active",
        # A commit raced an input_audio_buffer.clear: empty, as wanted.
        "input_audio_buffer_commit_empty",
    }

    def __init__(
        self, settings: Settings, client: AsyncOpenAI, session_setup: Callable[[], Awaitable[SessionSetup]]
    ):
        self.settings = settings
        self._client = client
        self._session_setup = session_setup
        # The open session's tools.
        self._tools = Tools()
        self.device = DeviceServer(
            settings.websocket_host,
            settings.websocket_port,
            secret=settings.device_secret,
            tls=server_context(settings.tls_dir),
            hello={
                "follow_up_ms": settings.follow_up_ms,
                "follow_up_open_delay_ms": settings.follow_up_open_delay_ms,
                "wake_open_delay_ms": settings.wake_open_delay_ms,
                "playback_prebuffer_ms": settings.playback_prebuffer_ms,
            },
            handler=self,
        )
        self._liveness = TurnLiveness()
        self.phases = PhaseTracker(
            send_phase=self._send_phase,
            liveness=self._liveness,
            idle_debounce_s=settings.phase_idle_debounce_s,
            on_idle=self._device_went_idle,
            on_dangling=self._arm_kill_window,
            on_real_speech=self._disarm_kills,
        )
        self._reply = ReplyAudio(self._play_chunk, self._reply_started, self._reply_stopped)
        self._resampler = InputResampler(settings.device_input_rate)
        self._conversation = Conversation()

        self._session: Optional[RealtimeSession] = None
        self._recording: Optional[SessionRecording] = None
        self._state = SessionState.CLOSED
        self._session_ready = asyncio.Event()
        self._opened_at = 0.0
        self._last_activity = 0.0
        self._buffered_audio: list[bytes] = []
        self._buffered_bytes = 0
        self._wake_while_closing = False
        # A wake that no speech has followed yet: the user wants a session now.
        self._awaiting_speech = False
        # Until when the conversation on the device lasts: forever while it
        # listens or a turn runs; a moment that has passed once it has ended.
        self._engaged_until = 0.0
        # A stop since the wake: the next `idle` opens no follow-up window.
        self._stopped = False
        # Until when a conversation whose session closed early may continue in the next.
        self._carry_until = 0.0
        self._open_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()

        # The response being generated, and those cancelled whose audio may still arrive.
        self._response_id: Optional[str] = None
        self._cancelled: set[str] = set()
        self._reply_text = ""
        # Tool calls of the response being generated, as (call_id, task).
        self._tool_calls: list[tuple[str, asyncio.Task]] = []
        self._kill_until = 0.0
        self._kill_next = False
        self._last_idle_unstick = 0.0

    # --- running ---------------------------------------------------------------

    async def run(self) -> None:
        """Serve the device until cancelled."""
        self._reply.start()
        try:
            await self.device.serve()
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        for task in (self._open_task, self._idle_task, *self._tasks):
            if task is not None:
                task.cancel()
        if self._session is not None:
            await self._session.close()
        self._stop_recording()
        self.phases.stop()
        await self._reply.stop()

    def _spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _note_activity(self) -> None:
        """Something happened in the conversation; the idle timeout restarts."""
        self._last_activity = time.monotonic()

    # --- the device ----------------------------------------------------------------

    async def device_connected(self) -> None:
        pass

    async def device_disconnected(self) -> None:
        self._end_engagement()

    async def device_message(self, message_type: str) -> None:
        if message_type == "start":
            # Once per connection: a reconnect mid-utterance may have left half
            # a sentence in the input buffer.
            logger.info("🎬 device (re)connected → input audio cleared")
            await self._clear_input_audio()
        elif message_type == "wake":
            logger.info("👋 device wake")
            self.phases.note_wake()
            self._kill_next = False
            self._on_wake()
        elif message_type == "flush":
            # A follow-up window closed without speech: drop the partial
            # utterance, so a later wake can't complete it into a stale answer.
            logger.info("🧽 device flush → input audio cleared")
            self._end_engagement()
            self.phases.note_wake()
            await self._clear_input_audio()
        elif message_type == "interrupt":
            await self._interrupt()

    async def device_audio(self, pcm: bytes) -> None:
        self._note_activity()
        self._engaged_until = float("inf")
        audio = self._resampler.resample(pcm)
        if not audio:
            return
        if self._state is SessionState.OPEN:
            await self._send_mic_audio(audio)
            return
        # Opening, closing or closed: hold it for the session that opens next.
        self._buffered_audio.append(audio)
        self._buffered_bytes += len(audio)
        limit = int(self.BUFFER_LIMIT_S * _INPUT_BYTES_PER_S)
        while self._buffered_bytes > limit and len(self._buffered_audio) > 1:
            self._buffered_bytes -= len(self._buffered_audio.pop(0))
        if self._state is SessionState.CLOSED:
            self._begin_open("mic audio")

    async def _interrupt(self) -> None:
        """The device's "stop": it has already silenced itself and closed its mic.

        The add-on drops the reply audio it still holds, the stop word's audio
        in OpenAI's input buffer, and the response being generated. If OpenAI
        committed the stop word as a turn before the clear landed, it creates a
        response to it moments later; so the next response to appear, whenever
        it does, and any within INTERRUPT_KILL_WINDOW_S, are cancelled too. Real
        speech disarms both; a wake disarms the first.
        """
        logger.info("🛑 device interrupt")
        self._stopped = True
        self._end_engagement()
        self._reply.flush()
        self._kill_until = time.monotonic() + self.INTERRUPT_KILL_WINDOW_S
        self._kill_next = True
        await self._clear_input_audio()
        if self._response_id is not None:
            logger.info("🛑 response.cancel (a response was active)")
            await self._cancel_response()

    async def _clear_input_audio(self) -> None:
        """Drop the user's uncommitted audio: OpenAI's input buffer, or ours."""
        self._resampler.reset()
        if self._state is SessionState.OPEN:
            await self._session.send({"type": "input_audio_buffer.clear"})
        else:
            self._drop_buffered_audio()

    def _drop_buffered_audio(self) -> None:
        self._buffered_audio = []
        self._buffered_bytes = 0

    def _send_phase(self, value: str) -> None:
        logger.info(f"➡️ phase '{value}' to {'the device' if self.device.connected else 'no device'}")
        self.device.send_json({"type": "phase", "value": value})

    def _end_engagement(self) -> None:
        """The conversation on the device has ended; the idle timeout counts from now."""
        self._engaged_until = min(self._engaged_until, time.monotonic())

    def _device_went_idle(self) -> None:
        s = self.settings
        if self._stopped or not s.follow_up_ms:
            self._end_engagement()
        else:
            follow_up_s = s.follow_up_open_delay_ms / 1000 + s.follow_up_ms / 1000
            self._engaged_until = (
                time.monotonic() + self.PLAYBACK_DRAIN_FALLBACK_S + follow_up_s + self.FOLLOW_UP_MARGIN_S
            )

    def _arm_kill_window(self) -> None:
        self._kill_until = time.monotonic() + self.INTERRUPT_KILL_WINDOW_S

    def _disarm_kills(self) -> None:
        self._kill_until = 0.0
        self._kill_next = False

    # --- reply audio -----------------------------------------------------------------

    def _play_chunk(self, chunk: bytes) -> None:
        self._note_activity()
        self.device.send_audio(chunk)
        if self._recording is not None:
            self._recording.reply(chunk)

    def _reply_started(self) -> None:
        self.phases.bot_started()

    def _reply_stopped(self) -> None:
        self._note_activity()
        self.phases.bot_stopped()

    # --- opening a session --------------------------------------------------------------

    def _on_wake(self) -> None:
        """The user is about to speak: open a session if none is open."""
        self._note_activity()
        self._awaiting_speech = True
        self._engaged_until = float("inf")
        self._stopped = False
        if self._state is SessionState.CLOSED:
            self._begin_open("wake")
        elif self._state is SessionState.CLOSING:
            self._wake_while_closing = True
        elif self._state is SessionState.OPEN and self._should_roll_over():
            self._state = SessionState.CLOSING
            self._spawn(self._roll_over(), "roll_over")

    def _busy(self) -> bool:
        return self._response_id is not None or self._liveness.in_flight > 0 or self._reply.playing

    def _should_roll_over(self) -> bool:
        return not self._busy() and time.monotonic() - self._opened_at >= self.ROLLOVER_AGE_S

    async def _roll_over(self) -> None:
        await self._close("rolling over before the 60-minute cap", keep_conversation=True)
        if self._state is SessionState.CLOSED:
            self._begin_open("rollover")

    def _begin_open(self, why: str) -> None:
        self._state = SessionState.OPENING
        self._open_task = asyncio.create_task(self._open(why), name="open_session")

    async def _open(self, why: str) -> None:
        started = time.monotonic()
        logger.info(f"🔌 opening an OpenAI session ({why})")
        self._session_ready.clear()
        # Decided now: the new session's events can arrive before it is open.
        carried = self._take_carried_conversation()
        session = RealtimeSession(self._client, self.settings.model, self._on_event, self._on_closed)
        self._session = session
        setup_task = asyncio.create_task(self._session_setup(), name="session_setup")
        try:
            try:
                await asyncio.wait_for(session.connect(), self.OPEN_TIMEOUT_S)
            except Exception as e:
                await self._fail_open(f"could not connect to OpenAI: {e!r}")
                return
            try:
                setup = await setup_task
            except Exception as e:
                await session.close()
                await self._fail_open(f"could not set up the session: {e!r}")
                return
        finally:
            setup_task.cancel()
        self._tools = setup.tools
        await session.send({"type": "session.update", "session": self._session_config(setup)})
        try:
            await asyncio.wait_for(self._session_ready.wait(), self.OPEN_TIMEOUT_S)
        except asyncio.TimeoutError:
            await session.close()
            await self._fail_open(f"OpenAI did not configure the session within {self.OPEN_TIMEOUT_S:.0f} s")
            return

        self._state = SessionState.OPEN
        self._opened_at = time.monotonic()
        if self.settings.enable_recording:
            try:
                self._recording = SessionRecording(self.settings.recording_dir)
            except OSError as e:
                logger.warning(f"⚠️ Could not start recording: {e!r}")
        if carried:
            for item in carried:
                await session.send({"type": "conversation.item.create", "item": item})
            logger.info("🧵 carried the conversation so far into the new session")
        buffered_ms = self._buffered_bytes * 1000 // _INPUT_BYTES_PER_S
        for audio in self._buffered_audio:
            await self._send_mic_audio(audio)
        self._drop_buffered_audio()
        self._note_activity()
        self._idle_task = asyncio.create_task(self._close_when_idle(), name="close_when_idle")
        logger.info(
            f"✅ OpenAI session open in {(self._opened_at - started) * 1000:.0f} ms "
            f"({buffered_ms} ms of audio held meanwhile)"
        )

    async def _send_mic_audio(self, audio: bytes) -> None:
        await self._session.send({"type": "input_audio_buffer.append", "audio": _b64(audio)})
        if self._recording is not None:
            self._recording.mic(audio)

    def _stop_recording(self) -> None:
        if self._recording is not None:
            self._recording.close()
            self._recording = None

    def _session_config(self, setup: SessionSetup) -> dict:
        s = self.settings
        if s.turn_detection_type == "semantic_vad":
            turn_detection = {
                "type": "semantic_vad",
                "eagerness": s.vad_eagerness,
                "create_response": s.create_response,
                "interrupt_response": s.interrupt_response,
            }
        else:
            turn_detection = {
                "type": "server_vad",
                "threshold": s.vad_threshold,
                "prefix_padding_ms": s.vad_prefix_padding_ms,
                "silence_duration_ms": s.vad_silence_duration_ms,
            }
        audio_input: dict = {"turn_detection": turn_detection}
        audio_input["transcription"] = {"model": s.transcription_model}
        if setup.transcription_language != "auto":
            audio_input["transcription"]["language"] = setup.transcription_language
        if s.noise_reduction:
            audio_input["noise_reduction"] = {"type": s.noise_reduction}
        config = {
            "type": "realtime",
            "instructions": s.instructions,
            "audio": {"input": audio_input, "output": {"voice": s.voice, "speed": s.speed}},
            "tools": setup.tools.definitions,
        }
        if s.max_output_tokens is not None:
            config["max_output_tokens"] = s.max_output_tokens
        truncation: dict = {"type": "retention_ratio", "retention_ratio": self.RETENTION_RATIO}
        if s.max_context_tokens:
            truncation["token_limits"] = {"post_instructions": s.max_context_tokens}
        config["truncation"] = truncation
        return config

    def _take_carried_conversation(self) -> list[dict]:
        """The items that start a new session with a transcript of the conversation so far."""
        if not self._conversation or time.monotonic() > self._carry_until:
            self._conversation.clear()
            return []  # nothing to carry, or it timed out while no session was open
        # No bigger than what OpenAI keeps after truncating.
        limit = self.settings.max_context_tokens
        max_chars = int(limit * self.RETENTION_RATIO * self.CHARS_PER_TOKEN) if limit else None
        return self._conversation.carry_items(max_chars)

    async def _fail_open(self, reason: str) -> None:
        if self._state is not SessionState.OPENING:
            return  # already failed by the other path
        logger.error(f"❌ OpenAI session failed to open: {reason}")
        self._state = SessionState.CLOSED
        self._session = None
        self._drop_buffered_audio()
        # The device plays its error sound and goes idle on `error`.
        self.device.send_json({"type": "error", "message": reason[:200]})
        self.phases.force_idle(f"session failed to open: {reason}")

    # --- closing a session ---------------------------------------------------------------

    async def _close_when_idle(self) -> None:
        while self._state is SessionState.OPEN:
            await asyncio.sleep(self.IDLE_CHECK_S)
            if self._state is not SessionState.OPEN:
                return
            now = time.monotonic()
            if self._busy() or now < self._engaged_until:
                continue
            quiet = now - max(self._last_activity, self._liveness.last_activity, self._engaged_until)
            if quiet >= self.settings.session_idle_timeout_s:
                self._idle_task = None
                await self._close(f"idle for {quiet:.0f} s")
                return

    async def _close(self, reason: str, keep_conversation: bool = False) -> None:
        """Close the session. The conversation ends with it unless kept for the next one."""
        self._state = SessionState.CLOSING
        self._drop_buffered_audio()
        current = asyncio.current_task()
        for task in (self._open_task, self._idle_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        self._open_task = self._idle_task = None
        session, self._session = self._session, None
        if session is not None:
            await session.close()
        self._stop_recording()
        self._response_id = None
        self._cancelled = set()
        self._tool_calls = []
        if keep_conversation:
            self._carry_until = self._last_activity + self.settings.session_idle_timeout_s
        else:
            self._conversation.clear()
        self._state = SessionState.CLOSED
        age = time.monotonic() - self._opened_at if self._opened_at else 0.0
        logger.info(f"🔌 OpenAI session closed after {age:.0f} s ({reason})")
        # The user woke the device or spoke while it closed: they want a session.
        if self._wake_while_closing or self._buffered_audio:
            self._wake_while_closing = False
            self._begin_open("woken while closing")

    async def _on_closed(self, session: RealtimeSession, reason: str) -> None:
        if session is self._session:
            await self._session_lost(reason)

    async def _session_lost(self, reason: str) -> None:
        """OpenAI's connection died, or the session ended. The next wake opens another."""
        if self._state is SessionState.OPENING:
            if self._open_task is not None:
                self._open_task.cancel()
            await self._session.close()
            await self._fail_open(reason)
        elif self._state is SessionState.OPEN:
            # Lost just after a wake (perhaps before it was noticed, so the
            # wake found it still open): open the next one now.
            if self._awaiting_speech:
                self._wake_while_closing = True
            await self._close(f"lost: {reason[:90]}", keep_conversation=True)
            # Mid-turn, the turn is lost with the session: say so. Between
            # turns there's nothing to tell the device.
            if self.phases.current not in (None, "idle"):
                self.device.send_json({"type": "error", "message": "connection to OpenAI lost"})
                self.phases.force_idle(f"session lost: {reason}")

    # --- OpenAI's events ------------------------------------------------------------------

    async def _on_event(self, session: RealtimeSession, event: dict) -> None:
        if session is not self._session:
            return  # from a session already replaced
        kind = event.get("type", "")
        if kind != "response.output_audio.delta":
            logger.debug(f"⬅️ {kind}")
        handler = getattr(self, "_on_" + kind.replace(".", "_"), None)
        if handler is not None:
            await handler(event)

    async def _on_session_updated(self, event: dict) -> None:
        self._session_ready.set()

    async def _on_input_audio_buffer_speech_started(self, event: dict) -> None:
        self._awaiting_speech = False
        self._reply.flush()
        self.phases.user_started()

    async def _on_input_audio_buffer_speech_stopped(self, event: dict) -> None:
        self.phases.user_stopped()

    async def _on_conversation_item_added(self, event: dict) -> None:
        item = event.get("item") or {}
        self._conversation.item_added(item)
        if item.get("role") == "assistant":
            await self._kill_racing_response()

    async def _kill_racing_response(self) -> None:
        within_window = time.monotonic() < self._kill_until
        if not within_window and not self._kill_next:
            return
        self._kill_next = False
        logger.info("🛑 a response raced in right after a device interrupt → response.cancel")
        await self._cancel_response()

    async def _cancel_response(self) -> None:
        """Cancel the response being generated; whatever audio of it still arrives is dropped."""
        if self._response_id is not None:
            self._cancelled.add(self._response_id)
        await self._session.send({"type": "response.cancel"})

    async def _on_conversation_item_input_audio_transcription_completed(self, event: dict) -> None:
        text = (event.get("transcript") or "").strip()
        self._conversation.set_text(event.get("item_id", ""), text)
        if text:
            logger.info(f"🗣️ user: {text}")

    async def _on_response_created(self, event: dict) -> None:
        self._response_id = (event.get("response") or {}).get("id", "")

    async def _on_response_output_audio_delta(self, event: dict) -> None:
        if event.get("response_id") not in self._cancelled:
            self._reply.feed(base64.b64decode(event.get("delta", "")))

    async def _on_response_output_audio_done(self, event: dict) -> None:
        if event.get("response_id") not in self._cancelled:
            self._reply.end_of_reply()

    async def _on_response_output_audio_transcript_delta(self, event: dict) -> None:
        delta = event.get("delta", "")
        self._conversation.append_text(event.get("item_id", ""), delta)
        self._reply_text += delta

    async def _on_response_function_call_arguments_done(self, event: dict) -> None:
        arguments = event.get("arguments") or ""
        self._conversation.set_text(event.get("item_id", ""), arguments)
        call_id = event.get("call_id", "")
        name = self._call_name(event)
        # One at a time, in the model's order: two calls on the same device
        # must not finish the wrong way round.
        previous = self._tool_calls[-1][1] if self._tool_calls else None
        task = self._spawn(self._run_tool(name, arguments, after=previous), f"tool_{name}")
        self._tool_calls.append((call_id, task))

    def _call_name(self, event: dict) -> str:
        for item in self._conversation.items:
            if item.item_id == event.get("item_id"):
                return item.name
        return event.get("name", "")

    async def _run_tool(self, name: str, arguments: str, after: Optional[asyncio.Task]) -> str:
        self._liveness.tool_started()
        try:
            if after is not None:
                await asyncio.wait({after})
            try:
                args = json.loads(arguments) if arguments else {}
            except ValueError:
                args = {}
            return await self._tools.call(name, args)
        except Exception as e:
            logger.exception(f"❌ tool {name} failed")
            return f"Error calling tool {name}: {e}"
        finally:
            self._liveness.tool_finished()

    async def _on_response_done(self, event: dict) -> None:
        response = event.get("response") or {}
        self._response_id = None
        self._cancelled.discard(response.get("id"))
        self._reply.end_of_reply()
        if self._reply_text.strip():
            logger.info(f"🤖 assistant: {self._reply_text.strip()}")
        self._reply_text = ""
        if response.get("status") == "failed":
            self._unstick(f"response failed: {response.get('status_details')}")
        if self._tool_calls:
            calls, self._tool_calls = self._tool_calls, []
            self._spawn(self._answer_tool_calls(self._session, calls), "tool_results")

    async def _answer_tool_calls(self, session: RealtimeSession, calls: list[tuple[str, asyncio.Task]]) -> None:
        """Send each call's result, then ask the model to answer with them."""
        for call_id, task in calls:
            result = await task
            if session is not self._session:
                return  # the session the calls belong to has closed
            await session.send({
                "type": "conversation.item.create",
                "item": {
                    "id": new_item_id(),
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                },
            })
        await session.send({"type": "response.create"})

    def _unstick(self, reason: str) -> None:
        """A turn ended without a reply: put the device back to idle, so the user can try again."""
        now = time.monotonic()
        if now - self._last_idle_unstick >= self.IDLE_UNSTICK_COOLDOWN_S:
            self._last_idle_unstick = now
            self.phases.force_idle(f"turn ended on error: {reason[:60]}")

    async def _on_error(self, event: dict) -> None:
        error = event.get("error") or {}
        code = error.get("code")
        if code in self.BENIGN_ERROR_CODES:
            logger.warning(f"⚠️ benign realtime error ignored: {code}")
            return
        logger.error(f"❌ OpenAI error: {error}")
        if code == "session_expired":
            await self._session_lost(f"{code}: {error.get('message', '')}")
        elif self.phases.current == "thinking":
            # Errors leave the session usable, but this turn may get no reply.
            self._unstick(f"{code}: {error.get('message', '')}")


def _b64(audio: bytes) -> str:
    return base64.b64encode(audio).decode("ascii")
