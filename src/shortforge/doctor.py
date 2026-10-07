"""``shortforge doctor`` - checks everything outside the Python environment and says how to fix it.

uv installs every Python dependency; the only things it cannot install are system binaries
(ffmpeg) and network-fetched assets (the Piper voice). ``doctor --fix`` fetches the voice.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import subprocess
from dataclasses import dataclass

from .config import Settings
from .providers.tts import ensure_voice


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True


def _install_hint(pkg_brew: str, pkg_apt: str) -> str:
    return f"brew install {pkg_brew}" if platform.system() == "Darwin" else f"sudo apt-get install -y {pkg_apt}"


def _ffmpeg_checks() -> list[Check]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    hint = _install_hint("ffmpeg", "ffmpeg")
    checks = [Check("ffmpeg", bool(ffmpeg), ffmpeg or "not found", hint),
              Check("ffprobe", bool(ffprobe), ffprobe or "not found", hint)]
    if ffmpeg:
        filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        missing = [f for f in ("subtitles", "zoompan", "loudnorm", "blackdetect") if f" {f} " not in filters]
        checks.append(Check("ffmpeg filters", not missing,
                            "subtitles, zoompan, loudnorm, blackdetect" if not missing else f"missing: {', '.join(missing)}",
                            f"{hint}  (needs a build with libass)"))
    return checks


def run(fix: bool = False) -> int:
    s = Settings.from_env()
    checks = _ffmpeg_checks()

    from .providers.tts import Piper

    piper = Piper(s)
    voice_ok = piper.model.is_file()
    if not voice_ok and fix:
        try:
            asyncio.run(ensure_voice(piper.model, piper.voice))
            voice_ok = True
        except Exception as e:  # noqa: BLE001 - report, don't crash the doctor
            print(f"  voice download failed: {e}")
    checks.append(Check("piper voice", voice_ok, str(piper.model),
                        "uv run shortforge doctor --fix   (or it downloads automatically on first run)",
                        required=False))
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    checks.append(Check("espeak-ng (fallback voice)", bool(espeak), espeak or "not found",
                        _install_hint("espeak-ng", "espeak-ng"), required=False))

    keys = {"GROQ_API_KEY": s.groq_api_key, "OPENROUTER_API_KEY": s.openrouter_api_key,
            "GEMINI_API_KEY": s.gemini_api_key, "HF_API_TOKEN": s.hf_api_token, "PEXELS_API_KEY": s.pexels_api_key}
    have = [k for k, v in keys.items() if v]
    checks.append(Check("LLM / image keys", bool(have), ", ".join(have) or "none - offline writer will be used",
                        "add a free GROQ_API_KEY to .env for real scripts", required=False))

    width = max(len(c.name) for c in checks)
    for c in checks:
        mark = "ok  " if c.ok else ("FAIL" if c.required else "warn")
        print(f"[{mark}] {c.name:<{width}}  {c.detail}")
        if not c.ok and c.fix:
            print(f"       {'':<{width}}  fix: {c.fix}")
    failed = [c for c in checks if c.required and not c.ok]
    print("\nready." if not failed else f"\n{len(failed)} required check(s) failed.")
    return 1 if failed else 0
