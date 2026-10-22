"""A real Home Assistant (pytest-homeassistant-custom-component), the Voice PE's
ESPHome device in it, and a fake of the add-on on its event bus."""

import pytest

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openai_realtime_voice_agent.const import (
    CONF_DEVICE_ID,
    DOMAIN,
    EVENT_ANNOUNCE,
    EVENT_DONE,
    EVENT_QUERY,
    EVENT_START_CONVERSATION,
    EVENT_STATE,
)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
async def core(hass: HomeAssistant):
    """Home Assistant's core integration, which every install has and conversation relies on."""
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture
def voice_pe(hass: HomeAssistant) -> dr.DeviceEntry:
    """The Voice PE's device, as the ESPHome integration makes it: known by its MAC address alone."""
    esphome = MockConfigEntry(domain="esphome", title="home-assistant-voice-a1b2c3")
    esphome.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=esphome.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, "02:00:00:a1:b2:c3")},
        name="Home Assistant Voice a1b2c3",
    )


class FakeAddon:
    """The add-on's side of the event bus: it answers the state when asked, and each request with `result`."""

    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.connected = True
        self.result = "spoken"
        # (event type, data) of each request to speak.
        self.requests: list[tuple[str, dict]] = []
        hass.bus.async_listen(EVENT_QUERY, self._query)
        for event_type in (EVENT_ANNOUNCE, EVENT_START_CONVERSATION):
            hass.bus.async_listen(event_type, self._request)

    @callback
    def _query(self, event: Event) -> None:
        self.state("idle")

    @callback
    def state(self, state: str, connected: bool | None = None) -> None:
        if connected is not None:
            self.connected = connected
        self.hass.bus.async_fire(EVENT_STATE, {"state": state, "connected": self.connected})

    @callback
    def _request(self, event: Event) -> None:
        self.requests.append((event.event_type, dict(event.data)))
        request = "announce" if event.event_type == EVENT_ANNOUNCE else "start_conversation"
        self.hass.bus.async_fire(
            EVENT_DONE, {"request_id": event.data["request_id"], "request": request, "result": self.result}
        )


@pytest.fixture
def addon(hass: HomeAssistant) -> FakeAddon:
    return FakeAddon(hass)


@pytest.fixture
async def satellite(hass: HomeAssistant, voice_pe: dr.DeviceEntry, addon: FakeAddon) -> str:
    """The integration set up for the Voice PE; its satellite's entity id."""
    entry = MockConfigEntry(domain=DOMAIN, title="Home Assistant Voice a1b2c3", data={CONF_DEVICE_ID: voice_pe.id})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return "assist_satellite.home_assistant_voice_a1b2c3_openai_assist_satellite"
