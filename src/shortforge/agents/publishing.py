from __future__ import annotations

import json
from typing import Any

from ..core.errors import FatalError
from ..core.worker import Agent, AgentContext
from ..models import Stage


class PublishingAgent(Agent):
    """Fans out to every configured publisher. Each success is written to a receipts file *before*
    moving on, so a retry after a partial failure never double-uploads to a target that succeeded."""

    stage = Stage.PUBLISHING

    async def run(self, ctx: AgentContext) -> dict[str, Any]:
        qa = ctx.job.output(Stage.QA)
        if not qa.get("passed"):
            raise FatalError("refusing to publish: QA did not pass")
        script, ed = ctx.job.output(Stage.SCRIPT), ctx.job.output(Stage.EDITING)
        video = await ctx.artifacts.local_path(ed["video_key"])
        thumb = await ctx.artifacts.local_path(ed["thumbnail_key"])
        meta = {
            "job_id": ctx.job.id, "topic": ctx.job.topic, "title": script["title"],
            "description": f"{script['description']}\n\n{' '.join(script['hashtags'])}",
            "hashtags": script["hashtags"], "duration": qa["duration"],
            "ai_generated": True, "degraded_stages": qa.get("degraded_stages", []),
            "sources": ctx.job.output(Stage.RESEARCH).get("sources", []),
        }

        receipts_key = ctx.key(Stage.PUBLISHING, "receipts.json")
        receipts: dict[str, Any] = {}
        if await ctx.artifacts.exists(receipts_key):
            receipts = json.loads((await ctx.artifacts.local_path(receipts_key)).read_text())

        for pub in ctx.providers.publishers:
            if pub.name in receipts:
                continue  # already done on a previous attempt - idempotent
            if not pub.available():
                receipts[pub.name] = {"skipped": "not configured"}
            else:
                receipts[pub.name] = await pub.publish(video, thumb, meta, ctx.job.id)
            await ctx.artifacts.put_text(json.dumps(receipts), receipts_key, ctx.scratch)
        # The rendered video always lives in the artifact store (GCS on GCP) - record it as a target.
        receipts["artifact"] = {"video_key": ed["video_key"], "thumbnail_key": ed["thumbnail_key"]}
        return {"targets": receipts, "title": script["title"]}
