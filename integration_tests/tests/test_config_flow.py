"""Setting the integration up: by hand, or from the add-on's discovery."""

import datetime
import hashlib
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openai_realtime_voice_agent.config_flow import link_fingerprint
from custom_components.openai_realtime_voice_agent.const import CONF_ADDON, CONF_DEVICE_ID, DOMAIN

DISCOVERY = HassioServiceInfo(
    config={}, name="OpenAI Realtime 2 Voice Agent", slug="0a1b2c3d_openai_realtime_voice_agent", uuid="0" * 32
)


def make_certificate() -> str:
    """A self-signed certificate, as the add-on makes its own."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OpenAI Realtime Voice Agent")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


CERTIFICATE = make_certificate()
LINK = {"port": 8080, "secret": "0123456789abcdef0123", "certificate": CERTIFICATE}


def offer(link=LINK, slug=DISCOVERY.slug, **config) -> HassioServiceInfo:
    return HassioServiceInfo(config={"link": link, **config}, name=DISCOVERY.name, slug=slug, uuid=DISCOVERY.uuid)


def set_up_already(hass: HomeAssistant, voice_pe: dr.DeviceEntry, **data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={CONF_DEVICE_ID: voice_pe.id, **data})
    entry.add_to_hass(hass)
    return entry


async def init_hassio(hass: HomeAssistant, info: HassioServiceInfo):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=info)


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
    assert result["description_placeholders"]["name"] == "Home Assistant Voice a1b2c3"
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


async def test_the_add_ons_offer_brings_the_link_and_shows_its_certificate(
    hass: HomeAssistant, voice_pe: dr.DeviceEntry
):
    result = await init_hassio(hass, offer(mac="02:00:00:A1:B2:C3"))
    assert result["description_placeholders"] == {
        "name": "Home Assistant Voice a1b2c3",
        "slug": DISCOVERY.slug,
        "fingerprint": link_fingerprint(LINK),
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"] == {CONF_DEVICE_ID: voice_pe.id, "link": LINK, CONF_ADDON: DISCOVERY.slug}


async def test_a_changed_link_waits_for_the_owner(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    entry = set_up_already(hass, voice_pe, link=LINK, addon=DISCOVERY.slug)
    new = {**LINK, "secret": "fedcba9876543210fedc"}
    result = await init_hassio(hass, offer(new))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_link"
    assert result["description_placeholders"]["fingerprint"] == link_fingerprint(new)
    assert entry.data["link"] == LINK  # not until the owner confirms
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "link_updated"
    assert entry.data["link"] == new


async def test_another_add_ons_offer_is_turned_away(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    entry = set_up_already(hass, voice_pe, link=LINK, addon=DISCOVERY.slug)
    result = await init_hassio(hass, offer({**LINK, "secret": "someone-elses-secret"}, slug="f0f0f0f0_other"))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "other_addon"
    assert entry.data["link"] == LINK


async def test_the_same_link_again_changes_nothing(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    entry = set_up_already(hass, voice_pe, link=LINK, addon=DISCOVERY.slug)
    result = await init_hassio(hass, offer())
    assert result["reason"] == "already_configured"
    assert entry.data == {CONF_DEVICE_ID: voice_pe.id, "link": LINK, CONF_ADDON: DISCOVERY.slug}


async def test_an_entry_from_before_records_the_add_on_offering_its_link(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    # Set up by 0.11.0, which didn't record the add-on: an offer of the very link it has records it.
    entry = set_up_already(hass, voice_pe, link=LINK)
    result = await init_hassio(hass, offer())
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_ADDON] == DISCOVERY.slug


async def test_an_entry_from_before_confirms_a_changed_link(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    entry = set_up_already(hass, voice_pe, link=LINK)
    result = await init_hassio(hass, offer({**LINK, "port": 8443}, slug="f0f0f0f0_other"))
    assert result["step_id"] == "confirm_link"
    assert result["description_placeholders"]["slug"] == "f0f0f0f0_other"
    assert entry.data["link"] == LINK


async def test_an_offer_out_of_shape_is_turned_away(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    for link in (
        {**LINK, "port": "8080"},
        {**LINK, "secret": "short"},
        {**LINK, "secret": "has a space in it, see"},
        {**LINK, "certificate": "-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----\n"},
        "a string",
    ):
        result = await init_hassio(hass, offer(link, mac="02:00:00:A1:B2:C3"))
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "invalid_offer"


async def test_the_link_fingerprint_is_the_add_ons(hass: HomeAssistant):
    # The add-on logs SHA-256 over "port\nsecret\n" and the certificate's DER (app/tls.py).
    der = ssl.PEM_cert_to_DER_cert(CERTIFICATE.strip())
    digest = hashlib.sha256(b"8080\n0123456789abcdef0123\n" + der).hexdigest().upper()
    assert link_fingerprint(LINK) == " ".join(digest[i : i + 4] for i in range(0, 32, 4))


async def test_a_waiting_offer_doesnt_turn_the_add_ons_away(hass: HomeAssistant, voice_pe: dr.DeviceEntry):
    # An entry from 0.11.0: another add-on's changed link waits for the owner; the add-on's own offer still lands.
    entry = set_up_already(hass, voice_pe, link=LINK)
    waiting = await init_hassio(hass, offer({**LINK, "port": 8443}, slug="f0f0f0f0_other"))
    assert waiting["step_id"] == "confirm_link"
    result = await init_hassio(hass, offer())
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_ADDON] == DISCOVERY.slug
    # The owner confirms the waiting one after all: it's no longer the entry's add-on's.
    result = await hass.config_entries.flow.async_configure(waiting["flow_id"], {})
    assert result["reason"] == "other_addon"
    assert entry.data["link"] == LINK
