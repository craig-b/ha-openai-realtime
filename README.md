<p align="center">
  <img src="openai_realtime_voice_agent/icon.png" alt="OpenAI Realtime 2 Voice Agent — a stylised Voice PE" width="160"/>
</p>

# OpenAI Realtime 2 Voice Agent (Home Assistant Voice PE)

> [!IMPORTANT]
> **You need both halves, and both live in this repo.** The stock Home Assistant voice
> pipeline won't talk to this add-on; it needs the custom Voice PE firmware:
> - 🧠 **Backend add-on** → [`openai_realtime_voice_agent/`](openai_realtime_voice_agent/) — runs inside Home Assistant
> - 🔌 **Device firmware** → [`firmware/`](firmware/) — flashed onto the Voice PE
>
> 📖 New here? The full **[INSTALL guide](firmware/INSTALL.md)** walks through both halves, step by step.

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

> Fork of **[xandervanerven/ha-openai-realtime](https://github.com/xandervanerven/ha-openai-realtime)**
> and **[xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe)**,
> merged into one repo. Xander's backend is itself a fork of
> **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)**,
> retargeted at `gpt-realtime-2`, the official HA MCP Server, optional web search,
> and the **Voice PE thin-client firmware**.

## Repository layout

- **[`openai_realtime_voice_agent/`](openai_realtime_voice_agent/)** — the Home
  Assistant add-on (Python).
  - [`DOCS.md`](openai_realtime_voice_agent/DOCS.md) — full setup: OpenAI key, the
    Home Assistant MCP connection, recommended settings, web search, all options.
  - [`CHANGELOG.md`](openai_realtime_voice_agent/CHANGELOG.md) — what changed per version.

- **[`custom_components/openai_realtime_voice_agent/`](custom_components/openai_realtime_voice_agent/)**
  — an optional Home Assistant integration, installed through HACS, that makes the
  add-on's Voice PE an Assist satellite: Home Assistant's own announce and start
  conversation actions reach it, and its state follows the conversation. Its tests
  run in a real Home Assistant: [`integration_tests/`](integration_tests/).

- **[`firmware/`](firmware/)** — the Voice PE firmware (ESPHome, with a custom
  `va_client` component, specific to the Voice PE hardware). ESPHome builds it
  straight from this repo; see [`firmware/README.md`](firmware/README.md).
  It is a `git subtree` of `esphome/home-assistant-voice-pe`, so upstream
  firmware fixes merge in with
  `git fetch esphome && git subtree merge --prefix=firmware esphome/dev`.

## Install

1. In Home Assistant, open **Settings → Add-ons → Add-on store → ⋮ → Repositories**
   and add `https://github.com/craig-b/ha-openai-realtime`.
2. Install **OpenAI Realtime 2 Voice Agent**. Home Assistant downloads the image
   that CI built and tested for its release (amd64 or aarch64).
3. Configure the add-on and flash the companion firmware — see
   [`openai_realtime_voice_agent/DOCS.md`](openai_realtime_voice_agent/DOCS.md).
4. Optionally, to make the Voice PE an Assist satellite: in HACS, add this repository
   as a custom repository of type **Integration**, install **OpenAI Realtime Voice
   Agent**, and restart Home Assistant. It then shows up as discovered under
   **Settings → Devices & services**; pick the Voice PE. See section 7 of DOCS.md.

How a release is made, and its images published: [`RELEASING.md`](RELEASING.md).

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
client. Interrupt a reply with the **"stop"** word or the center button.

## Known limitations

- **No voice timers or alarms yet** — every other Assist action (lights, switches,
  scenes, climate) and online questions work.
- **With the Realtime engine, past 50 minutes a conversation moves to a new OpenAI
  session** at the next wake (OpenAI caps sessions at 60), which starts afresh.
- **Rarely, the assistant may stop itself** on a word in its own reply that sounds
  like "stop" — just ask again.

## Credits

- Forked from **[xandervanerven/ha-openai-realtime](https://github.com/xandervanerven/ha-openai-realtime)** and **[xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe)**.
- Backend originally from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)**.
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)** (a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)**, Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).

## License

MIT — see [LICENSE](LICENSE). The firmware in `firmware/` is under ESPHome's licence (MIT and GPLv3): see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
