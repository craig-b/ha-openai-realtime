"""Setting the integration up: which device is the add-on's Voice PE.

Offered by the add-on through the Supervisor's discovery once this integration
is installed, or added by hand. The add-on's offer names the device by its MAC
address, by which the ESPHome integration knows it: then the owner only
confirms. Otherwise (added by hand, firmware that doesn't say its MAC, a device
Home Assistant doesn't know) the owner picks the Voice PE's ESPHome device.
"""

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import DeviceSelector, DeviceSelectorConfig
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import CONF_DEVICE_ID, DOMAIN


class OpenAIRealtimeVoiceAgentConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        # The Voice PE the add-on's offer named, once found.
        self._voice_pe: dr.DeviceEntry | None = None

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The add-on offered itself, naming its device if it could."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        mac = discovery_info.config.get("mac")
        if isinstance(mac, str) and mac:
            self._voice_pe = dr.async_get(self.hass).async_get_device(
                connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(mac))}
            )
        if self._voice_pe is None:
            self.context["title_placeholders"] = {"name": "OpenAI Realtime Voice Agent"}
            return await self.async_step_user()
        self.context["title_placeholders"] = {"name": _name(self._voice_pe)}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Set up for the device the add-on named, once the owner confirms."""
        assert self._voice_pe is not None
        if user_input is not None:
            return self.async_create_entry(
                title=_name(self._voice_pe), data={CONF_DEVICE_ID: self._voice_pe.id}
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="discovery_confirm", description_placeholders={"name": _name(self._voice_pe)}
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # The add-on serves one device, so there is one entry.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            device = dr.async_get(self.hass).async_get(user_input[CONF_DEVICE_ID])
            return self.async_create_entry(title=_name(device) if device else "Voice PE", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_DEVICE_ID): DeviceSelector(DeviceSelectorConfig(integration="esphome"))}
            ),
        )


def _name(device: dr.DeviceEntry) -> str:
    return device.name_by_user or device.name or "Voice PE"
