<p align="center">
  <img src="openai_realtime_voice_agent/icon.png" alt="OpenAI Realtime Voice Agent — a stylised Voice PE" width="160"/>
</p>

# OpenAI Realtime Voice Agent (Home Assistant Voice PE)

> [!IMPORTANT]
> **You need all three parts, and all live in this repo.** The stock Home Assistant
> voice pipeline won't talk to this add-on; it needs the custom Voice PE firmware:
> - 🧠 **Backend add-on** → [`openai_realtime_voice_agent/`](openai_realtime_voice_agent/) — runs inside Home Assistant
> - 🔗 **Integration** → [`custom_components/`](custom_components/openai_realtime_voice_agent/) — installed through HACS; links the Voice PE to the add-on
> - 🔌 **Device firmware** → [`firmware/`](firmware/) — installed onto the Voice PE from the **[install page](https://craig-b.github.io/ha-openai-realtime/)**
>
> 📖 New here? The full **[INSTALL guide](firmware/INSTALL.md)** walks through all three, step by step.

A Home Assistant **add-on** that turns a [Voice PE](https://www.home-assistant.io/voice-pe/)
device into a low-latency voice assistant built on OpenAI's voice models: the
**Realtime API** (`gpt-realtime-2.1`, the default), or **GPT-Live** (`gpt-live-1`),
a voice that listens and speaks at once and hands the work to a backend model.
The device streams microphone audio to this add-on over an encrypted WebSocket,
authenticated with a shared secret; the add-on runs the OpenAI session and
controls Home Assistant through the official
**[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)**
integration. Speech recognition, speech and the model all run in the OpenAI
session — there is no Home Assistant `voice_assistant` pipeline on the audio path.

## Where this comes from

This is a **hard fork**. It began as a fork of Xander van Erven's
**[ha-openai-realtime](https://github.com/xandervanerven/ha-openai-realtime)** and
**[home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe)**,
merged into one repository. Xander's backend is itself a fork of Felix Fricke's
**[ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)**: Xander
retargeted it at `gpt-realtime-2`, the official Home Assistant MCP Server, web search
and a thin-client firmware for the Voice PE.

Since then it has gone its own way: a direct bridge to OpenAI in place of the Pipecat
pipeline, a second voice engine (GPT-Live), a secured device connection, announcements
from automations, and an Assist satellite integration. It no longer follows those
repositories, so it stands on its own on GitHub rather than as a fork of them. Their
history is kept here in full: every commit of Felix's and Xander's is in this
repository, under their names. The firmware still takes in ESPHome's own changes, as
`firmware/` is a subtree of
[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe).

Thank you to Felix and Xander: this is built on their work.

## Repository layout

- **[`openai_realtime_voice_agent/`](openai_realtime_voice_agent/)** — the Home
  Assistant add-on (Python).
  - [`DOCS.md`](openai_realtime_voice_agent/DOCS.md) — full setup: OpenAI key, the
    Home Assistant MCP connection, recommended settings, web search, all options.
  - [`CHANGELOG.md`](openai_realtime_voice_agent/CHANGELOG.md) — what changed per version.

- **[`custom_components/openai_realtime_voice_agent/`](custom_components/openai_realtime_voice_agent/)**
  — the Home Assistant integration, installed through HACS. It links the Voice PE
  to the add-on, and makes it an Assist satellite: Home Assistant's own announce
  and start conversation actions reach it, and its state follows the conversation.
  The add-on's discovery offer carries its port, the device secret, its certificate
  and a key for the link's fingerprint. The integration gives the device the
  secret, the certificate and an address: Home Assistant's own local IP with that
  port, or one set in the integration's options. A changed secret, certificate or
  port takes effect once you confirm it in Home Assistant. Its tests run in a real
  Home Assistant: [`integration_tests/`](integration_tests/).

- **[`firmware/`](firmware/)** — the Voice PE firmware (ESPHome, with a custom
  `va_client` component, specific to the Voice PE hardware). One image serves every
  Voice PE: CI builds it, and each GitHub Release publishes it on GitHub Pages, with
  its install page and the manifest Voice PEs update from; see
  [`firmware/README.md`](firmware/README.md).
  It is a `git subtree` of `esphome/home-assistant-voice-pe`, at its release
  26.9.0, with ESPHome's `voice_kit` component and sounds kept here too, so a
  firmware build comes from this repository alone. A later upstream release
  merges in with the `esphome` remote (`https://github.com/esphome/home-assistant-voice-pe`):
  `git fetch esphome --tags && git merge -s subtree -Xsubtree=firmware <release tag>`.
  That updates upstream's own files; their changes to the stock config are then
  ported into ours by hand, as listed by
  `python3 firmware/tools/upstream_changes.py <previous tag> <release tag>`.

## Install

1. In Home Assistant, open **Settings → Add-ons → Add-on store → ⋮ → Repositories**
   and add `https://github.com/craig-b/ha-openai-realtime`.
2. Install **OpenAI Realtime Voice Agent**. Home Assistant downloads the image
   that CI built and tested for its release (amd64 or aarch64).
3. Configure the add-on — see
   [`openai_realtime_voice_agent/DOCS.md`](openai_realtime_voice_agent/DOCS.md).
4. In HACS, add this repository as a custom repository of type **Integration**,
   install **OpenAI Realtime Voice Agent**, and restart Home Assistant.
5. Install the firmware on the Voice PE from the
   **[install page](https://craig-b.github.io/ha-openai-realtime/)** (Chrome or Edge,
   over USB), and give it your Wi-Fi.
6. Add the Voice PE when Home Assistant shows it as discovered by ESPHome.
7. Add the integration when Home Assistant shows it as discovered, as **OpenAI
   Realtime Voice Agent** (the add-on offers it about a minute after it finds the
   integration installed), and pick the Voice PE. Then press the Voice PE's centre
   button when Home Assistant asks.

The [INSTALL guide](firmware/INSTALL.md) has each step in full.

How a release is made, and its images and firmware published: [`RELEASING.md`](RELEASING.md).

## How it works

```
Voice PE (ESP32-S3)  ──WSS, 16 kHz PCM up──▶   this add-on    ──▶  OpenAI Realtime API
  va_client firmware  ◀──── 24 kHz PCM down───  (Python)           (gpt-realtime-2.1)
                                                                    or GPT-Live
                                                   │ tools
                                                   ▼
                                         Home Assistant MCP Server
```

The device does wake-word detection and XMOS audio cleanup locally and is a thin
client. Start a conversation with the wake word **"Alexa"** (the only one) or the
centre button. Stop it by saying **"stop"** or pressing the centre button: "stop"
works while the assistant replies, in the listening window after a reply, and
while a tool (a web search, a Home Assistant action) runs.

## Known limitations

- **No voice timers or alarms yet** — every other Assist action (lights, switches,
  scenes, climate) and online questions work.
- **With the Realtime engine, past 50 minutes a conversation moves to a new OpenAI
  session** at the next wake (OpenAI caps sessions at 60), which starts afresh.
- **Rarely, the assistant may stop itself** on a word in its own reply that sounds
  like "stop" — just ask again.

## Credits

- A hard fork of **[xandervanerven/ha-openai-realtime](https://github.com/xandervanerven/ha-openai-realtime)** and **[xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe)** (Xander van Erven); see [Where this comes from](#where-this-comes-from).
- Backend originally from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)** (a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)**, Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).

## License

MIT — see [LICENSE](LICENSE). The firmware in `firmware/` is under ESPHome's licence (MIT and GPLv3): see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
