# OpenAI Realtime Voice Agent — Documentation

This add-on runs an OpenAI voice session — **`gpt-realtime-2.1`** by default, or
**GPT-Live** (section 5) — and bridges it to Home Assistant control and web search. It is one of three parts: the
others are custom **firmware for the Home Assistant Voice PE** device (see
[Firmware](#firmware-home-assistant-voice-pe-only) below) and this add-on's
integration (see [The integration](#the-integration)).

```
Voice PE device  ──encrypted WebSocket──▶  this add-on  ──▶  OpenAI (Realtime or GPT-Live)
(mic up / speaker down)                         │  tools
                                                ▼
                                 HA MCP Server (/api/mcp)  → controls your home
```

---

## 1. Install the add-on

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Top-right **⋮ → Repositories**, add:
   `https://github.com/craig-b/ha-openai-realtime`
3. Find **OpenAI Realtime Voice Agent** in the store and click **Install**.
   Home Assistant downloads its prebuilt image, built and tested for each release.
4. Open the add-on's **Configuration** tab to set it up (next sections).

A first run also needs this add-on's integration, which links the Voice PE to the
add-on: see [The integration](#the-integration).

## 2. Get an OpenAI API key and choose a device secret

1. Go to <https://platform.openai.com/> → **API keys** → **Create new secret key**.
2. Make sure **billing** is set up on your OpenAI account — `gpt-realtime-2.1` (audio)
   and web search are paid usage.
3. Paste the key into the add-on's **`openai_api_key`** option.
4. Set **`device_secret`** to a random string of 16 to 128 visible ASCII
   characters with no spaces, e.g. the output of `openssl rand -hex 32`. Your
   Voice PE sends the same value to connect; the add-on refuses any connection
   without it, so nothing else on your network can use your OpenAI key or your
   home's controls through the add-on. The add-on doesn't start until it's set.

The connection to the device is also encrypted, so nobody on your network can
listen to your microphone or read the secret. The add-on makes its own
certificate on first start, keeps it, and prints it in its log at every start.

You don't copy either to the device: the add-on's integration does (see
[The integration](#the-integration) below). The first time, the Voice PE asks for a
press of its centre button before it takes them. If you change `device_secret`,
Home Assistant asks you to confirm the add-on's new link, and the integration then
gives the device the new secret.

> **Heads-up on rate limits:** new accounts start at a low tokens-per-minute (TPM)
> tier. `gpt-realtime-2.1` audio is token-heavy, so if you see *"Rate limit reached"*
> in the logs, raise your usage tier in the OpenAI dashboard, or keep
> `realtime → max_context_tokens` modest (default 8000).

## 3. Let it control Home Assistant (MCP)

The assistant controls your home through Home Assistant's **official MCP Server**.

1. In HA: **Settings → Devices & Services → Add Integration → "Model Context
   Protocol Server"** and add it.
2. **Expose the entities** you want voice control over to **Assist**
   (Settings → Voice assistants → *Exposed entities*). The MCP server only offers
   what's exposed. The add-on reads the tools again for each conversation, so a
   change takes effect at the next one, with no restart.
3. In the add-on, leave **`ha_mcp_url`** **blank** — it then uses the built-in
   endpoint (`http://supervisor/core/api/mcp`) with the add-on's own token. Leave
   **`longlived_token`** blank too, unless the log shows a 401/403 on
   `/core/api/mcp` (then paste a HA long-lived token there).

`ha_mcp_url` also sets the Home Assistant API the add-on uses for everything else
(the URL without its `/mcp`): the event bus (announcements, the satellite's state,
the offer of the integration) and Home Assistant's configuration. So pointing it at
another MCP server breaks those.

You get a small fixed set of Assist tools (`HassTurnOn`, `HassTurnOff`,
`HassLightSet`, `GetLiveContext`, `GetDateTime`, …). **`GetLiveContext`** is the
"what's the current state?" tool — keep it; it's what answers *"is the light on?"*.

**`mcp_tool_allowlist`** (optional): a comma-separated whitelist of tool names. Leave
blank to expose all, or trim to just what you use, e.g.:
`HassTurnOn,HassTurnOff,HassLightSet,GetLiveContext,GetDateTime`

## 4. Recommended starting settings

**The defaults are the recommended settings** — for a first run you only need the
API key, the device secret, the MCP integration (section 3) and this add-on's
integration (see [The integration](#the-integration)). The
Configuration tab is grouped: **🔑 Basics → ⚡ Realtime settings → 🎙️ GPT-Live settings → 💬 Conversation →
🌐 Web search → 🎚️ Audio → 🏠 Home Assistant → ⚙️ Advanced → 🔍 Debug**, and every
option has plain-language inline help. The **⚡ Realtime settings** are a panel of
their own (`realtime → …` below): the model, its voice and everything else only the
Realtime models use; the **🎙️ GPT-Live settings** are its counterpart for the GPT-Live
engine (section 5).

| Option | Default | Note |
|---|---|---|
| `realtime → model` | `gpt-realtime-2.1` | newest speech-to-speech model |
| `realtime → voice` | `marin` | `marin`/`cedar` are the newest voices |
| `realtime → reasoning_effort` | `low` | how long it thinks before answering; `minimal` is quickest |
| `house_rules` | *(English default)* | your part of the prompt: personality, language, your home's particulars |
| `follow_up_listen_seconds` | `8` | mic stays open this long so you can answer back |
| `follow_up_open_delay_ms` | `700` | echo guard before the follow-up mic opens; lower = snappier but risks ghost turns |
| `wake_open_delay_ms` | `700` | the same echo guard right after the wake chime; lower = snappier wake but risks a ghost turn |
| `realtime → vad_eagerness` | `low` | waits longest before deciding you're done talking |
| `playback_prebuffer_ms` | `150` | raise to ~250 if you hear crackle; 0 = play immediately |
| `realtime → max_context_tokens` | `8000` | conversation size at which OpenAI forgets the oldest half; bounds per-reply token cost |
| `realtime → mute_mic_while_speaking` | `true` | the device stops sending your voice while it speaks: on the Voice PE, Realtime hears its own voice through an open mic |
| `live → mute_mic_while_speaking` | `false` | GPT-Live tells its voice from yours, so you can talk over it |
| `enable_end_conversation` | `true` | after a clear goodbye the device stops listening, without a follow-up window |
| `home_town` | *(blank)* | the town the home is in, so "what's the weather?" is local; Home Assistant supplies the country, time zone and units |
| `enable_web_search` | `true` | online lookups; set `false` to disable |
| `realtime → web_search_model` | `gpt-6-luna` | the quickest and cheapest; sol and astra think harder |

**The prompt.** Each model is told the add-on's built-in rules first: short spoken
replies, tools used without announcing them, devices looked up by name before asking,
and when to end the conversation. Then come your **`house_rules`**, then where the home
is. The built-in rules ship with the add-on, so they improve with its updates and
nothing you write can break them; your house rules win on personality, tone, language
and naming. Lock the language there, e.g. "Speak and understand only Dutch. Never
switch language." The built-in rules are in
[`app/prompts.py`](https://github.com/craig-b/ha-openai-realtime/blob/main/openai_realtime_voice_agent/app/prompts.py),
and with `log_level: DEBUG` the log shows the whole prompt each session gets.

The legacy `server_vad` turn-detection fields are at the bottom of the ⚡ Realtime
settings — leave them empty unless you have a specific reason.

## 5. Choosing an engine: Realtime or GPT-Live

The **Engine** option (🔑 Basics) picks which kind of OpenAI voice model runs the
conversation. Each has its own settings panel; only the chosen engine's are used.
OpenAI's current prices for every model are on its
[pricing page](https://openai.com/api/pricing/); OpenAI offers no way to look them
up automatically, so the add-on doesn't show them.

| | Realtime (default) | GPT-Live |
|---|---|---|
| How it works | one model (`gpt-realtime-2.1`) listens, thinks and speaks, in turns | a voice (`gpt-live-1`) that listens and speaks at once, and hands anything that needs doing to a backend model (`gpt-6-luna`), which calls the tools |
| Prompts | the built-in rules for the model, then `house_rules` | the built-in rules for the voice and for the backend, each followed by `house_rules` |
| Billing | per token of audio and text | per minute a conversation is open, plus the backend's tokens |
| Between conversations | the session stays open for `realtime → session_reuse_timeout_seconds` (300), costing nothing | closes as soon as the conversation ends (`live → session_reuse_timeout_seconds`, 0), since open minutes are billed |
| Web search | the add-on's own search call, on `realtime → web_search_model` | the backend model's own web search |
| Log of what you said | with `log_what_you_say` | always |

With GPT-Live the light ring shows what is really happening rather than turns:
**listening** while it hears you, **thinking** while the backend works, **replying**
while its voice is audible. It often acknowledges a request at once ("Okay, turning
on the kitchen light") and confirms when the backend is done; your house rules
can steer that.

## 6. Web search

When **`enable_web_search`** is on (**the default**), the assistant can look up current
or general info (weather, news, facts):

- **Realtime:** it gets a `web_search` tool. When it calls it, the add-on makes a
  **second, server-side OpenAI call** (the Responses API `web_search` built-in tool,
  on **`realtime → web_search_model`**) and reads a short spoken answer back.
- **GPT-Live:** its backend model gets OpenAI's own web search tool and searches
  itself; `realtime → web_search_model` doesn't apply.

Turn it off for an assistant that only knows your home, or if Home Assistant offers it
a search tool of its own. For the Realtime engine:

- Uses your **existing OpenAI key** — no extra account.
- Default model `gpt-6-luna`, with little reasoning and little search context: a
  voice assistant's lookups want a quick, short answer, grounded in what it finds.
  `gpt-6.1-sol` and `gpt-6-astra` think harder, at more time and cost.
- Adds ~1–3 s while it searches (the device shows "thinking").
- If the model name is rejected, the assistant just says it couldn't search — it
  won't crash the session, so you can change `realtime → web_search_model` and retry.

## 7. Announcements and conversations from automations

An automation can have the assistant speak, in its own voice, without anyone
waking it. It fires an event (the **Event** action), which the add-on hears on
Home Assistant's event bus; there is nothing to set up. It needs the add-on to
read Home Assistant (the default, section 3).

**An announcement** is said word for word, and the device doesn't listen after it:

```yaml
actions:
  - event: openai_realtime_voice_agent_announce
    event_data:
      message: "The washing machine has finished."
```

**Starting a conversation** has the assistant speak first about the prompt, in its
own words, and then listen for the answer, as after any reply:

```yaml
actions:
  - event: openai_realtime_voice_agent_start_conversation
    event_data:
      prompt: >-
        The back door has been open for ten minutes. Ask whether to leave it
        open; it can't be closed remotely.
```

To have it say a line of yours first, word for word, add a `message`; the `prompt`
then tells it what the conversation is about, for when you answer. Either one will do.

Both take templates, like any event data. A request waits until the device is free
(no conversation going on, nothing playing), for up to two minutes; after that it is
dropped, and the log says so.

**Waiting for it.** When a request is over, the add-on fires
`openai_realtime_voice_agent_done`, with the `request_id` the request carried and a
`result`: `spoken` (it has been said), `stopped` (someone said "stop"), `dropped` (the
device was busy for two minutes) or `failed`. Give each request its own id, and wait
for the answer that carries it:

```yaml
actions:
  - variables:
      request: "{{ context.id }}"
  - event: openai_realtime_voice_agent_announce
    event_data:
      message: "Good night. Turning the lights off."
      request_id: "{{ request }}"
  - wait_for_trigger:
      - trigger: event
        event_type: openai_realtime_voice_agent_done
        event_data:
          request_id: "{{ request }}"
    timeout: "00:03:00"
  - action: light.turn_off
    target:
      area_id: living_room
```

`{{ wait.trigger.event.data.result }}` then says how it went; a started conversation
counts as `spoken` once its opening has played. What was said is part of the conversation, so you can
ask about it straight after. "Stop" interrupts it as it does any reply. The log
shows each request as a `📣` line.

### As an Assist satellite

The add-on's integration (see [The integration](#the-integration)) also makes the
Voice PE an **Assist satellite**. Home Assistant's own **Assist
satellite: Announce** and **Start conversation** actions then reach it, with a form
in the action editor, and wait until it has been said; so do blueprints made for
voice satellites. Its state (idle, listening, processing, responding) follows the
conversation, for automations to use, and it's unavailable while the Voice PE isn't
connected to the add-on.

The satellite gets a device of its own, connected via the Voice PE's, so its entity
is `assist_satellite.<voice pe>_openai_assist_satellite`. It needs no Assist
pipeline: the OpenAI model speaks and converses, not Home Assistant's text-to-speech
or agent. It speaks messages only: it can't play media, plays no chime before an
announcement, and can't ask a question (**Ask question**) yet.

## 8. Options reference & tuning

Every option has a description on the **Configuration** tab. The ones worth knowing:

- **Model / voice** are dropdowns with a **`custom`** entry +
  a `*_custom` text field if you want a value not in the list.
- **`log_what_you_say`** (🔍 Debug, off by default) writes what the assistant heard
  you say into the log, as `🗣️ user: …` lines, using a separate transcription model.
  **`transcription_language`** tells it which languages to expect: blank for Home
  Assistant's language, codes such as `en, nl`, or `auto`. Neither changes what the
  model understands — it hears your audio natively — nor the language it answers in.
- **`follow_up_open_delay_ms` / `playback_prebuffer_ms`** default to `700` / `150`
  — an echo guard and jitter cushion. Lowering them makes the device feel
  snappier, but below ~700 ms open delay the reply's own speaker tail can leak
  into the fresh follow-up mic and become a ghost turn (the assistant "answers
  nobody" or repeats itself); raise the prebuffer if you hear crackle at the
  start of replies.

If OpenAI refuses a setting (a model that doesn't support it, say), the session
doesn't open: the device plays its error sound, and the log names the setting to
change, e.g. `OpenAI refused the session's settings: … check the setting realtime →
reasoning_effort`.

## 9. Reading the logs

The add-on log shows each turn: `🤖 assistant:` (the reply text), `🗣️ user:`
(what it heard you say, with `log_what_you_say` on), `📞 phase ->` (device state), tool calls, and
`🔌 opening an OpenAI session` / `🔌 OpenAI session closed` as conversations start
and end. View it on the add-on **Log** tab.

## Known limitations

- **No voice timers or alarms yet.** Setting a timer by voice isn't supported (the
  official Home Assistant timer intent isn't wired up). Everything else — lights,
  switches, scenes, climate, and questions — works.
- **With the Realtime engine, past 50 minutes a conversation moves to a new
  session.** OpenAI limits a realtime session to 60 minutes, so at the next wake after 50 the add-on opens
  a new one, which starts afresh: the assistant no longer remembers what was
  said before. The same happens if the connection to OpenAI drops.
- **Rarely, the assistant may stop itself** on a word in its own reply that sounds
  like "stop" — just ask again.

**Using "stop":** say "stop" (or press the centre button) to stop the assistant:
during a reply, in the short listening window right after one, and while a tool
(a web search, a Home Assistant action) runs. While you're still asking, "stop" is
ignored, as a misfire on your own words. A stop cancels the tools still running;
their results go into the conversation, but the assistant doesn't answer them.

## The integration

The repository has a Home Assistant integration, installed through HACS, that
links the Voice PE to this add-on: it gives the device the add-on's address, the
device secret and the certificate, and gives them again when they change. It also
makes the Voice PE an Assist satellite (section 7).

1. In HACS → ⋮ → **Custom repositories**, add
   `https://github.com/craig-b/ha-openai-realtime` as an **Integration**, then
   install **OpenAI Realtime Voice Agent** and restart Home Assistant.
2. Under **Settings → Devices & services** it shows up as discovered: the add-on
   offers it once it finds it installed. On a first install the Voice PE can't
   reach the add-on until it's linked, so the add-on can't say which device it is:
   after about a minute it offers the integration without naming it, as **OpenAI
   Realtime Voice Agent**, perhaps before the Voice PE is in Home Assistant. Add the Voice PE
   first, then **Add** the integration and pick the Voice PE. When the add-on can
   name the device, the integration shows up under its name and only asks you to
   confirm. Either way it shows which add-on the offer comes from and the link's
   fingerprint; the add-on's log shows its link's fingerprint at every start (the
   `🔗` line), so check they match. Or add it from **Add integration** yourself:
   it then has no link until the add-on offers one, which you confirm.
3. The first time, Home Assistant shows a repair asking you to press the Voice PE's
   **centre button**: the device takes its link only once someone at it agrees.
   Press it once, and only when the repair asks: the press accepts whatever link
   is waiting on the device, and the repair is what says it's the integration's.
   If the repair says *not* to press it, something else set a link on the device
   first: restart the device instead. If Home Assistant says to **reset** the
   Voice PE, it holds a link the integration didn't give it: factory reset it
   straight away (hold the centre button until the lights turn solid red, about
   22 seconds), then set it up again.

The integration takes the link only from the add-on it was set up with: any other
add-on's offer is turned away. When the add-on's secret, certificate or port
changes, Home Assistant shows the new offer as discovered, with the fingerprint, and
the device gets the new link once you confirm it.

It links the device to Home Assistant's own address on your network, which suits
the add-on running on Home Assistant's host. If the device needs another address,
set it in the integration's **Configure** dialog (`wss://`, with the add-on's port,
such as `wss://192.0.2.10:8080/`); the device gets it at once.

## Firmware (Home Assistant Voice PE only)

This add-on expects the custom **Voice PE firmware** that turns the device into a
thin client (it streams mic audio here and plays the reply). That firmware:

- is **specific to the Home Assistant Voice PE** hardware (ESP32-S3 + XMOS),
- is the same for every Voice PE: you install it from your browser, from the
  **[install page](https://craig-b.github.io/ha-openai-realtime/)**, and Home
  Assistant sets up the rest (Wi-Fi, its encryption key, and through the
  integration, its link to the add-on), and
- updates through Home Assistant, as a firmware update for the Voice PE, with each
  release.

Its source is the **[`firmware/`](https://github.com/craig-b/ha-openai-realtime/tree/main/firmware)**
folder of this add-on's repository. The [INSTALL guide](https://github.com/craig-b/ha-openai-realtime/blob/main/firmware/INSTALL.md)
covers installing everything from scratch, and building the firmware yourself.

## Credits

- A hard fork of **[xandervanerven/ha-openai-realtime](https://github.com/xandervanerven/ha-openai-realtime)** and **[xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe)** (Xander van Erven), with their full history; see [Where this comes from](https://github.com/craig-b/ha-openai-realtime#where-this-comes-from).
- Backend originally from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on the **OpenAI Realtime API**, **GPT-Live**, and the official **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration.
