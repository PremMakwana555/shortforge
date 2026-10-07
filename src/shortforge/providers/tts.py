"""Text-to-speech. Open-source engines only, all running locally (no per-character cost, no quota):

* ``piper``  - neural TTS (Piper, GPL-3.0). Needs ``uv sync --extra tts`` + a voice ``.onnx``.
* ``espeak`` - espeak-ng formant synth (GPL). Robotic but always available in the container image.
* ``silent`` - timed silence sized to the text; captions still carry the story. Flagged degraded.

Every provider writes a mono WAV and returns its duration; the voiceover agent then normalises
loudness and sample rate with ffmpeg so downstream mixing is uniform.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import Settings
from ..core.errors import ProviderError
from ..core.resilience import ProviderChain
from . import media


class TTSProvider:
    name = "base"

    def available(self) -> bool:
        return True

    async def synth(self, text: str, out_wav: Path) -> float:
        raise NotImplementedError


class Piper(TTSProvider):
    name = "piper"

    def __init__(self, settings: Settings):
        self.model = settings.piper_model_path

    def available(self) -> bool:
        return bool(self.model and Path(self.model).is_file() and shutil.which("piper"))

    async def synth(self, text: str, out_wav: Path) -> float:
        await media.run(["piper", "--model", self.model, "--output_file", str(out_wav),
                         "--sentence_silence", "0.25"], timeout=180, input_bytes=text.encode())
        if not out_wav.exists() or out_wav.stat().st_size < 1000:
            raise ProviderError("piper produced no audio")
        return await media.duration(out_wav)


class Espeak(TTSProvider):
    name = "espeak"

    def __init__(self, settings: Settings):
        self.voice, self.wpm = settings.espeak_voice, settings.espeak_speed_wpm
        self.bin = shutil.which("espeak-ng") or shutil.which("espeak")

    def available(self) -> bool:
        return self.bin is not None

    async def synth(self, text: str, out_wav: Path) -> float:
        assert self.bin
        await media.run([self.bin, "-v", self.voice, "-s", str(self.wpm), "-p", "35", "-g", "4",
                         "-w", str(out_wav), text], timeout=120)
        if not out_wav.exists() or out_wav.stat().st_size < 1000:
            raise ProviderError("espeak produced no audio")
        return media.wav_duration(out_wav)


class Silent(TTSProvider):
    name = "silent"
    words_per_second = 2.5

    async def synth(self, text: str, out_wav: Path) -> float:
        seconds = max(1.5, len(text.split()) / self.words_per_second + 0.3)
        media.write_silence(out_wav, seconds)
        return seconds


def build_tts_chain(settings: Settings) -> ProviderChain[TTSProvider]:
    catalog = {"piper": lambda: Piper(settings), "espeak": lambda: Espeak(settings), "silent": Silent}
    return ProviderChain("tts", [catalog[n]() for n in settings.tts_providers if n in catalog])
