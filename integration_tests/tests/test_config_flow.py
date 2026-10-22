"""Setting the integration up: by hand, or from the add-on's discovery."""

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from custom_components.openai_realtime_voice_agent.const import CONF_DEVICE_ID, DOMAIN

DISCOVERY = HassioServiceInfo(
    config={}, name="OpenAI Realtime 2 Voice Agent", slug="0a1b2c3d_openai_realtime_voice_agent", uuid="0" * 32
)


async def test_the_owner_picks_the_voice_pe(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DEVICE_ID: voice_pe.id})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home Assistant Voice a1b2c3"
    assert result["data"] == {CONF_DEVICE_ID: voice_pe.id}


async def test_the_add_ons_discovery_asks_for_the_voice_pe(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DEVICE_ID: voice_pe.id})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_one_voice_pe_only(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DEVICE_ID: voice_pe.id})
    for source, data in ((config_entries.SOURCE_USER, None), (config_entries.SOURCE_HASSIO, DISCOVERY)):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": source}, data=data)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "single_instance_allowed"
