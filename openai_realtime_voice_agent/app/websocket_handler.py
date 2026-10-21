"""WebSocket handler for managing WebSocket connections and pipelines."""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional, Callable, Awaitable, Dict

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.websocket.server import WebsocketServerTransport, WebsocketServerParams

from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EmulateUserStartedSpeakingFrame,
    EmulateUserStoppedSpeakingFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.audio.utils import create_stream_resampler
from pipecat.services.openai.realtime import events as openai_rt_events
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair

from app.device_transport import DeviceServerTransport
from app.realtime_service import SafeRealtimeLLMService
from app.raw_audio_serializer import RawAudioSerializer
from app.audio_recording_service import AudioRecordingService
from app.phase_emitter import PhaseEmitter
from app.transcript_logger import TranscriptLogger
from app.debug_frame_logger import DebugFrameLogger

logger = logging.getLogger(__name__)

# The OpenAI Realtime API works in 24 kHz PCM16. The Voice PE firmware plays
# 24 kHz back and streams 16 kHz up. IMPORTANT: pipecat 0.0.97's websocket INPUT
# transport does NOT resample (only the OUTPUT transport does), and OpenAI
# Realtime's pcm16 input rate is hard-locked to 24000 (PCMAudioFormat.rate =
# Literal[24000]) — you cannot tell it the audio is 16 kHz. So the device's
# 16 kHz frames would be read 1.5x too fast / pitched up, garbling the whole
# transcript. The InputResampler below upsamples 16k->24k in the pipeline.
PIPELINE_SAMPLE_RATE = 24000


class SessionActivityTracker(FrameProcessor):
    """Processor that tracks session activity by monitoring audio frames."""
    
    def __init__(self, activity_callback, **kwargs):
        super().__init__(**kwargs)
        self.activity_callback = activity_callback
    
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, StartFrame):
            logger.debug("🎬 SessionActivityTracker: Received StartFrame")
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            return
        elif isinstance(frame, EndFrame):
            logger.debug("🏁 SessionActivityTracker: Received EndFrame")
            await self.push_frame(frame, direction)
            return
        
        # Track activity on any audio frame, and on the bot finishing speaking:
        # reply audio passes here faster than the device plays it.
        if isinstance(frame, (InputAudioRawFrame, OutputAudioRawFrame, BotStoppedSpeakingFrame)):
            if self.activity_callback:
                self.activity_callback()
            logger.debug(f"🎵 SessionActivityTracker: Processing {type(frame).__name__}")
        
        # Pass frame through to next processor
        await self.push_frame(frame, direction)


class InputResampler(FrameProcessor):
    """Upsample incoming device mic audio to the OpenAI Realtime input rate.

    The Voice PE streams 16 kHz PCM16. pipecat 0.0.97's websocket input transport
    forwards those frames unchanged, and OpenAI Realtime reads pcm16 input at a
    fixed 24 kHz — so without this the audio is interpreted ~1.5x too fast,
    badly degrading transcription (e.g. first word dropped, words mangled). This
    sits right after transport.input() and resamples each InputAudioRawFrame to
    out_rate. Uses a streaming resampler so there are no per-chunk edge artifacts.
    """

    def __init__(self, out_rate: int = PIPELINE_SAMPLE_RATE, **kwargs):
        super().__init__(**kwargs)
        self._out_rate = out_rate
        self._resampler = create_stream_resampler()
        self._logged = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and frame.sample_rate != self._out_rate:
            if not frame.audio:
                return  # nothing to resample / forward; don't emit empty audio
            try:
                resampled = await self._resampler.resample(
                    frame.audio, frame.sample_rate, self._out_rate
                )
            except Exception as e:
                logger.warning(f"⚠️ input resample {frame.sample_rate}->{self._out_rate} failed: {e!r}")
                return  # drop rather than forward wrong-rate audio
            # The streaming resampler buffers internally and can return empty
            # bytes while priming or on a tiny chunk. OpenAI rejects an
            # input_audio_buffer.append with empty audio ("got empty bytes"), so
            # drop those frames — the samples stay buffered and come out next call.
            if not resampled:
                return
            if not self._logged:
                logger.info(
                    f"🎙️ Resampling device input {frame.sample_rate}Hz -> {self._out_rate}Hz for OpenAI"
                )
                self._logged = True
            frame = InputAudioRawFrame(
                audio=resampled,
                sample_rate=self._out_rate,
                num_channels=frame.num_channels,
            )
        await self.push_frame(frame, direction)


class PadReplyToChunk(FrameProcessor):
    """Pad the end of each reply's audio with silence to a whole output chunk.

    The output transport sends audio in fixed chunks and, when the bot stops
    speaking, discards the partial chunk left over, so up to a chunk of every
    reply's ending never reached the device. OpenAI ends each item's audio with
    response.output_audio.done, which Pipecat turns into TTSStoppedFrame; this
    sits before the output transport and tops the audio up there.
    """

    def __init__(self, chunk_bytes: int, sample_rate: int = PIPELINE_SAMPLE_RATE, **kwargs):
        super().__init__(**kwargs)
        self._chunk_bytes = chunk_bytes
        self._sample_rate = sample_rate
        self._pending = 0  # bytes since the last whole chunk

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            self._pending = (self._pending + len(frame.audio)) % self._chunk_bytes
        elif isinstance(frame, TTSStoppedFrame) and self._pending:
            await self.push_frame(TTSAudioRawFrame(
                audio=b"\x00" * (self._chunk_bytes - self._pending),
                sample_rate=self._sample_rate,
                num_channels=1,
            ), direction)
            self._pending = 0
        elif isinstance(frame, InterruptionFrame):
            self._pending = 0  # the output transport drops its partial chunk too
        await self.push_frame(frame, direction)


class DropEmulatedVAD(FrameProcessor):
    """Keep Pipecat from emulating the user's turns from transcripts.

    Pipecat's user aggregator emulates VAD for pipelines without one: a
    transcript arriving while it thinks the user isn't speaking makes it ask the
    input transport for a user turn, which then broadcasts User{Started,Stopped}
    SpeakingFrame and an interruption. Here OpenAI's turn detection is the VAD,
    and its transcripts routinely arrive after the turn has ended: the emulated
    turn sent `listening` mid-`thinking`, disarmed the post-stop response
    cancel, flushed reply audio already queued, and a late emulated stop could
    cancel the pending `idle`. The aggregator sends those requests upstream, so
    this sits between the input transport and it and drops them; the
    transcript still reaches the context.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (EmulateUserStartedSpeakingFrame, EmulateUserStoppedSpeakingFrame)):
            return
        await self.push_frame(frame, direction)


class SessionErrorWatch(FrameProcessor):
    """End the OpenAI session when its connection dies; unstick a turn that dies.

    pipecat 0.0.97's OpenAIRealtimeLLMService has NO reconnect logic: when the
    OpenAI WS drops (1011 keepalive ping timeout, 1001 going away on the 60-min
    cap, 1006, or any send/receive failure) it treats the send error as fatal and
    floods ErrorFrame — ~15/s, one per forwarded mic frame. This processor
    watches the ErrorFrames as they travel upstream to the task source; on the
    first connection-death signature it tells the service the session is lost,
    which closes it (the next wake opens a new one). Any other error that ends a
    turn without a reply — a rate limit, a failed response — leaves the device
    in `thinking`, so it is put back to idle.
    """

    # Substrings that mark a dead/closed OpenAI websocket (vs an app-level error
    # like a tool failure, which must NOT end the session). These appear on the
    # SEND-side flood ("Error sending client event: …"), so they're paired with
    # the "client event" check below to avoid reacting to a device disconnect.
    _DEATH_MARKERS = (
        "keepalive ping timeout",
        "going away",
        "no close frame",
        "ConnectionClosed",
        "connection is closed",
        "sent 1011",
        "sent 1001",
        "1006",
    )
    # Substrings that UNAMBIGUOUSLY mean OUR OpenAI session is gone. The
    # 60-minute cap can arrive as a proactive OpenAI *error event*
    # (code='session_expired', "Your session hit the maximum duration of 60
    # minutes.") with NO "client event" send-flood and NO close-code marker.
    # These can only come from OpenAI (not a device close), so no "client
    # event" guard is needed.
    _SESSION_DEAD_MARKERS = (
        "session_expired",
        "maximum duration",
    )
    IDLE_UNSTICK_COOLDOWN_S = 2.0

    def __init__(self, openai_service, phase_emitter, **kwargs):
        super().__init__(**kwargs)
        self._service = openai_service
        # PhaseEmitter.force_idle() keeps the emitter's phase state consistent
        # AND suppresses the racing `thinking` from VAD stop events still in
        # flight (observed: a raw broadcast idle was overridden 400 ms later and
        # the device sat in `thinking` with an open mic for 44 s).
        self._phase_emitter = phase_emitter
        self._last_idle_unstick = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, ErrorFrame):
            msg = str(getattr(frame, "error", "") or "")
            # Three signs the session is gone:
            #  (a) the OpenAI send-side flood ("Error sending client event: …" +
            #      a close-code marker) — OUR WS died mid-send. The "client
            #      event" signature keeps a DEVICE-side disconnect (also
            #      1011/ConnectionClosed) from ending the session.
            #  (b) an OpenAI session-dead error event (session_expired /
            #      "maximum duration"), the 60-min cap.
            #  (c) the OpenAI READ side died or ended (network drop / silent
            #      server close), which SafeRealtimeLLMService reports with
            #      this message because pipecat reports nothing.
            send_flood = "client event" in msg and any(m in msg for m in self._DEATH_MARKERS)
            session_dead = any(m in msg for m in self._SESSION_DEAD_MARKERS)
            reader_dead = "realtime receive loop" in msg
            if send_flood or session_dead or reader_dead:
                self._service.session_lost(msg)
            else:
                # A turn ended on an error without a reply: most importantly an
                # OpenAI rate-limit ("Rate limit reached …"). No bot speech was
                # produced, so PhaseEmitter never fires BotStopped→idle; the
                # device is left stuck in `thinking`. Emit one `idle` so the user
                # can just try again; the cooldown collapses a rare flood.
                now = time.monotonic()
                if now - self._last_idle_unstick >= self.IDLE_UNSTICK_COOLDOWN_S:
                    self._last_idle_unstick = now
                    asyncio.create_task(self._unstick_idle(msg))
        await self.push_frame(frame, direction)

    async def _unstick_idle(self, reason: str):
        try:
            logger.warning(f"⚠️ turn ended on error, emitting idle to unstick device ({reason[:90]})")
            await self._phase_emitter.force_idle(f"turn ended on error: {reason[:60]}")
        except Exception as e:
            logger.warning(f"⚠️ could not emit idle after turn-ending error: {e!r}")


class WebSocketHandler:
    """Handles WebSocket transport initialization, pipeline building, and event management."""
    
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        audio_recording_service: Optional[AudioRecordingService] = None,
        follow_up_ms: int = 0,
        follow_up_open_delay_ms: int = 700,
        wake_open_delay_ms: int = 700,
        playback_prebuffer_ms: int = 0,
    ):
        """
        Initialize WebSocket handler.

        Args:
            host: Host address to bind to
            port: Port to listen on
            audio_recording_service: Audio recording service instance
            follow_up_ms: How long (ms) the device should keep the mic open
                after a reply so the user can answer without a wake word. Sent to
                the device in the `hello` handshake. 0 = turn-based (no window).
            follow_up_open_delay_ms: How long (ms) the device waits after a reply
                finishes before opening that follow-up mic (bridges the speaker
                hardware tail). Sent in the `hello` handshake.
            wake_open_delay_ms: How long (ms) the device waits after the wake
                chime before opening the mic, so the chime's hardware tail can't
                leak into the fresh mic as a ghost turn. Sent in `hello`.
        """
        self.host = host
        self.port = port
        self.audio_recording_service = audio_recording_service
        self.follow_up_ms = max(0, int(follow_up_ms))
        self.follow_up_open_delay_ms = max(0, int(follow_up_open_delay_ms))
        self.wake_open_delay_ms = max(0, int(wake_open_delay_ms))
        self.playback_prebuffer_ms = max(0, int(playback_prebuffer_ms))

        self.transport: Optional[WebsocketServerTransport] = None
        self.pipeline: Optional[Pipeline] = None
        self.runner: Optional[PipelineRunner] = None
        self.current_task: Optional[PipelineTask] = None
        # The serializer instance the transport reads through. Kept so
        # build_pipeline can wire its device-interrupt callback to the OpenAI
        # service.
        self._serializer: Optional[RawAudioSerializer] = None
        self._transport_params: Optional[WebsocketServerParams] = None
        # The device connection, used to push va_client control/phase messages
        # as TEXT frames (the audio path uses the binary serializer). Only the
        # current one: a superseded connection may be half-open, and sending to
        # a closing connection waits until it has closed.
        self._websocket = None
    
    def create_transport(self) -> WebsocketServerTransport:
        """
        Create and initialize WebSocket transport.
        
        Returns:
            WebsocketServerTransport instance
        """
        logger.info("Initializing WebSocket transport...")
        
        # Use RawAudioSerializer for binary PCM audio. It tags incoming frames
        # with the device mic rate (16 kHz for Voice PE); the transport
        # resamples in/out to the 24 kHz pipeline rate below.
        serializer = RawAudioSerializer()
        self._serializer = serializer

        self._transport_params = WebsocketServerParams(
            serializer=serializer,
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
            audio_out_sample_rate=PIPELINE_SAMPLE_RATE,
        )
        # The transport will start its own server automatically
        self.transport = DeviceServerTransport(
            host=self.host,
            port=self.port,
            params=self._transport_params,
        )
        
        logger.info(f"✅ WebSocket transport created - will listen on ws://{self.host}:{self.port}/")
        return self.transport
    
    def build_pipeline(
        self,
        transport: WebsocketServerTransport,
        openai_service: SafeRealtimeLLMService,
    ) -> tuple[Pipeline, PipelineRunner, PipelineTask]:
        """
        Build pipeline for a WebSocket transport connection.
        
        Args:
            transport: The WebSocket transport instance
            openai_service: The OpenAI service instance
            
        Returns:
            Tuple of (Pipeline, PipelineRunner, PipelineTask)
        """
        if openai_service is None:
            raise RuntimeError("OpenAI service must be created before building pipeline")
        
        logger.info(f"🔗 Building pipeline with WebSocket transport and OpenAI service: {type(openai_service).__name__}")
        
        # Reply audio keeps the session from closing as idle; the service
        # notes mic audio itself.
        output_activity_tracker = SessionActivityTracker(activity_callback=openai_service.note_activity)

        # The conversation as Pipecat records it: transcripts, replies, tool calls.
        context_aggregator = LLMContextAggregatorPair(LLMContext())
        
        # Build pipeline components. InputResampler runs FIRST (right after the
        # transport) so every later stage — VAD, context aggregator, OpenAI
        # service — sees correctly-rated 24 kHz audio instead of the device's
        # raw 16 kHz (which OpenAI would otherwise read 1.5x too fast).
        # Built early so SessionErrorWatch can route its unstick idle through
        # PhaseEmitter.force_idle() (consistent phase state + racing-`thinking`
        # suppression); it is APPENDED near the end of the pipeline below,
        # before transport.output().
        phase_emitter = PhaseEmitter(send_phase=self.send_phase)

        pipeline_components = [
            transport.input(),
            # Watch for OpenAI connection-death ErrorFrames (they travel upstream
            # to the task source, so place this upstream of the service).
            SessionErrorWatch(openai_service=openai_service, phase_emitter=phase_emitter),
            DropEmulatedVAD(),
            InputResampler(out_rate=PIPELINE_SAMPLE_RATE),
        ]
        
        # Add input audio recorder to capture ONLY InputAudioRawFrame
        input_recorder = self.audio_recording_service.get_input_recorder() if self.audio_recording_service else None
        if input_recorder:
            pipeline_components.append(input_recorder)
        
        # Continue with rest of pipeline, with transcript-logging taps. The
        # assistant reply text (TTSTextFrame) flows DOWNSTREAM out of the LLM
        # while the user's TranscriptionFrame is pushed UPSTREAM (so the user
        # aggregator can consume it) — opposite directions, so they need taps on
        # opposite sides of the service (see transcript_logger.py): "user" before
        # the LLM, "assistant" after it.
        # Diagnostics: a passive frame logger that surfaces the turn / audio /
        # interruption lifecycle. Only active when LOG_LEVEL=DEBUG (off by
        # default). Placed right after the LLM so it sees the
        # response / TTS / interruption frames plus the VAD speech frames passing
        # through.
        _debug_taps = logging.getLogger().isEnabledFor(logging.DEBUG)
        pipeline_components.extend([
            context_aggregator.user(),
            TranscriptLogger(capture="user"),
            openai_service,
        ])
        if _debug_taps:
            pipeline_components.append(DebugFrameLogger(label="llm"))
        pipeline_components.extend([
            TranscriptLogger(capture="assistant"),
            context_aggregator.assistant(),
        ])

        pipeline_components.append(output_activity_tracker)

        # Emit va_client phase messages (listening/thinking/replying/idle) to
        # the device, derived from Pipecat speaking frames as they pass
        # downstream. Placed before transport.output() so it sees both the
        # user (UserStarted/Stopped) and bot (BotStarted/Stopped) frames.
        # (Constructed above, before SessionErrorWatch.)
        pipeline_components.append(phase_emitter)

        # Add output audio recorder to capture ONLY OutputAudioRawFrame
        output_recorder = self.audio_recording_service.get_output_recorder() if self.audio_recording_service else None
        if output_recorder:
            pipeline_components.append(output_recorder)

        params = self._transport_params
        pipeline_components.append(PadReplyToChunk(
            chunk_bytes=PIPELINE_SAMPLE_RATE // 100 * params.audio_out_channels * 2 * params.audio_out_10ms_chunks
        ))
        pipeline_components.append(transport.output())

        pipeline = Pipeline(pipeline_components)
        logger.info("✅ Pipeline created for WebSocket connection")
        
        # Audio recording is handled by AudioFrameRecorder processors in the pipeline
        if self.audio_recording_service:
            logger.info("🎙️ Audio recording enabled - will record input and output audio")
        
        # Create pipeline runner and task
        # Disable idle timeout - server should always stay ready for connections
        runner = PipelineRunner()
        task = PipelineTask(pipeline, idle_timeout_secs=None, cancel_on_idle_timeout=False)
        # The caller runs it (runner.run(task)), once.
        logger.info("✅ Pipeline initialized successfully")

        # Wire the device "stop" interrupt. The serializer calls this when it
        # sees {"type":"interrupt"} from the device.
        #
        # The device stops playback itself: on "stop" its firmware flushes its
        # queue and drops incoming audio until the next `listening`. The add-on
        # stops OpenAI generating more (response.cancel, only while a response
        # is active, to avoid response_cancel_not_active) and flushes the reply
        # audio it has queued for the device, which it would otherwise keep
        # sending at real-time pace: a `listening` arriving before that ran out
        # would lift the device's drop and play the rest of the stopped reply.
        #
        # The flush is an InterruptionFrame through the pipeline, the same frame
        # Pipecat sends when OpenAI hears the user start speaking. It runs
        # _handle_interruption, whose audio truncate SafeRealtimeLLMService
        # turns into a no-op: Pipecat computed its position from wall-clock
        # playback, overshooting audio OpenAI had burst faster than real time,
        # and the rejected truncate left the session not answering the next
        # turn.
        # FOLLOW-UP-WINDOW STOP (the "stop heard as a question" bug). During the
        # post-reply follow-up window the device mic is OPEN and streaming, so by
        # the time the device's local wake-word detects "stop" and sends us the
        # interrupt, the stop word's audio is ALREADY in OpenAI's input buffer.
        # Left alone, the server VAD commits it as a user turn and — with
        # create_response=true — the model literally ANSWERS the word "stop"
        # ("Ik hou me stil…"). The device's local detection must therefore be
        # authoritative on the cloud side too, in two layers:
        #   1) input_audio_buffer.clear discards the not-yet-committed stop-word
        #      audio (the device closed its own mic gate in the same instant),
        #      so in the common case no turn is created at all;
        #   2) if the server VAD committed BEFORE our clear landed (tight race),
        #      OpenAI creates a response moments later anyway — so any assistant
        #      conversation item that appears within INTERRUPT_KILL_WINDOW_S of
        #      a device interrupt is cancelled on arrival (handler below). A
        #      legitimate next turn cannot fall inside that window: after a stop
        #      the mic is closed, and a fresh wake-word turn needs the chime +
        #      speech + VAD end-of-turn (> 2 s) before a response is created.
        _interrupt_kill_until = {"t": 0.0}
        INTERRUPT_KILL_WINDOW_S = 1.5
        # A device "stop" must cancel the NEXT assistant response too, not only
        # the one currently playing. After a stop, the only responses OpenAI can
        # still produce before the user speaks again are unwanted:
        #   - the cancelled reply's already-generated tail;
        #   - a slow tool's answer (web search ~2-4 s) the user stopped mid-run,
        #     created on the tool result OUTSIDE the 1.5 s time-window;
        #   - most common: OpenAI's STT hearing the user's spoken "stop" as a
        #     turn and the model REPLYING to it ("Okay, I'll stop"), which lands
        #     ~1.8 s later — just outside 1.5 s (observed 2026-06-14 22:51: the
        #     device flashed red but a fresh "I'll be quiet" reply played, so the
        #     user had to say stop twice).
        # The time-window alone misses the >1.5 s cases. This flag, armed on
        # EVERY device interrupt, makes _kill_racing_response cancel that one
        # next response regardless of timing. It is consumed when used and
        # cleared at the next genuine turn boundary (real speech via
        # on_real_speech, and {"type":"wake"}) — and a legitimate next turn needs
        # the user to actually speak — so it can never cancel a real turn.
        _kill_next_response = {"v": False}

        async def _on_device_interrupt():
            await task.queue_frame(InterruptionFrame())
            _interrupt_kill_until["t"] = time.monotonic() + INTERRUPT_KILL_WINDOW_S
            # Arm the next-response kill on EVERY stop (see the flag comment):
            # the 1.5 s time-window alone misses responses that land later —
            # OpenAI replying to the spoken "stop", or a slow tool's answer.
            _kill_next_response["v"] = True
            await openai_service.clear_input_audio()
            logger.info("🛑 device interrupt → input audio cleared (drop in-flight user audio)")
            try:
                if getattr(openai_service, "_current_assistant_response", None) is not None:
                    await openai_service.send_client_event(openai_rt_events.ResponseCancelEvent())
                    logger.info("🛑 device interrupt → response.cancel sent (response was still active)")
                else:
                    logger.info("🛑 device interrupt → no active response to cancel (device already silenced)")
            except Exception as e:
                logger.info(f"🛑 device interrupt → response.cancel no-op ({e!r})")

        @openai_service.event_handler("on_conversation_item_created")
        async def _kill_racing_response(service, item_id, item):
            # Pipecat fires this for every conversation.item.added; only an
            # ASSISTANT item right after a device interrupt is the racing
            # response to the stop word the user just cancelled.
            if getattr(item, "role", None) != "assistant":
                return
            within_window = time.monotonic() < _interrupt_kill_until["t"]
            kill_armed = _kill_next_response["v"]
            if not within_window and not kill_armed:
                return
            # Consume the flag: this assistant item is the unwanted response the
            # user's stop pre-empted — a stop-acknowledgement ("Okay, I'll stop"),
            # a stopped tool's answer, or the cancelled reply's tail.
            _kill_next_response["v"] = False
            try:
                await openai_service.send_client_event(openai_rt_events.ResponseCancelEvent())
                logger.info(
                    "🛑 response raced in right after a device interrupt → "
                    "response.cancel (post-stop)"
                )
            except Exception as e:
                logger.info(f"🛑 post-interrupt racing-response cancel no-op ({e!r})")

        async def _on_device_session_start():
            # va_client sends {"type":"start"} once per WebSocket CONNECTION
            # (on connect) — NOT per wake. A reconnect mid-utterance (wifi
            # blip) can leave half an utterance in the open session's input
            # buffer; start every (re)connection with a clean one. The
            # per-WAKE/follow-up stale-buffer case is covered by the device's
            # {"type":"flush"} on follow-up timeout.
            await openai_service.clear_input_audio()
            logger.info("🎬 device (re)connected → input audio cleared (clean start)")

        async def _on_device_mic_flush():
            # The device sends {"type":"flush"} when a follow-up window times out
            # mid-stream. Drop any uncommitted partial utterance NOW, at the
            # cut-off, so a later wake can't "complete" it into a stale answer.
            # This replaced the reactive clear-on-mic-resume, which fired on
            # every wake and disturbed the server VAD → spurious garbage commits.
            # Also a turn boundary for the dangling-VAD guard: the follow-up
            # closed without speech, so any later server-VAD stop is dangling.
            phase_emitter.note_wake()
            await openai_service.clear_input_audio()
            logger.info("🧽 follow-up cut-off → input audio cleared (drop partial utterance)")

        async def _on_device_wake():
            # va_client sends {"type":"wake"} on every wake (start_session). Mark
            # the turn boundary for the dangling-VAD guard (A): until the user
            # actually speaks, a server-VAD end-of-turn is a stale pre-wake
            # segment closing late → suppress its thinking + cancel its garbage
            # response (handled in PhaseEmitter via the kill-window callbacks).
            phase_emitter.note_wake()
            # New turn boundary: drop any pending post-tool kill so it can't
            # leak onto this fresh turn's response.
            _kill_next_response["v"] = False
            # The user is about to speak: open a session if none is open.
            openai_service.on_wake()

        async def _on_session_failed(reason: str):
            # The device plays its error sound and goes idle on `error`.
            await self.send_to_device({"type": "error", "message": reason[:200]})
            await phase_emitter.force_idle(f"session failed to open: {reason}")

        async def _on_session_lost(reason: str):
            # Mid-turn, the turn is lost with the session: say so. Between
            # turns there's nothing to tell the device; the next wake reopens.
            if phase_emitter.current not in (None, "idle"):
                await self.send_to_device({"type": "error", "message": "connection to OpenAI lost"})
                await phase_emitter.force_idle(f"session lost: {reason}")

        openai_service.set_session_handlers(on_failed=_on_session_failed, on_lost=_on_session_lost)

        # Wire the dangling-VAD guard's kill-window into the PhaseEmitter. It
        # reuses the SAME _interrupt_kill_until + _kill_racing_response machinery
        # as the device stop: on a dangling stop, arm it so the auto-created
        # garbage response is cancelled; on a real UserStartedSpeaking, clear it
        # so a genuine new turn's response is never cancelled.
        def _clear_kill_window():
            # Real user speech = a genuine new turn — disarm BOTH the time
            # window and the post-tool flag so neither can cancel it.
            _interrupt_kill_until["t"] = 0.0
            _kill_next_response["v"] = False

        phase_emitter.set_kill_window_handlers(
            on_dangling=lambda: _interrupt_kill_until.__setitem__(
                "t", time.monotonic() + INTERRUPT_KILL_WINDOW_S),
            on_real_speech=_clear_kill_window,
        )

        if self._serializer is not None:
            self._serializer.set_interrupt_handler(_on_device_interrupt)
            self._serializer.set_session_start_handler(_on_device_session_start)
            self._serializer.set_mic_flush_handler(_on_device_mic_flush)
            self._serializer.set_wake_handler(_on_device_wake)

        return pipeline, runner, task
    
    def extract_client_id(self, websocket) -> str:
        """
        Extract client ID from websocket connection.
        
        Args:
            websocket: WebSocket connection object
            
        Returns:
            Client ID string
        """
        client_ip = None
        if hasattr(websocket, 'client') and websocket.client:
            client_ip = websocket.client.host
        elif hasattr(websocket, 'remote_address'):
            client_ip = str(websocket.remote_address[0]) if websocket.remote_address else None
        
        if not client_ip:
            client_ip = f"unknown_{uuid.uuid4().hex[:8]}"
            logger.warning("⚠️ Could not extract client IP, using generated ID")

        return client_ip

    async def _send_json(self, websocket, obj: dict) -> None:
        """Send a JSON object to one device as a TEXT websocket frame.

        IMPORTANT: use COMPACT separators (no space after ':' or ','). The Voice
        PE va_client does a literal substring match on `"value":"<phase>"`
        (va_client.cpp handle_text_), so the default json.dumps output
        `"value": "listening"` (with a space) would NOT match and the device
        would silently ignore every phase. Compact output `"value":"listening"`
        matches. This is what made listening/thinking/replying never reach the
        device (LED stuck idle, no-speech watchdog never cancelled).
        """
        try:
            await websocket.send(json.dumps(obj, separators=(",", ":")))
        except Exception as e:
            logger.warning(f"⚠️ Could not send {obj.get('type')} to device: {e!r}")

    async def send_to_device(self, obj: dict) -> None:
        """Send a JSON object to the connected device, if any, as a TEXT frame."""
        if self._websocket is not None:
            await self._send_json(self._websocket, obj)

    async def send_phase(self, value: str) -> None:
        """Send a va_client phase message to the connected device."""
        logger.info(f"➡️ phase '{value}' to {'the device' if self._websocket else 'no device'}")
        await self.send_to_device({"type": "phase", "value": value})
    
    def setup_event_handlers(
        self,
        transport: WebsocketServerTransport,
        on_client_connected_callback: Callable[[str], Awaitable[None]],
        on_client_disconnected_callback: Optional[Callable[[str], None]] = None,
    ):
        """
        Setup WebSocket event handlers.
        
        Args:
            transport: The WebSocket transport instance
            on_client_connected_callback: Async callback function(client_id) called when client connects
            on_client_disconnected_callback: Optional callback function(client_id) called when client disconnects
        """
        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport: WebsocketServerTransport, websocket):
            """Handle new WebSocket client connection."""
            client_id = self.extract_client_id(websocket)
            logger.info(f"🔗 New WebSocket connection from IP: {client_id}")
            # Track the raw connection so we can push phase/control TEXT frames.
            self._websocket = websocket
            # Handshake ack expected by the va_client protocol (server -> device
            # "hello"). The Voice PE firmware tolerates its absence, but sending
            # it keeps both sides in lockstep with the documented protocol.
            # follow_up_ms tells the device how long to keep the mic open after a
            # reply (post-reply follow-up window); 0/absent = turn-based. Sent on
            # every connect so an add-on config change takes effect on reconnect.
            await self._send_json(
                websocket,
                {
                    "type": "hello",
                    "follow_up_ms": self.follow_up_ms,
                    "follow_up_open_delay_ms": self.follow_up_open_delay_ms,
                    "wake_open_delay_ms": self.wake_open_delay_ms,
                    "playback_prebuffer_ms": self.playback_prebuffer_ms,
                },
            )
            await on_client_connected_callback(client_id)

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport: WebsocketServerTransport, websocket, *args, **kwargs):
            """Handle client disconnection."""
            if self._websocket is websocket:
                self._websocket = None
            client_id = self.extract_client_id(websocket)
            if client_id:
                logger.info(f"🔌 Client {client_id} disconnected")
                if on_client_disconnected_callback:
                    on_client_disconnected_callback(client_id)
    
    async def cleanup(self):
        """Cleanup WebSocket handler resources."""
        if self.runner:
            try:
                await self.runner.cancel()
            except Exception as e:
                logger.warning(f"⚠️ Error cancelling runner: {e}")
        
        if self.transport:
            try:
                if hasattr(self.transport, 'stop'):
                    await self.transport.stop()
            except Exception as e:
                logger.warning(f"⚠️ Error stopping transport: {e}")

