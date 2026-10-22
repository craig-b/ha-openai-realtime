"""Automations ask the device to speak, by firing events on Home Assistant's bus (app/automations.py)."""

import asyncio

import pytest_asyncio

from app.agent import Agent
from app.automations import ANNOUNCE, START_CONVERSATION
from tests.fakes import pcm_tone, wait_until
from tests.test_tools import FakeHomeAssistant, home_assistant  # noqa: F401 (fixture)


@pytest_asyncio.fixture
async def ha_device(start_addon, home_assistant):  # noqa: F811
    """A device connected to an add-on that reads Home Assistant."""

    async def connect(**overrides):
        addon = await start_addon(HA_MCP_URL=home_assistant.url, LONGLIVED_TOKEN="ha-token", **overrides)
        device = await addon.connect()
        await wait_until(lambda: home_assistant.subscribed(START_CONVERSATION), 5.0, "the subscriptions")
        return device

    return connect


def speak_messages(device) -> list[dict]:
    return [m for m in device.messages if m.get("type") == "speak"]


def system_texts(conn) -> list[str]:
    return [
        part["text"]
        for item in conn.created_items()
        if item.get("role") == "system"
        for part in item["content"]
    ]


async def test_an_announcement_is_said_and_the_device_does_not_listen_after(openai, home_assistant, ha_device):  # noqa: F811
    device = await ha_device(SESSION_REUSE_TIMEOUT_SECONDS="0")
    await home_assistant.fire(ANNOUNCE, {"message": "The washing is done."})
    conn = await openai.connection(0)
    await conn.wait_for("response.create")
    assert speak_messages(device) == [{"type": "speak", "listen_ms": 0}]
    [text] = system_texts(conn)
    assert text.endswith("word for word, and nothing else: The washing is done.")
    assert conn.index_of("conversation.item.create") < conn.index_of("response.create")

    await conn.reply(pcm_tone(400, 24000), transcript="The washing is done.")
    await device.wait_for_audio(400 * 48)
    await device.wait_for_phase("idle")
    assert device.phases == ["replying", "idle"]
    # Nobody is listening: the conversation is over, and the session closes.
    await asyncio.wait_for(conn.closed.wait(), 5.0)
    await device.close()


async def test_a_started_conversation_listens_for_the_answer(openai, home_assistant, ha_device):  # noqa: F811
    device = await ha_device(FOLLOW_UP_LISTEN_SECONDS="5")
    await home_assistant.fire(START_CONVERSATION, {"prompt": "Ask whether to turn the heating up."})
    conn = await openai.connection(0)
    await conn.wait_for("response.create")
    assert speak_messages(device) == [{"type": "speak", "listen_ms": 5000}]
    [text] = system_texts(conn)
    assert "about this: Ask whether to turn the heating up." in text
    await device.close()


async def test_a_started_conversation_listens_even_with_follow_ups_off(openai, home_assistant, ha_device):  # noqa: F811
    device = await ha_device(FOLLOW_UP_LISTEN_SECONDS="0")
    await home_assistant.fire(START_CONVERSATION, {"prompt": "Ask whether to turn the heating up."})
    await wait_until(lambda: speak_messages(device), 5.0, "speak")
    assert speak_messages(device) == [{"type": "speak", "listen_ms": Agent.STARTED_CONVERSATION_LISTEN_MS}]
    await device.close()


async def test_a_request_waits_until_the_conversation_on_the_device_is_over(
    openai, home_assistant, ha_device, wake  # noqa: F811
):
    device = await ha_device()
    conn = await wake(device)
    await conn.user_turn()
    await device.wait_for_phase("thinking")
    await home_assistant.fire(ANNOUNCE, {"message": "The washing is done."})
    await conn.reply(pcm_tone(300, 24000))
    await device.wait_for_phase("idle")
    # The device may be listening for an answer in its follow-up window.
    await asyncio.sleep(0.5)
    assert speak_messages(device) == []
    assert system_texts(conn) == []

    await device.send({"type": "flush"})  # the window closed without an answer
    await wait_until(lambda: speak_messages(device), 3.0, "speak")
    await wait_until(lambda: system_texts(conn), 3.0, "the announcement")
    await device.close()


async def test_a_stop_before_does_not_cancel_the_announcement(openai, home_assistant, ha_device, wake):  # noqa: F811
    device = await ha_device()
    conn = await wake(device)
    await conn.speech_started()  # stopped mid-sentence: the last phase sent stays `listening`
    await device.wait_for_phase("listening")
    await device.send({"type": "interrupt"})
    await home_assistant.fire(ANNOUNCE, {"message": "The washing is done."})
    await conn.wait_for("response.create")
    await conn.reply(pcm_tone(300, 24000))
    await device.wait_for_audio(300 * 48)
    assert conn.count("response.cancel") == 0
    await device.close()


async def test_a_request_without_its_text_is_ignored(openai, home_assistant, ha_device):  # noqa: F811
    device = await ha_device()
    await home_assistant.fire(ANNOUNCE, {"text": "The washing is done."})
    await asyncio.sleep(0.5)
    assert speak_messages(device) == []
    assert openai.connections == []
    await device.close()


async def test_a_request_is_dropped_while_no_device_is_connected(openai, home_assistant, start_addon, monkeypatch):  # noqa: F811
    monkeypatch.setattr(Agent, "SPEAK_WAIT_S", 0.5)
    await start_addon(HA_MCP_URL=home_assistant.url, LONGLIVED_TOKEN="ha-token")
    await home_assistant.fire(ANNOUNCE, {"message": "The washing is done."})
    await asyncio.sleep(1.0)
    assert openai.connections == []


async def test_gpt_live_is_told_to_speak(openai, home_assistant, ha_device):  # noqa: F811
    device = await ha_device(ENGINE="live")
    await home_assistant.fire(ANNOUNCE, {"message": "The washing is done."})
    conn = await openai.live_connection()
    appended = await conn.wait_for("session.instructions.append")
    assert appended["delegation_id"] is None
    assert appended["content"].endswith("nothing else: The washing is done.")
    await conn.says("The washing is done.", pcm_tone(300, 24000))
    await device.wait_for_phase("replying")
    await device.close()


async def test_a_wrong_token_is_retried_without_stopping_the_add_on(openai, home_assistant, start_addon):  # noqa: F811
    addon = await start_addon(HA_MCP_URL=home_assistant.url, LONGLIVED_TOKEN="wrong")
    device = await addon.connect()
    await asyncio.sleep(0.5)
    assert not home_assistant.subscribed(ANNOUNCE)
    await device.close()
