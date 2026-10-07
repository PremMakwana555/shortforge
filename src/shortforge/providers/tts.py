"""Text-to-speech. Open-source engines only, all running locally (no per-character cost, no quota):

* ``piper``  - neural TTS (Piper, GPL-3.0). Installed by ``uv sync``; the voice downloads on first use.
* ``espeak`` - espeak-ng formant synth (GPL). Robotic but always available in the container image.
* ``silent`` - timed silence sized to the text; captions still carry the story. Flagged degraded.

Every provider writes a mono WAV and returns its duration; the voiceover agent then normalises
loudness and sample rate with ffmpeg so downstream mixing is uniform.
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
from pathlib import Path

import httpx

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


VOICE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
_voice_lock = asyncio.Lock()


def voice_url(voice: str) -> str:
    """en_US-ryan-medium -> .../en/en_US/ryan/medium/en_US-ryan-medium"""
    lang_region, name, quality = voice.split("-", 2)
    return f"{VOICE_BASE}/{lang_region.split('_')[0]}/{lang_region}/{name}/{quality}/{voice}"


async def ensure_voice(model_path: Path, voice: str, timeout: float = 120) -> Path:
    """Download the Piper voice (.onnx + .onnx.json) once into the cache. Idempotent and atomic."""
    cfg = model_path.with_name(model_path.name + ".json")
    if model_path.is_file() and cfg.is_file():
        return model_path
    async with _voice_lock:
        if model_path.is_file() and cfg.is_file():
            return model_path
        model_path.parent.mkdir(parents=True, exist_ok=True)
        base = voice_url(voice)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            for suffix, dest in ((".onnx.json", cfg), (".onnx", model_path)):
                r = await c.get(base + suffix)
                if r.status_code != 200:
                    raise ProviderError(f"voice download failed ({r.status_code}): {base + suffix}")
                tmp = dest.with_name(dest.name + ".part")
                tmp.write_bytes(r.content)
                tmp.replace(dest)
    return model_path


class Piper(TTSProvider):
    """Neural TTS. ``piper-tts`` is a regular dependency; the voice is fetched on first use
    (or baked into the container image), so nothing needs installing by hand."""

    name = "piper"

    def __init__(self, settings: Settings):
        self.voice = settings.piper_voice
        self.model = Path(settings.piper_model_path).expanduser() if settings.piper_model_path else \
            settings.cache_dir / "voices" / f"{self.voice}.onnx"

    def available(self) -> bool:
        return importlib.util.find_spec("piper") is not None

    async def synth(self, text: str, out_wav: Path) -> float:
        try:
            model = await ensure_voice(self.model, self.voice)
        except (httpx.HTTPError, OSError) as e:
            raise ProviderError(f"piper voice unavailable: {e}") from e
        await media.run([sys.executable, "-m", "piper", "--model", str(model), "--output_file", str(out_wav),
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
