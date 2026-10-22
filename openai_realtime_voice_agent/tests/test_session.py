"""The add-on's OpenAI Realtime session: its configuration and its lifetime."""

import asyncio
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio

from app.agent import Agent
from app.realtime import takes_reasoning
from tests.fakes import pcm_tone, wait_until


@pytest_asyncio.fixture
async def open_session(wake):
    """Connect a device to an add-on and wake it; return both ends."""

    async def open_session(addon):
        device = await addon.connect()
        return device, await wake(device)

    return open_session


@pytest.fixture
def fast_idle_check(monkeypatch):
    monkeypatch.setattr(Agent, "IDLE_CHECK_S", 0.05)


# --- configuration -------------------------------------------------------------

async def test_the_session_uses_the_configured_model(openai, start_addon, open_session):
    _, conn = await open_session(await start_addon(OPENAI_MODEL="gpt-realtime-2.1-mini"))
    assert parse_qs(urlsplit(conn.uri).query)["model"] == ["gpt-realtime-2.1-mini"]


async def test_the_session_is_configured_for_a_voice_assistant(openai, start_addon, open_session):
    addon = await start_addon(INSTRUCTIONS="Be brief.", OPENAI_VOICE="cedar", OPENAI_SPEED="1.2")
    _, conn = await open_session(addon)
    session = conn.session
    assert session["instructions"] == "Be brief."
    assert session["audio"]["output"] == {"voice": "cedar", "speed": 1.2}
    assert session["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "low",
        "create_response": True,
        "interrupt_response": False,
    }
    assert session["tools"] == []


async def test_the_user_is_not_transcribed_by_default(openai, session):
    assert "transcription" not in session.session["audio"]["input"]


async def test_logging_what_you_say_transcribes_in_english_without_home_assistant(openai, start_addon, open_session):
    _, conn = await open_session(await start_addon(LOG_WHAT_YOU_SAY="true"))
    assert conn.session["audio"]["input"]["transcription"] == {"model": "gpt-live-transcribe", "languages": ["en"]}


async def test_transcription_can_detect_the_language(openai, start_addon, open_session):
    _, conn = await open_session(await start_addon(LOG_WHAT_YOU_SAY="true", TRANSCRIPTION_LANGUAGE="auto"))
    assert conn.session["audio"]["input"]["transcription"] == {"model": "gpt-live-transcribe"}


async def test_transcription_can_expect_several_languages(openai, start_addon, open_session):
    _, conn = await open_session(await start_addon(LOG_WHAT_YOU_SAY="true", TRANSCRIPTION_LANGUAGE="en, NL"))
    assert conn.session["audio"]["input"]["transcription"] == {"model": "gpt-live-transcribe", "languages": ["en", "nl"]}


async def test_a_custom_voice_is_sent_by_its_id(openai, start_addon, open_session):
    _, conn = await open_session(await start_addon(OPENAI_VOICE="custom", OPENAI_VOICE_CUSTOM="voice_1234"))
    assert conn.session["audio"]["output"]["voice"] == {"id": "voice_1234"}


async def test_web_search_adds_its_tool(openai, start_addon, open_session):
    _, conn = await open_session(await start_addon(ENABLE_WEB_SEARCH="true"))
    assert [t["name"] for t in conn.session["tools"]] == ["web_search"]


# --- opening ---------------------------------------------------------------------

async def test_no_session_opens_until_the_device_wakes(openai, device, wake):
    await asyncio.sleep(0.3)
    assert openai.connections == []
    await wake(device)


async def test_a_wake_during_a_session_continues_it(openai, device, session):
    await session.user_turn()
    await session.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("idle")
    await device.send({"type": "wake"})
    await session.user_turn()
    await device.wait_for_phase("thinking", count=2)
    assert len(openai.connections) == 1


async def test_mic_audio_without_a_wake_opens_a_session(openai, device):
    await device.send_audio(pcm_tone(100, 16000))
    conn = await openai.connection()
    await wait_until(conn.appended_audio, 2.0, "the audio appended")


async def test_audio_is_held_until_the_session_is_configured(openai, device):
    openai.session_updated_delay = 0.5
    await device.send({"type": "wake"})
    for _ in range(25):  # 500 ms, while the session opens
        await device.send_audio(pcm_tone(20, 16000))
        await asyncio.sleep(0.02)
    conn = await openai.connection()
    await wait_until(
        lambda: len(conn.appended_audio()) >= 0.95 * 500 * 48,
        3.0,
        lambda: f"the held audio appended (got {len(conn.appended_audio())} bytes)",
    )
    # Nothing went to OpenAI before the session was configured.
    assert conn.index_of("session.update") < conn.index_of("input_audio_buffer.append")


async def test_audio_held_while_opening_is_dropped_by_an_interrupt(openai, device):
    openai.session_updated_delay = 0.5
    await device.send({"type": "wake"})
    await device.send_audio(pcm_tone(200, 16000))
    await asyncio.sleep(0.1)
    await device.send({"type": "interrupt"})
    conn = await openai.connection()
    await asyncio.sleep(0.6)
    assert conn.appended_audio() == b""


async def test_a_session_that_cannot_connect_reports_an_error(openai, device):
    openai.reject_status = 401
    await device.send({"type": "wake"})
    await wait_until(lambda: device.errors, 3.0, "an error message")
    await device.wait_for_phase("idle")
    # The next wake tries again.
    openai.reject_status = None
    await device.send({"type": "wake"})
    await openai.connection()


async def test_a_session_that_is_never_configured_reports_an_error(openai, device, monkeypatch):
    monkeypatch.setattr(Agent, "OPEN_TIMEOUT_S", 0.5)
    openai.configure_sessions = False
    await device.send({"type": "wake"})
    conn = await openai.connection()
    await wait_until(lambda: device.errors, 3.0, "an error message")
    await asyncio.wait_for(conn.closed.wait(), 2.0)


async def test_refused_settings_fail_the_session_at_once_naming_the_setting(openai, device):
    openai.refuse_sessions_with = {
        "type": "invalid_request_error",
        "code": "invalid_value",
        "message": "Unsupported option for this model.",
        "param": "session.reasoning",
    }
    started = time.monotonic()
    await device.send({"type": "wake"})
    conn = await openai.connection()
    await wait_until(lambda: device.errors, 3.0, "an error message")
    assert time.monotonic() - started < 2.0  # not after the open timeout
    assert "realtime → reasoning_effort" in device.errors[0]["message"]
    await asyncio.wait_for(conn.closed.wait(), 2.0)


# --- reasoning -------------------------------------------------------------------

async def test_reasoning_effort_is_low_by_default(openai, session):
    assert session.session["reasoning"] == {"effort": "low"}


async def test_reasoning_effort_can_be_set(openai, start_addon, open_session):
    _, conn = await open_session(await start_addon(REASONING_EFFORT="minimal"))
    assert conn.session["reasoning"] == {"effort": "minimal"}


async def test_a_model_that_does_not_reason_is_not_sent_a_reasoning_effort(openai, start_addon, open_session):
    _, conn = await open_session(await start_addon(OPENAI_MODEL="gpt-realtime-1.5"))
    assert "reasoning" not in conn.session


def test_which_models_take_a_reasoning_effort():
    assert all(map(takes_reasoning, ["gpt-realtime-2", "gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime-3"]))
    assert not any(map(takes_reasoning, [
        "gpt-realtime-1.5", "gpt-realtime", "gpt-realtime-mini", "gpt-realtime-2025-08-28", "gpt-4o-realtime-preview",
    ]))


# --- closing ---------------------------------------------------------------------

async def test_the_session_closes_after_the_idle_timeout(openai, start_addon, open_session, fast_idle_check):
    device, conn = await open_session(await start_addon(SESSION_REUSE_TIMEOUT_SECONDS="1"))
    await conn.user_turn()
    await conn.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("idle")
    # The follow-up window closes without speech: the conversation on the device ends.
    await device.send({"type": "flush"})
    ended = time.monotonic()
    await asyncio.wait_for(conn.closed.wait(), 3.0)
    assert time.monotonic() - ended >= 1.0
    assert device.errors == []


async def test_the_idle_timeout_defaults_to_five_minutes(openai, start_addon, open_session):
    addon = await start_addon(SESSION_REUSE_TIMEOUT_SECONDS=None)
    await open_session(addon)
    assert addon.app.settings.session_idle_timeout_s == 300


async def test_mic_audio_keeps_the_session_open(openai, start_addon, open_session, fast_idle_check):
    device, conn = await open_session(await start_addon(SESSION_REUSE_TIMEOUT_SECONDS="1"))
    for _ in range(100):  # 2 s of the microphone streaming
        await device.send_audio(pcm_tone(20, 16000))
        await asyncio.sleep(0.02)
    assert not conn.closed.is_set()


async def test_a_response_in_progress_keeps_the_session_open(openai, start_addon, open_session, fast_idle_check):
    addon = await start_addon(SESSION_REUSE_TIMEOUT_SECONDS="0", FOLLOW_UP_LISTEN_SECONDS="0")
    device, conn = await open_session(addon)
    await conn.user_turn()
    response = await conn.start_response()
    await asyncio.sleep(0.5)
    assert not conn.closed.is_set()
    await response.audio(pcm_tone(320, 24000))
    await response.finish()
    await asyncio.wait_for(conn.closed.wait(), 3.0)


async def test_the_follow_up_window_keeps_the_session_open(openai, start_addon, open_session, fast_idle_check):
    device, conn = await open_session(await start_addon(SESSION_REUSE_TIMEOUT_SECONDS="0"))
    await conn.user_turn()
    await conn.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("idle")
    await asyncio.sleep(1.0)
    assert not conn.closed.is_set()
    await device.send({"type": "flush"})
    await asyncio.wait_for(conn.closed.wait(), 2.0)


async def test_without_a_flush_the_conversation_ends_with_its_follow_up_window(
    openai, start_addon, open_session, fast_idle_check, monkeypatch
):
    monkeypatch.setattr(Agent, "PLAYBACK_DRAIN_FALLBACK_S", 0.0)
    monkeypatch.setattr(Agent, "FOLLOW_UP_MARGIN_S", 0.5)
    addon = await start_addon(
        SESSION_REUSE_TIMEOUT_SECONDS="0", FOLLOW_UP_LISTEN_SECONDS="1", FOLLOW_UP_OPEN_DELAY_MS="0"
    )
    device, conn = await open_session(addon)
    await conn.user_turn()
    await conn.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("idle")
    idle = time.monotonic()
    await asyncio.wait_for(conn.closed.wait(), 3.0)
    # 1.5 s from when the add-on sent `idle`, a moment before it arrived here.
    assert time.monotonic() - idle >= 1.3


async def test_a_stop_ends_the_conversation(openai, start_addon, open_session, fast_idle_check):
    device, conn = await open_session(await start_addon(SESSION_REUSE_TIMEOUT_SECONDS="0"))
    await conn.user_turn()
    response = await conn.start_response()
    await response.audio(pcm_tone(500, 24000))
    await device.send({"type": "interrupt"})
    await response.finish("cancelled")
    await asyncio.wait_for(conn.closed.wait(), 4.0)


async def test_after_the_idle_timeout_a_wake_starts_afresh(openai, start_addon, open_session, wake, fast_idle_check):
    addon = await start_addon(SESSION_REUSE_TIMEOUT_SECONDS="0", FOLLOW_UP_LISTEN_SECONDS="0")
    device, conn = await open_session(addon)
    await conn.user_turn()
    await conn.reply(pcm_tone(320, 24000), transcript="It is sunny.")
    await asyncio.wait_for(conn.closed.wait(), 3.0)
    second = await wake(device)
    await asyncio.sleep(0.3)
    assert second.count("conversation.item.create") == 0


async def test_the_device_disconnecting_leaves_the_session_open(openai, device, session):
    await device.close()
    await asyncio.sleep(0.3)
    assert not session.closed.is_set()


async def test_the_device_disconnecting_ends_the_conversation(openai, start_addon, open_session, fast_idle_check):
    device, conn = await open_session(await start_addon(SESSION_REUSE_TIMEOUT_SECONDS="0"))
    await device.close()
    await asyncio.wait_for(conn.closed.wait(), 2.0)


# --- losing the connection ----------------------------------------------------------

async def test_a_session_lost_mid_turn_tells_the_device(openai, device, session, wake):
    await session.user_turn()
    await device.wait_for_phase("thinking")
    await session.close(1011, "keepalive ping timeout")
    await wait_until(lambda: device.errors, 3.0, "an error message")
    await device.wait_for_phase("idle")
    # Nothing reconnects by itself; the next wake opens a new session.
    await asyncio.sleep(0.3)
    assert len(openai.connections) == 1
    second = await wake(device)
    await second.user_turn()
    await device.wait_for_phase("listening", count=2)


async def test_a_session_lost_between_turns_tells_the_device_nothing(openai, device, session):
    await session.user_turn()
    await session.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("idle")
    await session.close(1001, "going away")
    await asyncio.sleep(0.3)
    assert device.errors == []
    assert device.phases[-1] == "idle"


async def test_a_session_lost_after_a_wake_reopens_at_once(openai, device, session):
    await session.user_turn()
    await session.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("idle")
    await device.send({"type": "wake"})
    await asyncio.sleep(0.1)
    # The connection dies before the user has said anything.
    await session.close(1011)
    await openai.connection(1)


async def test_session_expiry_ends_the_session(openai, device, session):
    await session.error("session_expired", "Your session hit the maximum duration of 60 minutes.")
    await asyncio.wait_for(session.closed.wait(), 2.0)


@pytest.mark.parametrize(
    "code",
    [
        "conversation_already_has_active_response",
        "response_cancel_not_active",
        "input_audio_buffer_commit_empty",
    ],
)
async def test_benign_errors_keep_the_session(openai, device, session, code):
    await session.error(code)
    await asyncio.sleep(0.3)
    assert not session.closed.is_set()
    await session.user_turn()
    await session.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("replying")


async def test_other_errors_keep_the_session(openai, device, session):
    await session.error("server_error", "The server had an error.", error_type="server_error")
    await asyncio.sleep(0.3)
    assert not session.closed.is_set()
    await session.user_turn()
    await session.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("replying")


async def test_an_error_mid_turn_returns_the_device_to_idle(openai, device, session):
    await session.user_turn()
    await device.wait_for_phase("thinking")
    await session.error("invalid_value", "Invalid value.")
    await device.wait_for_phase("idle")
    assert not session.closed.is_set()


# --- a new session ------------------------------------------------------------------------

async def test_an_old_session_rolls_over_at_a_wake(openai, start_addon, open_session, wake, monkeypatch):
    monkeypatch.setattr(Agent, "ROLLOVER_AGE_S", 0.5)
    device, conn = await open_session(await start_addon())
    await conn.user_turn()
    await conn.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("idle")
    await asyncio.sleep(0.5)
    second = await wake(device)
    await asyncio.wait_for(conn.closed.wait(), 2.0)
    await asyncio.sleep(0.3)
    assert second.created_items() == []  # it starts afresh


async def test_a_young_session_does_not_roll_over(openai, device, session):
    await device.send({"type": "wake"})
    await asyncio.sleep(0.3)
    assert len(openai.connections) == 1


async def test_the_session_after_a_lost_one_starts_afresh(openai, device, session, wake):
    await session.user_said("Turn on the kitchen light.")
    await session.reply(pcm_tone(320, 24000), transcript="The kitchen light is on.")
    await device.wait_for_phase("idle")
    await session.close(1011)
    second = await wake(device)
    await asyncio.sleep(0.3)
    assert second.created_items() == []



# --- keeping the conversation short --------------------------------------------------------

async def _exchanges(device, conn, count):
    """`count` question-and-answer turns; returns each turn's (user, assistant) item ids."""
    ids = []
    for n in range(count):
        user = await conn.speech_started()
        await conn.speech_stopped(user)
        response = await conn.reply(pcm_tone(320, 24000), transcript=f"Answer {n}.")
        await device.wait_for_phase("idle", count=n + 1)
        ids.append((user, response.item_id))
    return ids


async def test_openai_truncates_the_conversation_by_half_at_the_token_limit(openai, start_addon, open_session):
    _, conn = await open_session(await start_addon(MAX_CONTEXT_TOKENS="6000"))
    assert conn.session["truncation"] == {
        "type": "retention_ratio",
        "retention_ratio": 0.5,
        "token_limits": {"post_instructions": 6000},
    }


async def test_the_token_limit_defaults_to_8000(openai, session):
    assert session.session["truncation"]["token_limits"] == {"post_instructions": 8000}


async def test_no_token_limit_leaves_the_models_own(openai, start_addon, open_session):
    _, conn = await open_session(await start_addon(MAX_CONTEXT_TOKENS="0"))
    assert conn.session["truncation"] == {"type": "retention_ratio", "retention_ratio": 0.5}


async def test_the_add_on_deletes_no_conversation_items(openai, device, session):
    await _exchanges(device, session, 8)
    await asyncio.sleep(0.3)
    assert session.count("conversation.item.delete") == 0


# --- recording -----------------------------------------------------------------------------

async def test_recording_writes_each_sessions_audio(openai, start_addon, open_session, tmp_path):
    import wave

    device, conn = await open_session(await start_addon(ENABLE_RECORDING="true", RECORDING_DIR=str(tmp_path)))
    for _ in range(25):  # 500 ms at 16 kHz
        await device.send_audio(pcm_tone(20, 16000))
        await asyncio.sleep(0.02)
    await conn.user_turn()
    await conn.reply(pcm_tone(400, 24000))
    await device.wait_for_audio(400 * 48)
    await conn.close(1011)  # the session ends; its files are complete
    await asyncio.sleep(0.3)
    mic, reply = sorted(tmp_path.glob("*-mic.wav")), sorted(tmp_path.glob("*-reply.wav"))
    assert len(mic) == len(reply) == 1
    with wave.open(str(reply[0])) as wav:
        assert (wav.getframerate(), wav.getnframes()) == (24000, 400 * 24)
    with wave.open(str(mic[0])) as wav:
        assert wav.getframerate() == 24000
        assert 0.45 * 24000 <= wav.getnframes() <= 0.5 * 24000


async def test_recordings_older_than_a_week_are_deleted(openai, start_addon, open_session, tmp_path):
    import os
    import time as clock

    recordings = tmp_path / "recordings"
    recordings.mkdir()
    old, recent = recordings / "20260101-000000-mic.wav", recordings / "keep-mic.wav"
    old.write_bytes(b"")
    recent.write_bytes(b"")
    week_ago = clock.time() - 8 * 24 * 3600
    os.utime(old, (week_ago, week_ago))
    await open_session(await start_addon(ENABLE_RECORDING="true", RECORDING_DIR=str(recordings)))
    assert not old.exists() and recent.exists()


async def test_no_recording_unless_enabled(openai, start_addon, open_session, tmp_path):
    recordings = tmp_path / "recordings"
    device, conn = await open_session(await start_addon(RECORDING_DIR=str(recordings)))
    await device.send_audio(pcm_tone(100, 16000))
    await conn.close(1011)
    await asyncio.sleep(0.3)
    assert not recordings.exists() or list(recordings.iterdir()) == []
