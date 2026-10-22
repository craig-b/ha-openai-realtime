"""OpenAI Realtime Voice Agent: the add-on's Voice PE as an Assist satellite.

The add-on (openai_realtime_voice_agent/) runs the conversation with OpenAI;
this integration shows its device to Home Assistant as an Assist satellite, so
Home Assistant's own announce and start_conversation actions reach it, and its
state follows the conversation. The two talk over Home Assistant's event bus.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_DEVICE_ID, DOMAIN

PLATFORMS = [Platform.ASSIST_SATELLITE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _link_device(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _link_device(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The satellite's device, connected via the Voice PE's.

    A device belongs to one config entry, so the satellite can't join the
    ESPHome integration's device for the Voice PE. It's linked by device id:
    ESPHome knows the Voice PE by its MAC address alone, with no identifier
    a DeviceInfo could name.
    """
    registry = dr.async_get(hass)
    voice_pe = registry.async_get(entry.data[CONF_DEVICE_ID])
    name = (voice_pe.name_by_user or voice_pe.name) if voice_pe else "Voice PE"
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"{name} OpenAI",
        manufacturer="OpenAI Realtime Voice Agent",
        model="Assist satellite",
    )
    if voice_pe is not None and device.via_device_id != voice_pe.id:
        registry.async_update_device(device.id, via_device_id=voice_pe.id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
