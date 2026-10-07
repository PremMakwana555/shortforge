from __future__ import annotations

import asyncio
import io
from typing import Any

from PIL import Image, ImageOps

from ..core.worker import Agent, AgentContext
from ..models import Stage
from ..providers.images import stable_seed, validate_image

STYLE = "cinematic horror photograph, moody low-key lighting, volumetric fog, 35mm film grain, vertical 9:16"
GEN_W, GEN_H = 768, 1344  # 9:16-ish at a size free tiers generate quickly; upscaled at render time


class VisualAgent(Agent):
    """One image per beat, generated concurrently with bounded parallelism (free tiers rate-limit)."""

    stage = Stage.VISUAL
    concurrency = 3

    async def run(self, ctx: AgentContext) -> dict[str, Any]:
        beats = ctx.job.output(Stage.SCRIPT)["beats"]
        W, H = ctx.settings.video_width, ctx.settings.video_height
        fb = ctx.feedback
        extra = ", brighter exposure, clear readable subject, high contrast" if fb else ""
        sem = asyncio.Semaphore(self.concurrency)

        async def one(i: int, beat: dict[str, str]) -> dict[str, Any]:
            prompt = f"{beat['visual']}, {STYLE}{extra}"
            seed = stable_seed(ctx.job.id, str(i), str(ctx.job.revision))
            async with sem:
                res = await ctx.providers.images.run(
                    lambda p: p.generate(prompt, GEN_W, GEN_H, seed),
                    validate=lambda data: validate_image(data) and None,
                )
            img = validate_image(res.value)
            img = ImageOps.fit(img, (W, H), method=Image.LANCZOS, centering=(0.5, 0.45))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=92)
            key = await ctx.artifacts.put_bytes(buf.getvalue(), ctx.key(Stage.VISUAL, f"beat_{i:02d}.jpg"),
                                                ctx.scratch)
            return {"index": i, "key": key, "provider": res.provider, "prompt": prompt, "seed": seed,
                    "fallback": res.degraded}

        frames = await asyncio.gather(*(one(i, b) for i, b in enumerate(beats)))
        providers = sorted({f["provider"] for f in frames})
        ctx.metrics.update(image_providers=providers, images=len(frames))
        return {"frames": list(frames), "providers": providers,
                "degraded": any(f["provider"] == "procedural" or f["fallback"] for f in frames)}
