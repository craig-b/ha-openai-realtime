# Device protocol

The contract between the Voice PE firmware (`firmware/esphome/components/va_client/`)
and the add-on (`openai_realtime_voice_agent/app/`). Everything here is taken from
the code on both sides; where the two disagree, or one side has handling the other
never exercises, it is listed under [Gaps](#gaps).

## Connection

- One WebSocket over TLS. The device is the client, the add-on the server, at
  `wss://<host>:8080/` (`websocket_port` in the add-on). The add-on makes its own
  self-signed certificate on first start and keeps it; the device trusts that
  certificate alone, whatever host name it connects by. The device has the
  address and the certificate from its link (see Discovery below; a firmware built
  with them has them as `va_client`'s `url` and `certificate`), and refuses an
  address that isn't `wss://`.
- The device authenticates with a shared secret, sent on the upgrade request as
  `Authorization: Bearer <secret>` (`device_secret` in the add-on; the device has
  it from its link, or as `va_client`'s `secret` in a firmware built with it). The add-on answers anything else with HTTP 401, before the
  WebSocket opens, so it can't take over from the connected device. The device
  logs the 401 and retries on its usual backoff.
- One device at a time. When a second client connects, it takes over at once,
  and the add-on closes the first in the background.
- TEXT frames carry JSON control messages. BINARY frames carry audio.
- The device keeps the connection open permanently and reconnects itself on any
  disconnect, close or error, backing off 1 s → 2 s → 5 s → 10 s (capped). The
  backoff resets on a successful connect.
- After 5 consecutive failed connects the device plays an error chime
  (`on_repeated_failure`), once, until a connection has held for 30 s.

### JSON encoding

The add-on must send compact JSON (`separators=(",", ":")`): no spaces. The
device does not parse JSON; it matches substrings such as `"type":"hello"` and
`"value":"listening"`, so `"value": "listening"` is silently ignored.

## Audio

| Direction | Format | When |
|---|---|---|
| device → add-on | PCM, signed 16-bit little-endian, mono, **16 kHz** | Only while the device's mic gate is open (see [Mic gate](#mic-gate)) |
| add-on → device | PCM, signed 16-bit little-endian, mono, **24 kHz** | In 40 ms chunks, paced at real time |

- Device → add-on frame size is whatever one microphone callback yields; the add-on
  accepts any even length and drops odd-length frames.
- The add-on resamples device audio 16 → 24 kHz before sending it to OpenAI.
- OpenAI delivers reply audio faster than real time; the add-on queues it and
  sends it on one 40 ms chunk every 40 ms. It pads the end of each reply with
  silence to a whole chunk.
- The device queues incoming audio in a 2 MB ring and plays it out; if the ring
  is full, the excess is dropped. It scales samples by the media player volume
  before queueing.
- After an `interrupt` the device drops all incoming audio until the next
  `phase` = `listening` (see [interrupt](#interrupt)).

## Device → add-on messages

### `start`
`{"type":"start","mac":"AA:BB:CC:DD:EE:FF"}`

- **Sent:** once per connection, immediately on connect. `mac` is the device's
  MAC address (firmware 1.5.0 and later), by which Home Assistant's ESPHome
  integration knows it.
- **Add-on:** if an OpenAI session is open, clears its input audio buffer
  (`input_audio_buffer.clear`; GPT-Live has none), so a reconnect mid-utterance
  can't leave half a sentence behind. Opens no session. Passes `mac` on to the
  integration's discovery (see [Home Assistant's view of the
  device](#home-assistants-view-of-the-device)).

### `wake`
`{"type":"wake"}`

- **Sent:** at the start of every session (`start_session()`): the wake word,
  or the centre button pressed while idle. Sent after any `interrupt` that
  session start sends for a reply still playing or being generated.
- **Add-on:** clears the open session's input audio buffer (or the audio held
  while one opens), and opens an OpenAI session if none is open (see
  [OpenAI session](#openai-session)). The clear also resets OpenAI's turn
  detection: a segment whose end never came, because the mic closed on it,
  would otherwise take the new question's audio and report only its end, with
  no start. Also clears any pending post-stop response cancel. With GPT-Live, it lets GPT-Live's voice through again after an
  `interrupt`.

### `flush`
`{"type":"flush"}`

- **Sent:** when a follow-up window expires with the mic still open (no speech
  arrived).
- **Add-on:** clears the session's input audio buffer (or the audio held while
  one opens), dropping any uncommitted partial utterance and resetting turn
  detection, as for `wake`.

### `interrupt`
`{"type":"interrupt"}`

- **Sent:** on the "stop" word; the centre button pressed in any non-idle
  phase; the mute switch turned on while the device is waiting for or listening
  to a command; at session start if a previous reply is still playing or being
  generated; and when the no-speech watchdog fires (see [Timers](#timers)).
- **Device, at the same moment:** flushes its playback queue, closes the mic
  gate, cancels follow-up timers, drops all incoming audio until the next
  `listening`, ignores `thinking` until the next wake, and suppresses the
  follow-up window the next `idle` would open.
- **Add-on:** drops the reply audio it has queued for the device, and any
  more that arrives for the stopped response; clears the
  session's input audio buffer (or the audio held while one opens); sends
  `response.cancel` if a response is active;
  then cancels the next assistant response to appear, whenever it appears, plus
  any appearing within 1.5 s. Real user speech disarms both; a `wake` disarms
  the first. With GPT-Live, which has no responses to cancel, it drops GPT-Live's
  voice until the next `wake` and asks GPT-Live to stop speaking.

## Add-on → device messages

### `hello`
```json
{"type":"hello","follow_up_ms":8000,"follow_up_open_delay_ms":700,"wake_open_delay_ms":700,"playback_prebuffer_ms":150,"mic_while_replying":false}
```

- **Sent:** once per connection, on connect, before anything else.
- Every field is optional to the device. Missing fields keep the device's value.

| Field | Meaning | Add-on source (default, clamp) | Device default before `hello` | Device clamp |
|---|---|---|---|---|
| `follow_up_ms` | How long the mic stays open after a reply for an answer without the wake word. `0` = off | `follow_up_listen_seconds` × 1000 (8000) | 0 | ≤ 60000 |
| `follow_up_open_delay_ms` | Delay after a reply's audio drains before that mic opens | `follow_up_open_delay_ms` (700, 0–5000) | 1500 | ≤ 5000 |
| `wake_open_delay_ms` | Delay after the wake chime before the mic opens | `wake_open_delay_ms` (700, 0–5000) | 700 | ≤ 5000 |
| `playback_prebuffer_ms` | Audio to accumulate before starting playback of a reply | `playback_prebuffer_ms` (150, 0–2000) | 0 | ≤ 2000 |
| `mic_while_replying` | Keep the mic gate open while replying (barge-in) | not the engine's `mute_mic_while_speaking` (Realtime false, GPT-Live true) | the yaml's `barge_in` (false) | — |

### `phase`
`{"type":"phase","value":"listening"}`, with value `listening`, `thinking`,
`replying` or `idle`.

**When the add-on sends each** (`phases.py`; with GPT-Live, see
[With GPT-Live](#with-gpt-live)):

| Value | Sent when |
|---|---|
| `listening` | OpenAI's turn detection heard the user start speaking |
| `thinking` | OpenAI's turn detection heard the user stop; the model is working, including during tool calls |
| `replying` | Reply audio started |
| `idle` | Reply audio stopped and stayed stopped for `phase_idle_debounce_ms` (1500 ms); or a turn was declared dead (see below) |

- Reply audio arrives in segments (per sentence, around tool calls); `idle`
  waits out the debounce so the device doesn't flap between `replying` and
  `idle` mid-answer.
- **Forced `idle`:** if `thinking` sees no model activity for 15 s, and no tool
  call is running, the add-on sends `idle`. It also forces `idle` when a
  response fails (a rate limit), when OpenAI reports an error during
  `thinking`, and when a session fails to open or is lost mid-turn. An OpenAI
  error leaves the session open. After a forced `idle`,
  `thinking` is suppressed until real user or model speech follows.
- The add-on drops a phase equal to the last one it sent, except `listening`,
  which is always sent: the device lifts its post-`interrupt` audio drop on it.

**What the device does with each:**

| Value | Device behaviour |
|---|---|
| `listening` | Mic gate opens. Lifts the post-`interrupt` audio drop. Cancels the no-speech watchdog and any follow-up timeout. With barge-in on (`mic_while_replying`, off by default), also flushes any queued reply audio |
| `thinking` | Mic stays open (turn detection can flap `listening`↔`thinking` while the user is still speaking). Cancels follow-up timers. Ignored entirely after an `interrupt` until the next wake |
| `replying` | Mic gate closes (unless `mic_while_replying`). Cancels follow-up timers |
| `idle` | See below |

**`idle` on the device** depends on the previous phase:
- **After `thinking` or `replying`** (a turn just ended):
  - after an `interrupt`: close the session, no follow-up;
  - if reply audio is still queued: hold the LED change and follow-up until playback
    drains (or 3 s fallback), then continue as below;
  - otherwise: open a follow-up window of `follow_up_ms`, after
    `follow_up_open_delay_ms`. A follow-up that expires without speech closes
    the mic and sends `flush`.
  - Exception: `idle` after `replying` when no reply audio played since the last
    wake belongs to a stopped reply superseded by a new wake; it opens no
    follow-up.
- **Otherwise** (boot, reconnect, or from `listening`): no follow-up; if the mic
  gate is open, it closes.

The device also applies `idle` locally on every disconnect.

### `end`
`{"type":"end"}`

- **Sent:** when the model ends the conversation (its `end_conversation` tool,
  `enable_end_conversation`), typically while its goodbye is still to play.
- **Device:** the next follow-up window, after the reply has played, doesn't
  open: the `idle` that ends the reply closes the mic instead. A `wake` clears it.
- **Add-on:** the conversation on the device ends with that `idle`, as after an
  `interrupt`, so the session closes after the engine's idle timeout.

### `speak`
`{"type":"speak","listen_ms":0}`

- **Sent:** when an automation asks the add-on to speak unasked (see
  [Speaking unasked](#speaking-unasked)), just before it asks the model to: for an
  announcement with `listen_ms` 0; to start a conversation with the follow-up
  window's length (`follow_up_ms`, or 8000 when that is 0).
- **Device:** the reply that follows is played though nobody woke it: whatever an
  earlier `interrupt` or `end` left behind is cleared (the incoming-audio drop, the
  stale-`thinking` guard, the suppressed follow-up). The mic stays closed. After
  the reply, the `idle` that ends it opens a follow-up window of `listen_ms`
  instead of `follow_up_ms`; 0 opens none. A `wake` or an `interrupt` cancels that.
  "stop" works during the reply, as during any other.
- **Add-on:** the conversation on the device runs until that `idle`, and, if the
  device listens, until its window would have closed (as for a reply's follow-up
  window).

### `error`
`{"type":"error","message":"could not connect to OpenAI"}`

- **Sent:** when an OpenAI session can't be opened (the connection fails,
  OpenAI refuses its settings, or the session isn't configured within 10 s),
  and when the open session's connection dies mid-turn. `message` is for logs;
  for refused settings it names the add-on setting to change.
- **Device:** plays the error chime and goes `idle`.

### `request_follow_up` (handled by the device, never sent)
`{"type":"request_follow_up"}`. The device waits for playback to drain, plays a
chime, then opens the mic for 10 s.

## OpenAI session

The add-on keeps one OpenAI session per conversation: a Realtime session, or a
GPT-Live one (`engine`). What follows is the Realtime engine's; GPT-Live differs
as described [below](#with-gpt-live).

- **Opening:** on `wake`, on mic audio arriving with no session open, or for an
  automation's request to speak (see [Speaking unasked](#speaking-unasked)). Mic
  audio that arrives while the session opens is held, up to 10 s, and sent
  once OpenAI has configured the session. Opening typically takes under a
  second, about as long as the device's `wake_open_delay_ms`.
- **Continuing:** a `wake` while the session is open continues the same
  conversation. Once it passes `realtime → max_context_tokens` (default 8000), OpenAI
  drops its oldest items until half that remains, in one go, so the cached
  start of the conversation changes rarely.
- **Closing:** `realtime → session_reuse_timeout_seconds` (default 300) after the
  conversation on the device ends, with nothing running in the meantime (no
  reply playing, no response or tool). The conversation on the device runs
  from a `wake` (or mic audio) until a `flush`, an `interrupt`, a disconnect,
  or an `idle` that opens no follow-up window; failing those, until the
  follow-up window an `idle` opened would have closed. The OpenAI
  conversation ends with the session; the next `wake` starts afresh.
- **Rolling over:** at a `wake`, a session more than 50 minutes old (OpenAI
  caps sessions at 60) is replaced by a new one, which starts afresh.
- **Losing it:** if OpenAI's connection dies, the session ends. Mid-turn the
  device gets `error`; between turns it is told nothing. Nothing reconnects
  until the next `wake`, or at once if a `wake` is still waiting for the user
  to speak. The new session starts afresh.

### With GPT-Live

With `engine: live` the session is a GPT-Live session, and the device protocol is
unchanged. GPT-Live has no turns, and doesn't roll over. The phases follow what it reports:
`listening` when it hears the user's first words, `thinking` when it hands work to
its backend, `replying` while its voice is audible (silent audio isn't sent to the
device), and `idle` once the voice has been silent for 0.6 s, after the usual
debounce. The session closes when the conversation on the device ends
(`live → session_reuse_timeout_seconds`, default 0), as open minutes are billed.
A device `interrupt` drops GPT-Live's voice until the next `wake`, and asks
GPT-Live to stop speaking.

## Speaking unasked

Home Assistant's automations ask for it by firing an event, which the add-on
subscribes to over Home Assistant's WebSocket API (`app/event_bus.py`):

| Event | Data | What the model is told |
|---|---|---|
| `openai_realtime_voice_agent_announce` | `message` | Say it word for word, and nothing else. The device doesn't listen after it |
| `openai_realtime_voice_agent_start_conversation` | `message`, `prompt` (at least one) | Say `message` word for word, or else speak first about `prompt` in one short sentence ending with the question; `prompt` is the conversation's context either way. Then wait for the answer. The device listens after it |

- The request waits, up to 120 s, until the device is connected and free: no
  conversation going on it (see [OpenAI session](#openai-session)), no turn,
  nothing playing. After that it is dropped, with a warning in the log. Requests
  are taken one at a time.
- Then the add-on opens a session if none is open, sends `speak`, and tells the
  model as a system message in the conversation, followed by `response.create`
  (with GPT-Live, `session.instructions.append` to its voice). The reply goes
  like any other: `replying`, its audio, `idle`. It is part of the conversation,
  so "what did you say?" can follow.
- If the model hasn't begun speaking within 15 s, the request is given up.
- When it's over, the add-on fires `openai_realtime_voice_agent_done` through
  Home Assistant's REST API, with `request_id` (whatever the request carried, or
  null), `request` (`announce` or `start_conversation`) and `result`: `spoken`
  once the reply has played and the device has gone idle after it, `stopped` if an
  `interrupt` cut it off, `dropped` if the device wasn't free in time, `failed`
  otherwise (no text, the session didn't open, the model didn't speak, the device
  disconnected).

## Home Assistant's view of the device

For the add-on's integration (`custom_components/`), which makes the device an
Assist satellite:

- **State:** the add-on fires `openai_realtime_voice_agent_state` with `state`, the
  phase last sent to the device as an Assist satellite's state (`listening` →
  `listening`, `thinking` → `processing`, `replying` → `responding`, `idle` →
  `idle`), and `connected`, whether the device is connected. It fires it when
  either changes, when it (re)connects to Home Assistant, and when
  `openai_realtime_voice_agent_query` asks.
- **Discovery:** when the integration is installed (Home Assistant's
  `manifest/get` finds it), the add-on offers it through the Supervisor's
  discovery (`openai_realtime_voice_agent`), once per run, so Home Assistant shows
  it as discovered. The offer names the device, `{"mac": "..."}` from its
  `start`, so the integration finds its ESPHome device and only asks to confirm;
  it waits up to 60 s for the device to connect, then offers without (firmware
  before 1.5.0 doesn't send `mac`), and the integration asks which device it is.
  The offer also carries what a device needs to connect, `{"link": {"port",
  "secret", "certificate"}}`, which the integration sets on the device (its
  `set_addon_link` action) when the device's "Add-on link" sensor differs (the
  first 16 hex digits of SHA-256 over `url\ncertificate`; not the secret, as
  every Home Assistant user sees it) or its "Add-on connection" sensor says
  `refused` (a wrong secret). Anything in Home Assistant can call that action,
  so a linked device takes a new link only with `proof`, the HMAC-SHA256 of
  `url\nsecret\ncertificate` keyed by the secret it has. An unlinked one has
  no secret to key it, so it takes no link before its API has a key (from the
  Home Assistant that adopted it), and holds its first link until someone
  presses its centre button: meanwhile "Add-on connection" says `awaiting
  button`, "Add-on link" shows the link waiting (it refuses any other until the
  press or a restart), and its LEDs twinkle. The integration raises a repair
  issue asking for the press if the link waiting is its own, or warning not to
  press, and to restart the device, if it isn't. The offer goes from the
  Supervisor to Home Assistant alone, but any add-on can make an offer for the
  integration, and the integration signs the link it takes for the device. So
  it takes a link only in the form the device takes, records which add-on (by
  its Supervisor slug) the entry's link came from, and turns other add-ons'
  offers away; a new or changed link waits for the owner to confirm it, shown
  with the add-on's slug and the link's fingerprint, which the add-on logs at
  every start: the first 32 hex digits of SHA-256 over `port\nsecret\n` and the
  certificate's DER bytes, in groups of four. (Not the add-on's name: the add-on
  chooses it.) Offers to a set-up entry don't wait on one another, and the
  add-on is checked again when the owner confirms. (An entry from 0.11.0, with no add-on recorded, records the one that
  offers the link it already has.)
  Without the integration it offers nothing, as Home Assistant logs an error for
  a discovery no integration takes.

## Mic gate

The device forwards mic audio only while its gate is open:

| Opens | Closes |
|---|---|
| Session start (`start_session()`): the wake word, after `wake_open_delay_ms`; or the centre button, after its sound finishes plus a fixed 600 ms | `replying` (unless `mic_while_replying`) |
| `listening` | `interrupt` |
| A follow-up window opening | A follow-up window expiring (sends `flush`) |
| | `idle` arriving with the mic open and no turn just ended |
| | No-speech watchdog |

While the gate is closed the device keeps a rolling 600 ms pre-roll, and discards
it when the gate opens: it contains the wake chime.

## Timers

| Where | Name | Value | Effect |
|---|---|---|---|
| Device | No-speech watchdog | 7 s after wake | No `listening` yet: send `interrupt`, close mic, LED `idle` |
| Device | Follow-up window | `follow_up_ms` | Expiry: close mic, send `flush`, LED `idle` |
| Device | Requested follow-up | 10 s | Window length for `request_follow_up` |
| Device | Playback drain fallback | 3 s | Proceed with held `idle` even if the speaker never reports drained |
| Device | Reconnect backoff | 1, 2, 5, 10 s | See [Connection](#connection) |
| Add-on | Idle debounce | 1500 ms | See `phase` |
| Add-on | Thinking watchdog | 15 s (paused while a tool runs) | Forced `idle` |
| Add-on | Post-interrupt cancel window | 1.5 s (Realtime) | See `interrupt` |
| Add-on | Session open timeout | 10 s | Gives up opening; sends `error` |
| Add-on | Wait to speak unasked | 120 s | An automation's request is dropped if the device isn't free by then |
| Add-on | Unasked reply start | 15 s | An automation's request is given up if the model hasn't begun speaking |
| Add-on | Conversation idle timeout | `realtime → session_reuse_timeout_seconds` (300 s), `live → session_reuse_timeout_seconds` (0 s) | Closes the OpenAI session |
| Add-on | GPT-Live end of speech | 0.6 s of silence | The reply ends (`idle` follows after the debounce) |

## Typical sequences

**Question and answer, follow-up unused**
```
device                         add-on
  ── connect ─────────────────▶
  ◀──────────────────── hello
  ── start ───────────────────▶
  (wake word; wake_open_delay_ms)
  ── wake ────────────────────▶   opens an OpenAI session
  ── mic audio … ─────────────▶
  ◀──────── phase listening
  ◀───────── phase thinking
  ◀───────── phase replying
  ◀──────── reply audio …
  ◀───────────── phase idle   (after 1.5 s debounce)
  (playback drains; follow_up_open_delay_ms; mic opens for follow_up_ms)
  ── mic audio … ─────────────▶
  (follow-up expires)
  ── flush ───────────────────▶
```

**An automation's announcement**
```
  (the automation fires openai_realtime_voice_agent_announce)
  ◀────── speak listen_ms 0   then the add-on asks the model to say it
  ◀───────── phase replying
  ◀──────── reply audio …
  ◀───────────── phase idle   no follow-up: listen_ms is 0
```

**"Stop" during a reply**
```
  ◀──────── reply audio …
  ── interrupt ───────────────▶   device flushes playback, drops further audio
  ◀───────────── phase idle       no follow-up
```

## Gaps

- **A device handler the add-on never triggers:** `request_follow_up`. The
  add-on has no `request_follow_up` tool.
