# OpenAI Realtime 2 Voice Agent — Documentation

This add-on runs an OpenAI voice session — **`gpt-realtime-2.1`** by default, or
**GPT-Live** (section 5) — and bridges it to Home Assistant control and web search. It is the backend half of a two-part project; the
front half is custom **firmware for the Home Assistant Voice PE** device (see
[Firmware](#firmware-home-assistant-voice-pe-only) below).

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
3. Find **OpenAI Realtime 2 Voice Agent** in the store and click **Install**.
   (There is no prebuilt image — Home Assistant builds it locally the first time,
   which takes a few minutes.)
4. Open the add-on's **Configuration** tab to set it up (next sections).

## 2. Get an OpenAI API key and choose a device secret

1. Go to <https://platform.openai.com/> → **API keys** → **Create new secret key**.
2. Make sure **billing** is set up on your OpenAI account — `gpt-realtime-2.1` (audio)
   and web search are paid usage.
3. Paste the key into the add-on's **`openai_api_key`** option.
4. Set **`device_secret`** to a random string of at least 16 characters with no
   spaces, e.g. the output of `openssl rand -hex 32`. Your Voice PE sends the same
   value (`va_secret` in your ESPHome secrets) to connect; the add-on refuses any
   connection without it, so nothing else on your network can use your OpenAI key
   or your home's controls through the add-on. The add-on doesn't start until it's
   set.

The connection to the device is also encrypted, so nobody on your network can
listen to your microphone or read the secret. The add-on makes its own
certificate on first start, keeps it, and prints it in its log at every start;
the device needs it as `va_certificate` in your ESPHome secrets (see the
firmware's INSTALL guide).

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

You get a small fixed set of Assist tools (`HassTurnOn`, `HassTurnOff`,
`HassLightSet`, `GetLiveContext`, `GetDateTime`, …). **`GetLiveContext`** is the
"what's the current state?" tool — keep it; it's what answers *"is the light on?"*.

**`mcp_tool_allowlist`** (optional): a comma-separated whitelist of tool names. Leave
blank to expose all, or trim to just what you use, e.g.:
`HassTurnOn,HassTurnOff,HassLightSet,GetLiveContext,GetDateTime`

## 4. Recommended starting settings

**The defaults are the recommended settings** — for a first run you only need the
API key, the device secret, and the MCP integration (section 3). The
Configuration tab is grouped: **🔑 Basics → ⚡ Realtime settings → 🎙️ GPT-Live settings → 💬 Conversation →
🌐 Web search → 🎚️ Audio → 🏠 Home Assistant → ⚙️ Advanced → 🔍 Debug**, and every
option has plain-language inline help. The **⚡ Realtime settings** are a panel of
their own (`realtime → …` below): the model, its voice and everything else only the
Realtime models use; the **🎙️ GPT-Live settings** are its counterpart for the GPT-Live
engine (section 5). Settings saved before the panels existed are moved into them
automatically the first time the add-on starts, after which it restarts once.

| Option | Default | Note |
|---|---|---|
| `realtime → model` | `gpt-realtime-2.1` | newest speech-to-speech model |
| `realtime → voice` | `marin` | `marin`/`cedar` are the newest voices |
| `realtime → reasoning_effort` | `low` | how long it thinks before answering; `minimal` is quickest |
| `realtime → instructions` | *(English default)* | the system prompt; swap the LANGUAGE line for your language |
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
| `realtime → web_search_model` | `gpt-5.5` | best-quality search model; mini/nano are cheaper |

The legacy `server_vad` turn-detection fields are at the bottom of the ⚡ Realtime
settings — leave them empty unless you have a specific reason.

## 5. Choosing an engine: Realtime or GPT-Live

The **Engine** option (🔑 Basics) picks which kind of OpenAI voice model runs the
conversation. Each has its own settings panel; only the chosen engine's are used.

| | Realtime (default) | GPT-Live |
|---|---|---|
| How it works | one model (`gpt-realtime-2.1`) listens, thinks and speaks, in turns | a voice (`gpt-live-1`) that listens and speaks at once, and hands anything that needs doing to a backend model (`gpt-6-luna`), which calls the tools |
| Prompts | one: `realtime → instructions` | two: the voice's (`live → instructions`) and the backend's (`live → backend_instructions`) |
| Billing | per token of audio and text | $0.05 per minute a conversation is open, plus the backend's tokens |
| Between conversations | the session stays open for `realtime → session_reuse_timeout_seconds` (300), costing nothing | closes as soon as the conversation ends (`live → session_reuse_timeout_seconds`, 0), since open minutes are billed |
| Web search | the add-on's own search call, on `realtime → web_search_model` | the backend model's own web search |
| Log of what you said | with `log_what_you_say` | always |

With GPT-Live the light ring shows what is really happening rather than turns:
**listening** while it hears you, **thinking** while the backend works, **replying**
while its voice is audible. It often acknowledges a request at once ("Okay, turning
on the kitchen light") and confirms when the backend is done; the backend prompt
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
- Default model `gpt-5.5` (best quality). Cheaper options trade price/quality
  (`gpt-5.4`, `gpt-5-mini`, the nano models, …) — a few cents per search.
- Adds ~1–3 s while it searches (the device shows "thinking").
- If the model name is rejected, the assistant just says it couldn't search — it
  won't crash the session, so you can change `realtime → web_search_model` and retry.

## 7. Options reference & tuning

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

## 8. Reading the logs

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

**Using "stop":** say "stop" (or press the center button) to interrupt the assistant
*while it's speaking* — during a reply, or during the short listening window right
after one. It has no effect before the assistant has started answering (there's
nothing to stop yet).

## Firmware (Home Assistant Voice PE only)

This add-on expects the custom **Voice PE firmware** that turns the device into a
thin client (it streams mic audio here and plays the reply). That firmware:

- is **specific to the Home Assistant Voice PE** hardware (ESP32-S3 + XMOS), and
- lives in the **[`firmware/`](https://github.com/craig-b/ha-openai-realtime/tree/main/firmware)** folder of this add-on's repository.

You flash it once via a tiny per-device "stub" in ESPHome Builder; after that, firmware
updates are **one click** — no tokens, no copy-pasting. The [INSTALL guide](https://github.com/craig-b/ha-openai-realtime/blob/main/firmware/INSTALL.md) has the full
from-scratch guide (flashing + adopting the device in ESPHome Builder).

## Credits

- Backend forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on the **OpenAI Realtime API**, **GPT-Live**, and the official **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration.
