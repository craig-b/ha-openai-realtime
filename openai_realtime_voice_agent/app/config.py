"""The add-on's settings, read from the environment run.sh exports."""
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    return _str(name, "true" if default else "false").lower() == "true"


def _int(name: str, default: int, low: int | None = None, high: int | None = None) -> int:
    try:
        value = int(_str(name, str(default)))
    except ValueError:
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _float(name: str, default: float, low: float | None = None, high: float | None = None) -> float:
    try:
        value = float(_str(name, str(default)))
    except ValueError:
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _choice(name: str, choices: tuple[str, ...], default: str) -> str:
    value = _str(name, default).lower()
    if value not in choices:
        logger.warning(f"⚠️ Unknown {name} {value!r}, falling back to {default}")
        return default
    return value


def _resolve_choice(name: str, custom_name: str, default: str) -> str:
    """A dropdown option with a "custom" escape hatch.

    The add-on UI renders these as a `list(...|custom)` dropdown plus a sibling
    free-text *_custom field. "custom" picks the custom field's value; an empty
    value falls back to `default`.
    """
    choice = _str(name, default)
    if choice.lower() == "custom":
        custom = _str(custom_name)
        if custom:
            return custom
        logger.warning(f"⚠️ {name}=custom but {custom_name} is empty; falling back to {default!r}")
        return default
    return choice or default


@dataclass
class Settings:
    openai_api_key: str
    instructions: str
    model: str
    voice: str
    speed: float
    # None = no limit (the API default).
    max_output_tokens: int | None
    # semantic_vad, OpenAI's recommended mode, or the classic silence-based server_vad.
    turn_detection_type: str
    vad_eagerness: str
    vad_threshold: float
    vad_prefix_padding_ms: int
    vad_silence_duration_ms: int
    # Whether OpenAI hearing the user stops the reply (barge-in). Off: speaker
    # echo would cut replies short; the device's "stop" interrupts instead.
    interrupt_response: bool
    # Whether OpenAI creates a response when it detects the end of a turn.
    create_response: bool
    # The language hint for transcribing the user: an ISO 639-1 code, "auto"
    # for none, or "" for Home Assistant's language (resolved at startup). The
    # model hears the audio itself; the transcript is for the log and for
    # carrying a conversation into a new session.
    transcription_language: str
    transcription_model: str
    noise_reduction: str
    enable_web_search: bool
    web_search_model: str
    # Off by default: the device owns its own session lifecycle.
    enable_disconnect_tool: bool
    ha_mcp_url: str
    ha_token: str
    # Empty = offer every Home Assistant tool.
    mcp_tool_allowlist: list[str]
    websocket_host: str
    websocket_port: int
    device_input_rate: int
    follow_up_ms: int
    follow_up_open_delay_ms: int
    wake_open_delay_ms: int
    playback_prebuffer_ms: int
    phase_idle_debounce_s: float
    # How long the OpenAI session, and with it the conversation, stays open
    # after the last thing that happened in it.
    session_idle_timeout_s: int
    # The conversation's size, in tokens, at which OpenAI truncates it (0 = the model's limit).
    max_context_tokens: int
    enable_recording: bool

    @classmethod
    def from_env(cls) -> "Settings":
        max_output_tokens = _int("MAX_OUTPUT_TOKENS", 0)
        noise_reduction = _str("NOISE_REDUCTION").lower()
        return cls(
            openai_api_key=_str("OPENAI_API_KEY"),
            instructions=os.environ.get(
                "INSTRUCTIONS", "You are the Home Assistant Voice Agent and can control the Smart Home."
            ),
            model=_resolve_choice("OPENAI_MODEL", "OPENAI_MODEL_CUSTOM", "gpt-realtime-2"),
            voice=_resolve_choice("OPENAI_VOICE", "OPENAI_VOICE_CUSTOM", "marin"),
            speed=_float("OPENAI_SPEED", 1.0, 0.25, 1.5),
            max_output_tokens=max_output_tokens if max_output_tokens > 0 else None,
            turn_detection_type=_choice("TURN_DETECTION_TYPE", ("semantic_vad", "server_vad"), "semantic_vad"),
            vad_eagerness=_choice("VAD_EAGERNESS", ("low", "medium", "high", "auto"), "low"),
            vad_threshold=_float("VAD_THRESHOLD", 0.5),
            vad_prefix_padding_ms=_int("VAD_PREFIX_PADDING_MS", 300),
            vad_silence_duration_ms=_int("VAD_SILENCE_DURATION_MS", 800),
            interrupt_response=_bool("INTERRUPT_RESPONSE", False),
            create_response=_bool("SEMANTIC_VAD_CREATE_RESPONSE", True),
            transcription_language=_str("TRANSCRIPTION_LANGUAGE").lower(),
            transcription_model=_resolve_choice(
                "TRANSCRIPTION_MODEL", "TRANSCRIPTION_MODEL_CUSTOM", "gpt-4o-transcribe"
            ),
            noise_reduction=noise_reduction if noise_reduction in ("near_field", "far_field") else "",
            enable_web_search=_bool("ENABLE_WEB_SEARCH", True),
            web_search_model=_resolve_choice("WEB_SEARCH_MODEL", "WEB_SEARCH_MODEL_CUSTOM", "gpt-5.5"),
            enable_disconnect_tool=_bool("ENABLE_DISCONNECT_TOOL", False),
            ha_mcp_url=_str("HA_MCP_URL") or "http://supervisor/core/api/mcp",
            ha_token=_str("LONGLIVED_TOKEN") or _str("SUPERVISOR_TOKEN"),
            mcp_tool_allowlist=[t.strip() for t in _str("MCP_TOOL_ALLOWLIST").split(",") if t.strip()],
            websocket_host=_str("WEBSOCKET_HOST", "0.0.0.0"),
            websocket_port=_int("WEBSOCKET_PORT", 8080),
            device_input_rate=_int("DEVICE_INPUT_SAMPLE_RATE", 16000),
            follow_up_ms=_int("FOLLOW_UP_LISTEN_SECONDS", 8, 0, 60) * 1000,
            follow_up_open_delay_ms=_int("FOLLOW_UP_OPEN_DELAY_MS", 700, 0, 5000),
            wake_open_delay_ms=_int("WAKE_OPEN_DELAY_MS", 700, 0, 5000),
            playback_prebuffer_ms=_int("PLAYBACK_PREBUFFER_MS", 150, 0, 2000),
            phase_idle_debounce_s=_int("PHASE_IDLE_DEBOUNCE_MS", 1500, 0) / 1000,
            session_idle_timeout_s=_int("SESSION_REUSE_TIMEOUT_SECONDS", 300, 0),
            max_context_tokens=_int("MAX_CONTEXT_TOKENS", 8000, 0),
            enable_recording=_bool("ENABLE_RECORDING", False),
        )
