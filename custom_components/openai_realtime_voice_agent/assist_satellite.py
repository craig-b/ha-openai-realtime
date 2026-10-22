"""The add-on's Voice PE as an Assist satellite.

Home Assistant's announce and start_conversation actions become requests to
the add-on, which has the OpenAI model say them in its own voice; the action
returns once the add-on says it has been said. Its state is the add-on's:
whatever the conversation on the device is doing, and unavailable while the
device isn't connected to the add-on. It has a device of its own, connected
via the Voice PE's.

Home Assistant's own versions of these actions assume the satellite plays
Home Assistant's text-to-speech and that the pipeline's agent carries the
conversation on: they synthesize the message first, and refuse to start a
conversation with the built-in agent. Neither applies here, as the OpenAI
model speaks and converses, so this entity replaces them
(async_internal_announce, async_internal_start_conversation) and needs no
pipeline at all.
"""

import asyncio
import logging
from typing import Any
import uuid

from homeassistant.components.assist_pipeline import PipelineEvent
from homeassistant.components.assist_satellite import (
    AssistSatelliteAnswer,
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
    AssistSatelliteEntityFeature,
    SatelliteBusyError,
)
from homeassistant.components.assist_satellite.entity import AssistSatelliteState
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_DEVICE_ID,
    DOMAIN,
    EVENT_ANNOUNCE,
    EVENT_DONE,
    EVENT_QUERY,
    EVENT_START_CONVERSATION,
    EVENT_STATE,
    REQUEST_TIMEOUT_S,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([VoicePESatellite(entry)])


class VoicePESatellite(AssistSatelliteEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "assist_satellite"
    _attr_supported_features = AssistSatelliteEntityFeature.ANNOUNCE | AssistSatelliteEntityFeature.START_CONVERSATION
    # Unavailable until the add-on says the device is connected.
    _attr_available = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}-assist_satellite"

    @property
    def device_info(self) -> DeviceInfo:
        """A device of its own, connected via the Voice PE's.

        A device belongs to one config entry, so the satellite can't join the
        ESPHome integration's device for the Voice PE.
        """
        voice_pe = dr.async_get(self.hass).async_get(self._entry.data[CONF_DEVICE_ID])
        name = (voice_pe.name_by_user or voice_pe.name) if voice_pe else "Voice PE"
        info = DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=f"{name} OpenAI",
            manufacturer="OpenAI Realtime Voice Agent",
            model="Assist satellite",
        )
        if voice_pe is not None:
            info["via_device"] = next(iter(voice_pe.identifiers))
        return info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(EVENT_STATE, self._on_state))
        # The add-on answers with the state now; until then the entity is unavailable.
        self.hass.bus.async_fire(EVENT_QUERY, {})

    @callback
    def _on_state(self, event: Event) -> None:
        try:
            state = AssistSatelliteState(event.data.get("state"))
        except ValueError:
            _LOGGER.warning("Unknown state from the add-on: %s", event.data)
            return
        self._attr_available = bool(event.data.get("connected"))
        self._set_state(state)

    # --- what Home Assistant asks of a satellite -------------------------------------------

    @callback
    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        """The device detects its own wake word, which isn't set from here."""
        return AssistSatelliteConfiguration(available_wake_words=[], active_wake_words=[], max_active_wake_words=0)

    async def async_set_configuration(self, config: AssistSatelliteConfiguration) -> None:
        raise HomeAssistantError("The Voice PE's wake word is set on the device, not here")

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        """No Assist pipeline runs: the add-on and OpenAI do the listening and talking."""

    async def async_internal_announce(
        self,
        message: str | None = None,
        media_id: str | None = None,
        preannounce: bool = True,
        preannounce_media_id: str | None = None,
    ) -> None:
        """Have the model say `message`, word for word; return once it has been said.

        The device plays no chime first (`preannounce`), and media can't be
        played: the add-on speaks only in the OpenAI voice.
        """
        if media_id:
            raise HomeAssistantError("This satellite speaks messages in the OpenAI voice; it can't play media")
        if not message:
            raise HomeAssistantError("An announcement needs a message")
        await self._request(EVENT_ANNOUNCE, {"message": message})

    async def async_internal_start_conversation(
        self,
        start_message: str | None = None,
        start_media_id: str | None = None,
        extra_system_prompt: str | None = None,
        preannounce: bool = True,
        preannounce_media_id: str | None = None,
    ) -> None:
        """Have the model say `start_message` word for word, then listen for the answer.

        `extra_system_prompt` tells the model what the conversation is about.
        Returns once the opening line has been said; the conversation carries
        on with the add-on.
        """
        if start_media_id:
            raise HomeAssistantError("This satellite speaks in the OpenAI voice; it can't play media")
        if not start_message:
            raise HomeAssistantError("Starting a conversation needs a start message")
        data: dict[str, Any] = {"message": start_message}
        if extra_system_prompt:
            data["prompt"] = extra_system_prompt
        await self._request(EVENT_START_CONVERSATION, data)

    async def async_internal_ask_question(self, *args: Any, **kwargs: Any) -> AssistSatelliteAnswer | None:
        """Answers to a question need what the user said as text, which the add-on doesn't report yet."""
        raise HomeAssistantError("This satellite can't ask a question yet")

    # --- the add-on ---------------------------------------------------------------------------

    async def _request(self, event_type: str, data: dict[str, Any]) -> None:
        """Ask the add-on, and wait for its answer to this request (its request_id).

        Home Assistant doesn't call an unavailable entity's actions, so the
        device is connected to the add-on.
        """
        request_id = uuid.uuid4().hex
        answer: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()

        @callback
        def on_done(event: Event) -> None:
            if event.data.get("request_id") == request_id and not answer.done():
                answer.set_result(dict(event.data))

        stop_listening = self.hass.bus.async_listen(EVENT_DONE, on_done)
        try:
            self.hass.bus.async_fire(event_type, {**data, "request_id": request_id}, context=self._context)
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                result = (await answer).get("result")
        except TimeoutError:
            raise HomeAssistantError("The add-on didn't answer: is it running?") from None
        finally:
            stop_listening()
        if result == "dropped":
            raise SatelliteBusyError("The Voice PE was busy for two minutes")
        if result == "failed":
            raise HomeAssistantError("The add-on couldn't say it: its log says why")
        # "spoken", or "stopped": someone said stop, which is theirs to say.
