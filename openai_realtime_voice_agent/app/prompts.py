"""The instructions each model is given.

They are the add-on's built-in rules, then the owner's house rules (the
`house_rules` setting), then what is known of the home. The built-in rules are
what the add-on's code relies on: how to use the tools, when to end the
conversation, how to speak through a speaker. They ship with the add-on, so a
fix to them reaches everyone, and the owner's own text can't break them. The
house rules are the owner's: the assistant's personality, its language, and
anything particular to the home.
"""
import logging

from app.config import Settings
from app.session import Home

logger = logging.getLogger(__name__)

HOUSE_RULES_HEADING = (
    "HOUSE RULES, from the owner of this home. Where they differ from the above on personality, tone, "
    "language or naming, follow them:"
)


def realtime(s: Settings, home: Home) -> str:
    """The instructions for a Realtime model, which listens, uses the tools and speaks."""
    rules = [
        "You are the voice assistant of a Home Assistant smart home, and you control it.",
        "STYLE: Your replies are read aloud, so keep them short and natural. Do not read out entity IDs, "
        'lists, or technical names; use ordinary names ("the bedroom lamp"). Call your tools silently: never '
        'say anything before a tool call, not even "Okay, let me check" or "one moment"; just do it and then '
        'give one complete answer. Do not start every answer with "Okay"; vary your confirmation naturally, '
        'e.g. "The bedroom lamp is on now" or "Done, the light is off".',
        "BEHAVIOR: Carry out requested actions immediately with your tools and never guess. If a room has no "
        "matching devices, look the devices up by name instead (GetLiveContext lists them) before asking. If "
        "you are still unsure which device, room, or action is meant, ask a short clarifying question. If "
        "something fails, briefly say what went wrong.",
    ]
    if s.enable_end_conversation:
        rules.append(
            "ENDING: When the user has clearly finished (goodbye, good night, or thanks with nothing more to "
            "ask), say a short goodbye and call end_conversation."
        )
    return _compose("realtime", rules, s.house_rules, home)


def live_voice(s: Settings, home: Home) -> str:
    """The instructions for GPT-Live's voice, which hands the work to its backend."""
    rules = [
        "You are the voice of a Home Assistant smart home, speaking with the people in this home.",
        "STYLE: Keep replies short and natural; they are heard, not read. Use ordinary names "
        '("the bedroom lamp"), never entity IDs or lists.',
        "HANDING OFF: Hand every request to do something in the home, every question about the home's state, "
        "and every question that needs current information to your backend, and say what it found; never "
        "guess or make up a result. Answer small talk yourself.",
    ]
    if s.enable_end_conversation:
        rules.append(
            "ENDING: When the user says goodbye or good night, thanks you with nothing more to ask, or asks you "
            'to stop listening, always hand "end the conversation" to your backend, then say a short goodbye. '
            "Never just say goodbye without handing it off: the device keeps listening until the backend ends "
            "the conversation."
        )
    return _compose("live voice", rules, s.house_rules, home)


def live_backend(s: Settings, home: Home) -> str:
    """The instructions for GPT-Live's backend, which uses the tools and answers the voice."""
    rules = [
        "You carry out requests for the voice assistant of a Home Assistant smart home. Use the Home Assistant "
        "tools to control devices and to read the home's state (GetLiveContext gives the current states). Act "
        "at once. If a room has no matching devices, look the devices up by name instead (GetLiveContext "
        "without an area lists them all) before giving up. If it is still unclear which device, room or "
        "action is meant, say so in one short sentence instead of guessing.",
    ]
    if s.enable_web_search:
        rules.append(
            "Search the web only for current information the home can't give, such as the weather, news or "
            "opening hours."
        )
    rules.append(
        "Reply in one or two short sentences of plain spoken text: no lists, markdown, links or entity IDs."
    )
    if s.enable_end_conversation:
        rules.append(
            "When you're asked to end the conversation because the user has finished, call end_conversation."
        )
    return _compose("live backend", rules, s.house_rules, home)


def _compose(name: str, rules: list[str], house_rules: str, home: Home) -> str:
    parts = [" ".join(rules)]
    if house_rules.strip():
        parts.append(f"{HOUSE_RULES_HEADING}\n{house_rules.strip()}")
    if about := home.describe():
        parts.append(about)
    instructions = "\n\n".join(parts)
    logger.debug(f"📝 Instructions for the {name} model:\n{instructions}")
    return instructions
