"""What the agent and an engine's session say to each other.

The agent (app/agent.py) owns the device, the conversation's lifecycle, the
phases, reply playback and running tools. A session (app/realtime.py) is one
connection to OpenAI: it takes the user's audio and turns OpenAI's events into
the calls on SessionEvents below.
"""
from dataclasses import dataclass, field
from typing import Protocol

from app.tools import Tools


@dataclass
class Home:
    """Where the home is, so "what's the weather?" needs no town, and gets the
    home's units. The town is the add-on's setting; the rest is Home
    Assistant's own configuration."""

    town: str = ""
    # ISO 3166-1 alpha-2, e.g. "GB".
    country: str = ""
    # IANA, e.g. "Europe/London".
    time_zone: str = ""
    # e.g. "°C".
    temperature_unit: str = ""

    def describe(self) -> str:
        """A line for the model's instructions; empty if nothing is known."""
        place = ", ".join(part for part in (self.town, self.country) if part)
        parts = []
        if place:
            parts.append(f"The home is in {place}.")
        if self.time_zone:
            parts.append(f"Its time zone is {self.time_zone}.")
        if self.temperature_unit:
            parts.append(f"Give temperatures in {self.temperature_unit}.")
        if not parts:
            return ""
        return "ABOUT THE HOME: " + " ".join(parts) + " Unless the user names another place, questions about the weather or local things are about the home."

    def web_search_location(self) -> dict | None:
        """The home as OpenAI's web search takes a user's approximate location."""
        location = {"type": "approximate"}
        if self.town:
            location["city"] = self.town
        if self.country:
            location["country"] = self.country
        if self.time_zone:
            location["timezone"] = self.time_zone
        return location if len(location) > 1 else None


@dataclass
class SessionSetup:
    """What a session is configured with that is read anew for each one."""

    tools: Tools
    # The languages to expect when transcribing the user; none for no hint.
    transcription_languages: list[str]
    home: Home = field(default_factory=Home)


class SessionFailed(Exception):
    """The session could not be opened; the message says why."""


class SessionEvents(Protocol):
    def user_started(self) -> None:
        """The user started speaking."""

    def user_stopped(self) -> None:
        """The user stopped speaking."""

    def reply_audio(self, audio: bytes) -> None:
        """Reply audio to play, 24 kHz PCM."""

    def reply_ended(self) -> None:
        """No more reply audio for now."""

    def tool_call(self, call_id: str, name: str, arguments: str) -> None:
        """The model calls a tool; its result goes back with the others at response_done."""

    def work_started(self) -> None:
        """The model began work the user is waiting on (a backend response): the turn isn't over."""

    def work_finished(self) -> None:
        """That work ended."""

    def response_done(self, session: "Session") -> None:
        """The model's response is complete: send its tool calls' results, if any."""

    def turn_failed(self, reason: str) -> None:
        """The turn ended without a reply."""

    def error(self, reason: str) -> None:
        """OpenAI reported an error that leaves the session usable; the turn may get no reply."""

    async def session_lost(self, session: "Session", reason: str) -> None:
        """The session ended without the agent closing it."""


class Session(Protocol):
    # Whether the agent opens a new session before this one's age limit.
    ROLLS_OVER: bool

    @property
    def busy(self) -> bool:
        """Whether a response is being generated."""

    async def connect(self, timeout: float) -> None: ...

    async def configure(self, setup: SessionSetup, timeout: float) -> None:
        """Raises SessionFailed if OpenAI doesn't take the settings."""

    async def close(self) -> None: ...

    async def send_audio(self, audio: bytes) -> None:
        """The user's audio, 24 kHz PCM."""

    async def clear_input(self) -> None:
        """Drop the user's audio not yet taken as a turn."""

    async def interrupt(self) -> None:
        """The device's "stop": drop the reply being made."""

    async def send_tool_results(self, results: list[tuple[str, str]]) -> None:
        """Each (call_id, output), then continue the response."""

    async def speak(self, instruction: str) -> None:
        """Have the model speak first, unasked, as `instruction` says (app/prompts.py)."""

    def woke(self) -> None:
        """The device woke."""

    def dangling_turn(self) -> None:
        """A turn ended with no speech since the wake."""

    def real_speech(self) -> None:
        """The user really spoke."""
