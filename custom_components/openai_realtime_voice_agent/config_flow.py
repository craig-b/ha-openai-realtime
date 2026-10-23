"""Setting the integration up: which device is the add-on's Voice PE.

Offered by the add-on through the Supervisor's discovery once this integration
is installed, or added by hand. The add-on's offer names the device by its MAC
address, by which the ESPHome integration knows it: then the owner only
confirms. Otherwise (added by hand, firmware that doesn't say its MAC, a device
Home Assistant doesn't know) the owner picks the Voice PE's ESPHome device.

The offer also carries the link the integration signs for the device (link.py),
so the integration takes it only from the add-on: any installed add-on can make
an offer for this integration. The entry records which add-on its link came
from and turns other add-ons' offers away, and a link the owner hasn't seen
(a first one, or a changed one) waits for them to confirm it, with the add-on's
slug and the link's fingerprint, which the add-on's log shows.
"""

import hashlib
import hmac
import re
import ssl
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import DeviceSelector, DeviceSelectorConfig
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import CONF_ADDON, CONF_DEVICE_ID, CONF_LINK, CONF_URL, DOMAIN


def valid_link(link: Any) -> dict[str, Any] | None:
    """The offer's link if it has the form the device takes, else None."""
    if not isinstance(link, dict):
        return None
    port, secret, certificate = link.get("port"), link.get("secret"), link.get("certificate")
    if not (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536):
        return None
    if not (isinstance(secret, str) and 16 <= len(secret) <= 128 and all("!" <= c <= "~" for c in secret)):
        return None
    if not isinstance(certificate, str) or _der(certificate) is None:
        return None
    key = link.get("fingerprint_key")
    if not (isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)):
        return None
    return {"port": port, "secret": secret, "certificate": certificate, "fingerprint_key": key}


def same_link(offered: dict[str, Any], kept: Any) -> bool:
    """The offer is the link the entry has: the same port, secret and certificate.

    (Not the fingerprint key, which only the fingerprint is made with, and which
    a link from 0.11.0 doesn't have.)"""
    return isinstance(kept, dict) and all(offered[k] == kept.get(k) for k in ("port", "secret", "certificate"))


def _der(certificate: str) -> bytes | None:
    try:
        return ssl.PEM_cert_to_DER_cert(certificate.strip())
    except ValueError:
        return None


def link_fingerprint(link: dict[str, Any]) -> str:
    """The link's fingerprint, as the add-on's log shows it: HMAC-SHA256 of its
    port, the secret and the certificate (DER), keyed by the add-on's
    fingerprint key, 32 hex digits in fours. An offer with any of them changed,
    the secret too, shows another fingerprint than the add-on's log; and the
    log, without the key, says nothing about the secret."""
    message = f"{link['port']}\n{link['secret']}\n".encode() + (_der(link["certificate"]) or b"")
    digest = hmac.new(bytes.fromhex(link["fingerprint_key"]), message, hashlib.sha256).hexdigest()
    return " ".join(digest[i : i + 4] for i in range(0, 32, 4)).upper()


class OpenAIRealtimeVoiceAgentConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        # The Voice PE the add-on's offer named, once found.
        self._voice_pe: dr.DeviceEntry | None = None
        # The link to the add-on from its offer, for the device, and the add-on
        # (its slug, which the Supervisor sets) that made the offer.
        self._link: dict[str, Any] | None = None
        self._addon: str | None = None
        # The entry a changed link is for, while the owner confirms it.
        self._entry: ConfigEntry | None = None

    def _data(self, device_id: str) -> dict[str, Any]:
        data: dict[str, Any] = {CONF_DEVICE_ID: device_id}
        if self._link:
            data |= {CONF_LINK: self._link, CONF_ADDON: self._addon}
        return data

    def _offer_placeholders(self) -> dict[str, str]:
        """Which add-on made the offer, and the link's fingerprint, for the owner to check.

        Not the add-on's name: the add-on chooses that, so it could pass for another.
        """
        return {"slug": self._addon or "-", "fingerprint": link_fingerprint(self._link) if self._link else "-"}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OpenAIRealtimeVoiceAgentOptionsFlow()

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The add-on offered itself, naming its device if it could.

        Its offer carries the link a device needs; when the integration is set
        up already, a new offer from the same add-on (with a new secret or
        certificate, say) updates it once the owner confirms, and the device is
        linked again.
        """
        if CONF_LINK in discovery_info.config:
            self._link = valid_link(discovery_info.config[CONF_LINK])
            if self._link is None:
                return self.async_abort(reason="invalid_offer")
        self._addon = discovery_info.slug
        entry = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, DOMAIN)
        if entry is not None:
            # Offers to the entry don't wait on one another: one waiting for the
            # owner mustn't turn the add-on's own away.
            await self.async_set_unique_id(DOMAIN, raise_on_progress=False)
            return await self._async_offer_for(entry)
        await self.async_set_unique_id(DOMAIN)
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

    async def _async_offer_for(self, entry: ConfigEntry) -> ConfigFlowResult:
        """An offer when the integration is set up: the link it brings, if new, once the owner confirms."""
        if entry.source == SOURCE_IGNORE:
            return self.async_abort(reason="already_configured")
        if self._other_addon(entry):
            return self.async_abort(reason="other_addon")
        recorded = entry.data.get(CONF_ADDON)
        if self._link is None or same_link(self._link, entry.data.get(CONF_LINK)):
            updates: dict[str, Any] = {}
            if self._link is not None and self._link != entry.data.get(CONF_LINK):
                updates[CONF_LINK] = self._link  # a link from 0.11.0 gets its fingerprint key
            if recorded is None and self._link is not None:
                # An entry from before the add-on was recorded: an offer of the
                # link it has, secret and all, comes from the add-on holding it.
                updates[CONF_ADDON] = self._addon
            if updates:
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
            return self.async_abort(reason="already_configured")
        self._entry = entry
        self.context["title_placeholders"] = {"name": entry.title}
        return await self.async_step_confirm_link()

    async def async_step_confirm_link(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Take the add-on's new link for the device, once the owner confirms."""
        assert self._entry is not None and self._link is not None
        if user_input is not None:
            # Which add-on the entry's link is from may have been recorded
            # since this offer came: check again.
            entry = self.hass.config_entries.async_get_entry(self._entry.entry_id)
            if entry is None:
                return self.async_abort(reason="already_configured")
            if self._other_addon(entry):
                return self.async_abort(reason="other_addon")
            return self.async_update_reload_and_abort(
                self._entry, data_updates={CONF_LINK: self._link, CONF_ADDON: self._addon}, reason="link_updated"
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm_link", description_placeholders={"name": self._entry.title, **self._offer_placeholders()}
        )

    def _other_addon(self, entry: ConfigEntry) -> bool:
        """The entry's link is from another add-on than this offer's."""
        recorded = entry.data.get(CONF_ADDON)
        return recorded is not None and recorded != self._addon

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Set up for the device the add-on named, once the owner confirms."""
        assert self._voice_pe is not None
        if user_input is not None:
            return self.async_create_entry(title=_name(self._voice_pe), data=self._data(self._voice_pe.id))
        self._set_confirm_only()
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={"name": _name(self._voice_pe), **self._offer_placeholders()},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # The add-on serves one device, so there is one entry. (Not by
        # single_config_entry: that turns the add-on's later offers away
        # before async_step_hassio can update the link from them.)
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self._create_entry_for(user_input[CONF_DEVICE_ID])
        return self.async_show_form(step_id="user", data_schema=_PICK_VOICE_PE)

    def _create_entry_for(self, device_id: str) -> ConfigFlowResult:
        device = dr.async_get(self.hass).async_get(device_id)
        return self.async_create_entry(title=_name(device) if device else "Voice PE", data=self._data(device_id))


_PICK_VOICE_PE = vol.Schema({vol.Required(CONF_DEVICE_ID): DeviceSelector(DeviceSelectorConfig(integration="esphome"))})


def _name(device: dr.DeviceEntry) -> str:
    return device.name_by_user or device.name or "Voice PE"


class OpenAIRealtimeVoiceAgentOptionsFlow(OptionsFlow):
    """The add-on's address for the device, if Home Assistant's own isn't the one to use."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input.get(CONF_URL, "").strip()
            if url and not url.startswith("wss://"):
                errors[CONF_URL] = "not_wss"
            else:
                return self.async_create_entry(data={CONF_URL: url} if url else {})
        current = self.config_entry.options.get(CONF_URL, "")
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Optional(CONF_URL, description={"suggested_value": current}): str}),
            errors=errors,
        )
