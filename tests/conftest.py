from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from shortforge.config import Settings
from shortforge.core.artifacts import LocalArtifactStore
from shortforge.core.bus import InMemoryBus
from shortforge.core.resilience import reset_breakers
from shortforge.core.state import MemoryStateStore
from shortforge.core.worker import Agent, AgentContext, Orchestrator
from shortforge.models import Stage


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reset_breakers()
    for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "HF_API_TOKEN", "PEXELS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SF_ENV_FILE", str(tmp_path / "none.env"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return dataclasses.replace(Settings.from_env(), data_dir=tmp_path / "data", output_dir=tmp_path / "out",
                               max_stage_attempts=3, max_qa_revisions=2)


class FakeAgent(Agent):
    """Scriptable agent: ``plan`` is a list of outcomes consumed per call (Exception or dict)."""

    def __init__(self, stage: Stage, plan: list[Any] | None = None):
        self.stage = stage
        self.plan = list(plan or [])
        self.calls = 0

    async def run(self, ctx: AgentContext) -> dict[str, Any]:
        self.calls += 1
        if self.plan:
            nxt = self.plan.pop(0)
            if isinstance(nxt, BaseException):
                raise nxt
            return dict(nxt)
        return {"ok": True, "stage": self.stage.value, "revision": ctx.job.revision}


@pytest.fixture
def make_orch(settings: Settings, tmp_path: Path):
    def _make(plans: dict[Stage, list[Any]] | None = None, **overrides: Any):
        s = dataclasses.replace(settings, **overrides)
        state = MemoryStateStore()
        bus = InMemoryBus(max_deliveries=6, backoff_base=0.01, backoff_cap=0.05)
        agents = {st: FakeAgent(st, (plans or {}).get(st)) for st in Stage}
        orch = Orchestrator(s, state, bus, LocalArtifactStore(tmp_path / "art"), providers=None,
                            agents=agents)  # type: ignore[arg-type]
        for st in Stage:
            bus.subscribe(st, orch.handle)
        bus.start()
        return orch, state, bus, agents

    return _make
