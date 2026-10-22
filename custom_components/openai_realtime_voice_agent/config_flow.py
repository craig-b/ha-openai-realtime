"""Setting the integration up: which device is the add-on's Voice PE.

Added by hand, or offered by the add-on through the Supervisor's discovery once
this integration is installed. Either way the owner picks the Voice PE's
ESPHome device, which the satellite then joins.
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

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The add-on offered itself."""
        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # The add-on serves one device, so there is one entry.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            device = dr.async_get(self.hass).async_get(user_input[CONF_DEVICE_ID])
            title = (device.name_by_user or device.name) if device else "Voice PE"
            return self.async_create_entry(title=title, data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_DEVICE_ID): DeviceSelector(DeviceSelectorConfig(integration="esphome"))}
            ),
        )
