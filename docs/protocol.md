# Device protocol

The contract between the Voice PE firmware (`firmware/esphome/components/va_client/`)
and the add-on (`openai_realtime_voice_agent/app/`). Everything here is taken from
the code on both sides; where the two disagree, or one side has handling the other
never exercises, it is listed under [Gaps](#gaps).

## Connection

- One WebSocket. The device is the client, the add-on the server, at
  `ws://<host>:8080/` (`va_url` in the firmware, `websocket_port` in the add-on).
- One device at a time. When a second client connects, Pipecat closes the first
  and keeps the new one.
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
  sends it on one 40 ms chunk every 40 ms. A final partial chunk is dropped
  (see [Gaps](#gaps)).
- The device queues incoming audio in a 2 MB ring and plays it out; if the ring
  is full, the excess is dropped. It scales samples by the media player volume
  before queueing.
- After an `interrupt` the device drops all incoming audio until the next
  `phase` = `listening` (see [interrupt](#interrupt)).

## Device → add-on messages

### `start`
`{"type":"start"}`

- **Sent:** once per connection, immediately on connect.
- **Add-on:** clears OpenAI's input audio buffer (`input_audio_buffer.clear`),
  so a reconnect mid-utterance can't leave half a sentence behind.

### `wake`
`{"type":"wake"}`

- **Sent:** at the start of every session (`start_session()`): the wake word,
  or the centre button pressed while idle. Sent after any `interrupt` that
  session start sends for a reply still playing or being generated.
- **Add-on:** marks a turn boundary. Until the user actually speaks, an
  end-of-turn from OpenAI's turn detection is treated as a stale segment from
  before the wake: its `thinking` is suppressed and the response it creates is
  cancelled. Also clears any pending post-stop response cancel.

### `flush`
`{"type":"flush"}`

- **Sent:** when a follow-up window expires with the mic still open (no speech
  arrived).
- **Add-on:** clears OpenAI's input audio buffer, dropping any uncommitted
  partial utterance, and marks a turn boundary as for `wake`.

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
- **Add-on:** clears OpenAI's input audio buffer; sends `response.cancel` if a
  response is active; then cancels the next assistant response to appear,
  whenever it appears, plus any appearing within 1.5 s. Real user speech disarms
  both; a `wake` disarms the first.

## Add-on → device messages

### `hello`
```json
{"type":"hello","audio_out":"pcm","follow_up_ms":8000,"follow_up_open_delay_ms":700,"wake_open_delay_ms":700,"playback_prebuffer_ms":150}
```

- **Sent:** once per connection, on connect, before anything else.
- Every field is optional to the device. Missing fields keep the device's value.

| Field | Meaning | Add-on source (default, clamp) | Device default before `hello` | Device clamp |
|---|---|---|---|---|
| `follow_up_ms` | How long the mic stays open after a reply for an answer without the wake word. `0` = off | `follow_up_listen_seconds` × 1000 (8000) | 0 | ≤ 60000 |
| `follow_up_open_delay_ms` | Delay after a reply's audio drains before that mic opens | `follow_up_open_delay_ms` (700, 0–5000) | 1500 | ≤ 5000 |
| `wake_open_delay_ms` | Delay after the wake chime before the mic opens | `wake_open_delay_ms` (700, 0–5000) | 700 | ≤ 5000 |
| `playback_prebuffer_ms` | Audio to accumulate before starting playback of a reply | `playback_prebuffer_ms` (150, 0–2000) | 0 | ≤ 2000 |
| `audio_out` | — | always `"pcm"` | ignored | — |

### `phase`
`{"type":"phase","value":"listening"}`, with value `listening`, `thinking`,
`replying` or `idle`.

**When the add-on sends each** (`phase_emitter.py`, from Pipecat frames):

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
  call is running, the add-on sends `idle`. It also forces `idle` when it
  recovers from a rate limit or reconnects to OpenAI. After a forced `idle`,
  `thinking` is suppressed until real user or model speech follows.
- The add-on drops a phase equal to the last one it sent, except `listening`,
  which is always sent: the device lifts its post-`interrupt` audio drop on it.

**What the device does with each:**

| Value | Device behaviour |
|---|---|
| `listening` | Mic gate opens. Lifts the post-`interrupt` audio drop. Cancels the no-speech watchdog and any follow-up timeout. With barge-in enabled (`barge_in`, off in this firmware), also flushes any queued reply audio |
| `thinking` | Mic stays open (turn detection can flap `listening`↔`thinking` while the user is still speaking). Cancels follow-up timers. Ignored entirely after an `interrupt` until the next wake |
| `replying` | Mic gate closes (unless barge-in is on). Cancels follow-up timers |
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

### `error` (handled by the device, never sent)
`{"type":"error",...}`. The device plays the error chime and goes `idle`.

### `request_follow_up` (handled by the device, never sent)
`{"type":"request_follow_up"}`. The device waits for playback to drain, plays a
chime, then opens the mic for 10 s.

## Mic gate

The device forwards mic audio only while its gate is open:

| Opens | Closes |
|---|---|
| Session start (`start_session()`): the wake word, after `wake_open_delay_ms`; or the centre button, after its sound finishes plus a fixed 600 ms | `replying` (barge-in off) |
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
| Add-on | Post-interrupt cancel window | 1.5 s | See `interrupt` |

## Typical sequences

**Question and answer, follow-up unused**
```
device                         add-on
  ── connect ─────────────────▶
  ◀──────────────────── hello
  ── start ───────────────────▶
  (wake word; wake_open_delay_ms)
  ── wake ────────────────────▶
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

**"Stop" during a reply**
```
  ◀──────── reply audio …
  ── interrupt ───────────────▶   device flushes playback, drops further audio
  ◀───────────── phase idle       no follow-up
```

## Gaps

- **Dead add-on handler.** `websocket_handler.py` registers `on_client_message`
  (answers `ping` with `pong`, handles `start` and `interrupt`), but Pipecat
  0.0.97's server transport never emits that event. Every text frame actually
  arrives through `RawAudioSerializer.deserialize`. The firmware sends no `ping`.
- **Device handlers the add-on never triggers:** `error` and `request_follow_up`.
  The add-on has no `request_follow_up` tool.
- **`audio_out`** in `hello` is sent but ignored.
- **Second client.** Pipecat closes the old connection before serving the new
  one, and waits for the old one's close handshake to finish. A device that
  reconnects after a Wi-Fi drop leaves its old connection half-open, which
  never answers, so the new connection gets no `hello` and nothing it sends is
  read until the close times out after 10 s.
- **A stopped reply's audio keeps coming.** After an `interrupt` the add-on
  cancels the response at OpenAI but keeps sending, at real-time pace, the
  reply audio it has already queued. The device drops it only until the next
  `listening`; if that comes first, the rest of the stopped reply plays.
- **The last partial chunk of each reply is dropped.** Up to 40 ms at the end
  of every reply never reaches the device.
