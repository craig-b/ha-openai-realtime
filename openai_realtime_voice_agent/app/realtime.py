"""The Realtime engine: one OpenAI Realtime session, gpt-realtime-*.

One model listens, thinks and speaks, in turns: OpenAI's turn detection hears
the user start and stop, and each reply is a response with a start and an end.

Within a session, OpenAI bills the whole conversation on every reply. Once it
passes `max_context_tokens`, OpenAI drops its oldest items down to
RETENTION_RATIO of that limit in one go: dropping a little on every turn would
change the start of the conversation each time, and with it lose the prompt
cache for everything after.

A session lasts at most 60 minutes, so the agent rolls it over to a new one
before then (ROLLS_OVER).
"""
import asyncio
import base64
import json
import logging
import re
import time
import uuid
from typing import Optional

from openai import AsyncOpenAI
from openai.resources.realtime.realtime import AsyncRealtimeConnection
from websockets.exceptions import ConnectionClosed

from app.config import Settings
from app import prompts
from app.session import SessionEvents, SessionFailed, SessionSetup

logger = logging.getLogger(__name__)


class RealtimeSession:
    """A Realtime connection: the session's settings, the user's audio in, OpenAI's events out.

    The SDK's own reconnect stays off: a new connection is a new session,
    which the agent opens itself.
    """

    ROLLS_OVER = True
    # Transcribes the user for the log (log_what_you_say).
    TRANSCRIPTION_MODEL = "gpt-live-transcribe"
    # Share of max_context_tokens kept when OpenAI truncates the conversation.
    RETENTION_RATIO = 0.5
    # After a device "stop", any response OpenAI starts within this window is
    # cancelled: the stop word's audio may already have been committed as a turn.
    INTERRUPT_KILL_WINDOW_S = 1.5
    # OpenAI errors that are harmless protocol races, not a broken session.
    BENIGN_ERROR_CODES = {
        # Turn detection split an utterance; its second response collided with the first.
        "conversation_already_has_active_response",
        # A response.cancel landed after the response had finished.
        "response_cancel_not_active",
        # A commit raced an input_audio_buffer.clear: empty, as wanted.
        "input_audio_buffer_commit_empty",
    }

    def __init__(self, settings: Settings, client: AsyncOpenAI, events: SessionEvents):
        self.settings = settings
        self._client = client
        self._events = events
        self._connection: Optional[AsyncRealtimeConnection] = None
        self._reader: Optional[asyncio.Task] = None
        self._closing = False
        self._configured = asyncio.Event()
        # Why OpenAI refused the session's settings, if it did while it opened.
        self._refused: Optional[str] = None
        # The response being generated, and those cancelled whose audio may still arrive.
        self._response_id: Optional[str] = None
        self._cancelled: set[str] = set()
        self._reply_text = ""
        # The names of the tool calls in the session, by item id: a call's
        # arguments arrive without its name.
        self._call_names: dict[str, str] = {}
        self._kill_until = 0.0
        self._kill_next = False

    @property
    def busy(self) -> bool:
        """Whether a response is being generated."""
        return self._response_id is not None

    # --- opening and closing ---------------------------------------------------------

    async def connect(self, timeout: float) -> None:
        self._connection = await asyncio.wait_for(
            self._client.realtime.connect(model=self.settings.model).enter(), timeout
        )
        self._reader = asyncio.create_task(self._read())

    async def configure(self, setup: SessionSetup, timeout: float) -> None:
        """Send the session's settings; raises SessionFailed if OpenAI doesn't take them."""
        await self._send({"type": "session.update", "session": self._session_config(setup)})
        try:
            await asyncio.wait_for(self._configured.wait(), timeout)
        except asyncio.TimeoutError:
            raise SessionFailed(f"OpenAI did not configure the session within {timeout:.0f} s") from None
        if self._refused is not None:
            raise SessionFailed(self._refused)

    async def close(self) -> None:
        self._closing = True
        if self._connection is not None:
            await self._connection.close()
        if self._reader is not None and self._reader is not asyncio.current_task():
            await asyncio.gather(self._reader, return_exceptions=True)

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
        if s.log_what_you_say:
            audio_input["transcription"] = {"model": self.TRANSCRIPTION_MODEL}
            if setup.transcription_languages:
                audio_input["transcription"]["languages"] = setup.transcription_languages
        if s.noise_reduction:
            audio_input["noise_reduction"] = {"type": s.noise_reduction}
        config = {
            "type": "realtime",
            "instructions": prompts.realtime(s, setup.home),
            "audio": {"input": audio_input, "output": {"voice": voice(s.voice), "speed": s.speed}},
            "tools": setup.tools.definitions,
        }
        if s.max_output_tokens is not None:
            config["max_output_tokens"] = s.max_output_tokens
        if takes_reasoning(s.model):
            config["reasoning"] = {"effort": s.reasoning_effort}
        truncation: dict = {"type": "retention_ratio", "retention_ratio": self.RETENTION_RATIO}
        if s.max_context_tokens:
            truncation["token_limits"] = {"post_instructions": s.max_context_tokens}
        config["truncation"] = truncation
        return config

    # --- what the agent sends ----------------------------------------------------------

    async def send_audio(self, audio: bytes) -> None:
        await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(audio).decode("ascii")})

    async def clear_input(self) -> None:
        await self._send({"type": "input_audio_buffer.clear"})

    async def interrupt(self) -> None:
        """The device's "stop".

        The stop word's audio in OpenAI's input buffer and the response being
        generated are dropped. If OpenAI committed the stop word as a turn
        before the clear landed, it creates a response to it moments later; so
        the next response to appear, whenever it does, and any within
        INTERRUPT_KILL_WINDOW_S, are cancelled too. Real speech disarms both; a
        wake disarms the first.
        """
        self._kill_until = time.monotonic() + self.INTERRUPT_KILL_WINDOW_S
        self._kill_next = True
        await self.clear_input()
        if self._response_id is not None:
            logger.info("🛑 response.cancel (a response was active)")
            await self._cancel_response()

    def woke(self) -> None:
        self._kill_next = False

    def real_speech(self) -> None:
        self._kill_until = 0.0
        self._kill_next = False

    async def send_tool_results(self, results: list[tuple[str, str]], *, answer: bool) -> None:
        """Each call's result, then, if `answer`, a request to answer with them."""
        for call_id, output in results:
            await self._send({
                "type": "conversation.item.create",
                "item": {
                    "id": f"item_{uuid.uuid4().hex[:24]}",
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            })
        if answer:
            await self._send({"type": "response.create"})

    async def speak(self, instruction: str) -> None:
        """Tell the model, as a system message in the conversation, and ask for a response.

        Nothing armed by an earlier "stop" may cancel it: it isn't the stop
        word's turn.
        """
        self._kill_until = 0.0
        self._kill_next = False
        await self._send({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "system", "content": [{"type": "input_text", "text": instruction}]},
        })
        await self._send({"type": "response.create"})

    async def _send(self, event: dict) -> None:
        if self._connection is None or self._closing:
            return
        try:
            await self._connection.send_raw(json.dumps(event))
        except ConnectionClosed:
            pass  # the reader reports it

    async def _cancel_response(self) -> None:
        """Cancel the response being generated; whatever audio of it still arrives is dropped."""
        if self._response_id is not None:
            self._cancelled.add(self._response_id)
        await self._send({"type": "response.cancel"})

    # --- OpenAI's events ------------------------------------------------------------

    async def _read(self) -> None:
        reason = "OpenAI closed the connection"
        try:
            while True:
                event = json.loads(await self._connection.recv_bytes())
                try:
                    await self._on_event(event)
                except Exception:
                    logger.exception(f"❌ handling OpenAI's {event.get('type')} failed")
        except ConnectionClosed as e:
            reason = f"connection closed: {e}"
        if not self._closing:
            await self._events.session_lost(self, reason)

    async def _on_event(self, event: dict) -> None:
        if self._closing:
            return  # the agent has moved on from this session
        kind = event.get("type", "")
        if kind != "response.output_audio.delta":
            logger.debug(f"⬅️ {kind}")
        handler = getattr(self, "_on_" + kind.replace(".", "_"), None)
        if handler is not None:
            await handler(event)

    async def _on_session_updated(self, event: dict) -> None:
        self._configured.set()

    async def _on_input_audio_buffer_speech_started(self, event: dict) -> None:
        self._events.user_started()

    async def _on_input_audio_buffer_speech_stopped(self, event: dict) -> None:
        self._events.user_stopped()

    async def _on_conversation_item_added(self, event: dict) -> None:
        item = event.get("item") or {}
        if item.get("type") == "function_call":
            self._call_names[item.get("id", "")] = item.get("name", "")
        if item.get("role") == "assistant":
            await self._kill_racing_response()

    async def _kill_racing_response(self) -> None:
        within_window = time.monotonic() < self._kill_until
        if not within_window and not self._kill_next:
            return
        self._kill_next = False
        logger.info("🛑 a response raced in right after a device interrupt → response.cancel")
        await self._cancel_response()

    async def _on_conversation_item_input_audio_transcription_completed(self, event: dict) -> None:
        text = (event.get("transcript") or "").strip()
        if text:
            logger.info(f"🗣️ user: {text}")

    async def _on_response_created(self, event: dict) -> None:
        self._response_id = (event.get("response") or {}).get("id", "")

    async def _on_response_output_audio_delta(self, event: dict) -> None:
        if event.get("response_id") not in self._cancelled:
            self._events.reply_audio(base64.b64decode(event.get("delta", "")))

    async def _on_response_output_audio_done(self, event: dict) -> None:
        if event.get("response_id") not in self._cancelled:
            self._events.reply_ended()

    async def _on_response_output_audio_transcript_delta(self, event: dict) -> None:
        self._reply_text += event.get("delta", "")

    async def _on_response_function_call_arguments_done(self, event: dict) -> None:
        name = self._call_names.get(event.get("item_id", ""), event.get("name", ""))
        self._events.tool_call(event.get("call_id", ""), name, event.get("arguments") or "")

    async def _on_response_done(self, event: dict) -> None:
        response = event.get("response") or {}
        self._response_id = None
        self._cancelled.discard(response.get("id"))
        self._events.reply_ended()
        if self._reply_text.strip():
            logger.info(f"🤖 assistant: {self._reply_text.strip()}")
        self._reply_text = ""
        if response.get("status") == "failed":
            self._events.turn_failed(f"response failed: {response.get('status_details')}")
        self._events.response_done(self)

    async def _on_error(self, event: dict) -> None:
        error = event.get("error") or {}
        code = error.get("code")
        if code in self.BENIGN_ERROR_CODES:
            logger.warning(f"⚠️ benign realtime error ignored: {code}")
            return
        if not self._configured.is_set():
            # The session's settings were refused: say which setting to change.
            param = error.get("param") or ""
            setting = setting_for(param)
            advice = f"; check the setting {setting}" if setting else ""
            self._refused = f"OpenAI refused the session's settings: {error.get('message', code)} ({param or code}){advice}"
            self._configured.set()
            return
        logger.error(f"❌ OpenAI error: {error}")
        if code == "session_expired":
            await self._events.session_lost(self, f"{code}: {error.get('message', '')}")
        else:
            self._events.error(f"{code}: {error.get('message', '')}")


def voice(name: str) -> str | dict:
    """A built-in voice by name; a custom voice (OpenAI's id, voice_…) by id."""
    return {"id": name} if name.startswith("voice_") else name


def takes_reasoning(model: str) -> bool:
    """Whether `model` takes a reasoning effort: gpt-realtime-2 and later do.

    Others reject the session with it, so it isn't sent to them. A dated
    snapshot (gpt-realtime-2025-08-28) is of the first model, which doesn't.
    """
    match = re.match(r"gpt-realtime-(\d+)(?:[.-]|$)", model)
    return bool(match) and 2 <= int(match.group(1)) < 2000


# The setting behind each part of the session OpenAI may reject, by the
# error's `param`, most specific first.
SETTING_FOR_PARAM = (
    ("session.reasoning", "realtime → reasoning_effort"),
    ("session.audio.output.voice", "realtime → voice"),
    ("session.audio.output.speed", "realtime → speed"),
    ("session.audio.input.transcription", "log_what_you_say / transcription_language"),
    ("session.audio.input.noise_reduction", "realtime → noise_reduction"),
    ("session.audio.input.turn_detection", "realtime → vad_eagerness, or the legacy turn detection settings"),
    ("session.max_output_tokens", "realtime → max_output_tokens"),
    ("session.truncation", "realtime → max_context_tokens"),
    ("session.tools", "the Home Assistant tools (mcp_tool_allowlist)"),
    ("session.instructions", "house_rules"),
    ("session.model", "realtime → model"),
)


def setting_for(param: str) -> str | None:
    """The add-on setting behind the session field `param`, if one is."""
    for prefix, setting in SETTING_FOR_PARAM:
        if param == prefix or param.startswith(prefix + ".") or param.startswith(prefix + "["):
            return setting
    return None
