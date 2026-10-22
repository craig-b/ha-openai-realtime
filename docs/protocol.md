# Device protocol

The contract between the Voice PE firmware (`firmware/esphome/components/va_client/`)
and the add-on (`openai_realtime_voice_agent/app/`). Everything here is taken from
the code on both sides; where the two disagree, or one side has handling the other
never exercises, it is listed under [Gaps](#gaps).

## Connection

- One WebSocket. The device is the client, the add-on the server, at
  `ws://<host>:8080/` (`va_url` in the firmware, `websocket_port` in the add-on).
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
`{"type":"start"}`

- **Sent:** once per connection, immediately on connect.
- **Add-on:** if an OpenAI session is open, clears its input audio buffer
  (`input_audio_buffer.clear`), so a reconnect mid-utterance can't leave half a
  sentence behind. Opens no session.

### `wake`
`{"type":"wake"}`

- **Sent:** at the start of every session (`start_session()`): the wake word,
  or the centre button pressed while idle. Sent after any `interrupt` that
  session start sends for a reply still playing or being generated.
- **Add-on:** opens an OpenAI session if none is open (see
  [OpenAI session](#openai-session)). Marks a turn boundary: until the user
  actually speaks, an end-of-turn from OpenAI's turn detection is treated as a
  stale segment from before the wake: its `thinking` is suppressed and the
  response it creates is cancelled. Also clears any pending post-stop response
  cancel.

### `flush`
`{"type":"flush"}`

- **Sent:** when a follow-up window expires with the mic still open (no speech
  arrived).
- **Add-on:** clears the session's input audio buffer (or the audio held while
  one opens), dropping any uncommitted partial utterance, and marks a turn
  boundary as for `wake`.

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
- **Add-on:** drops the reply audio it has queued for the device; clears the
  session's input audio buffer (or the audio held while one opens); sends
  `response.cancel` if a response is active;
  then cancels the next assistant response to appear, whenever it appears, plus
  any appearing within 1.5 s. Real user speech disarms both; a `wake` disarms
  the first.

## Add-on → device messages

### `hello`
```json
{"type":"hello","follow_up_ms":8000,"follow_up_open_delay_ms":700,"wake_open_delay_ms":700,"playback_prebuffer_ms":150}
```

- **Sent:** once per connection, on connect, before anything else.
- Every field is optional to the device. Missing fields keep the device's value.

| Field | Meaning | Add-on source (default, clamp) | Device default before `hello` | Device clamp |
|---|---|---|---|---|
| `follow_up_ms` | How long the mic stays open after a reply for an answer without the wake word. `0` = off | `follow_up_listen_seconds` × 1000 (8000) | 0 | ≤ 60000 |
| `follow_up_open_delay_ms` | Delay after a reply's audio drains before that mic opens | `follow_up_open_delay_ms` (700, 0–5000) | 1500 | ≤ 5000 |
| `wake_open_delay_ms` | Delay after the wake chime before the mic opens | `wake_open_delay_ms` (700, 0–5000) | 700 | ≤ 5000 |
| `playback_prebuffer_ms` | Audio to accumulate before starting playback of a reply | `playback_prebuffer_ms` (150, 0–2000) | 0 | ≤ 2000 |

### `phase`
`{"type":"phase","value":"listening"}`, with value `listening`, `thinking`,
`replying` or `idle`.

**When the add-on sends each** (`phases.py`):

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

### `error`
`{"type":"error","message":"could not connect to OpenAI"}`

- **Sent:** when an OpenAI session can't be opened (the connection fails, or
  the session isn't configured within 10 s), and when the open session's
  connection dies mid-turn. `message` is for logs.
- **Device:** plays the error chime and goes `idle`.

### `request_follow_up` (handled by the device, never sent)
`{"type":"request_follow_up"}`. The device waits for playback to drain, plays a
chime, then opens the mic for 10 s.

## OpenAI session

The add-on keeps one OpenAI Realtime session per conversation.

- **Opening:** on `wake`, or on mic audio arriving with no session open. Mic
  audio that arrives while the session opens is held, up to 10 s, and sent
  once OpenAI has configured the session. Opening typically takes under a
  second, about as long as the device's `wake_open_delay_ms`.
- **Continuing:** a `wake` while the session is open continues the same
  conversation. After each response the add-on deletes the oldest whole turns
  from OpenAI's conversation until at most `max_context_messages` items remain
  (default 12; each question, reply, tool call and tool result is one); the
  current turn always stays.
- **Closing:** once nothing has happened for `session_reuse_timeout_seconds`
  (default 300): no mic audio, no reply playing, no response or tool running.
  The conversation ends with it; the next `wake` starts afresh.
- **Rolling over:** at a `wake`, a session more than 50 minutes old (OpenAI
  caps sessions at 60) is replaced by a new one, which starts with a transcript
  of the conversation so far. The transcript has the user's words only when
  input transcription is on (`transcription_language`).
- **Losing it:** if OpenAI's connection dies, the session ends. Mid-turn the
  device gets `error`; between turns it is told nothing. Nothing reconnects
  until the next `wake`, or at once if a `wake` is still waiting for the user
  to speak; if that comes within `session_reuse_timeout_seconds`, the new
  session starts with the transcript as for a rollover.

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
| Add-on | Session open timeout | 10 s | Gives up opening; sends `error` |
| Add-on | Conversation idle timeout | `session_reuse_timeout_seconds` (300 s) | Closes the OpenAI session |

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

**"Stop" during a reply**
```
  ◀──────── reply audio …
  ── interrupt ───────────────▶   device flushes playback, drops further audio
  ◀───────────── phase idle       no follow-up
```

## Gaps

- **A device handler the add-on never triggers:** `request_follow_up`. The
  add-on has no `request_follow_up` tool.
