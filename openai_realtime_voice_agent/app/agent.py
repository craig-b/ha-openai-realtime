"""The voice agent: the device's conversations, carried out by OpenAI sessions.

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

The conversation is the session's: OpenAI holds it, and it ends with the
session. At a wake once a session that has an age limit nears it, the add-on
rolls over to a new one, so a turn isn't cut off by the limit; the new session
starts afresh, as one after a lost connection does.

The session itself, the connection to OpenAI and what its events mean, is the
engine's (app/session.py); the agent owns the rest.
"""
import asyncio
import enum
import json
import logging
import time
from typing import Awaitable, Callable, Optional

from openai import AsyncOpenAI

from app.audio import OPENAI_RATE, InputResampler, ReplyAudio, SessionRecording
from app.config import Settings
from app.device import DeviceServer
from app.phases import PhaseTracker, TurnLiveness
from app.live import LiveSession
from app.realtime import RealtimeSession
from app.session import Session, SessionFailed, SessionSetup
from app.tls import server_context
from app.tools import Tools

logger = logging.getLogger(__name__)

_INPUT_BYTES_PER_S = OPENAI_RATE * 2


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
    IDLE_UNSTICK_COOLDOWN_S = 2.0
    # The device holds a follow-up window for up to this long while its reply
    # audio drains, then opens it; this much more covers the network.
    PLAYBACK_DRAIN_FALLBACK_S = 3.0
    FOLLOW_UP_MARGIN_S = 1.0
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
            on_dangling=self._dangling_turn,
            on_real_speech=self._real_speech,
        )
        self._reply = ReplyAudio(self._play_chunk, self._reply_started, self._reply_stopped)
        self._resampler = InputResampler(settings.device_input_rate)

        self._session: Optional[Session] = None
        self._recording: Optional[SessionRecording] = None
        self._state = SessionState.CLOSED
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
        self._open_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()

        # Tool calls of the response being generated, as (call_id, task).
        self._tool_calls: list[tuple[str, asyncio.Task]] = []
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
            if self._session is not None:
                self._session.woke()
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

        The add-on drops the reply audio it still holds, and the session drops
        the stop word's audio and the reply being made.
        """
        logger.info("🛑 device interrupt")
        self._stopped = True
        self._end_engagement()
        self._reply.flush()
        self._resampler.reset()
        if self._state is SessionState.OPEN:
            await self._session.interrupt()
        else:
            self._drop_buffered_audio()

    async def _clear_input_audio(self) -> None:
        """Drop the user's uncommitted audio: the session's, or ours."""
        self._resampler.reset()
        if self._state is SessionState.OPEN:
            await self._session.clear_input()
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

    def end_conversation(self) -> None:
        """The model ended the conversation: the device opens no follow-up window after this reply."""
        self._stopped = True
        self.device.send_json({"type": "end"})

    def _dangling_turn(self) -> None:
        if self._session is not None:
            self._session.dangling_turn()

    def _real_speech(self) -> None:
        if self._session is not None:
            self._session.real_speech()

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
        elif self._state is SessionState.OPEN and self._session.ROLLS_OVER and self._should_roll_over():
            self._state = SessionState.CLOSING
            self._spawn(self._roll_over(), "roll_over")

    def _busy(self) -> bool:
        busy = self._session is not None and self._session.busy
        return busy or self._liveness.in_flight > 0 or self._reply.playing

    def _should_roll_over(self) -> bool:
        return not self._busy() and time.monotonic() - self._opened_at >= self.ROLLOVER_AGE_S

    async def _roll_over(self) -> None:
        await self._close("rolling over before the 60-minute cap")
        if self._state is SessionState.CLOSED:
            self._begin_open("rollover")

    def _begin_open(self, why: str) -> None:
        self._state = SessionState.OPENING
        self._open_task = asyncio.create_task(self._open(why), name="open_session")

    async def _open(self, why: str) -> None:
        started = time.monotonic()
        logger.info(f"🔌 opening an OpenAI session ({why})")
        engine = LiveSession if self.settings.engine == "live" else RealtimeSession
        session = engine(self.settings, self._client, self)
        self._session = session
        setup_task = asyncio.create_task(self._session_setup(), name="session_setup")
        try:
            try:
                await session.connect(self.OPEN_TIMEOUT_S)
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
        try:
            await session.configure(setup, self.OPEN_TIMEOUT_S)
        except SessionFailed as e:
            await session.close()
            await self._fail_open(str(e))
            return

        self._state = SessionState.OPEN
        self._opened_at = time.monotonic()
        if self.settings.enable_recording:
            try:
                self._recording = SessionRecording(self.settings.recording_dir)
            except OSError as e:
                logger.warning(f"⚠️ Could not start recording: {e!r}")
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
        await self._session.send_audio(audio)
        if self._recording is not None:
            self._recording.mic(audio)

    def _stop_recording(self) -> None:
        if self._recording is not None:
            self._recording.close()
            self._recording = None

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

    async def _close(self, reason: str) -> None:
        """Close the session, and with it the conversation."""
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
        self._tool_calls = []
        self._state = SessionState.CLOSED
        age = time.monotonic() - self._opened_at if self._opened_at else 0.0
        logger.info(f"🔌 OpenAI session closed after {age:.0f} s ({reason})")
        # The user woke the device or spoke while it closed: they want a session.
        if self._wake_while_closing or self._buffered_audio:
            self._wake_while_closing = False
            self._begin_open("woken while closing")

    async def session_lost(self, session: Session, reason: str) -> None:
        """OpenAI's connection died, or the session ended. The next wake opens another."""
        if session is not self._session:
            return  # one already replaced
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
            await self._close(f"lost: {reason[:90]}")
            # Mid-turn, the turn is lost with the session: say so. Between
            # turns there's nothing to tell the device.
            if self.phases.current not in (None, "idle"):
                self.device.send_json({"type": "error", "message": "connection to OpenAI lost"})
                self.phases.force_idle(f"session lost: {reason}")

    # --- the session's events ---------------------------------------------------------------

    def user_started(self) -> None:
        self._awaiting_speech = False
        self._reply.flush()
        self.phases.user_started()

    def user_stopped(self) -> None:
        self.phases.user_stopped()

    def reply_audio(self, audio: bytes) -> None:
        self._reply.feed(audio)

    def reply_ended(self) -> None:
        self._reply.end_of_reply()

    def tool_call(self, call_id: str, name: str, arguments: str) -> None:
        # One at a time, in the model's order: two calls on the same device
        # must not finish the wrong way round.
        previous = self._tool_calls[-1][1] if self._tool_calls else None
        task = self._spawn(self._run_tool(name, arguments, after=previous), f"tool_{name}")
        self._tool_calls.append((call_id, task))

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

    def response_done(self, session: Session) -> None:
        if self._tool_calls:
            calls, self._tool_calls = self._tool_calls, []
            self._spawn(self._answer_tool_calls(session, calls), "tool_results")

    async def _answer_tool_calls(self, session: Session, calls: list[tuple[str, asyncio.Task]]) -> None:
        """Send the calls' results, once all are in, for the model to answer with."""
        results = [(call_id, await task) for call_id, task in calls]
        if session is self._session:  # not if the session the calls belong to has closed
            await session.send_tool_results(results)

    def turn_failed(self, reason: str) -> None:
        """A turn ended without a reply: put the device back to idle, so the user can try again."""
        now = time.monotonic()
        if now - self._last_idle_unstick >= self.IDLE_UNSTICK_COOLDOWN_S:
            self._last_idle_unstick = now
            self.phases.force_idle(f"turn ended on error: {reason[:60]}")

    def error(self, reason: str) -> None:
        # Errors leave the session usable, but a turn waiting for a reply may get none.
        if self.phases.current == "thinking":
            self.turn_failed(reason)

