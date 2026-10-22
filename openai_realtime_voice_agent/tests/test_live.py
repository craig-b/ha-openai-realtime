"""The GPT-Live engine (app/live.py): a session per conversation on /v1/live/sessions."""

import asyncio
import time

import pytest
import pytest_asyncio

from app.agent import Agent
from app.live import LiveSession
from tests.fakes import pcm_silence, pcm_tone, wait_until
from tests.test_tools import FakeHomeAssistant


@pytest.fixture
def fast_idle_check(monkeypatch):
    monkeypatch.setattr(Agent, "IDLE_CHECK_S", 0.05)


@pytest_asyncio.fixture
async def live_addon(start_addon):
    return await start_addon(ENGINE="live")


@pytest_asyncio.fixture
async def live(openai, live_addon):
    """A connected device, woken, and the GPT-Live session it opened."""
    device = await live_addon.connect()
    await device.send({"type": "wake"})
    conn = await openai.live_connection()
    await wait_until(lambda: conn.count("session.start"), 3.0, "session.start")
    yield device, conn
    await device.close()


async def test_a_wake_starts_a_gpt_live_session_with_its_settings(openai, live):
    _, conn = live
    assert conn.authorization == "Bearer test-key"
    session = conn.session
    assert session["model"] == "gpt-live-1"
    assert session["audio"] == {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": "marin"}}
    backend = session["delegation"]["responses"]
    assert session["delegation"]["type"] == "responses"
    assert backend["model"] == "gpt-6-luna"
    assert backend["reasoning"] == {"effort": "low"}
    assert backend["tools"] == []
    assert "service_tier" not in backend
    assert openai.connections == []  # no Realtime session


async def test_settings_reach_the_session(openai, start_addon):
    addon = await start_addon(
        ENGINE="live", LIVE_VOICE="cedar", LIVE_BACKEND_MODEL="gpt-6.1-sol", LIVE_REASONING_EFFORT="medium",
        LIVE_SERVICE_TIER="priority", LIVE_INSTRUCTIONS="Be the house.", LIVE_BACKEND_INSTRUCTIONS="Use tools.",
    )
    device = await addon.connect()
    await device.send({"type": "wake"})
    session = (await openai.live_connection()).session
    assert session["audio"]["output"]["voice"] == "cedar"
    assert session["instructions"] == "Be the house."
    backend = session["delegation"]["responses"]
    assert backend["model"] == "gpt-6.1-sol"
    assert backend["instructions"] == "Use tools."
    assert backend["reasoning"] == {"effort": "medium"}
    assert backend["service_tier"] == "priority"
    await device.close()


async def test_web_search_is_the_backends_own_tool(openai, start_addon):
    addon = await start_addon(ENGINE="live", ENABLE_WEB_SEARCH="true")
    device = await addon.connect()
    await device.send({"type": "wake"})
    assert (await openai.live_connection()).session["delegation"]["responses"]["tools"] == [{"type": "web_search"}]
    await device.close()


async def test_mic_audio_reaches_gpt_live_at_24_khz(openai, live):
    device, conn = live
    for _ in range(25):  # 500 ms, as the device sends it
        await device.send_audio(pcm_tone(20, 16000))
    # The resampler holds a few ms back.
    await wait_until(lambda: len(conn.appended_audio()) > 450 * 48, 3.0, "mic audio")


async def test_the_phases_follow_what_gpt_live_hears_and_says(openai, live):
    device, conn = live
    await conn.heard("turn on the kitchen light")
    await device.wait_for_phase("listening")
    await conn.delegates()
    await device.wait_for_phase("thinking")
    await conn.says("Done.", pcm_tone(400, 24000) + pcm_silence(1000, 24000))
    await device.wait_for_phase("replying")
    await device.wait_for_phase("idle", timeout=4.0)


async def test_only_its_audible_voice_is_played(openai, live):
    device, conn = live
    await conn.says("", pcm_silence(500, 24000))  # silence between replies
    await conn.says("Hi.", pcm_tone(400, 24000))
    await device.wait_for_audio(400 * 48)
    await asyncio.sleep(LiveSession.SPEECH_HANG_S + 0.3)
    # The tone, and nothing of the silence before it (padded to a whole chunk at its end).
    assert len(device.audio) < (400 + 40) * 48


async def test_the_reply_ends_when_the_voice_does(openai, live):
    device, conn = live
    await conn.says("Hi.", pcm_tone(300, 24000))  # no more audio after it
    await device.wait_for_phase("replying")
    await device.wait_for_phase("idle", timeout=LiveSession.SPEECH_HANG_S + 3.0)


async def test_a_backend_tool_call_runs_and_its_result_goes_back(openai, start_addon):
    ha = FakeHomeAssistant()
    await ha.start()
    try:
        addon = await start_addon(ENGINE="live", HA_MCP_URL=ha.url, LONGLIVED_TOKEN="ha-token")
        device = await addon.connect()
        await device.send({"type": "wake"})
        conn = await openai.live_connection()
        tools = conn.session["delegation"]["responses"]["tools"]
        assert sorted(t["name"] for t in tools) == ["HassTurnOff", "HassTurnOn"]
        await conn.delegates()
        call_id = await conn.function_call("HassTurnOn", {"name": "kitchen light"})
        result = await conn.wait_for("response.item.create")
        assert result["item"] == {"type": "function_call_output", "call_id": call_id, "output": "Turned on kitchen light"}
        await conn.wait_for("response.create")
        assert conn.index_of("response.item.create") < conn.index_of("response.create")
        await device.close()
    finally:
        await ha.stop()


async def test_a_stop_silences_gpt_live(openai, live):
    device, conn = live
    await conn.says("A long answer", pcm_tone(400, 24000))
    await device.wait_for_phase("replying")
    await device.send({"type": "interrupt"})
    appended = await conn.wait_for("session.instructions.append")
    assert "stop" in appended["content"].lower()
    received = len(device.audio)
    await conn.says(" that goes on", pcm_tone(400, 24000))
    await asyncio.sleep(0.5)
    assert len(device.audio) == received


async def test_the_session_closes_when_the_conversation_ends(openai, live, fast_idle_check):
    device, conn = live
    await device.send({"type": "flush"})  # the follow-up window closed without speech
    await conn.wait_for("session.close", timeout=3.0)
    await asyncio.wait_for(conn.closed.wait(), 3.0)


async def test_refused_settings_fail_the_session_naming_the_setting(openai, live_addon):
    openai.refuse_live_with = {
        "type": "invalid_request_error", "code": "invalid_value",
        "message": "Unknown model.", "param": "session.delegation.responses.model",
    }
    device = await live_addon.connect()
    await device.send({"type": "wake"})
    await wait_until(lambda: device.errors, 3.0, "an error message")
    assert "live → backend_model" in device.errors[0]["message"]
    await device.close()


async def test_gpt_live_ending_the_session_mid_reply_tells_the_device(openai, live):
    device, conn = live
    await conn.heard("what's the weather")
    await conn.delegates()
    await device.wait_for_phase("thinking")
    await conn.send({"type": "session.closed", "reason": "content", "usage": {"seconds": 3.0}})
    await conn.websocket.close()
    await wait_until(lambda: device.errors, 3.0, "an error message")
    await device.wait_for_phase("idle")


async def test_the_backend_can_end_the_conversation(openai, start_addon, fast_idle_check):
    addon = await start_addon(ENGINE="live", ENABLE_END_CONVERSATION="true")
    device = await addon.connect()
    await device.send({"type": "wake"})
    conn = await openai.live_connection()
    assert [t["name"] for t in conn.session["delegation"]["responses"]["tools"]] == ["end_conversation"]
    await conn.heard("thanks, that's all")
    await conn.delegates()
    await conn.function_call("end_conversation", {})
    await wait_until(lambda: {"type": "end"} in device.messages, 3.0, "end sent to the device")
    await conn.says("Goodbye!", pcm_tone(300, 24000))
    await device.wait_for_phase("idle", timeout=4.0)
    await conn.wait_for("session.close", timeout=3.0)
    await device.close()


async def test_the_ring_stays_on_thinking_while_the_backend_works(openai, live):
    device, conn = live
    await conn.heard("what's the weather")
    await conn.delegates()
    await conn.backend({"type": "response.created", "response": {"id": "resp_1"}})
    await conn.says("Checking that now.", pcm_tone(300, 24000))  # it acknowledges at once
    await device.wait_for_phase("replying")
    # The acknowledgement ends, but the backend is still searching.
    await asyncio.sleep(LiveSession.SPEECH_HANG_S + 0.3 + 0.5)
    assert device.phases[-1] == "thinking"
    await conn.backend({"type": "response.completed", "response": {"id": "resp_1"}})
    await conn.says("It's sunny.", pcm_tone(300, 24000))
    await device.wait_for_phase("replying", count=2)
    await device.wait_for_phase("idle", timeout=4.0)


async def test_backend_work_that_goes_silent_doesnt_keep_the_session_open(openai, live, fast_idle_check, monkeypatch):
    monkeypatch.setattr(LiveSession, "BACKEND_SILENCE_S", 0.5)
    device, conn = live
    await conn.heard("what's the weather")
    await conn.delegates()
    await conn.backend({"type": "response.created", "response": {"id": "resp_1"}})  # it never completes
    await device.send({"type": "flush"})  # the conversation on the device ends
    await conn.wait_for("session.close", timeout=4.0)


async def test_late_words_from_before_a_reply_dont_cut_it(openai, live):
    device, conn = live
    said_at = conn.ms
    await conn.heard("thanks that's all")
    await conn.says("Happy to help.", pcm_tone(600, 24000))
    await device.wait_for_phase("replying")
    await conn.heard("anyway", start_ms=said_at)  # said before the reply, transcribed only now
    await asyncio.sleep(0.3)
    assert device.phases[-1] == "replying"


async def test_words_said_over_a_reply_cut_it(openai, live):
    device, conn = live
    await conn.heard("tell me a story")
    await conn.says("Once upon a time", pcm_tone(600, 24000))
    await device.wait_for_phase("replying")
    await asyncio.sleep(0.1)
    await conn.heard("actually what time is it")
    await device.wait_for_phase("listening", count=2)


async def test_a_wake_while_a_session_closes_doesnt_wait_for_gpt_live_to_confirm(openai, live, fast_idle_check):
    openai.live_close_delay = 3.0
    device, conn = live
    await device.send({"type": "flush"})  # the conversation ends: the session closes
    await conn.wait_for("session.close", timeout=3.0)
    started = time.monotonic()
    await device.send({"type": "wake"})
    await openai.live_connection(1, timeout=3.0)
    assert time.monotonic() - started < 1.5
