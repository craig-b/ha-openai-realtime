"""The add-on's OpenAI Realtime session: its configuration and its lifetime."""

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest

from app.websocket_handler import ConnectionRecovery
from tests.fakes import pcm_tone, wait_until


# --- configuration -------------------------------------------------------------

async def test_the_session_uses_the_configured_model(openai, start_addon):
    await start_addon(OPENAI_MODEL="gpt-realtime-mini")
    conn = await openai.connection()
    assert parse_qs(urlsplit(conn.uri).query)["model"] == ["gpt-realtime-mini"]


async def test_the_session_is_configured_for_a_voice_assistant(openai, start_addon):
    await start_addon(INSTRUCTIONS="Be brief.", OPENAI_VOICE="cedar", OPENAI_SPEED="1.2")
    conn = await openai.connection()
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


async def test_transcription_is_off_without_a_language(openai, addon):
    conn = await openai.connection()
    assert "transcription" not in conn.session["audio"]["input"]


async def test_transcription_is_on_with_a_language(openai, start_addon):
    await start_addon(TRANSCRIPTION_LANGUAGE="en")
    conn = await openai.connection()
    assert conn.session["audio"]["input"]["transcription"] == {"model": "gpt-4o-transcribe", "language": "en"}


async def test_web_search_adds_its_tool(openai, start_addon):
    await start_addon(ENABLE_WEB_SEARCH="true")
    conn = await openai.connection()
    assert [t["name"] for t in conn.session["tools"]] == ["web_search"]


# --- lifetime ----------------------------------------------------------------------

async def test_the_session_opens_when_the_add_on_starts(openai, addon):
    await openai.connection()
    await asyncio.sleep(0.3)
    assert len(openai.connections) == 1


async def test_devices_connecting_share_the_one_session(openai, addon):
    conn = await openai.connection()
    for _ in range(3):
        device = await addon.connect()
        await device.close()
    device = await addon.connect()
    await conn.user_turn()
    await device.wait_for_phase("listening")
    assert len(openai.connections) == 1
    await device.close()


async def test_a_dropped_session_reconnects_and_idles_the_device(openai, device):
    conn = await openai.connection()
    await conn.user_turn()
    await device.wait_for_phase("thinking")
    await conn.close(1011, "keepalive ping timeout")
    second = await openai.connection(1)
    await device.wait_for_phase("idle")
    await second.user_turn()
    await device.wait_for_phase("listening", count=2)


async def test_a_reconnected_session_does_not_restore_the_conversation(openai, start_addon):
    addon = await start_addon(TRANSCRIPTION_LANGUAGE="en")
    device = await addon.connect()
    conn = await openai.connection()
    await conn.user_turn()
    await conn.reply(pcm_tone(320, 24000), transcript="It is sunny.")
    await device.wait_for_phase("idle")
    await conn.close(1011)
    second = await openai.connection(1)
    await asyncio.sleep(0.3)
    assert second.count("conversation.item.create") == 0
    await device.close()


async def test_session_expiry_reconnects(openai, device):
    conn = await openai.connection()
    await conn.error("session_expired", "Your session hit the maximum duration of 60 minutes.")
    await openai.connection(1)


@pytest.mark.parametrize(
    "code",
    [
        "conversation_already_has_active_response",
        "response_cancel_not_active",
        "input_audio_buffer_commit_empty",
    ],
)
async def test_benign_errors_keep_the_session(openai, device, code):
    conn = await openai.connection()
    await conn.error(code)
    await asyncio.sleep(0.3)
    assert len(openai.connections) == 1
    await conn.user_turn()
    await conn.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("replying")


async def test_other_errors_reconnect(openai, device):
    conn = await openai.connection()
    await conn.error("server_error", "The server had an error.", error_type="server_error")
    await openai.connection(1)
    await device.wait_for_phase("idle")


@pytest.fixture
def fast_refresh(monkeypatch):
    monkeypatch.setattr(ConnectionRecovery, "REFRESH_AGE_S", 1.0)
    monkeypatch.setattr(ConnectionRecovery, "REFRESH_QUIET_S", 0.5)
    monkeypatch.setattr(ConnectionRecovery, "REFRESH_CHECK_S", 0.2)


async def test_an_old_quiet_session_is_refreshed(openai, fast_refresh, addon):
    await openai.connection()
    await openai.connection(1, timeout=3.0)
    await wait_until(lambda: openai.connections[0].closed.is_set(), 2.0, "the old session closed")


async def test_a_session_in_use_is_not_refreshed(openai, fast_refresh, device):
    await openai.connection()
    # The microphone streaming means someone is talking to the assistant.
    for _ in range(100):
        await device.send_audio(pcm_tone(20, 16000))
        await asyncio.sleep(0.02)
    assert len(openai.connections) == 1
