"""docs/protocol.md: the phase message, and the add-on's timers behind it."""

import asyncio
import time

import pytest

from app.phase_emitter import PhaseEmitter
from tests.fakes import pcm_tone


@pytest.fixture
def short_thinking_timeout(monkeypatch):
    monkeypatch.setattr(PhaseEmitter, "THINKING_TIMEOUT_S", 1.0)


async def test_a_turn_runs_listening_thinking_replying_idle(openai, device):
    conn = await openai.connection()
    await conn.user_turn()
    await conn.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("idle")
    assert device.phases == ["listening", "thinking", "replying", "idle"]


async def test_listening_follows_speech_started(openai, device):
    conn = await openai.connection()
    await conn.speech_started()
    await device.wait_for_phase("listening")
    assert device.phases == ["listening"]


async def test_thinking_follows_speech_stopped(openai, device):
    conn = await openai.connection()
    await conn.user_turn()
    await device.wait_for_phase("thinking")


async def test_idle_waits_out_the_debounce(openai, start_addon):
    addon = await start_addon(PHASE_IDLE_DEBOUNCE_MS="1000")
    device = await addon.connect()
    conn = await openai.connection()
    await conn.user_turn()
    audio = pcm_tone(320, 24000)
    await conn.reply(audio)
    await device.wait_for_audio(len(audio))
    audio_done = time.monotonic()
    await device.wait_for_phase("idle", timeout=4.0)
    # Pipecat declares the bot stopped 0.35 s after the last audio; then the debounce.
    assert time.monotonic() - audio_done >= 1.0
    await device.close()


async def test_the_debounce_defaults_to_1500_ms(openai, start_addon):
    addon = await start_addon(PHASE_IDLE_DEBOUNCE_MS=None)
    device = await addon.connect()
    conn = await openai.connection()
    await conn.user_turn()
    audio = pcm_tone(320, 24000)
    await conn.reply(audio)
    await device.wait_for_audio(len(audio))
    audio_done = time.monotonic()
    await device.wait_for_phase("idle", timeout=4.0)
    assert time.monotonic() - audio_done >= 1.5
    await device.close()


async def test_the_debounce_bridges_gaps_inside_a_reply(openai, start_addon):
    addon = await start_addon(PHASE_IDLE_DEBOUNCE_MS="1500")
    device = await addon.connect()
    conn = await openai.connection()
    await conn.user_turn()
    first = pcm_tone(320, 24000)
    await conn.reply(first)
    await device.wait_for_audio(len(first))
    await asyncio.sleep(0.8)  # the bot stops speaking, but within the debounce
    await conn.reply(pcm_tone(320, 24000))
    await device.wait_for_phase("idle", timeout=5.0)
    assert device.phases == ["listening", "thinking", "replying", "idle"]
    await device.close()


async def test_a_repeated_phase_is_not_resent(openai, device):
    conn = await openai.connection()
    await conn.user_turn()
    await device.wait_for_phase("thinking")
    await conn.speech_stopped()
    await device.assert_no_phase("thinking", within=0.3, after=1)


async def test_listening_is_always_sent(openai, device):
    conn = await openai.connection()
    await conn.speech_started()
    await conn.speech_started()
    await device.wait_for_phase("listening", count=2)


async def test_end_of_turn_during_a_reply_keeps_replying(openai, device):
    conn = await openai.connection()
    await conn.user_turn()
    response = await conn.start_response()
    await response.audio(pcm_tone(2000, 24000))
    await device.wait_for_phase("replying")
    # A late end-of-turn from the same question, while the reply plays.
    await conn.speech_stopped()
    await device.assert_no_phase("thinking", within=0.3, after=1)
    assert device.phases[-1] == "replying"


async def test_thinking_without_activity_is_forced_idle(openai, short_thinking_timeout, device):
    conn = await openai.connection()
    await conn.user_turn()
    await device.wait_for_phase("thinking")
    started = time.monotonic()
    await device.wait_for_phase("idle", timeout=4.0)
    # The watchdog's clock starts when it arms, a moment before ours.
    assert time.monotonic() - started >= 0.9


async def test_after_a_forced_idle_thinking_waits_for_real_speech(openai, short_thinking_timeout, device):
    conn = await openai.connection()
    await conn.user_turn()
    await device.wait_for_phase("idle", timeout=4.0)
    await conn.speech_stopped()
    await device.assert_no_phase("thinking", within=0.3, after=1)
    await conn.user_turn()
    await device.wait_for_phase("thinking", count=2)


async def test_a_failed_response_forces_idle(openai, device):
    conn = await openai.connection()
    await conn.user_turn()
    await device.wait_for_phase("thinking")
    response = await conn.start_response()
    body = response.body("failed")
    body["status_details"] = {"error": {"type": "rate_limit_exceeded", "message": "Rate limit reached"}}
    await conn.send({"type": "response.done", "response": body})
    await device.wait_for_phase("idle")
    assert device.phases == ["listening", "thinking", "idle"]
