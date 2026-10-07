"""Stage worker / orchestrator.

One ``Orchestrator.handle`` call == one message delivery for one stage. The same code runs
in-process locally and behind a Cloud Run Pub/Sub push endpoint. Guarantees:

* **Idempotent under at-least-once delivery** - a completed stage is never re-executed; a duplicate
  delivery just re-emits the downstream message (downstream dedupes the same way).
* **Single executor per stage** - a lease (owner + expiry) is claimed transactionally. A crashed
  worker's lease expires and the next delivery takes over.
* **Stale message rejection** - every message carries the job ``revision``; after a QA rework
  bumps the revision, in-flight messages from the old revision are dropped.
* **Bounded retries** - transient errors nack (bus backoff); after ``max_stage_attempts`` the job
  fails with the last error recorded, and ``resume`` restarts from the failed stage.
"""

from __future__ import annotations

import abc
import asyncio
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import log
from ..config import Settings
from ..models import Job, JobStatus, Stage, StageMessage, StageStatus, downstream, next_stage, now
from .artifacts import ArtifactStore
from .bus import Bus
from .errors import FatalError, LeaseBusy
from .state import JobNotFound, StateStore

_log = log.get("shortforge.worker")


@dataclass
class AgentContext:
    job: Job
    settings: Settings
    artifacts: ArtifactStore
    providers: Any  # providers.registry.Providers - typed loosely to avoid an import cycle
    scratch: Path
    stage: Stage
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def feedback(self) -> str | None:
        """QA feedback addressed to this stage on a rework pass (None on the first pass)."""
        return self.job.feedback.get(self.stage.value)

    def key(self, stage: Stage, name: str) -> str:
        # revision in the path: QA rework never overwrites artifacts an older revision may still read
        return f"jobs/{self.job.id}/r{self.job.revision}/{stage.value}/{name}"


class Agent(abc.ABC):
    stage: Stage

    @abc.abstractmethod
    async def run(self, ctx: AgentContext) -> dict[str, Any]:
        """Do the stage's work and return its JSON-serialisable output.

        Special key for the QA agent: ``{"rework": {"stage": "<stage>", "feedback": "..."}}``
        sends the job back to that stage instead of continuing.
        """


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        state: StateStore,
        bus: Bus,
        artifacts: ArtifactStore,
        providers: Any,
        agents: dict[Stage, Agent],
    ):
        self.settings = settings
        self.state = state
        self.bus = bus
        self.artifacts = artifacts
        self.providers = providers
        self.agents = agents
        self.worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------------ control plane
    async def submit(self, topic: str, job_id: str | None = None) -> Job:
        topic = topic.strip()
        if not topic or len(topic) > 200:
            raise FatalError("topic must be 1..200 characters")
        job = Job(topic=topic, niche=self.settings.niche, **({"id": job_id} if job_id else {}))
        job.log("submitted", topic=topic)
        await self.state.create(job)
        await self.bus.publish(StageMessage(job_id=job.id, stage=Stage.RESEARCH, revision=0))
        log.kv(_log, 20, "job submitted", job_id=job.id, topic=topic)
        return job

    async def resume(self, job_id: str) -> Job:
        """Restart a failed / stuck job from its first incomplete stage. Completed work is reused."""

        def _reset(job: Job) -> Stage | None:
            if job.status == JobStatus.COMPLETED:
                return None
            stage = job.first_incomplete()
            if stage is None:
                job.status = JobStatus.COMPLETED
                return None
            st = job.stage(stage)
            st.status, st.attempts, st.error = StageStatus.PENDING, 0, None
            st.lease_owner = st.lease_expires_at = None
            if job.status == JobStatus.NEEDS_REVIEW:
                job.revision += 1  # explicit human go-ahead: one more QA cycle
            job.status, job.error = JobStatus.RUNNING, None
            job.log("resumed", stage=stage.value)
            return stage

        job, stage = await self.state.mutate(job_id, _reset)
        if stage:
            await self.bus.publish(StageMessage(job_id=job_id, stage=stage, revision=job.revision))
        return job

    # ------------------------------------------------------------------ data plane
    async def handle(self, msg: StageMessage) -> None:
        log.clear()
        log.bind(job_id=msg.job_id, stage=msg.stage.value, trace_id=msg.trace_id, worker=self.worker_id)
        try:
            job = await self.state.get(msg.job_id)
        except JobNotFound:
            log.kv(_log, 30, "unknown job - dropping message")
            return
        if job.status.terminal:
            return
        if msg.revision < job.revision:
            log.kv(_log, 20, "stale revision - dropping", msg_revision=msg.revision, job_revision=job.revision)
            return

        verdict = await self._claim(msg)
        if verdict == "done":
            await self._emit_next(msg.job_id, msg.stage)  # crash-between-commit-and-publish recovery
            return
        if verdict == "busy":
            raise LeaseBusy(f"{msg.stage} lease held by another worker")
        if verdict in ("exhausted", "skip"):
            return

        agent = self.agents[msg.stage]
        job = await self.state.get(msg.job_id)
        ctx = AgentContext(
            job=job, settings=self.settings, artifacts=self.artifacts, providers=self.providers,
            scratch=self.artifacts.scratch_dir(job.id, msg.stage.value), stage=msg.stage,
        )
        t0 = time.perf_counter()
        log.kv(_log, 20, "stage started", attempt=job.stage(msg.stage).attempts)
        try:
            timeout = max(30, self.settings.stage_lease_seconds - 30)
            output = await asyncio.wait_for(agent.run(ctx), timeout=timeout)
        except FatalError as e:
            await self._fail(msg, f"fatal: {e}", fatal=True)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # retryable by default, bounded by max_stage_attempts
            ms = int((time.perf_counter() - t0) * 1000)
            exhausted = await self._fail(msg, f"{type(e).__name__}: {e}", fatal=False, duration_ms=ms)
            log.kv(_log, 40 if exhausted else 30, "stage failed", error=str(e)[:400], latency_ms=ms,
                   exhausted=exhausted)
            if not exhausted:
                raise  # nack -> bus redelivers with backoff
            return

        ms = int((time.perf_counter() - t0) * 1000)
        if ctx.metrics:
            output = {**output, "_metrics": ctx.metrics}
        await self._complete(msg, output, ms)

    # ------------------------------------------------------------------ transitions
    async def _claim(self, msg: StageMessage) -> str:
        me, lease = self.worker_id, self.settings.stage_lease_seconds

        def _do(job: Job) -> str:
            if job.status.terminal or msg.revision < job.revision:
                return "skip"
            idx = list(Stage).index(msg.stage)
            if any(job.stage(s).status != StageStatus.COMPLETED for s in list(Stage)[:idx]):
                return "skip"  # out-of-order / pre-rework message; resume() re-drives correctly
            st = job.stage(msg.stage)
            if st.status == StageStatus.COMPLETED:
                return "done"
            if (st.status == StageStatus.RUNNING and st.lease_owner != me
                    and st.lease_expires_at and st.lease_expires_at > now()):
                return "busy"
            if st.attempts >= self.settings.max_stage_attempts:
                st.status = StageStatus.FAILED
                job.status = JobStatus.FAILED
                job.error = f"{msg.stage.value}: attempts exhausted ({st.error})"
                return "exhausted"
            if st.status == StageStatus.RUNNING:
                job.log("lease_takeover", stage=msg.stage.value, previous_owner=st.lease_owner)
            st.status = StageStatus.RUNNING
            st.attempts += 1
            st.lease_owner, st.lease_expires_at = me, now() + lease
            st.started_at, st.error = now(), None
            job.status = JobStatus.RUNNING
            job.log("stage_started", stage=msg.stage.value, attempt=st.attempts, worker=me)
            return "claimed"

        _, verdict = await self.state.mutate(msg.job_id, _do)
        return verdict

    async def _complete(self, msg: StageMessage, output: dict[str, Any], duration_ms: int) -> None:
        me = self.worker_id
        max_rev = self.settings.max_qa_revisions

        def _do(job: Job) -> tuple[str, Stage | None]:
            st = job.stage(msg.stage)
            if st.lease_owner != me or msg.revision < job.revision:
                return "lost_lease", None  # someone took over (we were too slow) - discard result
            st.status, st.output = StageStatus.COMPLETED, output
            st.finished_at, st.duration_ms = now(), duration_ms
            st.lease_owner = st.lease_expires_at = None
            job.log("stage_completed", stage=msg.stage.value, ms=duration_ms)

            rework = output.get("rework")
            if rework:
                target = Stage(rework["stage"])
                if job.revision >= max_rev:
                    job.status = JobStatus.NEEDS_REVIEW
                    job.error = f"QA failed after {job.revision} revisions: {rework.get('feedback')}"
                    job.log("needs_review", reason=rework.get("feedback"))
                    return "needs_review", None
                job.revision += 1
                job.feedback[target.value] = str(rework.get("feedback", ""))[:2000]
                for s in downstream(target):
                    job.stages[s.value] = type(st)()  # reset to pending, discard stale outputs
                job.log("rework", target=target.value, revision=job.revision, feedback=rework.get("feedback"))
                return "rework", target

            nxt = next_stage(msg.stage)
            if nxt is None:
                job.status = JobStatus.COMPLETED
                job.log("job_completed", total_s=round(now() - job.created_at, 1))
                return "completed", None
            return "next", nxt

        job, (verdict, target) = await self.state.mutate(msg.job_id, _do)
        log.kv(_log, 20, f"stage {verdict}", latency_ms=duration_ms)
        if verdict in ("next", "rework") and target:
            await self.bus.publish(StageMessage(job_id=job.id, stage=target, revision=job.revision))

    async def _fail(self, msg: StageMessage, error: str, *, fatal: bool, duration_ms: int | None = None) -> bool:
        me = self.worker_id
        max_attempts = self.settings.max_stage_attempts

        def _do(job: Job) -> bool:
            st = job.stage(msg.stage)
            if st.lease_owner != me:
                return False
            st.error, st.duration_ms = error[:1000], duration_ms
            st.lease_owner = st.lease_expires_at = None
            exhausted = fatal or st.attempts >= max_attempts
            st.status = StageStatus.FAILED if exhausted else StageStatus.RETRYING
            job.log("stage_failed", stage=msg.stage.value, attempt=st.attempts, error=error[:300],
                    final=exhausted)
            if exhausted:
                job.status, job.error = JobStatus.FAILED, f"{msg.stage.value}: {error[:500]}"
            return exhausted

        _, exhausted = await self.state.mutate(msg.job_id, _do)
        return exhausted

    async def _emit_next(self, job_id: str, stage: Stage) -> None:
        job = await self.state.get(job_id)
        nxt = next_stage(stage)
        if nxt and job.stage(nxt).status != StageStatus.COMPLETED and not job.status.terminal:
            await self.bus.publish(StageMessage(job_id=job_id, stage=nxt, revision=job.revision))
