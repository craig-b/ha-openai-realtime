"""Moving settings saved under an old name to where they live now.

Home Assistant ignores a saved option the add-on no longer has, so a setting
saved under an old name would quietly fall back to its default. At startup the
add-on reads its saved options through the Supervisor; if any are under an old
name, it saves them under the new one and restarts itself, once, to run with
them.
"""
import logging

import httpx

logger = logging.getLogger(__name__)

# Old top-level option -> (section, option in it) where it lives now.
MOVED = {
    "instructions": ("realtime", "instructions"),
    "session_reuse_timeout_seconds": ("realtime", "session_reuse_timeout_seconds"),
    "openai_model": ("realtime", "model"),
    "openai_model_custom": ("realtime", "model_custom"),
    "openai_voice": ("realtime", "voice"),
    "openai_voice_custom": ("realtime", "voice_custom"),
    "openai_speed": ("realtime", "speed"),
    "max_output_tokens": ("realtime", "max_output_tokens"),
    "vad_eagerness": ("realtime", "vad_eagerness"),
    "noise_reduction": ("realtime", "noise_reduction"),
    "web_search_model": ("realtime", "web_search_model"),
    "web_search_model_custom": ("realtime", "web_search_model_custom"),
    "max_context_tokens": ("realtime", "max_context_tokens"),
    "turn_detection_type": ("realtime", "turn_detection_type"),
    "vad_threshold": ("realtime", "vad_threshold"),
    "vad_prefix_padding_ms": ("realtime", "vad_prefix_padding_ms"),
    "vad_silence_duration_ms": ("realtime", "vad_silence_duration_ms"),
}

# Models OpenAI retires (shut down on 20 January 2027), and their replacements.
RETIRED_MODELS = {
    "gpt-realtime": "gpt-realtime-2.1",
    "gpt-realtime-mini": "gpt-realtime-2.1-mini",
}
# The search models offered before the gpt-6 models: any of them moves to the
# default, the quickest, as a voice assistant's lookups want speed.
OLD_SEARCH_MODELS = {"gpt-5-nano", "gpt-5.4-nano", "gpt-5-mini", "gpt-5.4-mini", "gpt-5", "gpt-5.4", "gpt-5.5"}


def migrated(options: dict) -> dict | None:
    """`options` with any old names moved to their new place, or None if there are none."""
    if not any(name in options for name in MOVED):
        return None
    result = {name: value for name, value in options.items() if name not in MOVED}
    for name, value in options.items():
        if name in MOVED:
            section, key = MOVED[name]
            if name == "openai_model":
                value = RETIRED_MODELS.get(value, value)
            elif name == "web_search_model" and value in OLD_SEARCH_MODELS:
                value = "gpt-6-luna"
            result[section] = {**result.get(section, {}), key: value}
    return result


async def migrate_saved_options(supervisor_url: str, token: str) -> bool:
    """Move saved settings to their new names; True if they were, and the add-on is restarting."""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(base_url=supervisor_url, headers=headers, timeout=10) as http:
            response = await http.get("/addons/self/info")
            response.raise_for_status()
            options = migrated(response.json()["data"]["options"])
            if options is None:
                return False
            logger.info("🔀 Moving settings saved under their old names into the new sections")
            response = await http.post("/addons/self/options", json={"options": options})
            response.raise_for_status()
    except Exception as e:
        logger.warning(f"⚠️ Could not move settings saved under old names; they stay at their defaults: {e!r}")
        return False
    logger.info("🔀 Settings moved; restarting to use them")
    try:
        async with httpx.AsyncClient(base_url=supervisor_url, headers=headers, timeout=10) as http:
            await http.post("/addons/self/restart")
    except Exception:
        pass  # the Supervisor may stop the add-on before it answers
    return True
