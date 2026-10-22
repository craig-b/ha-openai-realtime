"""docs/protocol.md: Device -> add-on messages (wake, flush, interrupt)."""

import asyncio

from tests.fakes import pcm_tone


# --- wake --------------------------------------------------------------------

async def test_wake_suppresses_a_stale_end_of_turn(openai, device, session):
    conn = session  # opened by a wake
    # Turn detection closes a segment from before the wake: no speech started.
    await conn.speech_stopped()
    await device.assert_no_phase("thinking", within=0.3)


async def test_wake_cancels_the_response_to_a_stale_end_of_turn(openai, device, session):
    conn = session  # opened by a wake
    await conn.speech_stopped()
    # The guard arms when the end-of-turn reaches the end of the pipeline;
    # OpenAI creates the response some time after the end-of-turn.
    await asyncio.sleep(0.1)
    await conn.start_response()
    await conn.wait_for("response.cancel")


async def test_real_speech_after_wake_is_a_normal_turn(openai, device, session):
    conn = session  # opened by a wake
    await conn.user_turn()
    await device.wait_for_phase("thinking")
    await conn.start_response()
    await conn.assert_none("response.cancel", within=0.3)


async def test_wake_disarms_the_post_interrupt_cancel(openai, device, session):
    conn = session
    await device.send({"type": "interrupt"})
    await asyncio.sleep(1.6)  # past the 1.5 s window
    await device.send({"type": "wake"})
    await asyncio.sleep(0.1)
    await conn.start_response()
    await conn.assert_none("response.cancel", within=0.3)


# --- flush -------------------------------------------------------------------

async def test_flush_clears_the_openai_input_buffer(openai, device, session):
    conn = session
    clears = conn.count("input_audio_buffer.clear")
    await device.send({"type": "flush"})
    await conn.wait_for("input_audio_buffer.clear", count=clears + 1)


async def test_flush_marks_a_turn_boundary(openai, device, session):
    conn = session
    await conn.user_turn()
    await device.wait_for_phase("thinking")
    await conn.reply(pcm_tone(200, 24000))
    await device.wait_for_phase("idle")

    await device.send({"type": "flush"})
    await asyncio.sleep(0.1)
    await conn.speech_stopped()
    await device.assert_no_phase("thinking", within=0.3, after=1)
    await conn.start_response()
    await conn.wait_for("response.cancel")


# --- interrupt ---------------------------------------------------------------

async def test_interrupt_clears_the_openai_input_buffer(openai, device, session):
    conn = session
    clears = conn.count("input_audio_buffer.clear")
    await device.send({"type": "interrupt"})
    await conn.wait_for("input_audio_buffer.clear", count=clears + 1)


async def test_interrupt_cancels_an_active_response(openai, device, session):
    conn = session
    await conn.user_turn()
    response = await conn.start_response()
    await response.audio(pcm_tone(500, 24000))
    await device.send({"type": "interrupt"})
    await conn.wait_for("response.cancel")


async def test_interrupt_sends_no_cancel_without_an_active_response(openai, device, session):
    conn = session
    await conn.user_turn()
    await conn.reply(pcm_tone(200, 24000))
    await asyncio.sleep(0.1)
    await device.send({"type": "interrupt"})
    await conn.wait_for("input_audio_buffer.clear")
    await conn.assert_none("response.cancel", within=0.3)


async def test_interrupt_cancels_the_next_response_whenever_it_appears(openai, device, session):
    conn = session
    await device.send({"type": "interrupt"})
    await asyncio.sleep(2.0)  # well past the 1.5 s window
    await conn.start_response()
    await conn.wait_for("response.cancel")
    # Only that one: a later response, outside the window, plays.
    await asyncio.sleep(1.6)
    await conn.start_response()
    await conn.assert_none("response.cancel", within=0.3, after=1)


async def test_interrupt_cancels_every_response_within_its_window(openai, device, session):
    conn = session
    await device.send({"type": "interrupt"})
    await asyncio.sleep(0.1)
    await conn.start_response()
    await conn.start_response()
    await conn.wait_for("response.cancel", count=2)


async def test_real_speech_disarms_the_post_interrupt_cancel(openai, device, session):
    conn = session
    await device.send({"type": "interrupt"})
    await asyncio.sleep(0.1)
    await conn.user_turn()
    await device.wait_for_phase("listening")
    await conn.start_response()
    await conn.assert_none("response.cancel", within=0.3)


async def test_ping_gets_no_reply(openai, device):
    await device.send({"type": "ping"})
    await asyncio.sleep(0.2)
    assert [m["type"] for m in device.messages] == ["hello"]


async def test_unknown_text_is_ignored(openai, device, session):
    conn = session
    before = list(conn.events)
    await device.send({"type": "nonsense"})
    await device.websocket.send("not json")
    await asyncio.sleep(0.2)
    assert conn.events == before
    assert [m["type"] for m in device.messages] == ["hello"]


async def test_interrupt_stops_the_reply_audio_being_sent(openai, device, session):
    conn = session
    await conn.user_turn()
    response = await conn.start_response()
    await response.audio(pcm_tone(3000, 24000))
    await device.wait_for_audio(24000)  # 0.5 s has arrived
    await device.send({"type": "interrupt"})
    await asyncio.sleep(0.3)
    sent = len(device.audio)
    await asyncio.sleep(0.5)
    assert len(device.audio) == sent


async def test_a_stopped_replys_remaining_audio_is_not_sent(openai, device, session):
    conn = session
    await conn.user_turn()
    response = await conn.start_response()
    await response.audio(pcm_tone(200, 24000))
    await device.wait_for_audio(200 * 48)
    await device.send({"type": "interrupt"})
    await conn.wait_for("response.cancel")
    # Audio OpenAI had already generated arrives after the cancel.
    await response.audio(pcm_tone(1000, 24000))
    await response.finish("cancelled")
    await asyncio.sleep(0.5)
    assert len(device.audio) == 200 * 48


async def test_a_cancelled_racing_responses_audio_is_not_sent(openai, device, session):
    conn = session
    await device.send({"type": "interrupt"})
    await asyncio.sleep(0.1)
    response = await conn.start_response()
    await conn.wait_for("response.cancel")
    await response.audio(pcm_tone(500, 24000))
    await response.finish("cancelled")
    await asyncio.sleep(0.5)
    assert len(device.audio) == 0
