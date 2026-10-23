"""Linking the Voice PE to the add-on.

The add-on's discovery offer carries what a device needs to connect to it: its
port, the device secret and its certificate. The integration keeps them in its
entry, works out the add-on's address (Home Assistant's own on the local
network, as the add-on runs on its host; the owner can set another), and sets
them on the Voice PE through its ESPHome action, set_addon_link, whenever the
device needs it: its "Add-on link" sensor (a hash of the address and the
certificate) differs, or its "Add-on connection" sensor says the add-on refused
its secret. It checks at setup, and whenever either sensor changes.

Anything in Home Assistant can call that action, so once the device is linked
it takes a new link only with proof: an HMAC of the new link keyed by the
secret it has. That is the secret the integration last set on it, which it
remembers (in its own storage, not the entry, so that recording it doesn't
reload the entry). A device not linked yet takes its first link without, once
its centre button is pressed: until then its "Add-on connection" says "awaiting
button", and nothing can replace the link waiting. The integration sends its
link again to tell whether that's it: if the device takes it, a repair issue asks
the owner to press the button; if not, one warns them not to, and to restart the
device.

A linked device that refuses the integration's link holds one the integration
didn't give it, and may be streaming its microphone anywhere: a repair issue tells
the owner to factory reset it, until it takes the integration's link.
"""

import hashlib
import hmac
import logging
from typing import Any

from homeassistant.components import network
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store

from .const import CONF_DEVICE_ID, CONF_LINK, CONF_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

# What the firmware calls its sensors and its action.
LINK_SENSOR_NAME = "Add-on link"
CONNECTION_SENSOR_NAME = "Add-on connection"
LINK_ACTION = "set_addon_link"
REFUSED = "refused"
AWAITING_BUTTON = "awaiting button"
NOT_LINKED = "not linked"
PRESS_BUTTON_ISSUE = "press_button"
OTHER_LINK_ISSUE = "other_link_waiting"
LINKED_ELSEWHERE_ISSUE = "linked_elsewhere"

STORAGE_VERSION = 1


def _pem(certificate: str) -> str:
    """The certificate as the firmware keeps it: one trailing newline."""
    return certificate.rstrip("\r\n") + "\n"


def fingerprint(url: str, certificate: str) -> str:
    """The device's "Add-on link" for this link: as the firmware computes it."""
    return hashlib.sha256(f"{url}\n{_pem(certificate)}".encode()).hexdigest()[:16]


def proof(device_secret: str, url: str, secret: str, certificate: str) -> str:
    """What a linked device needs to take a new link: an HMAC of it keyed by the secret it has."""
    link = f"{url}\n{secret}\n{_pem(certificate)}"
    return hmac.new(device_secret.encode(), link.encode(), hashlib.sha256).hexdigest()


class Linker:
    """Keeps the Voice PE linked to the add-on."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.link")
        self._link_sensor: str | None = None
        self._connection_sensor: str | None = None
        self._action: str | None = None

    async def async_start(self) -> None:
        """Find the device's sensors and action, link it if it needs it, and watch the sensors."""
        if CONF_LINK not in self._entry.data:
            _LOGGER.info("No link to the add-on yet: the add-on offers it when it starts")
            return
        voice_pe = dr.async_get(self._hass).async_get(self._entry.data[CONF_DEVICE_ID])
        if voice_pe is None:
            _LOGGER.warning("The Voice PE's device is gone: remove and add this integration again")
            return
        esphome = self._hass.config_entries.async_get_entry(voice_pe.primary_config_entry or "")
        device_name = esphome.data.get("device_name") if esphome else None
        self._action = f"{device_name.replace('-', '_')}_{LINK_ACTION}" if device_name else None
        sensors = {
            e.original_name: e.entity_id
            for e in er.async_entries_for_device(er.async_get(self._hass), voice_pe.id)
            if e.platform == "esphome"
        }
        self._link_sensor = sensors.get(LINK_SENSOR_NAME)
        self._connection_sensor = sensors.get(CONNECTION_SENSOR_NAME)
        if self._action is None or self._link_sensor is None or self._connection_sensor is None:
            _LOGGER.warning(
                "The Voice PE can't be linked to the add-on: its firmware is older than 1.6.0 "
                "(no %s sensor). Update its firmware",
                LINK_SENSOR_NAME,
            )
            return
        self._entry.async_on_unload(
            async_track_state_change_event(
                self._hass, [self._link_sensor, self._connection_sensor], self._on_sensor
            )
        )
        await self._async_link_if_needed()

    @callback
    def _on_sensor(self, event: Event[EventStateChangedData]) -> None:
        self._entry.async_create_background_task(self._hass, self._async_link_if_needed(), "link the Voice PE")

    async def _async_link_if_needed(self) -> None:
        link_state = self._hass.states.get(self._link_sensor)
        connection_state = self._hass.states.get(self._connection_sensor)
        if any(s is None or s.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) for s in (link_state, connection_state)):
            return  # offline: linked when it comes back
        link = await self._async_link()
        awaiting = connection_state.state == AWAITING_BUTTON
        if not awaiting:
            self._raise_issue(None)
            if link_state.state == fingerprint(link["url"], link["certificate"]) and connection_state.state != REFUSED:
                self._flag_linked_elsewhere(False)
                return
        if not self._hass.services.has_service("esphome", self._action):
            return  # not connected to Home Assistant yet
        # While a first link waits for the button, sending ours again is how to
        # tell whether it's ours: the device takes it only if it's the same,
        # secret and all (the fingerprint it shows leaves the secret out).
        # The secret the device has: the one last set on it (or holds for its
        # button), or else the add-on's (a firmware built with it compiled in).
        stored = await self._store.async_load() or {}
        device_secret = stored.get("secret", link["secret"])
        _LOGGER.info("Linking the Voice PE to the add-on at %s", link["url"])
        try:
            await self._hass.services.async_call(
                "esphome",
                self._action,
                {**link, "proof": proof(device_secret, link["url"], link["secret"], link["certificate"])},
                blocking=True,
            )
        except HomeAssistantError as e:
            _LOGGER.error("The Voice PE didn't take the link to the add-on: %s", e)
            if awaiting:
                self._raise_issue(OTHER_LINK_ISSUE)
            elif self._holds_another_link():
                self._flag_linked_elsewhere(True)
            return
        await self._store.async_save({"secret": link["secret"]})
        self._flag_linked_elsewhere(False)
        if awaiting:
            self._raise_issue(PRESS_BUTTON_ISSUE)

    def _holds_another_link(self) -> bool:
        """The device, refusing the integration's link, is linked and online: its link isn't the integration's.

        Not when it went offline meanwhile, which fails the call too.
        """
        state = self._hass.states.get(self._connection_sensor)
        return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN, NOT_LINKED, AWAITING_BUTTON)

    @callback
    def _flag_linked_elsewhere(self, flagged: bool) -> None:
        """Tell the owner to reset the device while it holds a link the integration didn't give it."""
        issue_id = f"{LINKED_ELSEWHERE_ISSUE}_{self._entry.entry_id}"
        if not flagged:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.CRITICAL,
            translation_key=LINKED_ELSEWHERE_ISSUE,
            translation_placeholders={"name": self._entry.title},
        )

    @callback
    def _raise_issue(self, raised: str | None) -> None:
        """While a first link waits for the centre button: ask for the press if it's ours, warn if not."""
        for issue in (PRESS_BUTTON_ISSUE, OTHER_LINK_ISSUE):
            issue_id = f"{issue}_{self._entry.entry_id}"
            if issue != raised:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
                continue
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING if issue == PRESS_BUTTON_ISSUE else ir.IssueSeverity.ERROR,
                translation_key=issue,
                translation_placeholders={"name": self._entry.title},
            )

    async def async_forget(self) -> None:
        await self._store.async_remove()
        self._raise_issue(None)
        self._flag_linked_elsewhere(False)

    async def _async_link(self) -> dict[str, Any]:
        link = self._entry.data[CONF_LINK]
        url = self._entry.options.get(CONF_URL) or await self._async_default_url(link["port"])
        return {"url": url, "secret": link["secret"], "certificate": link["certificate"]}

    async def _async_default_url(self, port: int) -> str:
        """The add-on runs on Home Assistant's host: its address on the local network."""
        address = await network.async_get_source_ip(self._hass)
        host = f"[{address}]" if ":" in address else address
        return f"wss://{host}:{port}/"
