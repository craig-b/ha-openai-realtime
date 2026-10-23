# Home Assistant Voice PE — OpenAI Realtime 2 fork

> [!IMPORTANT]
> **You need all three parts.** This folder is the **device firmware**; on its own it
> does nothing. It streams audio to a backend add-on that runs the OpenAI Realtime
> session and controls Home Assistant, and the add-on's integration links it there.
> - 🔌 **Device firmware** (this folder) — installed onto the Voice PE from the **[install page](https://craig-b.github.io/ha-openai-realtime/)**
> - 🧠 **Backend add-on** → [`../openai_realtime_voice_agent/`](../openai_realtime_voice_agent/) (runs in Home Assistant)
> - 🔗 **Integration** → [`../custom_components/openai_realtime_voice_agent/`](../custom_components/openai_realtime_voice_agent/) (installed through HACS)
>
> 📖 New here? The full **[INSTALL guide](INSTALL.md)** sets up all three, step by step.

> **Customized fork** of `maxmaxme/home-assistant-voice-pe` (itself a fork of
> `esphome/home-assistant-voice-pe`). The Voice PE runs as a **thin client**: it
> streams microphone audio over an encrypted WebSocket, authenticated with a shared
> secret, to a backend add-on, which runs an OpenAI voice session (`gpt-realtime-2.1`,
> or GPT-Live) and controls Home Assistant through the official
> **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)**
> integration. There is no Home Assistant `voice_assistant` pipeline on the audio
> path — speech recognition, speech and the model all live in the OpenAI session.
>
> The firmware config is [`home-assistant-voice.realtime.yaml`](home-assistant-voice.realtime.yaml).
> One image built from it serves every Voice PE: CI builds it, and each GitHub Release
> publishes it on GitHub Pages, with its install page ([`web/`](web/)) and the
> manifest Voice PEs update from.

## What this fork changes vs. upstream

- **Custom `va_client` component** replaces the stock `voice_assistant`
  component: a thin WebSocket client (mic up / speaker down + an idle → listening
  → thinking → replying phase/LED state machine). All the voice intelligence
  runs in the backend add-on, not on the device.
- **One image for every Voice PE**, with nothing of yours in it: Wi-Fi is set from
  the install page or over Bluetooth, Home Assistant sets the API's encryption key
  as it adds the device, and the add-on's integration sets the link to the add-on
  (its address, the device secret and its certificate, kept in flash; the first
  link only once the centre button is pressed). Updates come from the published
  releases, as a firmware update in Home Assistant. It takes no uploads over the
  network.
- **"stop" word + button interrupt**: say *"stop"* while the assistant is
  talking, or press the center button, to cancel the reply. This is the reliable
  way to interrupt.
- **Conversational, with online answers**: because the brain is an OpenAI voice
  model in the backend, you get a natural back-and-forth — and with web search enabled it
  can look things up online (weather, news, facts), not just control your devices.

## Setup

Install it from the **[install page](https://craig-b.github.io/ha-openai-realtime/)**,
then let Home Assistant set it up: the [INSTALL guide](INSTALL.md), Part 3.

## Building it yourself

To change it, or to keep Wi-Fi, the API key or the link to the add-on in the build,
build it in ESPHome Device Builder from a per-device stub that takes this config as a
remote package: [`esphome-builder.dhcp.yaml`](esphome-builder.dhcp.yaml), or
[`esphome-builder.static-ip.yaml`](esphome-builder.static-ip.yaml) for a fixed IP,
with the secrets in [`secrets.yaml.example`](secrets.yaml.example). Builder then
rebuilds it for each update. The [INSTALL guide](INSTALL.md), Part 6, has the steps.

To build it from a checkout instead, as CI does,
`python3 firmware/tools/local_sources.py` points the config's components and sounds
at the checkout (it rewrites the file: don't commit the result).

## Known limitations

- **No voice timers or alarms yet** — every other Assist action (lights, switches,
  scenes, climate) and online questions work.
- **With the Realtime engine, past 50 minutes a conversation starts afresh** at the
  next wake (OpenAI caps sessions at 60 minutes).
- **Rarely, the assistant may stop itself** on a word in its own reply that sounds
  like "stop" — just ask again.
- **Using "stop":** interrupts the assistant *while it's speaking* (during a reply
  or the short listening window right after one); it has no effect before it has
  started answering.

---

Based on the ESPHome source of the [Home Assistant Voice: Preview Edition](https://www.home-assistant.io/voice-pe/).
See [the upstream documentation](https://voice-pe.home-assistant.io/) for hardware setup and troubleshooting.
