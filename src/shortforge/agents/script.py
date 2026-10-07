from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..core.errors import ValidationFailed
from ..core.worker import Agent, AgentContext
from ..models import Stage
from ..providers.llm import generate_json
from . import prompts

# Topics that get a channel demonetised or flagged. The LLM is told to avoid them; this is the backstop.
BLOCKLIST = re.compile(r"\b(suicide|self[- ]harm|rape|molest|child abuse|nsfw|porn)\b", re.IGNORECASE)


def word_count(text: str) -> int:
    return len(re.findall(r"[\w'-]+", text))


def make_script_validator(min_words: int, max_words: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def validate(d: dict[str, Any]) -> dict[str, Any]:
        beats_in = d.get("beats")
        if not isinstance(beats_in, list):
            raise ValidationFailed("'beats' must be a list")
        beats = []
        for b in beats_in:
            if not isinstance(b, dict):
                raise ValidationFailed("each beat must be an object with 'text' and 'visual'")
            text = re.sub(r"\s+", " ", str(b.get("text", ""))).strip()
            visual = re.sub(r"\s+", " ", str(b.get("visual", ""))).strip()
            if text:
                beats.append({"text": text[:400], "visual": (visual or text)[:400]})
        if not 5 <= len(beats) <= 10:
            raise ValidationFailed(f"need 5-10 beats, got {len(beats)}")
        words = sum(word_count(b["text"]) for b in beats)
        if not min_words <= words <= max_words:
            raise ValidationFailed(f"narration is {words} words; must be {min_words}-{max_words}")
        full = " ".join(b["text"] for b in beats)
        if m := BLOCKLIST.search(full + " " + " ".join(b["visual"] for b in beats)):
            raise ValidationFailed(f"contains disallowed theme '{m.group(0)}' - rewrite without it")
        title = re.sub(r"\s+", " ", str(d.get("title", ""))).strip().strip('"')
        if not 5 <= len(title) <= 100:
            raise ValidationFailed("title must be 5-100 characters")
        tags = []
        for h in d.get("hashtags") or []:
            tag = "#" + re.sub(r"[^\w]", "", str(h))
            if len(tag) > 1 and tag.lower() not in {t.lower() for t in tags}:
                tags.append(tag)
        if "#shorts" not in {t.lower() for t in tags}:
            tags.append("#shorts")
        return {
            "title": title,
            "beats": beats,
            "description": str(d.get("description", "")).strip()[:900] or title,
            "hashtags": tags[:8],
            "word_count": words,
            "est_seconds": round(words / 2.6, 1),
        }

    return validate


class ScriptAgent(Agent):
    """Turns the brief into narrated beats + one image prompt per beat. Accepts QA feedback on rework."""

    stage = Stage.SCRIPT

    async def run(self, ctx: AgentContext) -> dict[str, Any]:
        s, brief = ctx.settings, ctx.job.output(Stage.RESEARCH)
        feedback = ctx.feedback
        fb = f"\nA reviewer rejected the previous version: {feedback}\nFix these issues." if feedback else ""
        out = await generate_json(
            ctx.providers.llm, task="script",
            system=prompts.SCRIPT_SYSTEM.format(min_words=s.target_words_min, max_words=s.target_words_max),
            user=prompts.SCRIPT_USER.format(
                topic=ctx.job.topic, summary=brief.get("summary", ""), angle=brief.get("angle", ""),
                facts="\n".join(f"- {f}" for f in brief.get("facts", [])), feedback=fb),
            context={"topic": ctx.job.topic, "brief": brief, "feedback": feedback, "revision": ctx.job.revision},
            validate=make_script_validator(s.target_words_min, s.target_words_max),
            temperature=0.9,
        )
        ctx.metrics.update(llm_provider=out.provider, llm_model=out.model, tokens=out.usage,
                           fallback_attempts=out.attempts)
        return {**out.data, "degraded": out.degraded, "prompt_version": prompts.PROMPT_VERSION}
