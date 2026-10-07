from __future__ import annotations

import re
from typing import Any

from ..core.errors import ProviderError
from ..core.worker import Agent, AgentContext
from ..models import Stage
from ..providers import media

GAP_SECONDS = 0.35  # breath between beats - also where visual cuts land
MAX_TEMPO = 1.25


def speakable(text: str) -> str:
    """Light text normalisation for TTS engines (they stumble on symbols and ellipses)."""
    t = text.replace("…", "...").replace("—", ", ").replace("–", ", ").replace("&", " and ")
    t = re.sub(r"\.{2,}", ".", t)
    return re.sub(r"[*_#\[\]<>]", "", t).strip()


class VoiceoverAgent(Agent):
    """Synthesises narration per beat so every beat has an exact start/end - the editor uses these
    timings to cut visuals and time captions, which keeps A/V sync deterministic."""

    stage = Stage.VOICEOVER

    async def run(self, ctx: AgentContext) -> dict[str, Any]:
        beats = ctx.job.output(Stage.SCRIPT)["beats"]
        work = ctx.scratch

        # Pick the engine with the chain on beat 0, then pin it: one consistent voice per video.
        raw0 = work / "raw_00.wav"
        first = await ctx.providers.tts.run(lambda p: p.synth(speakable(beats[0]["text"]), raw0))
        engine = next(p for p in ctx.providers.tts.providers if p.name == first.provider)

        raws = [raw0]
        for i, beat in enumerate(beats[1:], start=1):
            raw = work / f"raw_{i:02d}.wav"
            try:
                await engine.synth(speakable(beat["text"]), raw)
            except ProviderError as e:
                raise ProviderError(f"{engine.name} failed mid-script on beat {i}: {e}") from e
            raws.append(raw)

        # Duration budget: Shorts cap at 60s. Speed narration up by at most MAX_TEMPO (still natural);
        # anything longer is a script problem and the QA gate routes it back to the script stage.
        raw_total = sum([await media.duration(r) for r in raws]) + GAP_SECONDS * len(raws)
        budget = ctx.settings.max_video_seconds - 1.5
        tempo = min(MAX_TEMPO, raw_total / budget) if raw_total > budget else 1.0

        timeline, parts, t = [], [], 0.0
        for i, raw in enumerate(raws):
            norm = work / f"beat_{i:02d}.wav"
            # uniform format + trailing breath; trim leading dead air some engines emit
            trim = "" if engine.name == "silent" else "silenceremove=start_periods=1:start_threshold=-50dB,"
            speed = f"atempo={tempo:.3f}," if tempo > 1.0 else ""
            await media.ffmpeg("-i", str(raw), "-af", f"{trim}{speed}apad=pad_dur={GAP_SECONDS}",
                               "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(norm), timeout=60)
            d = await media.duration(norm)
            timeline.append({"index": i, "start": round(t, 3), "end": round(t + d, 3), "duration": round(d, 3)})
            parts.append(norm)
            t += d

        listfile = work / "concat.txt"
        listfile.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
        narration = work / "narration.wav"
        await media.ffmpeg("-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(narration),
                           timeout=120)
        total = await media.duration(narration)
        key = await ctx.artifacts.put_file(narration, ctx.key(Stage.VOICEOVER, "narration.wav"))
        ctx.metrics.update(tts_provider=engine.name, fallback_attempts=first.attempts)
        return {
            "audio_key": key,
            "duration": round(total, 3),
            "timeline": timeline,
            "provider": engine.name,
            "tempo": round(tempo, 3),
            "degraded": engine.name == "silent" or first.degraded,
        }
