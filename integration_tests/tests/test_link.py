"""Linking the Voice PE to the add-on: the integration sets the link on the device when it needs it."""

import hashlib
import hmac
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openai_realtime_voice_agent.const import CONF_DEVICE_ID, CONF_LINK, CONF_URL, DOMAIN
from custom_components.openai_realtime_voice_agent.link import fingerprint, proof

CERTIFICATE = "-----BEGIN CERTIFICATE-----\nMIIBtest\n-----END CERTIFICATE-----\n"
LINK = {"port": 8080, "secret": "0123456789abcdef0123", "certificate": CERTIFICATE}
URL = "wss://192.0.2.10:8080/"


class FakeFirmware:
    """The Voice PE's link handling, as va_client does it: its two sensors and set_addon_link."""

    def __init__(self, hass: HomeAssistant, voice_pe: dr.DeviceEntry, linked: dict | None = None):
        self.hass = hass
        self.link = linked  # {"url", "secret", "certificate"}, or None
        self.pending: dict | None = None  # a first link, waiting for the centre button
        self.connection = "connected" if linked else "not linked"
        self.calls: list[dict] = []
        registry = er.async_get(hass)
        self.link_sensor = registry.async_get_or_create(
            "text_sensor", "esphome", "02:00:00:a1:b2:c3-addon_link", device_id=voice_pe.id,
            original_name="Add-on link",
        ).entity_id
        self.connection_sensor = registry.async_get_or_create(
            "text_sensor", "esphome", "02:00:00:a1:b2:c3-addon_connection", device_id=voice_pe.id,
            original_name="Add-on connection",
        ).entity_id
        hass.services.async_register(
            "esphome", "home_assistant_voice_a1b2c3_set_addon_link", self._set, supports_response=SupportsResponse.NONE
        )
        self.publish()

    def publish(self) -> None:
        shown = self.link or self.pending  # the link it has, or the one waiting for the button
        state = fingerprint(shown["url"], shown["certificate"]) if shown else "not linked"
        self.hass.states.async_set(self.link_sensor, state)
        self.hass.states.async_set(self.connection_sensor, self.connection)

    async def _set(self, call: ServiceCall) -> None:
        self.calls.append(dict(call.data))
        new = {k: call.data[k] for k in ("url", "secret", "certificate")}
        if self.link is not None:
            expected = hmac.new(
                self.link["secret"].encode(),
                f"{new['url']}\n{new['secret']}\n{new['certificate'].rstrip(chr(10)) + chr(10)}".encode(),
                hashlib.sha256,
            ).hexdigest()
            if call.data["proof"] != expected:
                raise HomeAssistantError("the proof doesn't match the secret this device has")
        else:
            if self.pending is not None and self.pending != new:
                raise HomeAssistantError("another first link is waiting for the centre button")
            self.pending = new
            self.connection = "awaiting button"
            self.publish()
            return
        self.link = new
        self.connection = "connected"
        self.publish()

    def press(self) -> None:
        """Someone presses the centre button."""
        if self.pending is not None:
            self.link, self.pending = self.pending, None
            self.connection = "connected"
            self.publish()


@pytest.fixture(autouse=True)
def source_ip():
    with patch(
        "homeassistant.components.network.async_get_source_ip", return_value="192.0.2.10"
    ) as mocked:
        yield mocked


async def set_up(hass: HomeAssistant, voice_pe: dr.DeviceEntry, link=LINK, options=None) -> MockConfigEntry:
    data = {CONF_DEVICE_ID: voice_pe.id, **({CONF_LINK: link} if link else {})}
    entry = MockConfigEntry(domain=DOMAIN, title="Home Assistant Voice a1b2c3", data=data, options=options or {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def press_button_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"press_button_{entry.entry_id}")


def other_link_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"other_link_waiting_{entry.entry_id}")


async def link_at_the_button(hass: HomeAssistant, firmware: FakeFirmware) -> None:
    firmware.press()
    await hass.async_block_till_done()


async def test_an_unlinked_device_is_linked_once_its_button_is_pressed(hass: HomeAssistant, voice_pe, addon):
    firmware = FakeFirmware(hass, voice_pe)
    entry = await set_up(hass, voice_pe)
    call = firmware.calls[0]
    assert (call["url"], call["secret"], call["certificate"]) == (URL, LINK["secret"], CERTIFICATE)
    assert firmware.link is None
    assert press_button_issue(hass, entry) is not None
    await link_at_the_button(hass, firmware)
    assert firmware.link["url"] == URL
    assert press_button_issue(hass, entry) is None


async def test_a_linked_device_is_left_alone(hass: HomeAssistant, voice_pe, addon):
    firmware = FakeFirmware(hass, voice_pe, linked={"url": URL, "secret": LINK["secret"], "certificate": CERTIFICATE})
    await set_up(hass, voice_pe)
    assert firmware.calls == []


async def test_a_new_secret_reaches_the_device_with_proof_of_the_old(hass: HomeAssistant, voice_pe, addon):
    firmware = FakeFirmware(hass, voice_pe)
    entry = await set_up(hass, voice_pe)
    await link_at_the_button(hass, firmware)  # with the first secret
    new_link = {**LINK, "secret": "fedcba9876543210fedc"}
    # The add-on restarted with a new secret and offered itself again; the device is refused meanwhile.
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_LINK: new_link})
    firmware.connection = "refused"
    firmware.publish()
    await hass.async_block_till_done()
    assert firmware.link["secret"] == new_link["secret"]
    assert firmware.calls[-1]["proof"] == proof(LINK["secret"], URL, new_link["secret"], CERTIFICATE)


async def test_a_device_with_another_secret_stays_as_it_is(hass: HomeAssistant, voice_pe, addon, caplog):
    # Linked by something else, with a secret the integration doesn't know: no proof it can make.
    firmware = FakeFirmware(
        hass, voice_pe, linked={"url": "wss://203.0.113.5:8080/", "secret": "someone-elses-secret", "certificate": CERTIFICATE}
    )
    await set_up(hass, voice_pe)
    assert firmware.link["url"] == "wss://203.0.113.5:8080/"
    assert "didn't take the link" in caplog.text


async def test_the_owner_can_give_another_address(hass: HomeAssistant, voice_pe, addon):
    firmware = FakeFirmware(hass, voice_pe)
    await set_up(hass, voice_pe, options={CONF_URL: "wss://voice.example.com:8080/"})
    await link_at_the_button(hass, firmware)
    assert firmware.link["url"] == "wss://voice.example.com:8080/"


async def test_nothing_is_set_without_the_add_ons_offer(hass: HomeAssistant, voice_pe, addon):
    firmware = FakeFirmware(hass, voice_pe)
    await set_up(hass, voice_pe, link=None)
    assert firmware.calls == []


async def test_an_offline_device_is_linked_when_it_comes_back(hass: HomeAssistant, voice_pe, addon):
    firmware = FakeFirmware(hass, voice_pe)
    hass.states.async_set(firmware.link_sensor, "unavailable")
    hass.states.async_set(firmware.connection_sensor, "unavailable")
    await set_up(hass, voice_pe)
    assert firmware.calls == []
    firmware.publish()  # back online, not linked
    await hass.async_block_till_done()
    await link_at_the_button(hass, firmware)
    assert firmware.link["url"] == URL


async def test_the_issue_goes_with_the_entry(hass: HomeAssistant, voice_pe, addon):
    FakeFirmware(hass, voice_pe)
    entry = await set_up(hass, voice_pe)
    assert press_button_issue(hass, entry) is not None
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert press_button_issue(hass, entry) is None


async def test_another_link_waiting_for_the_button_is_not_pressed_for(hass: HomeAssistant, voice_pe, addon, caplog):
    # Something else set a first link before the integration did: it waits for the button, and stays.
    firmware = FakeFirmware(hass, voice_pe)
    firmware.pending = {"url": "wss://203.0.113.5:8080/", "secret": "someone-elses-secret", "certificate": CERTIFICATE}
    firmware.connection = "awaiting button"
    firmware.publish()
    entry = await set_up(hass, voice_pe)
    assert firmware.pending["url"] == "wss://203.0.113.5:8080/"
    assert press_button_issue(hass, entry) is None
    assert other_link_issue(hass, entry) is not None
    assert "didn't take the link" in caplog.text


async def test_a_waiting_link_with_our_address_but_another_secret_is_not_pressed_for(
    hass: HomeAssistant, voice_pe, addon
):
    # Its fingerprint (address and certificate) matches ours; its secret doesn't.
    firmware = FakeFirmware(hass, voice_pe)
    firmware.pending = {"url": URL, "secret": "someone-elses-secret", "certificate": CERTIFICATE}
    firmware.connection = "awaiting button"
    firmware.publish()
    entry = await set_up(hass, voice_pe)
    assert press_button_issue(hass, entry) is None
    assert other_link_issue(hass, entry) is not None
