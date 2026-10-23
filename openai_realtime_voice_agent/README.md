# OpenAI Realtime Voice Agent

Talk to your home with **OpenAI's `gpt-realtime-2.1`** speech-to-speech model. This
Home Assistant add-on runs the realtime voice session and bridges it to Home
Assistant device control (via the official **MCP Server** integration) and optional
**web search** — so you can say *"alexa, turn off the bedroom lamp"* or *"what's the
weather tomorrow?"* and get a spoken answer.

It is one of three parts. The others are custom **firmware for the Home Assistant
Voice PE** device, which streams microphone audio to this add-on and plays the
reply back, and an **integration**, installed through HACS, which links the Voice PE
to this add-on and makes it an Assist satellite. **This add-on is designed for that Voice PE
firmware** (a thin client that talks a small WebSocket protocol); it is not a
drop-in for the stock HA voice pipeline.

> **You need all three parts.** This add-on does nothing without the **Voice PE firmware**
> that streams audio to it → **[`firmware/`](https://github.com/craig-b/ha-openai-realtime/tree/main/firmware)**,
> and the **integration** that links the device to it →
> **[`custom_components/`](https://github.com/craig-b/ha-openai-realtime/tree/main/custom_components/openai_realtime_voice_agent)**, both in this repository.

## What it does

- **Natural voice conversations** with `gpt-realtime-2.1` (speech in → speech out, no
  separate STT/TTS step), or with **GPT-Live**, a voice that listens and speaks at
  once and hands the work to a smarter backend model.
- **Controls Home Assistant** through the official HA *MCP Server* integration —
  lights, switches, scenes, climate, etc., scoped to the entities you expose to
  Assist.
- **Web search** (on by default) — the assistant can look things up online
  (weather, news, facts) via OpenAI's web search. Turn it off if you don't want it.
- **Tunable from the UI** — model, voice, speaking speed, turn detection, a
  post-reply follow-up window, and more. Every option has
  inline help on the Configuration tab.

## Quick start

1. Add this repository to Home Assistant (Settings → Add-ons → Add-on Store → ⋮ →
   **Repositories**): `https://github.com/craig-b/ha-openai-realtime`
2. Install **OpenAI Realtime Voice Agent** and open its **Configuration** tab.
3. Paste your **OpenAI API key**, choose a **device secret**, install the HA **MCP
   Server** integration, expose a few entities to Assist, and **Start** the add-on.
4. In HACS, add this repository as a custom repository of type **Integration**,
   install **OpenAI Realtime Voice Agent**, and restart Home Assistant.
5. Install the **Voice PE firmware** from the
   **[install page](https://craig-b.github.io/ha-openai-realtime/)** (Chrome or Edge,
   over USB), give it your Wi-Fi, and add the Voice PE when Home Assistant
   discovers it.
6. Add the integration when Home Assistant discovers it, and pick the Voice PE: the
   integration gives the device its link to the add-on. Press the Voice PE's centre
   button when Home Assistant asks.

To build the firmware yourself, see Part 6 of the
[INSTALL guide](https://github.com/craig-b/ha-openai-realtime/blob/main/firmware/INSTALL.md).

Full step-by-step instructions are on the **Documentation** tab (`DOCS.md`).

## Credits

- A hard fork of **[xandervanerven/ha-openai-realtime](https://github.com/xandervanerven/ha-openai-realtime)** and **[xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe)** (Xander van Erven), with their full history; see [Where this comes from](https://github.com/craig-b/ha-openai-realtime#where-this-comes-from).
- Backend originally from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on the **OpenAI Realtime API**, **GPT-Live**, and the official **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration.
