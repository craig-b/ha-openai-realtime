# Install guide — Home Assistant Voice PE + OpenAI Realtime 2

A complete, from-zero walkthrough for a newcomer. You'll set up three parts:

1. the **backend add-on** (the voice "brain" that runs the OpenAI session),
2. the **integration**, which links the Voice PE to the add-on and makes it an
   Assist satellite, and
3. the **device firmware** (turns the Voice PE into a thin client that listens and
   speaks), installed from your browser.

After that, updates come to Home Assistant like any other.

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
- **Home Assistant OS** (so you can install add-ons), with **[HACS](https://hacs.xyz/)**
  (to install the integration).
- An **OpenAI account** with **billing enabled** (the voice runs on OpenAI's paid API).
- For the first install: a computer with **Chrome or Edge**, and a **USB-C cable that
  carries data**.
- The device on the **same network** as Home Assistant.

---

## Part 1 — The backend add-on (the brain)

### 1.1 Add the repository & install

1. In Home Assistant: **Settings → Add-ons → Add-on Store**.
2. Top-right **⋮ → Repositories** → paste and add:
   `https://github.com/craig-b/ha-openai-realtime`
3. Find **OpenAI Realtime 2 Voice Agent** in the store and click **Install**.
   Home Assistant downloads its prebuilt image.

### 1.2 Add your OpenAI API key

1. Go to <https://platform.openai.com/> → **API keys** → **Create new secret key**,
   and make sure **billing** is enabled on the account.
2. Open the add-on's **Configuration** tab and paste the key into **`openai_api_key`**.
3. Set **`device_secret`** to a random string of at least 16 characters with no
   spaces, e.g. the output of `openssl rand -hex 32`. The add-on refuses any
   connection without it, and doesn't start until it's set. You don't need to keep
   it: the integration gives it to the device (Part 2).

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
| `house_rules` | the English default: the assistant's personality and language (to change language, see Part 7) |

### 1.5 Start it

Click **Start**, then open the **Log** tab. A healthy start shows
`✅ Listening for the device on wss://0.0.0.0:8080/`. The add-on reads Home
Assistant's tools each time a conversation starts, so the first time you talk to
it (Part 4) the log shows `🔧 Home Assistant offers N tools` with `Hass*` names.

The log also prints the add-on's **certificate**, a block from
`-----BEGIN CERTIFICATE-----` to `-----END CERTIFICATE-----`. The connection to
the device is encrypted with it, and the device only connects to an add-on with
exactly this certificate. The integration gives it to the device, so there's
nothing to copy (unless you build the firmware yourself, Part 6). The add-on makes
it on first start and keeps it, in `certificate.pem` in the `tls` folder of its
`addon_configs` folder.

---

## Part 2 — The integration

The integration links the Voice PE to the add-on: it gives the device the add-on's
address, the device secret and the certificate, and gives them again whenever they
change. It also makes the Voice PE an **Assist satellite**, so Home Assistant's
announce and start conversation actions reach it (see the add-on's DOCS.md,
section 7).

1. In **HACS → ⋮ → Custom repositories**, add
   `https://github.com/craig-b/ha-openai-realtime` with the type **Integration**.
2. Install **OpenAI Realtime Voice Agent**, then **restart Home Assistant**.

You add it in Part 3, once the Voice PE is in Home Assistant.

---

## Part 3 — The device firmware

This replaces the stock Home Assistant voice pipeline on the Voice PE with a thin
client that streams audio to the add-on. There is one firmware for every Voice PE,
with nothing of yours in it: Home Assistant sets up the rest.

### 3.1 Install it from your browser

1. Open the **[install page](https://craig-b.github.io/ha-openai-realtime/)** in
   Chrome or Edge on a computer.
2. Connect the Voice PE to the computer with the USB-C cable, press **Install**, pick
   the Voice PE's port, and confirm. It replaces the firmware on the device.
3. When it's done, the installer offers to connect the Voice PE to your **Wi-Fi**:
   pick your network and enter its password.

   If you skip it, set Wi-Fi up over Bluetooth instead: with Bluetooth on your Home
   Assistant, it finds the Voice PE nearby (**Improv via BLE**). Start the setup there,
   and press the Voice PE's **centre button** when its lights twinkle warm white.

### 3.2 Add it to Home Assistant

1. Under **Settings → Devices & services**, Home Assistant shows the Voice PE as
   discovered by **ESPHome**, as `home-assistant-voice-` and six characters of its
   address. **Add** it and confirm. Home Assistant gives the device its own
   encryption key as it adds it.
2. The integration then shows up as discovered, under the Voice PE's name: the
   add-on offers it. **Add** it. The confirmation names the add-on the offer comes
   from and its certificate's SHA-256 fingerprint: check it matches the
   `🔐 … fingerprint` line in the add-on's **Log**, then confirm.

### 3.3 Press the centre button

The first time, the Voice PE takes its link to the add-on only once someone presses
its centre button: that's how the device knows someone at it agreed to it. Its
lights twinkle warm white, and Home Assistant shows a repair, **"Press the centre
button on …"** (under **Settings → System → Repairs**, and on the Settings badge).
Press the button once.

The Voice PE connects to the add-on and its light ring settles to **idle**.

> If the repair says **"Don't press the centre button"**, something else in Home
> Assistant set a link on the device before the integration did. Don't press it:
> restart the Voice PE (unplug it and plug it back in), and the integration's link
> takes its place.

### Coming from a firmware you built yourself

A Voice PE that ran a firmware built in ESPHome Device Builder (Part 6) moves to
this one with a USB install from the install page, as in 3.1. Then delete the
device from ESPHome Device Builder, so it stops offering its own updates. Home
Assistant keeps the device; if it asks about its encryption key, follow its
prompt.

---

## Part 4 — First run & verify

1. The light ring settles to **idle** once the device reaches the add-on. On the
   Voice PE's device page in Home Assistant, **Add-on connection** says `connected`.
2. Say **"alexa"** → a wake chime plays and the ring turns to **listening**.
3. Ask for something you exposed, e.g. *"turn on the bedroom lamp"* → the ring shows
   **thinking** → it acts and replies.
4. To interrupt a reply: say **"stop"** or press the **center button**.

**If something's off:**
- **Add-on connection** on the Voice PE's device page says where it stands:
  `not linked` (the integration hasn't linked it yet: is it set up, and the add-on
  running?), `awaiting button` (press the centre button), `connecting` (the add-on
  isn't reachable at the address it was given), or `refused` (the add-on turned down
  its secret; the integration gives it the new one).
- The integration connects the device to Home Assistant's own address on your
  network. If the device needs another address for the add-on, set it in the
  integration's **Configure** dialog.
- Add-on **Log** tab: `🤖 assistant:` lines (and `🗣️ user:` lines, with
  `log_what_you_say` on, or always with GPT-Live), tool calls, `📞 phase ->` changes,
  and `🔌 opening an OpenAI session` / `🔌 OpenAI session closed`.

---

## Part 5 — Updating later

- **Firmware:** when a new version is released, Home Assistant shows a firmware
  **update** for the Voice PE (**Settings → Updates**). Install it there: it
  downloads the released firmware onto the device over Wi-Fi.
- **Add-on:** Home Assistant shows an **Update** badge on the add-on (with a changelog).
  Click **Update**.
- **Integration:** HACS shows its update; install it and restart Home Assistant.

Your Wi-Fi, the device's encryption key and its link to the add-on live on the
device, and an update keeps them. When the add-on's secret, certificate or address
changes, the integration gives the device the new ones.

---

## Part 6 — Building the firmware yourself

Most people don't need this: the prebuilt firmware is the same config, built by
this repository's CI for each release. Build it yourself to change it, or to keep
Wi-Fi, the API key or the link to the add-on in the build rather than setting them
at runtime. You build it in the **ESPHome Device Builder** add-on, from a small
per-device stub that takes the firmware from this repository as a package, and
updates come from Builder (**Update**, which recompiles), not from Home Assistant's
firmware update.

1. Install **ESPHome Device Builder** (**Settings → Add-ons → Add-on Store**,
   [one-click open](https://my.home-assistant.io/redirect/supervisor_addon/?addon=5c53de3b_esphome)),
   and open its web UI.
2. In **Builder → Secrets** (top-right ⋮), add the keys your stub uses, from
   [`secrets.yaml.example`](secrets.yaml.example). Always `ota_password`, the
   password for Builder's uploads to the device; the rest only for the blocks
   you keep:
   - `wifi_ssid`, `wifi_password`: Wi-Fi in the build.
   - `api_key`: your own API encryption key (`openssl rand -base64 32`), which you
     give Home Assistant when you add the device.
   - `va_url`, `va_secret`, `va_certificate`: the link to the add-on in the build:
     its address (`wss://`, Home Assistant's address, the add-on's port), its
     `device_secret`, and the certificate its log prints.
3. Create a device, and replace its YAML with a stub:
   [`esphome-builder.dhcp.yaml`](esphome-builder.dhcp.yaml), or
   [`esphome-builder.static-ip.yaml`](esphome-builder.static-ip.yaml) for a fixed IP
   (with `static_ip`, `gateway`, `subnet`, `dns1`, `dns2` in your secrets). Set
   `name` and `friendly_name`, keep the `packages:`, `ota:` and `dashboard_import:`
   blocks, and delete the optional blocks you don't want: whatever you leave out is
   set as with the prebuilt firmware.
4. **Install → Plug into this computer** the first time onto a device running the
   prebuilt firmware, which takes no uploads from Builder. After that, Builder's
   **Install → Wirelessly** works, with your `ota_password`.

A link in the build counts as the device's first link, so no button press is
needed; the integration still gives it a new one when the add-on's secret,
certificate or address changes.

---

## Part 7 — Tuning tips

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
  You are calm and brief, like a good concierge: warm, never chatty. Speak and
  understand only English, with a natural, neutral accent. Never switch language, not
  even for stray foreign words or an accent; do not infer the language from accent or
  a single word.
  ```

  To use another language, lock that language instead. Example (Dutch):

  ```
  Je bent rustig en kort van stof, als een goede conciërge: hartelijk, nooit
  babbelziek. Spreek en versta uitsluitend Nederlands, met een natuurlijke, neutrale
  tongval. Wissel nooit van taal, ook niet bij losse Engelse woorden ("living",
  "dimmer") of een accent.
  ```

---

## Credits

- Backend forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on the **OpenAI Realtime API** and the official **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration.
