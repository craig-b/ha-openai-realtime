# Changelog

All notable changes to this add-on. Newest first.

## 0.11.1

Update the add-on, and the integration in HACS (then restart Home Assistant). The
firmware is unchanged.

- **The integration takes the add-on's link only from the add-on itself.** Any
  installed add-on can make an offer to the integration, and the integration signs
  the link it takes for the Voice PE, so another add-on could have redirected the
  device's microphone to itself. The integration now records which add-on its link
  came from and turns other add-ons' offers away. A new or changed link waits for
  you to confirm it, with the add-on and the link's fingerprint, and an offer not
  in the form the device takes is refused. If the integration is set up already,
  it records the add-on the next time the add-on offers the link it has, when the
  add-on starts. *(integration)*
- **The add-on logs its link's fingerprint** at every start (its port, the device
  secret and its certificate), to check against the one Home Assistant shows when
  it asks you to confirm a link.

## 0.11.0

**If you build the firmware in ESPHome Device Builder, don't click its Update with
your current stub.** The firmware no longer takes Wi-Fi, the API key or uploads from
that stub: the device would come back off your Wi-Fi (set it up again over
Bluetooth, INSTALL guide, step 3.1). Instead:

1. Update the add-on, and the integration in HACS (then restart Home Assistant).
   The integration is no longer optional: it's what links the Voice PE to the
   add-on.
2. Move the Voice PE to the prebuilt firmware: install it from the
   [install page](https://craig-b.github.io/ha-openai-realtime/) over USB (INSTALL
   guide, Part 3), then delete the device from ESPHome Device Builder. Or, to keep
   building it yourself, replace your stub with the new one first (INSTALL guide,
   Part 6).
3. Press the Voice PE's centre button when Home Assistant asks.

- **Prebuilt firmware.** One firmware for every Voice PE, installed from your
  browser at the [install page](https://craig-b.github.io/ha-openai-realtime/), with
  nothing of yours in it: you set Wi-Fi in the installer or over Bluetooth, Home
  Assistant sets the device's encryption key as it adds it, and the integration
  links it to the add-on. Its updates come to Home Assistant as a firmware update
  for the Voice PE, with each release. *(firmware)*
- **The integration links the Voice PE to the add-on:** the add-on's address, the
  device secret and its certificate, from the add-on's offer, and again whenever
  they change, so there's nothing to copy into the device. The first time, the
  device takes the link only once its centre button is pressed, and Home Assistant
  asks for the press. Once linked, it takes a new link only from the integration
  that set the last one. *(integration, firmware)*
- **The device's link settings show on its page:** "Add-on link" (a hash of the
  add-on's address and certificate) and "Add-on connection" (connected,
  connecting, refused, awaiting button, or not linked). *(firmware)*
- **The firmware takes in ESPHome's Voice PE release 26.9.0:** a switch to disable
  the buttons, and its LED and button fixes. *(firmware)*

## 0.10.0

Update the add-on, the integration in HACS (then restart Home Assistant), and the
firmware (ESPHome Device Builder → Update). If the integration is set up already,
nothing changes for it: this is for setting it up.

- **The integration shows up as discovered under your Voice PE's name,** and adding
  it only asks you to confirm: the Voice PE now tells the add-on which device it is
  (its MAC address, by which Home Assistant knows it), and the add-on passes that
  on. With older firmware you pick the Voice PE, as before. *(firmware)*

## 0.9.2

Update the add-on. The integration and the firmware are unchanged.

- **The satellite's state goes back to idle after a stop.** Stopping the assistant
  while it was working on an answer, or a follow-up window closing unanswered, left
  the Assist satellite showing the last state, such as "listening", until the next
  conversation.

## 0.9.1

Update the add-on, and the integration in HACS (then restart Home Assistant). The
firmware is unchanged.

- **The integration now adds the Voice PE's Assist satellite.** It failed to: the
  ESPHome integration knows the Voice PE by its MAC address alone, which the
  integration didn't expect. *(integration)*
- **The add-on stays connected to Home Assistant through its restarts.** When Home
  Assistant restarted, the add-on stopped hearing automations' requests and stopped
  reporting the device's state, until the add-on itself was restarted. It now
  reconnects whatever ended the connection, and logs each outage.

## 0.9.0

Update the add-on. The new integration is optional: to make the Voice PE an Assist
satellite, install it through HACS (Documentation tab, section 7). The firmware is
unchanged.

**The Voice PE as an Assist satellite** *(new integration, through HACS)*

- **Home Assistant's own Announce and Start conversation actions reach the Voice
  PE,** with a form in the action editor, and wait until it has been said.
  Blueprints made for voice satellites work too.
- **Its state follows the conversation:** idle, listening, processing or
  responding, and unavailable while the Voice PE isn't connected to the add-on.
- **No Assist pipeline needed:** the OpenAI model speaks and converses, not Home
  Assistant's text-to-speech or agent.
- **It shows up as discovered** once it's installed: the add-on offers it.

**Starting a conversation**

- **It can open with a line of yours, word for word:** the start-conversation event
  takes a `message`, with the `prompt` as what the conversation is about.

## 0.8.1

Update the add-on; there is nothing else to do. Firmware 1.4.1 only changes a log
line, so there's no need to update the device.

- **A question asked right after waking the assistant is answered.** When the mic
  had closed while OpenAI still heard speech, OpenAI reported only the end of the
  next question, and the add-on threw that question away as a leftover: no
  answer, and the device gave up after a few seconds. Every wake now clears
  OpenAI's input, which also resets its turn detection, and the guard that
  guessed is gone.
- **A conversation an automation starts opens with one sentence** ending with
  its question, without an offer tacked on.

## 0.8.0

> **Two steps, in this order:**
>
> 1. **Update this add-on.** On its first start it moves your saved prompts and
>    restarts itself once: a prompt you never edited gives way to the add-on's
>    built-in rules; one you had edited becomes your **House rules**, and the log
>    asks you to trim it to what is yours.
> 2. **Update the firmware** (ESPHome Device Builder → Update), for
>    announcements. Everything else works on the old firmware.

House rules, a less chatty assistant, and announcements from automations.

**House rules**

- **Your part of the prompt is now one setting, House rules** (🔑 Basics): the
  assistant's personality, its language, and anything particular to your home.
  Both engines get it.
- **The rest is built in:** using the tools without announcing it, looking devices
  up by name before asking, ending the conversation, speaking for a speaker. It
  ships with the add-on, so fixes to it reach you with updates; before, a saved
  prompt kept its old text for good. Your house rules win on personality, tone,
  language and naming. With `log_level: DEBUG` the log shows the whole prompt.

**A less chatty assistant**

- **One short sentence by default,** answering only what was asked: no "Let me
  check…" before a tool, no "If you'd like, I can…" after.
- **It looks a device up by name** when a room has none, instead of asking with
  made-up names.
- **It names the place** when an answer depends on one, such as the weather, and
  says goodbye in a word or two.

**Announcements and conversations from automations**

- **An automation can have the assistant speak,** in its own voice, by firing an
  event: `openai_realtime_voice_agent_announce` with a `message` says it word for
  word; `openai_realtime_voice_agent_start_conversation` with a `prompt` has it
  speak first and then listen for the answer. Nothing to set up. See the
  Documentation tab, section 7. *(firmware)*
- **An automation can wait for it:** when it's over, the add-on fires
  `openai_realtime_voice_agent_done` with the request's `request_id` and whether it
  was spoken, stopped, dropped or failed.

**Installing**

- **Home Assistant downloads the add-on** instead of building it on your machine:
  the image CI built and tested for the release.

## 0.7.0

> ⚠️ **This update needs a few manual steps, in this order.** The Voice PE now
> proves who it is to the add-on and talks to it over an encrypted connection,
> so the device can't connect until both halves have the new settings.
>
> 1. **Update this add-on.** On its first start it moves your saved settings into
>    the new **⚡ Realtime settings** panel and restarts itself once.
> 2. On the **Configuration** tab, set **`device_secret`** to a random string of at
>    least 16 characters with no spaces (e.g. the output of `openssl rand -hex 32`),
>    and restart the add-on.
> 3. Open the add-on's **Log** and copy the **certificate** it prints, the block from
>    `-----BEGIN CERTIFICATE-----` to `-----END CERTIFICATE-----`.
> 4. In **ESPHome Device Builder → Secrets**, add `va_secret` (the same value as
>    `device_secret`) and `va_certificate` (the certificate, as a `|` block; see
>    `secrets.yaml.example`).
> 5. **Edit your device's YAML** (the stub): under `substitutions:` add
>    `va_secret: !secret va_secret` and `va_certificate: !secret va_certificate`.
>    If you set your own `va_url`, change `ws://` to `wss://`.
> 6. **Update the firmware** (ESPHome Device Builder → Update / Install).
>
> The full steps are in the firmware's INSTALL guide (Part 4).

A rebuild of the add-on, a second voice engine, and a secured device connection.

**A secured device connection**

- **The add-on only accepts your Voice PE.** Before, anything on your network could
  connect and use your OpenAI key and your home's controls through it. The device
  now presents a shared secret, and the connection is encrypted, with a
  certificate the add-on makes for itself and the device trusts alone. *(firmware)*

**A new engine: GPT-Live**

- **Choose the engine** under 🔑 Basics: **Realtime** (the default, OpenAI's
  `gpt-realtime` models) or **GPT-Live**, a voice that listens and speaks at once
  and hands anything that needs doing to a smarter backend model.
- **With GPT-Live you can talk over the assistant,** and it stops to listen.
- GPT-Live bills each minute a conversation is open, so its conversations close
  as soon as they end. Each engine has its own settings panel.

**Conversations**

- **Say goodbye to end a conversation.** "Thanks, that's all" or "good night" now
  ends it, and the device stops listening straight away instead of keeping its
  follow-up window open. *(firmware)*
- **One OpenAI session per conversation.** A conversation opens when you wake the
  device and closes after the idle timeout, instead of one session kept open all
  day, re-billing everything said so far on every reply.
- **Long conversations stay affordable:** past the conversation memory size,
  OpenAI drops the oldest half in one go.
- **Answers are local.** The assistant knows the home's town (new optional
  **`home_town`** setting), country, time zone and units, so "what's the weather?"
  needs no town and answers in °C.
- **Smart-home commands run in the order you gave them.**
- **Home Assistant needn't be up first.** Its tools and settings are read for each
  conversation, so changes to what's exposed apply at the next one, with no
  restart.

**Models and settings**

- **New default model `gpt-realtime-2.1`.** `gpt-realtime` and `gpt-realtime-mini`
  are being retired by OpenAI on 20 January 2027; if you used one, you're moved
  to its replacement.
- **New "Reasoning effort" setting** (low by default), for models that reason.
- **Web search** now uses `gpt-6-luna` by default: quicker and cheaper. If you had
  chosen another search model, you're moved to it too.
- **"Log what you say"** (🔍 Debug) writes what the assistant heard into the log.
  It's off by default; the log no longer shows it otherwise.
- **"Mute the mic while it speaks"**, per engine.
- **Recordings** (🔍 Debug) now work, and are deleted after a week.
- **Custom voices:** a Realtime voice can be one you've made with OpenAI.
- **Clearer errors:** if OpenAI refuses a setting, the log names the setting to change.

**Under the hood**

- The add-on no longer uses Pipecat: it talks to OpenAI directly, with half the code.
- A test suite covers the device protocol and both engines, and runs in CI.

## 0.6.0

> ⚠️ **This update has two parts — please update both:**
> 1. **This add-on** (the update you're installing now).
> 2. **The Voice PE firmware** — open **ESPHome Device Builder** and click **Update** (or **Install**) on your device.
>
> The device and the add-on use one shared protocol; updating only one half can cause odd behaviour.

A reliability and voice-control polish release.

**Stop word**

- **Saying "stop" now usually works on the first try.** The spoken "stop" could
  previously be answered by the assistant a moment later, so you sometimes had to
  repeat it; that follow-on reply is now cancelled, so a single "stop" is
  typically enough.
- **Saying "stop" during a web search returns the device to rest promptly** — the
  light ring no longer keeps showing the "replying" animation for several seconds.
- **Fewer accidental stops** on the assistant's own speech.
- The light ring briefly flashes **red** to confirm your "stop" was registered. *(firmware)*

**Reliability**

- **No more unresponsive sessions.** A silently dropped connection to OpenAI is
  now detected and repaired within seconds, instead of leaving the assistant deaf
  until a restart.
- **The roughly hourly reconnect now happens proactively during a quiet moment**,
  so it practically never interrupts a conversation.
- **Smart-home commands are no longer cancelled** if you keep talking while they run.
- The light can no longer get **stuck on "thinking"**, and long web searches get
  all the time they need.

**No more "answers out of nowhere"**

- The assistant no longer occasionally replies — or repeats its previous answer —
  right after the wake word when you said nothing.
- A sentence that got cut off is no longer answered minutes later on your next wake.

**Settings**

- New **"Wake mic delay"** setting: a short pause after the wake chime before the
  mic opens, so the chime can't be mistaken for speech (default 700 ms).
- The **"Follow-up mic delay"** default is now **700 ms**. Existing installs keep
  their saved value — raise yours if the assistant ever answers right after its
  own reply.

## 0.5.0

A big stable release: everything built and tested on the dev channel over the
past days. **Also update the Voice PE firmware** (v1.1.0 — one click in ESPHome
Builder) to get the full effect of the "stop" improvements; the two halves
work best together.

- **"Stop" now works through the whole reply AND the after-reply listening
  window.** The device detects the word more reliably, and the bridge treats
  it as authoritative: in-flight audio is discarded and an answer OpenAI had
  already started for the stop word itself is cancelled on arrival — no more
  "Okay, I'll be quiet" replies to your "stop".
- **Fixed: an answer could cut off mid-sentence, after which the assistant
  went deaf** until the next reconnect. Harmless protocol races (e.g. your
  sentence being split into two turns by a pause) no longer kill the session.
- **Fixed an audio race that could inject noise/hiss into replies** (firmware,
  paired with this release).
- **Mute behaves properly now** (firmware): the ring goes dark with red
  markers by the microphones, and muting also ends an open listening window
  immediately — both from Home Assistant and with the physical side switch.
- **The LED Ring switch in Home Assistant works again** (firmware): entity off
  = device dark at rest; entity on = the gentle "ready" pulse.
- **Completely reworked Configuration tab**: options grouped logically
  (Basics → Model & voice → Conversation → Web search → Audio →
  Home Assistant → Advanced), every description rewritten in plain practical
  language, and a full Dutch translation included (shown automatically when
  your HA is set to Dutch). Confusing or broken switches were removed; rarely
  needed expert fields stay hidden until you need them.
- **The add-on now has its own icon.**
- Friendlier defaults for new installs: follow-up mic delay 200 ms and
  playback buffer 150 ms. **Existing installs keep their saved values** — if
  yours still say 0, consider setting 200/150 manually (Conversation / Audio
  groups) for fewer ghost triggers and less crackle.

### Heads-up: the firmware stub template was improved

The per-device stub in ESPHome Builder used to reference the firmware in a
form that lets ESPHome **cache the downloaded YAML for a day** — clicking
Update shortly after a release could then silently rebuild yesterday's code.
The stub templates in the firmware repo are fixed; existing users can apply
the same fix once by replacing **only the `packages:` block** in their
device's YAML in ESPHome Builder (everything else — your name, secrets,
`dashboard_import` — stays exactly the same):

```yaml
packages:
  realtime:
    url: https://github.com/xandervanerven/home-assistant-voice-pe
    ref: main
    files: [home-assistant-voice.realtime.yaml]
    refresh: 0s
```

Current templates for reference:
[esphome-builder.dhcp.yaml](https://github.com/xandervanerven/home-assistant-voice-pe/blob/main/esphome-builder.dhcp.yaml) ·
[esphome-builder.static-ip.yaml](https://github.com/xandervanerven/home-assistant-voice-pe/blob/main/esphome-builder.static-ip.yaml)

## 0.4.26

- **Web search is now ON by default**, using **gpt-5.5** (the best-quality search
  model), so the assistant can look things up online — weather, news, facts — out
  of the box. **Existing installs keep their saved setting**: if you had it off,
  switch `enable_web_search` on (and set `web_search_model` to `gpt-5.5`) in the
  add-on Configuration. The cheaper mini/nano models stay available.

## 0.4.25

- **Fix:** the first thing you said in the few seconds right after an automatic
  reconnect (e.g. after the 60-minute session cap) could be ignored
  (`conversation_already_has_active_response`). The reconnected session no longer
  creates a duplicate response, so that turn answers normally.

## 0.4.24

- **Renamed** to **OpenAI Realtime 2 Voice Agent**.
- Rewrote the store/info description and added a full **Documentation** tab
  (install steps, OpenAI key, Home Assistant MCP setup, recommended settings, web
  search, credits). Removed stale text from the original upstream client.
- Default system prompt is now an English, voice-tuned prompt (silent tool calls,
  varied confirmations, language pinning). Your own saved prompt is not changed.
- Default `follow_up_open_delay_ms` and `playback_prebuffer_ms` set to `0` (raise
  them if the device hears its own tail or you hear crackle).

## 0.4.23

- **Fix:** the 60-minute session cap sometimes left the session dead until a
  restart. It now reconnects automatically in all cases (both the keepalive-drop
  and the `session_expired` forms).

## 0.4.22

- **New options:** voice **speed** (0.25–1.5), **max reply length**
  (`max_output_tokens`), and **input noise reduction** (off / near-field /
  far-field). All default to current behaviour.

## 0.4.21

- Model, voice, web-search-model and transcription-model options are now
  **dropdowns** with the known-good values, each with a **custom** entry if you
  need a value not in the list.

## 0.4.20

- **New:** optional **web search**. Turn on `enable_web_search` to let the
  assistant look things up online (weather, news, facts). Uses your OpenAI key;
  off by default. Model configurable via `web_search_model` (default gpt-5.4-mini).

## 0.4.19

- Clarified the MCP option help text for both the built-in HA MCP Server and the
  unofficial ha-mcp add-on.

## 0.4.18

- **Fix:** removed a meaningless filler reply ("I'm ready to continue…") that could
  appear on the first turn of a session.

## 0.4.17

- **Fix:** cap restored conversation history (`max_context_messages`, default 12) to
  bound per-turn token cost and avoid hitting OpenAI's rate limit.

## 0.4.16

- **Fix:** the device no longer gets stuck blinking "thinking" after a turn-ending
  error (e.g. a rate limit) — it returns to idle so you can retry.

## 0.4.14

- **New:** `playback_prebuffer_ms` jitter buffer to reduce occasional crackle at the
  start of replies.

## 0.4.12 – 0.4.13

- **Fix:** "say stop, then immediately ask again → silence". Disabled the broken
  server-side audio truncation that wedged the next turn.

## 0.4.9 – 0.4.11

- **New:** auto-reconnect the OpenAI Realtime session when its connection drops
  (keepalive timeout / 60-minute cap), instead of going dead until a restart.
  Refined so a normal device disconnect doesn't trigger an unnecessary reconnect.

## 0.4.6 – 0.4.8

- **New:** configurable post-reply **follow-up listening window** (answer back
  without re-saying the wake word) + its open-delay, and per-option help text in the
  UI.
- **New:** the assistant's and user's transcripts are logged to the add-on log
  (`🤖 assistant:` / `🗣️ user:`).

## 0.4.0 – 0.4.4

- **Fix:** resample the device's 16 kHz mic to the 24 kHz OpenAI requires (garbled
  speech), and drop empty audio chunks.
- **New:** device **"stop"** interrupt now actually cancels the reply and clears
  buffered audio.

## 0.3.x

- Switched the target to **gpt-realtime-2**, pinned pipecat-ai 0.0.97, and tuned
  turn detection (semantic VAD), phase delivery to the device, and the startup
  sequence to stop double-responses. Made the disconnect tool and transcription
  model configurable.

## Earlier

- Initial pipecat + WebSocket implementation (forked from
  [fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)).
