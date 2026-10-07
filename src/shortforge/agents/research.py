from __future__ import annotations

from typing import Any

from ..core.errors import AllProvidersFailed, ValidationFailed
from ..core.worker import Agent, AgentContext
from ..models import Stage
from ..providers.llm import generate_json
from . import prompts


def validate_brief(d: dict[str, Any]) -> dict[str, Any]:
    summary, angle = str(d.get("summary", "")).strip(), str(d.get("angle", "")).strip()
    facts = [str(f).strip() for f in d.get("facts", []) if str(f).strip()]
    keywords = [str(k).strip().lower() for k in d.get("keywords", []) if str(k).strip()]
    if len(summary) < 20:
        raise ValidationFailed("summary missing or too short")
    if not facts:
        raise ValidationFailed("facts must contain at least one item")
    return {"summary": summary[:600], "angle": angle[:400], "facts": facts[:6], "keywords": keywords[:6]}


class ResearchAgent(Agent):
    """Grounds the story: retrieves reference material, then distils it into a creative brief."""

    stage = Stage.RESEARCH

    async def run(self, ctx: AgentContext) -> dict[str, Any]:
        topic = ctx.job.topic
        try:
            res = await ctx.providers.research.run(lambda p: p.search(f"{topic} legend", limit=3))
            sources, research_provider = res.value, res.provider
        except AllProvidersFailed as e:
            sources, research_provider = [], f"none ({e.errors})"

        snippets = [s.extract for s in sources]
        snippet_text = "\n".join(f"- [{s.title}] {s.extract[:600]}" for s in sources) or "(none)"
        out = await generate_json(
            ctx.providers.llm, task="research_brief",
            system=prompts.RESEARCH_SYSTEM,
            user=prompts.RESEARCH_USER.format(topic=topic, niche=ctx.job.niche, snippets=snippet_text),
            context={"topic": topic, "snippets": snippets}, validate=validate_brief, temperature=0.4,
        )
        ctx.metrics.update(llm_provider=out.provider, llm_model=out.model, tokens=out.usage,
                           research_provider=research_provider, fallback_attempts=out.attempts)
        return {
            **out.data,
            "sources": [{"title": s.title, "url": s.url} for s in sources],
            "degraded": out.degraded or not sources,
            "prompt_version": prompts.PROMPT_VERSION,
        }
