"""The `phase` messages that drive the device (docs/protocol.md).

The Voice PE firmware drives its LED ring, mic gate and no-speech watchdog
from `{"type":"phase","value":...}`. They follow the turn as OpenAI and the
reply playback report it:

    OpenAI hears the user start speaking  -> listening
    OpenAI hears the user stop            -> thinking
    reply audio starts playing            -> replying
    reply audio stops, and stays stopped  -> idle

Idle debounce: OpenAI's reply audio comes in segments (per sentence, around
tool calls), so playback stops several times within one reply. Going idle at
each would flap the LED and, since the firmware only listens for "stop" while
replying, briefly lose the stop word. So idle waits `idle_debounce_s` for more
speech first.

Thinking has no natural end when a turn dies without a reply (a rate limit,
a failed response): the device would sit in `thinking` with its mic open.
`force_idle` ends such a turn and suppresses `thinking` until real speech
follows, so an end-of-turn event still in flight can't re-stick it. And a
watchdog forces idle when `thinking` sees no activity for THINKING_TIMEOUT_S,
except while a tool runs: a long web search gets all the time it needs, and
every tool is bounded by its own client timeout.

Two late end-of-turn events are not new turns:
- During a reply (the mic is gated then), a stop is the tail of the question
  being answered: `replying` stays.
- After a wake or a flush with no speech since, a stop is a segment from
  before the wake closing late: its `thinking` is suppressed, and
  `on_dangling` lets the add-on cancel the response OpenAI creates for it.
"""
import asyncio
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class TurnLiveness:
    """Whether the model is still doing something, for the watchdog and the idle timeout."""

    def __init__(self) -> None:
        self.in_flight = 0  # tools running
        self.last_activity = 0.0

    def tool_started(self) -> None:
        self.in_flight += 1
        self.last_activity = time.monotonic()

    def tool_finished(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        self.last_activity = time.monotonic()


class PhaseTracker:
    THINKING_TIMEOUT_S = 15.0
    WATCHDOG_POLL_S = 1.0
    # How often to log that the watchdog is waiting on a running tool.
    INFLIGHT_LOG_EVERY_S = 30.0

    def __init__(
        self,
        send_phase: Callable[[str], None],
        liveness: TurnLiveness,
        idle_debounce_s: float,
        on_dangling: Callable[[], None],
        on_real_speech: Callable[[], None],
    ):
        self._send_phase = send_phase
        self._liveness = liveness
        self._idle_debounce_s = max(0.0, idle_debounce_s)
        self._on_dangling = on_dangling
        self._on_real_speech = on_real_speech
        self._idle_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._current: Optional[str] = None
        self._suppress_thinking = False
        # Defaults True so nothing is suppressed before the first wake.
        self._speech_since_wake = True

    @property
    def current(self) -> Optional[str]:
        """The phase last sent to the device, or None before the first."""
        return self._current

    def note_wake(self) -> None:
        """The device woke, or a follow-up window closed without speech."""
        self._speech_since_wake = False

    def user_started(self) -> None:
        self._suppress_thinking = False
        self._speech_since_wake = True
        self._on_real_speech()
        self._cancel_pending_idle()
        self._cancel_watchdog()
        self._emit("listening")

    def user_stopped(self) -> None:
        self._cancel_pending_idle()
        if self._current == "replying":
            logger.info("📞 'thinking' suppressed: replying (stale end of turn)")
        elif not self._speech_since_wake:
            logger.info("📞 'thinking' suppressed: no speech since the wake (dangling end of turn)")
            self._on_dangling()
        elif self._suppress_thinking:
            logger.info("📞 'thinking' suppressed: the turn was already declared dead")
        else:
            self._emit("thinking")
            self._arm_watchdog()

    def bot_started(self) -> None:
        self._suppress_thinking = False
        self._cancel_pending_idle()
        self._cancel_watchdog()
        self._emit("replying")

    def bot_stopped(self) -> None:
        self._cancel_pending_idle()
        self._idle_task = asyncio.create_task(self._idle_after_debounce())

    def force_idle(self, reason: str = "") -> None:
        """Declare the current turn dead and put the device in idle."""
        self._cancel_pending_idle()
        self._cancel_watchdog()
        self._suppress_thinking = True
        if reason:
            logger.warning(f"📞 forcing phase idle ({reason[:90]})")
        self._emit("idle")

    def stop(self) -> None:
        self._cancel_pending_idle()
        self._cancel_watchdog()

    def _emit(self, value: str) -> None:
        # `listening` is always sent: the device lifts its post-stop audio
        # drop only on `listening`, and a stop can set that drop again after
        # the last one without any other phase in between.
        if value == self._current and value != "listening":
            return
        self._current = value
        logger.info(f"📞 phase -> {value}")
        self._send_phase(value)

    def _cancel_pending_idle(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _cancel_watchdog(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        self._watchdog_task = asyncio.create_task(self._thinking_watchdog())

    async def _idle_after_debounce(self) -> None:
        await asyncio.sleep(self._idle_debounce_s)
        self._idle_task = None
        # A tool can still be running when a reply's filler ends: the turn
        # isn't over. Show `thinking` until its result's reply plays.
        if self._liveness.in_flight > 0:
            self._emit("thinking")
            self._arm_watchdog()
            return
        self._emit("idle")

    async def _thinking_watchdog(self) -> None:
        armed_at = time.monotonic()
        last_inflight_log = 0.0
        while True:
            await asyncio.sleep(self.WATCHDOG_POLL_S)
            if self._current != "thinking":
                return
            now = time.monotonic()
            last = max(armed_at, self._liveness.last_activity)
            if self._liveness.in_flight > 0:
                if now - last_inflight_log >= self.INFLIGHT_LOG_EVERY_S:
                    last_inflight_log = now
                    logger.info(
                        f"⏳ thinking-watchdog: {self._liveness.in_flight} tool(s) "
                        f"running for {now - last:.0f}s, waiting (no cap)"
                    )
                continue
            if now - last < self.THINKING_TIMEOUT_S:
                continue
            self._watchdog_task = None
            self.force_idle(f"thinking-watchdog: no model activity for {now - last:.0f}s")
            return
