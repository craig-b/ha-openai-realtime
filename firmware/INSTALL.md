# Install guide — Home Assistant Voice PE + OpenAI Realtime 2

A complete, from-zero walkthrough for a newcomer. You'll set up two halves:

1. the **backend add-on** (the voice "brain" that runs the OpenAI session), and
2. the **device firmware** (turns the Voice PE into a thin client that listens and speaks).

Plan ~30–45 minutes the first time. After that, updates are one click.

```
Home Assistant Voice PE          Home Assistant (your box)             Cloud
┌──────────────────────────┐ encrypted WS ┌────────────────────────┐  WS  ┌─────────────┐
│ custom firmware           │ ───────────▶│ OpenAI Realtime 2      │ ───▶ │ OpenAI       │
│  (va_client thin client)  │ 16k mic up  │  Voice Agent add-on    │ 24k  │ gpt-realtime │
│  wake word + XMOS DSP      │ ◀───────────│  (Python)              │ ◀─── │ or GPT-Live  │
└──────────────────────────┘ 24k spkr dn  │          │ tools         │     └─────────────┘
                                           ▼          ▼
                                  HA MCP Server (/api/mcp) → controls your home
```

Once it's set up you can **control your smart home by voice** (lights, switches,
scenes, climate), hold a **natural back-and-forth** conversation, and **look things
up online** (weather, news, facts) — web search is on by default.

## What you need

- A **Home Assistant Voice PE** device (this firmware is **only** for that hardware).
- **Home Assistant OS** (so you can install add-ons).
- An **OpenAI account** with **billing enabled** (the voice runs on OpenAI's paid API).
- A few minutes at the keyboard, and the device on the **same network** as Home Assistant.

---

## Part 1 — The backend add-on (the brain)

### 1.1 Add the repository & install

1. In Home Assistant: **Settings → Add-ons → Add-on Store**.
2. Top-right **⋮ → Repositories** → paste and add:
   `https://github.com/craig-b/ha-openai-realtime`
3. Find **OpenAI Realtime 2 Voice Agent** in the store and click **Install**.
   Home Assistant builds it locally — this takes a few minutes the first time.

### 1.2 Add your OpenAI API key

1. Go to <https://platform.openai.com/> → **API keys** → **Create new secret key**,
   and make sure **billing** is enabled on the account.
2. Open the add-on's **Configuration** tab and paste the key into **`openai_api_key`**.
3. Set **`device_secret`** to a random string of at least 16 characters with no
   spaces, e.g. the output of `openssl rand -hex 32`. Keep it to hand: the device
   needs the same value (step 2.3). The add-on refuses any connection without it,
   and doesn't start until it's set.

> New OpenAI accounts start on a low rate-limit tier. If you later see *"Rate limit
> reached"* in the log, raise your usage tier on the OpenAI dashboard.

### 1.3 Let it control your home (Home Assistant MCP)

The assistant controls your home through Home Assistant's official, built-in
**[MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration —
that's what lets the voice turn your lights, switches, scenes and climate on and off.

1. Add it: **Settings → Devices & Services → Add Integration**, search **"Model Context
   Protocol Server"**, and add it
   ([one-click add](https://my.home-assistant.io/redirect/config_flow_start/?domain=mcp_server)).
2. **Settings → Voice assistants → Expose** → tick the lights, switches, scenes and
   climate you want to control by voice. **Only exposed entities are controllable** —
   this is your safety boundary.
3. In the add-on Configuration, leave **`ha_mcp_url`** and **`longlived_token`**
   **blank**. The add-on then uses Home Assistant's built-in MCP endpoint with its own
   token. (Only fill `longlived_token` if the log shows a 401/403 on
   `/core/api/mcp` when you talk to the assistant.)

### 1.4 Recommended starting settings

Set these on the **Configuration** tab (each option has inline help text). This is a
known-good starting point — you can fine-tune later. `realtime → …` options are in
the **⚡ Realtime settings** panel:

| Option | Value |
|---|---|
| `engine` | `realtime` (or `live` for GPT-Live; see the add-on's DOCS.md, section 5) |
| `realtime → model` | `gpt-realtime-2.1` |
| `realtime → voice` | `marin` (or `cedar`) |
| `realtime → speed` | `1.0` |
| `realtime → reasoning_effort` | `low` |
| `realtime → max_output_tokens` | `0` (unlimited) |
| `realtime → noise_reduction` | `off` |
| `realtime → vad_eagerness` | `low` |
| `follow_up_listen_seconds` | `8` |
| `follow_up_open_delay_ms` | `700` |
| `wake_open_delay_ms` | `700` |
| `playback_prebuffer_ms` | `150` |
| `realtime → max_context_tokens` | `8000` |
| `mcp_tool_allowlist` | **blank** (use all of the official server's tools — it's already a small set) |
| `enable_web_search` | `true` (online lookups on by default; set `false` to disable) |
| `realtime → web_search_model` | `gpt-6-luna` (the quickest and cheapest) |
| `house_rules` | the English default: the assistant's personality and language (to change language, see Part 5) |

### 1.5 Start it

Click **Start**, then open the **Log** tab. A healthy start shows
`✅ Listening for the device on wss://0.0.0.0:8080/`. The add-on reads Home
Assistant's tools each time a conversation starts, so the first time you talk to
it (Part 3) the log shows `🔧 Home Assistant offers N tools` with `Hass*` names.

The log also prints the add-on's **certificate**, a block from
`-----BEGIN CERTIFICATE-----` to `-----END CERTIFICATE-----`. The connection to
the device is encrypted with it, and the device only connects to an add-on with
exactly this certificate. Copy the whole block for step 2.3. The add-on makes it
on first start and keeps it, in `certificate.pem` in the `tls` folder of its
`addon_configs` folder.

---

## Part 2 — The device firmware

This replaces the stock Home Assistant voice pipeline on the Voice PE with a thin
client that streams audio to the add-on. You set it up **once** via a tiny "stub"
config; after that, firmware updates are one click (no copy-pasting).

### 2.1 Install the ESPHome Builder add-on

You build and flash the firmware with the **ESPHome Device Builder** add-on — the
official [ESPHome](https://esphome.io/) tool that runs inside Home Assistant. You need
it to adopt the device and flash this firmware onto it.

1. Open **Settings → Add-ons → Add-on Store**, search **ESPHome Device Builder**, and
   click **Install**
   ([one-click open](https://my.home-assistant.io/redirect/supervisor_addon/?addon=5c53de3b_esphome)).
2. Enable **Show in sidebar**, then **Start** → **Open Web UI**.

### 2.2 Adopt the Voice PE

1. The Voice PE (on its stock firmware) should appear in ESPHome Builder as a
   **discovered device**. If it doesn't, add it by its `home-assistant-voice-xxxx.local`
   address.
2. Click **Adopt**. ESPHome creates a device entry. **Don't install the stock config
   yet.**
3. ESPHome generates an **API encryption key** and an **OTA password** for the device —
   note both; you'll put them in `secrets.yaml` next.

### 2.3 Add your secrets

In ESPHome Builder → **Secrets** (top-right ⋮), add:

```yaml
wifi_ssid: "Your-WiFi"
wifi_password: "your-wifi-password"
ota_password: "the-OTA-password-from-step-2.2"
api_key: "the-API-encryption-key-from-step-2.2"   # 44-char base64
va_secret: "the-device_secret-from-step-1.2"
va_certificate: |
  -----BEGIN CERTIFICATE-----
  the lines of the certificate from step 1.5, indented like this
  -----END CERTIFICATE-----

# Optional — ONLY if you want a fixed IP (otherwise the device uses DHCP):
# static_ip: "192.168.1.50"
# gateway:   "192.168.1.1"
# subnet:    "255.255.255.0"
# dns1:      "1.1.1.1"
# dns2:      "1.0.0.1"
```

> The firmware itself is pulled from the **public** repo at build time — no token needed.

### 2.4 Paste the device stub & flash

1. In ESPHome Builder, **Edit** the adopted device and **replace its entire YAML** with
   a ready-made stub from the repo's `firmware/` folder:
   - DHCP: [`esphome-builder.dhcp.yaml`](https://github.com/craig-b/ha-openai-realtime/blob/main/firmware/esphome-builder.dhcp.yaml)
   - Fixed IP: [`esphome-builder.static-ip.yaml`](https://github.com/craig-b/ha-openai-realtime/blob/main/firmware/esphome-builder.static-ip.yaml)

   Set `name` and `friendly_name`, and **keep** the `packages:` / `dashboard_import:`
   lines — those are what pull the full firmware from the repo. Save.

   > Optional: if your add-on isn't reachable at `wss://homeassistant.local:8080/`, add a
   > `va_url:` line under `substitutions:` with your HA host, e.g.
   > `va_url: "wss://192.168.1.10:8080/"`.

2. Click **Install →**
   - **First time:** choose **Plug into this computer** — the first flash from stock
     firmware needs the device connected by **USB** to the machine running your browser.
   - **After that:** **Wirelessly (OTA)** — every later flash goes over Wi-Fi.

That's it — the device boots, connects to the add-on, and you're ready to talk to it.

---

## Part 3 — First run & verify

1. After boot, the LED ring should settle to **idle (blue)** — that means the device
   reached the add-on's WebSocket.
2. Say **"alexa"** → a wake chime plays and the ring turns to **listening**.
3. Ask for something you exposed, e.g. *"turn on the bedroom lamp"* → the ring shows
   **thinking** → it acts and replies.
4. To interrupt a reply: say **"stop"** or press the **center button**.

**If something's off, check the logs:**
- Add-on **Log** tab: `🤖 assistant:` lines (and `🗣️ user:` lines, with
  `log_what_you_say` on, or always with GPT-Live), tool calls, `📞 phase ->` changes,
  and `🔌 opening an OpenAI session` / `🔌 OpenAI session closed`. If the device
  can't connect, the device log says whether `va_secret` was refused.
- Device logs: ESPHome Builder → your device → **Logs**.

---

## Part 4 — Updating later (one click)

- **Firmware:** when a new version is released, ESPHome Builder shows **"Update
  available"** for your device. Click it → it recompiles with the latest config + code
  and flashes over Wi-Fi. No copy-pasting, ever again.
- **Add-on:** Home Assistant shows an **Update** badge on the add-on (with a changelog).
  Click **Update** — it rebuilds and restarts.

Your device-specific settings (name, Wi-Fi, IP) live in your stub + `secrets.yaml` and
are **never** overwritten by an update. That also means an update that needs a new
secret can't add it for you: when the changelog says so, add it to `secrets.yaml` and
your stub yourself, as in Part 2. Updating from a version without the device secret
needs exactly that: `device_secret` in the add-on; `va_secret` and `va_certificate` in
`secrets.yaml` and in the stub's `substitutions:`; and `wss://` in place of `ws://` in
any `va_url` you set.

---

## Part 5 — Tuning tips

- **It hears its own tail** (a stray turn right after a reply, or it repeats
  its last answer): the default `follow_up_open_delay_ms` of `700` normally
  prevents this — raise it further (~1000) if it still happens.
- **Crackle at the start of a reply:** raise `playback_prebuffer_ms` above the
  default 150 (e.g. ~250).
- **See what it understood:** turn on `log_what_you_say` (🔍 Debug) for `🗣️ user: …` lines in
  the add-on log.
- **Web search:** it answers weather / news / facts online out of the box
  (`enable_web_search` is on by default, model `gpt-6-luna`; set it `false` if you
  don't want it). See the add-on's DOCS.md.
- **House rules (`house_rules`)** are your part of the assistant's prompt: its
  personality, the language it speaks, and anything particular to your home. They
  come after the add-on's built-in rules (short spoken replies, silent tool calls,
  looking devices up by name, ending the conversation), which ship with the add-on
  and improve with its updates, and they win on personality, tone, language and
  naming. The default:

  ```
  You are cheerful and friendly. Speak and understand only English, with a natural,
  neutral accent. Never switch language, not even for stray foreign words or an
  accent; do not infer the language from accent or a single word.
  ```

  To use another language, lock that language instead. Example (Dutch):

  ```
  Je bent vrolijk en vriendelijk. Spreek en versta uitsluitend Nederlands, met een
  natuurlijke, neutrale tongval. Wissel nooit van taal, ook niet bij losse Engelse
  woorden ("living", "dimmer") of een accent.
  ```

---

## Credits

- Backend forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on the **OpenAI Realtime API** and the official **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration.
