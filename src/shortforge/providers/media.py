"""Thin async wrappers around ffmpeg / ffprobe. Every call has a timeout and captures stderr so a
failed render produces an actionable error instead of a hung worker."""

from __future__ import annotations

import asyncio
import json
import shutil
import wave
from pathlib import Path
from typing import Any

from ..core.errors import FatalError, RetryableError


def require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise FatalError(f"'{binary}' not found on PATH - install it (e.g. apt-get install ffmpeg)")
    return path


async def run(cmd: list[str], timeout: float = 600, input_bytes: bytes | None = None) -> tuple[bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input_bytes), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RetryableError(f"{Path(cmd[0]).name} timed out after {timeout}s") from None
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip().splitlines()[-6:]
        raise RetryableError(f"{Path(cmd[0]).name} exited {proc.returncode}: {' | '.join(tail)}")
    return out, err


async def ffmpeg(*args: str, timeout: float = 600) -> str:
    _, err = await run([require("ffmpeg"), "-hide_banner", "-y", "-nostdin", *args], timeout=timeout)
    return err.decode(errors="replace")


async def probe(path: Path) -> dict[str, Any]:
    out, _ = await run([require("ffprobe"), "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(path)], timeout=60)
    return json.loads(out)


async def duration(path: Path) -> float:
    info = await probe(path)
    return float(info["format"]["duration"])


def write_silence(path: Path, seconds: float, rate: int = 24000) -> None:
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * frames)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())
