"""The Voice PE as an Assist satellite: Home Assistant's actions reach the add-on, and its state follows it."""

import pytest

from homeassistant.components.assist_satellite import SatelliteBusyError
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.openai_realtime_voice_agent.const import EVENT_ANNOUNCE, EVENT_START_CONVERSATION


async def announce(hass: HomeAssistant, satellite: str, **data) -> None:
    await hass.services.async_call("assist_satellite", "announce", {"entity_id": satellite, **data}, blocking=True)


async def test_the_satellite_is_connected_via_the_voice_pe(
    hass: HomeAssistant, satellite: str, voice_pe: dr.DeviceEntry
):
    entity = er.async_get(hass).async_get(satellite)
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device.name == "Home Assistant Voice a1b2c3 OpenAI"
    assert device.via_device_id == voice_pe.id
    # Not a second copy of the Voice PE's own device.
    assert (dr.CONNECTION_NETWORK_MAC, "02:00:00:a1:b2:c3") not in device.connections


async def test_it_asks_the_add_on_for_the_state_when_set_up(hass: HomeAssistant, satellite: str):
    assert hass.states.get(satellite).state == "idle"


async def test_the_state_follows_the_add_on(hass: HomeAssistant, satellite: str, addon):
    for state in ("listening", "processing", "responding", "idle"):
        addon.state(state)
        await hass.async_block_till_done()
        assert hass.states.get(satellite).state == state
    addon.state("idle", connected=False)
    await hass.async_block_till_done()
    assert hass.states.get(satellite).state == STATE_UNAVAILABLE


async def test_an_announcement_is_asked_of_the_add_on(hass: HomeAssistant, satellite: str, addon):
    # No Assist pipeline, and so no text-to-speech engine, is set up: none is needed.
    await announce(hass, satellite, message="The washing is done.")
    [(event_type, data)] = addon.requests
    assert event_type == EVENT_ANNOUNCE
    assert data["message"] == "The washing is done."
    assert data["request_id"]


async def test_a_busy_device_makes_the_announcement_fail_as_busy(hass: HomeAssistant, satellite: str, addon):
    addon.result = "dropped"
    with pytest.raises(SatelliteBusyError):
        await announce(hass, satellite, message="The washing is done.")


async def test_an_announcement_the_add_on_couldnt_say_fails(hass: HomeAssistant, satellite: str, addon):
    addon.result = "failed"
    with pytest.raises(HomeAssistantError, match="log says why"):
        await announce(hass, satellite, message="The washing is done.")


async def test_a_stopped_announcement_is_no_error(hass: HomeAssistant, satellite: str, addon):
    addon.result = "stopped"
    await announce(hass, satellite, message="The washing is done.")


async def test_media_cant_be_announced(hass: HomeAssistant, satellite: str, addon):
    with pytest.raises(HomeAssistantError, match="can't play media"):
        await announce(hass, satellite, media_id="media-source://media_source/local/chime.mp3")
    assert addon.requests == []


async def test_nothing_is_asked_while_the_device_is_offline(hass: HomeAssistant, satellite: str, addon):
    addon.state("idle", connected=False)
    await hass.async_block_till_done()
    # Home Assistant skips an unavailable entity's actions.
    await announce(hass, satellite, message="The washing is done.")
    assert addon.requests == []


async def test_a_conversation_starts_with_its_message_and_context(hass: HomeAssistant, satellite: str, addon):
    # Home Assistant itself refuses this when the pipeline's agent is its built-in one;
    # the add-on's model converses, so no pipeline is needed.
    await hass.services.async_call(
        "assist_satellite",
        "start_conversation",
        {
            "entity_id": satellite,
            "start_message": "Shall I turn the heating up?",
            "extra_system_prompt": "The living room is at 17 degrees.",
        },
        blocking=True,
    )
    [(event_type, data)] = addon.requests
    assert event_type == EVENT_START_CONVERSATION
    assert data["message"] == "Shall I turn the heating up?"
    assert data["prompt"] == "The living room is at 17 degrees."


async def test_asking_a_question_isnt_supported_yet(hass: HomeAssistant, satellite: str, addon):
    with pytest.raises(HomeAssistantError, match="can't ask a question"):
        await hass.services.async_call(
            "assist_satellite", "ask_question", {"entity_id": satellite, "question": "Tea?"},
            blocking=True, return_response=True,
        )
    assert addon.requests == []
