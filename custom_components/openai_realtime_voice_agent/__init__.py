"""OpenAI Realtime Voice Agent: the add-on's Voice PE as an Assist satellite.

The add-on (openai_realtime_voice_agent/) runs the conversation with OpenAI;
this integration shows its device to Home Assistant as an Assist satellite, so
Home Assistant's own announce and start_conversation actions reach it, and its
state follows the conversation. The two talk over Home Assistant's event bus.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

PLATFORMS = [Platform.ASSIST_SATELLITE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
