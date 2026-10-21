"""docs/protocol.md: Connection, JSON encoding, Audio, and the hello message."""

import asyncio
import json
import time

import pytest

from tests.fakes import OUTPUT_CHUNK_MS, pcm_silence, pcm_tone, wait_until


async def test_hello_is_the_first_message(device):
    assert device.messages[0]["type"] == "hello"


async def test_hello_carries_the_defaults(device):
    assert device.messages[0] == {
        "type": "hello",
        "follow_up_ms": 8000,
        "follow_up_open_delay_ms": 700,
        "wake_open_delay_ms": 700,
        "playback_prebuffer_ms": 150,
    }


async def test_hello_carries_the_configured_values(start_addon):
    addon = await start_addon(
        FOLLOW_UP_LISTEN_SECONDS="12",
        FOLLOW_UP_OPEN_DELAY_MS="900",
        WAKE_OPEN_DELAY_MS="400",
        PLAYBACK_PREBUFFER_MS="250",
    )
    device = await addon.connect()
    hello = device.messages[0]
    assert hello["follow_up_ms"] == 12000
    assert hello["follow_up_open_delay_ms"] == 900
    assert hello["wake_open_delay_ms"] == 400
    assert hello["playback_prebuffer_ms"] == 250
    await device.close()


@pytest.mark.parametrize(
    "env, field, expected",
    [
        ({"FOLLOW_UP_LISTEN_SECONDS": "600"}, "follow_up_ms", 60000),
        ({"FOLLOW_UP_LISTEN_SECONDS": "-3"}, "follow_up_ms", 0),
        ({"FOLLOW_UP_OPEN_DELAY_MS": "9000"}, "follow_up_open_delay_ms", 5000),
        ({"WAKE_OPEN_DELAY_MS": "9000"}, "wake_open_delay_ms", 5000),
        ({"PLAYBACK_PREBUFFER_MS": "9000"}, "playback_prebuffer_ms", 2000),
    ],
)
async def test_hello_values_are_clamped(start_addon, env, field, expected):
    addon = await start_addon(**env)
    device = await addon.connect()
    assert device.messages[0][field] == expected
    await device.close()


async def test_add_on_sends_compact_json(openai, device, session):
    conn = session
    await conn.user_turn()
    await device.wait_for_phase("thinking")
    for text in device.raw_text:
        assert text == json.dumps(json.loads(text), separators=(",", ":"))


async def test_start_clears_the_open_sessions_input_buffer(openai, addon, wake):
    first = await addon.connect()
    conn = await wake(first)
    await first.close()
    # The device reconnects mid-conversation.
    second = await addon.connect(send_start=False)
    await conn.assert_none("input_audio_buffer.clear", within=0.2)
    await second.send({"type": "start"})
    await conn.wait_for("input_audio_buffer.clear")
    await second.close()


async def test_start_opens_no_session(openai, device):
    await asyncio.sleep(0.3)
    assert openai.connections == []


async def test_device_can_reconnect(openai, addon, wake):
    first = await addon.connect()
    conn = await wake(first)
    await first.close()

    second = await addon.connect()
    await conn.user_turn()
    await second.wait_for_phase("listening")
    await second.close()


async def test_new_connection_replaces_the_old_one(openai, addon, wake):
    old = await addon.connect()
    conn = await wake(old)

    new = await addon.connect()
    await asyncio.wait_for(old.closed.wait(), 3.0)

    await asyncio.sleep(0.3)
    assert not new.closed.is_set()
    await conn.user_turn()
    await new.wait_for_phase("listening")
    await new.close()


async def test_new_connection_is_served_while_the_old_one_is_half_open(openai, addon, wake):
    # A device that dropped off Wi-Fi and reconnected leaves its old TCP
    # connection half-open: the old socket never answers the close handshake.
    old = await addon.connect()
    conn = await wake(old)
    old.websocket.transport.pause_reading()
    try:
        new = await addon.device().connect()
        await wait_until(lambda: new.messages, 1.0, "hello on the new connection")
        await conn.user_turn()
        await new.wait_for_phase("listening", timeout=1.0)
    finally:
        old.websocket.transport.resume_reading()
    await new.close()


async def test_the_old_connection_ending_late_leaves_the_new_one_alone(openai, addon, wake):
    old = await addon.connect()
    conn = await wake(old)
    old.websocket.transport.pause_reading()
    new = await addon.connect()
    # The old connection's close completes only after the new one took over.
    old.websocket.transport.resume_reading()
    await asyncio.wait_for(old.closed.wait(), 3.0)
    await asyncio.sleep(0.3)
    assert not new.closed.is_set()
    await conn.user_turn()
    await new.wait_for_phase("listening")
    await conn.reply(pcm_tone(OUTPUT_CHUNK_MS * 5, 24000))
    await new.wait_for_audio(OUTPUT_CHUNK_MS * 5 * 48)
    await new.close()


async def test_device_audio_is_resampled_to_24_khz(openai, device, session):
    conn = session
    # 1 s at 16 kHz, in 20 ms frames as the microphone delivers it.
    frame = pcm_tone(20, 16000)
    for _ in range(50):
        await device.send_audio(frame)
    expected = 24000 * 2  # 1 s at 24 kHz, 16-bit
    await wait_until(
        lambda: len(conn.appended_audio()) >= expected * 0.95,
        3.0,
        lambda: f"~{expected} bytes appended (got {len(conn.appended_audio())})",
    )
    assert len(conn.appended_audio()) <= expected


async def test_odd_length_audio_frames_are_dropped(openai, device, session):
    conn = session
    await device.send_audio(pcm_silence(100, 16000) + b"\x00")
    await device.send_audio(pcm_tone(500, 16000))
    await wait_until(lambda: conn.appended_audio(), 3.0, "audio appended")
    await asyncio.sleep(0.2)
    # Only the 500 ms frame reaches OpenAI (12000 samples at 24 kHz).
    assert len(conn.appended_audio()) <= 500 * 24 * 2


async def test_reply_audio_reaches_the_device_unchanged(openai, device, session):
    conn = session
    await conn.user_turn()
    audio = pcm_tone(1000, 24000)
    await conn.reply(audio)
    await device.wait_for_audio(len(audio))
    assert bytes(device.audio) == audio


async def test_a_reply_that_is_not_whole_chunks_reaches_the_device_in_full(openai, device, session):
    conn = session
    await conn.user_turn()
    audio = pcm_tone(OUTPUT_CHUNK_MS * 7 + 20, 24000)
    await conn.reply(audio)
    await device.wait_for_phase("idle")
    # Padded with silence to a whole chunk.
    assert bytes(device.audio) == audio + bytes(20 * 48)


async def test_two_replies_back_to_back_reach_the_device_in_full(openai, device, session):
    conn = session
    await conn.user_turn()
    first = pcm_tone(OUTPUT_CHUNK_MS * 3 + 10, 24000)
    second = pcm_tone(OUTPUT_CHUNK_MS * 2 + 30, 24000)
    await conn.reply(first)
    await conn.reply(second)
    await device.wait_for_phase("idle")
    assert bytes(device.audio) == first + bytes(30 * 48) + second + bytes(10 * 48)


async def test_reply_audio_is_paced_at_real_time(openai, device, session):
    conn = session
    await conn.user_turn()
    audio = pcm_tone(3000, 24000)
    started = time.monotonic()
    await conn.reply(audio)  # the fake bursts it all at once
    await device.wait_for_audio(len(audio), timeout=5.0)
    assert 2.5 < time.monotonic() - started < 3.6
