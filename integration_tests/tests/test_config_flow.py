"""Setting the integration up: by hand, or from the add-on's discovery."""

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
        assert result["reason"] == "already_configured"


async def test_the_add_on_naming_its_device_needs_only_a_confirmation(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    # The device says its MAC in upper case; the ESPHome integration stores it in lower case.
    named = HassioServiceInfo(
        config={"mac": "02:00:00:A1:B2:C3"}, name=DISCOVERY.name, slug=DISCOVERY.slug, uuid=DISCOVERY.uuid
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=named
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"] == {"name": "Home Assistant Voice a1b2c3"}
    [flow] = hass.config_entries.flow.async_progress()
    assert flow["context"]["title_placeholders"] == {"name": "Home Assistant Voice a1b2c3"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_DEVICE_ID: voice_pe.id}


async def test_a_device_home_assistant_doesnt_know_is_picked_by_hand(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    unknown = HassioServiceInfo(
        config={"mac": "02:00:00:ff:ff:ff"}, name=DISCOVERY.name, slug=DISCOVERY.slug, uuid=DISCOVERY.uuid
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=unknown
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_the_add_ons_offer_brings_the_link(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    link = {"port": 8080, "secret": "0123456789abcdef0123", "certificate": "-----BEGIN CERTIFICATE-----\n…"}
    offer = HassioServiceInfo(
        config={"mac": "02:00:00:A1:B2:C3", "link": link}, name=DISCOVERY.name, slug=DISCOVERY.slug, uuid=DISCOVERY.uuid
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=offer)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"] == {CONF_DEVICE_ID: voice_pe.id, "link": link}


async def test_a_later_offer_updates_the_link(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={CONF_DEVICE_ID: voice_pe.id, "link": {"port": 8080, "secret": "old" * 6}}
    )
    entry.add_to_hass(hass)
    link = {"port": 8080, "secret": "new" * 6, "certificate": "-----BEGIN CERTIFICATE-----\n…"}
    offer = HassioServiceInfo(config={"link": link}, name=DISCOVERY.name, slug=DISCOVERY.slug, uuid=DISCOVERY.uuid)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=offer)
    assert result["type"] is FlowResultType.ABORT
    assert entry.data["link"] == link
