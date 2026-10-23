"""Constants: the events the add-on and this integration speak in (docs/protocol.md)."""

DOMAIN = "openai_realtime_voice_agent"

# The Voice PE's device in Home Assistant, which the satellite joins.
CONF_DEVICE_ID = "device_id"
# What the device needs to connect to the add-on, from its offer: its port, the
# device secret and its certificate (link.py).
CONF_LINK = "link"
# The add-on the link came from (its Supervisor slug): only its offers update it.
CONF_ADDON = "addon"
# The add-on's address for the device, when the owner sets one (options).
CONF_URL = "url"

# Asking the add-on to speak, and its answer.
EVENT_ANNOUNCE = f"{DOMAIN}_announce"
EVENT_START_CONVERSATION = f"{DOMAIN}_start_conversation"
EVENT_DONE = f"{DOMAIN}_done"
# The conversation's state on the device, and asking for it.
EVENT_STATE = f"{DOMAIN}_state"
EVENT_QUERY = f"{DOMAIN}_query"

# How long a request may take before the add-on answers: it waits up to two
# minutes for the device to be free, then the reply has to be said.
REQUEST_TIMEOUT_S = 300.0
