"""The add-on's OpenAI Realtime session: its configuration and its lifetime."""

import asyncio
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio

from app.realtime_service import SafeRealtimeLLMService
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
    monkeypatch.setattr(SafeRealtimeLLMService, "IDLE_CHECK_S", 0.05)


# --- configuration -------------------------------------------------------------

async def test_the_session_uses_the_configured_model(openai, start_addon, open_session):
    _, conn = await open_session(await start_addon(OPENAI_MODEL="gpt-realtime-mini"))
    assert parse_qs(urlsplit(conn.uri).query)["model"] == ["gpt-realtime-mini"]


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


async def test_transcription_is_off_without_a_language(openai, session):
    assert "transcription" not in session.session["audio"]["input"]


async def test_transcription_is_on_with_a_language(openai, start_addon, open_session):
    _, conn = await open_session(await start_addon(TRANSCRIPTION_LANGUAGE="en"))
    assert conn.session["audio"]["input"]["transcription"] == {"model": "gpt-4o-transcribe", "language": "en"}


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
    monkeypatch.setattr(SafeRealtimeLLMService, "OPEN_TIMEOUT_S", 0.5)
    openai.configure_sessions = False
    await device.send({"type": "wake"})
    conn = await openai.connection()
    await wait_until(lambda: device.errors, 3.0, "an error message")
    await asyncio.wait_for(conn.closed.wait(), 2.0)


# --- closing ---------------------------------------------------------------------

async def test_the_session_closes_after_the_idle_timeout(openai, start_addon, open_session, fast_idle_check):
    device, conn = await open_session(await start_addon(SESSION_REUSE_TIMEOUT_SECONDS="1"))
    await conn.user_turn()
    await conn.reply(pcm_tone(320, 24000))
    await device.wait_for_audio(320 * 48)
    played = time.monotonic()
    await asyncio.wait_for(conn.closed.wait(), 3.0)
    # Counted from the end of the reply as the device plays it.
    assert time.monotonic() - played >= 1.0
    assert device.errors == []


async def test_the_idle_timeout_defaults_to_five_minutes(openai, start_addon, open_session):
    addon = await start_addon(SESSION_REUSE_TIMEOUT_SECONDS=None)
    await open_session(addon)
    assert addon.app.openai_service._idle_timeout_s == 300


async def test_mic_audio_keeps_the_session_open(openai, start_addon, open_session, fast_idle_check):
    device, conn = await open_session(await start_addon(SESSION_REUSE_TIMEOUT_SECONDS="1"))
    for _ in range(100):  # 2 s of the microphone streaming
        await device.send_audio(pcm_tone(20, 16000))
        await asyncio.sleep(0.02)
    assert not conn.closed.is_set()


async def test_a_response_in_progress_keeps_the_session_open(openai, start_addon, open_session, fast_idle_check):
    device, conn = await open_session(await start_addon(SESSION_REUSE_TIMEOUT_SECONDS="0"))
    await conn.user_turn()
    response = await conn.start_response()
    await asyncio.sleep(0.5)
    assert not conn.closed.is_set()
    await response.finish()
    await asyncio.wait_for(conn.closed.wait(), 2.0)


async def test_after_the_idle_timeout_a_wake_starts_afresh(openai, start_addon, open_session, wake, fast_idle_check):
    addon = await start_addon(SESSION_REUSE_TIMEOUT_SECONDS="0", TRANSCRIPTION_LANGUAGE="en")
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


async def test_other_errors_end_the_session(openai, device, session):
    await session.error("server_error", "The server had an error.", error_type="server_error")
    await asyncio.wait_for(session.closed.wait(), 2.0)
