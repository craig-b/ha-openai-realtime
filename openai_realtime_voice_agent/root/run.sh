#!/usr/bin/with-contenv bashio
set -e

# --- 🔑 Basics ---
OPENAI_API_KEY=$(bashio::config 'openai_api_key')
DEVICE_SECRET=$(bashio::config 'device_secret')
ENGINE=$(bashio::config 'engine')

# --- ⚡ Realtime (the `realtime` section) ---
INSTRUCTIONS=$(bashio::config 'realtime.instructions')
SESSION_REUSE_TIMEOUT_SECONDS=$(bashio::config 'realtime.session_reuse_timeout_seconds')
OPENAI_MODEL=$(bashio::config 'realtime.model')
OPENAI_VOICE=$(bashio::config 'realtime.voice')
OPENAI_SPEED=$(bashio::config 'realtime.speed')
REASONING_EFFORT=$(bashio::config 'realtime.reasoning_effort')
MAX_OUTPUT_TOKENS=$(bashio::config 'realtime.max_output_tokens')
VAD_EAGERNESS=$(bashio::config 'realtime.vad_eagerness')
NOISE_REDUCTION=$(bashio::config 'realtime.noise_reduction')
WEB_SEARCH_MODEL=$(bashio::config 'realtime.web_search_model')
MAX_CONTEXT_TOKENS=$(bashio::config 'realtime.max_context_tokens')

# --- 🎙️ GPT-Live (the `live` section) ---
LIVE_INSTRUCTIONS=$(bashio::config 'live.instructions')
LIVE_MODEL=$(bashio::config 'live.model')
LIVE_VOICE=$(bashio::config 'live.voice')
LIVE_BACKEND_MODEL=$(bashio::config 'live.backend_model')
LIVE_BACKEND_INSTRUCTIONS=$(bashio::config 'live.backend_instructions')
LIVE_REASONING_EFFORT=$(bashio::config 'live.reasoning_effort')
LIVE_SERVICE_TIER=$(bashio::config 'live.service_tier')
LIVE_SESSION_REUSE_TIMEOUT_SECONDS=$(bashio::config 'live.session_reuse_timeout_seconds')

# --- 💬 Conversation ---
FOLLOW_UP_LISTEN_SECONDS=$(bashio::config 'follow_up_listen_seconds')
FOLLOW_UP_OPEN_DELAY_MS=$(bashio::config 'follow_up_open_delay_ms')
WAKE_OPEN_DELAY_MS=$(bashio::config 'wake_open_delay_ms')
PHASE_IDLE_DEBOUNCE_MS=$(bashio::config 'phase_idle_debounce_ms')

# --- 🌐 Web search ---
ENABLE_WEB_SEARCH=$(bashio::config 'enable_web_search')

# --- 🎚️ Audio ---
PLAYBACK_PREBUFFER_MS=$(bashio::config 'playback_prebuffer_ms')

# --- 🏠 Home Assistant ---
HA_MCP_URL=$(bashio::config 'ha_mcp_url')
LONGLIVED_TOKEN=$(bashio::config 'longlived_token')
MCP_TOOL_ALLOWLIST=$(bashio::config 'mcp_tool_allowlist')

# --- ⚙️ Advanced ---
WEBSOCKET_PORT=$(bashio::config 'websocket_port')

# --- 🔍 Debug ---
LOG_WHAT_YOU_SAY=$(bashio::config 'log_what_you_say')
TRANSCRIPTION_LANGUAGE=$(bashio::config 'transcription_language')
ENABLE_RECORDING=$(bashio::config 'enable_recording')
LOG_LEVEL=$(bashio::config 'log_level')

# Validate required configuration
if [ -z "$OPENAI_API_KEY" ]; then
    bashio::log.error "OPENAI_API_KEY is required but not set"
    exit 1
fi

# Export environment variables
export OPENAI_API_KEY
export DEVICE_SECRET
export ENGINE
export LIVE_INSTRUCTIONS
export LIVE_MODEL
export LIVE_VOICE
export LIVE_BACKEND_MODEL
export LIVE_BACKEND_INSTRUCTIONS
export LIVE_REASONING_EFFORT
export LIVE_SERVICE_TIER
export LIVE_SESSION_REUSE_TIMEOUT_SECONDS
export INSTRUCTIONS
export TRANSCRIPTION_LANGUAGE
export OPENAI_MODEL
export OPENAI_VOICE
export OPENAI_SPEED
export REASONING_EFFORT
export MAX_OUTPUT_TOKENS
export FOLLOW_UP_LISTEN_SECONDS
export FOLLOW_UP_OPEN_DELAY_MS
export WAKE_OPEN_DELAY_MS
export VAD_EAGERNESS
export PHASE_IDLE_DEBOUNCE_MS
export ENABLE_WEB_SEARCH
export WEB_SEARCH_MODEL
export PLAYBACK_PREBUFFER_MS
export NOISE_REDUCTION
export LONGLIVED_TOKEN
export MCP_TOOL_ALLOWLIST
export WEBSOCKET_PORT
export SESSION_REUSE_TIMEOUT_SECONDS
export MAX_CONTEXT_TOKENS
export LOG_WHAT_YOU_SAY
export ENABLE_RECORDING
export LOG_LEVEL

# The *_custom escape hatches (⚡ Realtime, 🎙️ GPT-Live) are optional WITHOUT defaults —
# bashio::config prints "null" for unset optionals, and config.py's
# _resolve_choice would treat that literal string as a real custom value.
# Only export when actually set.
if bashio::config.has_value 'realtime.model_custom'; then
    OPENAI_MODEL_CUSTOM=$(bashio::config 'realtime.model_custom')
    export OPENAI_MODEL_CUSTOM
fi
if bashio::config.has_value 'realtime.voice_custom'; then
    OPENAI_VOICE_CUSTOM=$(bashio::config 'realtime.voice_custom')
    export OPENAI_VOICE_CUSTOM
fi
if bashio::config.has_value 'live.voice_custom'; then
    LIVE_VOICE_CUSTOM=$(bashio::config 'live.voice_custom')
    export LIVE_VOICE_CUSTOM
fi
if bashio::config.has_value 'live.backend_model_custom'; then
    LIVE_BACKEND_MODEL_CUSTOM=$(bashio::config 'live.backend_model_custom')
    export LIVE_BACKEND_MODEL_CUSTOM
fi
if bashio::config.has_value 'realtime.web_search_model_custom'; then
    WEB_SEARCH_MODEL_CUSTOM=$(bashio::config 'realtime.web_search_model_custom')
    export WEB_SEARCH_MODEL_CUSTOM
fi

# Legacy server_vad escape hatch (⚡ Realtime, optional WITHOUT defaults).
# bashio::config prints the string "null" for unset optional keys, which
# config.py would read as an invalid value — so only export when actually set.
# Unset = config.py's hardwired defaults (semantic_vad; 0.5/300/800 if server_vad
# is ever selected).
if bashio::config.has_value 'realtime.turn_detection_type'; then
    TURN_DETECTION_TYPE=$(bashio::config 'realtime.turn_detection_type')
    export TURN_DETECTION_TYPE
fi
if bashio::config.has_value 'realtime.vad_threshold'; then
    VAD_THRESHOLD=$(bashio::config 'realtime.vad_threshold')
    export VAD_THRESHOLD
fi
if bashio::config.has_value 'realtime.vad_prefix_padding_ms'; then
    VAD_PREFIX_PADDING_MS=$(bashio::config 'realtime.vad_prefix_padding_ms')
    export VAD_PREFIX_PADDING_MS
fi
if bashio::config.has_value 'realtime.vad_silence_duration_ms'; then
    VAD_SILENCE_DURATION_MS=$(bashio::config 'realtime.vad_silence_duration_ms')
    export VAD_SILENCE_DURATION_MS
fi

# Removed options (v0.4.29) — no longer exported; config.py env defaults take
# over: SEMANTIC_VAD_CREATE_RESPONSE=true, ENABLE_DISCONNECT_TOOL=false,
# INTERRUPT_RESPONSE=false, DEVICE_INPUT_SAMPLE_RATE=16000.

# Export HA_MCP_URL if set (empty string means use default in config.py)
if [ -n "$HA_MCP_URL" ]; then
    export HA_MCP_URL
fi

# SUPERVISOR_TOKEN is automatically provided by Home Assistant when homeassistant_api: true

# Start the application
export PYTHONUNBUFFERED=1
exec python3 -m app.main
