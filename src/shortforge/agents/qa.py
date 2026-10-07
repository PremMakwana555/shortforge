from __future__ import annotations

import re
from typing import Any

from ..core.errors import AllProvidersFailed, ValidationFailed
from ..core.worker import Agent, AgentContext
from ..models import Stage
from ..providers import media
from ..providers.llm import generate_json
from . import prompts


def _check(name: str, passed: bool, value: Any, expect: str, route: Stage | None = None) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "value": value, "expect": expect,
            "route": route.value if route else None}


def validate_judge(d: dict[str, Any]) -> dict[str, Any]:
    try:
        scores = {k: int(d[k]) for k in ("hook", "coherence", "payoff")}
    except (KeyError, TypeError, ValueError) as e:
        raise ValidationFailed(f"judge output missing scores: {e}") from e
    if not all(1 <= v <= 10 for v in scores.values()):
        raise ValidationFailed("scores must be 1-10")
    return {**scores, "issues": [str(i)[:200] for i in d.get("issues", [])][:5]}


class QAAgent(Agent):
    """Automated release gate. Objective media checks + optional LLM-as-judge on the script.

    A failing check carries a ``route``: the earliest stage that can fix it. The worker then resets
    that stage and everything downstream (bounded by ``max_qa_revisions``) and re-runs the pipeline
    with the failure text as feedback - a closed loop instead of a dead end.
    """

    stage = Stage.QA

    async def run(self, ctx: AgentContext) -> dict[str, Any]:
        s = ctx.settings
        ed, vo, script = ctx.job.output(Stage.EDITING), ctx.job.output(Stage.VOICEOVER), ctx.job.output(Stage.SCRIPT)
        video = await ctx.artifacts.local_path(ed["video_key"])
        info = await media.probe(video)
        vstreams = [x for x in info["streams"] if x["codec_type"] == "video"]
        astreams = [x for x in info["streams"] if x["codec_type"] == "audio"]
        dur = float(info["format"]["duration"])
        checks: list[dict[str, Any]] = []

        checks.append(_check("has_video_stream", bool(vstreams), len(vstreams), "== 1", Stage.EDITING))
        checks.append(_check("has_audio_stream", bool(astreams), len(astreams), "== 1", Stage.EDITING))
        if vstreams:
            v = vstreams[0]
            res = f"{v.get('width')}x{v.get('height')}"
            checks.append(_check("resolution", res == f"{s.video_width}x{s.video_height}", res,
                                 f"{s.video_width}x{s.video_height}", Stage.EDITING))
            checks.append(_check("codec", v.get("codec_name") == "h264", v.get("codec_name"), "h264", Stage.EDITING))
        checks.append(_check("max_duration", dur <= s.max_video_seconds, round(dur, 2),
                             f"<= {s.max_video_seconds}s (Shorts limit)", Stage.SCRIPT))
        checks.append(_check("min_duration", dur >= 15, round(dur, 2), ">= 15s", Stage.SCRIPT))
        checks.append(_check("av_sync", abs(dur - ed["expected_duration"]) <= 0.5, round(dur - ed["expected_duration"], 2),
                             "|render - narration| <= 0.5s", Stage.EDITING))

        # Black frames: catches failed/blank image generations that slipped past per-image validation.
        err = await media.ffmpeg("-i", str(video), "-vf", "blackdetect=d=0.4:pix_th=0.06", "-an", "-f", "null", "-",
                                 timeout=300)
        black = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", err))
        checks.append(_check("black_frames_ratio", black / dur <= 0.15, round(black / dur, 3), "<= 0.15",
                             Stage.VISUAL))

        # Loudness: narration must actually be audible (skip if TTS was knowingly silent).
        err = await media.ffmpeg("-i", str(video), "-af", "volumedetect", "-vn", "-f", "null", "-", timeout=300)
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", err)
        mean_db = float(m.group(1)) if m else -99.0
        if vo.get("provider") != "silent":
            checks.append(_check("audio_level", -35 <= mean_db <= -8, mean_db, "-35..-8 dB mean", Stage.VOICEOVER))

        judge = await self._judge(ctx, script)
        if judge.get("scores"):
            sc = judge["scores"]
            ok = min(sc["hook"], sc["coherence"], sc["payoff"]) >= 5
            checks.append(_check("script_quality", ok, sc, "every score >= 5", Stage.SCRIPT))

        failed = [c for c in checks if not c["passed"]]
        warnings = [st.value for st in (Stage.RESEARCH, Stage.SCRIPT, Stage.VOICEOVER, Stage.VISUAL)
                    if ctx.job.output(st).get("degraded")]
        out: dict[str, Any] = {"passed": not failed, "checks": checks, "duration": round(dur, 2),
                               "mean_volume_db": mean_db, "degraded_stages": warnings, "judge": judge}
        if failed:
            order = list(Stage)
            target = min((Stage(c["route"]) for c in failed if c["route"]), key=order.index, default=Stage.EDITING)
            reasons = "; ".join(f"{c['name']}={c['value']} (expected {c['expect']})" for c in failed)
            if judge.get("issues") and target == Stage.SCRIPT:
                reasons += " | reviewer: " + "; ".join(judge["issues"])
            if any(c["name"] == "max_duration" for c in failed):
                reasons += f" | narration too long: cut to about {int(s.max_video_seconds * 2.4)} words"
            out["rework"] = {"stage": target.value, "feedback": reasons}
        return out

    async def _judge(self, ctx: AgentContext, script: dict[str, Any]) -> dict[str, Any]:
        real = [p for p in ctx.providers.llm.providers if p.name != "offline" and p.available()]
        if not real:
            return {"status": "skipped", "reason": "no LLM configured"}
        text = "\n".join(f"{i + 1}. {b['text']}" for i, b in enumerate(script["beats"]))
        try:
            res = await generate_json(ctx.providers.llm, task="judge", system=prompts.JUDGE_SYSTEM,
                                      user=f"Title: {script['title']}\nScript:\n{text}", context={},
                                      validate=validate_judge, temperature=0.1)
        except AllProvidersFailed as e:
            return {"status": "unavailable", "reason": str(e)[:300]}
        scores = {k: res.data[k] for k in ("hook", "coherence", "payoff")}
        return {"status": "ok", "provider": res.provider, "scores": scores, "issues": res.data["issues"]}
