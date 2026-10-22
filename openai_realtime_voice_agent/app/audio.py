"""Audio between the device and OpenAI: mic resampling and paced reply playback."""
import asyncio
import collections
import logging
import os
import time
import wave
from datetime import datetime
from typing import Callable

import numpy as np
import soxr

logger = logging.getLogger(__name__)

# OpenAI Realtime's PCM: 24 kHz, 16-bit, mono, in both directions.
OPENAI_RATE = 24000


class InputResampler:
    """Resample the device's mic audio (16 kHz) to OpenAI's 24 kHz.

    A stream resampler, so chunk boundaries leave no clicks. It holds a few ms
    of audio back; `reset` drops them at the start of each new mic stream.
    """

    def __init__(self, in_rate: int, out_rate: int = OPENAI_RATE):
        self._in_rate = in_rate
        self._out_rate = out_rate
        self._stream = soxr.ResampleStream(in_rate, out_rate, 1, dtype="int16", quality="VHQ")

    def resample(self, pcm: bytes) -> bytes:
        if self._in_rate == self._out_rate:
            return pcm
        return self._stream.resample_chunk(np.frombuffer(pcm, dtype=np.int16)).tobytes()

    def reset(self) -> None:
        self._stream.clear()


class ReplyAudio:
    """Play reply audio to the device: whole 40 ms chunks, at real-time pace.

    OpenAI sends a reply faster than real time. Pacing it keeps the device's
    buffer small, so a "stop" leaves little queued on either side; a flush
    drops what the add-on still holds. The end of each reply is padded with
    silence to a whole chunk.
    """

    CHUNK_MS = 40

    def __init__(
        self,
        send: Callable[[bytes], None],
        on_started: Callable[[], None],
        on_stopped: Callable[[], None],
        rate: int = OPENAI_RATE,
    ):
        self._send = send
        self._on_started = on_started
        self._on_stopped = on_stopped
        self._chunk_bytes = rate * 2 * self.CHUNK_MS // 1000
        self._partial = bytearray()
        self._chunks: collections.deque[bytes] = collections.deque()
        self._ready = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.playing = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._play())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def feed(self, pcm: bytes) -> None:
        self._partial += pcm
        whole = len(self._partial) - len(self._partial) % self._chunk_bytes
        for i in range(0, whole, self._chunk_bytes):
            self._chunks.append(bytes(self._partial[i:i + self._chunk_bytes]))
        del self._partial[:whole]
        if self._chunks:
            self._ready.set()

    def end_of_reply(self) -> None:
        """A reply's audio is complete: pad its last chunk with silence and queue it."""
        if self._partial:
            self._chunks.append(bytes(self._partial) + bytes(self._chunk_bytes - len(self._partial)))
            self._partial.clear()
            self._ready.set()

    def flush(self) -> None:
        """Drop all reply audio not yet sent."""
        self._partial.clear()
        self._chunks.clear()

    async def _play(self) -> None:
        while True:
            await self._ready.wait()
            self._ready.clear()
            if not self._chunks:
                continue
            self.playing = True
            self._on_started()
            due = time.monotonic()
            while self._chunks:
                self._send(self._chunks.popleft())
                due += self.CHUNK_MS / 1000
                await asyncio.sleep(max(0.0, due - time.monotonic()))
            self.playing = False
            self._on_stopped()


class SessionRecording:
    """One OpenAI session's audio, as two WAV files: the mic audio sent to
    OpenAI, and the reply audio sent to the device.

    Recordings older than KEEP_DAYS are deleted as each new one starts: they
    are for troubleshooting, and hold what was said in the home.
    """

    KEEP_DAYS = 7

    def __init__(self, directory: str):
        os.makedirs(directory, exist_ok=True)
        self._delete_old(directory)
        stem = os.path.join(directory, datetime.now().strftime("%Y%m%d-%H%M%S"))
        self._mic = self._open(f"{stem}-mic.wav")
        self._reply = self._open(f"{stem}-reply.wav")
        logger.info(f"🎙️ recording this session to {stem}-{{mic,reply}}.wav")

    @classmethod
    def _delete_old(cls, directory: str) -> None:
        cutoff = time.time() - cls.KEEP_DAYS * 24 * 3600
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if name.endswith(".wav") and os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                try:
                    os.remove(path)
                except OSError as e:
                    logger.warning(f"⚠️ Could not delete old recording {name}: {e!r}")

    @staticmethod
    def _open(path: str) -> wave.Wave_write:
        wav = wave.open(path, "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(OPENAI_RATE)
        return wav

    def mic(self, pcm: bytes) -> None:
        self._mic.writeframes(pcm)

    def reply(self, pcm: bytes) -> None:
        self._reply.writeframes(pcm)

    def close(self) -> None:
        self._mic.close()
        self._reply.close()
