"""Full pipeline with real FFmpeg, offline providers, reduced resolution for CI speed."""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import pytest

from shortforge.core.bus import InMemoryBus
from shortforge.models import JobStatus, Stage
from shortforge.providers import media
from shortforge.runtime.app import build

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed"),
]


async def test_offline_pipeline_renders_a_valid_short(settings):
    s = dataclasses.replace(
        settings, offline=True, llm_providers=["offline"], research_providers=["offline"],
        image_providers=["procedural"], tts_providers=["espeak", "silent"],
        video_width=360, video_height=640, render_preset="ultrafast",
    )
    rt = build(s)
    assert isinstance(rt.bus, InMemoryBus)
    rt.bus.start()
    job = await rt.orchestrator.submit("the radio station that broadcasts at 3am")
    await rt.bus.drain(timeout=900)
    await rt.close()

    job = await rt.state.get(job.id)
    assert job.status == JobStatus.COMPLETED, job.error
    qa = job.output(Stage.QA)
    assert qa["passed"], qa["checks"]

    out_dir = s.output_dir / job.id
    video = Path(job.output(Stage.PUBLISHING)["targets"]["local"]["path"])
    info = await media.probe(video)
    v = next(x for x in info["streams"] if x["codec_type"] == "video")
    assert (v["width"], v["height"]) == (360, 640)
    assert 15 <= float(info["format"]["duration"]) <= 59
    meta = json.loads((out_dir / "metadata.json").read_text())
    assert meta["ai_generated"] is True and "#shorts" in meta["hashtags"]
    assert (out_dir / "thumbnail.jpg").stat().st_size > 1000

    # Every stage recorded timing + provenance for observability
    assert all(job.stage(st).duration_ms is not None for st in Stage)
    assert job.output(Stage.VISUAL)["providers"] == ["procedural"]
