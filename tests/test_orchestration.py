"""Workflow semantics: ordering, idempotency, retries, resume, QA rework loop, stale messages."""

from __future__ import annotations

import pytest

from shortforge.core.errors import FatalError, LeaseBusy, RetryableError
from shortforge.models import JobStatus, Stage, StageMessage, StageStatus, now


async def _run(orch, bus, topic: str = "haunted lighthouse"):
    job = await orch.submit(topic)
    await bus.drain(timeout=10)
    return job


async def test_happy_path_runs_every_stage_once_in_order(make_orch):
    orch, state, bus, agents = make_orch()
    job = await _run(orch, bus)
    job = await state.get(job.id)
    assert job.status == JobStatus.COMPLETED
    assert all(a.calls == 1 for a in agents.values())
    started = [e["stage"] for e in job.events if e["kind"] == "stage_started"]
    assert started == [s.value for s in Stage]
    await bus.close()


async def test_duplicate_delivery_does_not_re_execute(make_orch):
    orch, state, bus, agents = make_orch()
    job = await _run(orch, bus)
    # Pub/Sub is at-least-once: replay every stage message after completion.
    for s in Stage:
        await bus.publish(StageMessage(job_id=job.id, stage=s))
    await bus.drain(timeout=10)
    assert all(a.calls == 1 for a in agents.values())
    await bus.close()


async def test_transient_failure_is_retried(make_orch):
    orch, state, bus, agents = make_orch({Stage.VISUAL: [RetryableError("429 rate limited")]})
    job = await _run(orch, bus)
    job = await state.get(job.id)
    assert job.status == JobStatus.COMPLETED
    assert job.stage(Stage.VISUAL).attempts == 2
    assert agents[Stage.VISUAL].calls == 2
    await bus.close()


async def test_exhausted_retries_fail_job_and_resume_reuses_completed_work(make_orch):
    boom = [RetryableError("down")] * 3
    orch, state, bus, agents = make_orch({Stage.EDITING: boom})
    job = await _run(orch, bus)
    job = await state.get(job.id)
    assert job.status == JobStatus.FAILED
    assert job.stage(Stage.EDITING).status == StageStatus.FAILED
    assert "editing" in (job.error or "")

    await orch.resume(job.id)
    await bus.drain(timeout=10)
    job = await state.get(job.id)
    assert job.status == JobStatus.COMPLETED
    # upstream stages were NOT recomputed - resumability from the failed stage
    assert agents[Stage.RESEARCH].calls == 1 and agents[Stage.VISUAL].calls == 1
    assert agents[Stage.EDITING].calls == 4
    await bus.close()


async def test_fatal_error_fails_immediately(make_orch):
    orch, state, bus, agents = make_orch({Stage.SCRIPT: [FatalError("bad topic")]})
    job = await _run(orch, bus)
    job = await state.get(job.id)
    assert job.status == JobStatus.FAILED
    assert agents[Stage.SCRIPT].calls == 1
    assert agents[Stage.VOICEOVER].calls == 0
    await bus.close()


async def test_qa_rework_loops_back_and_completes(make_orch):
    rework = {"passed": False, "rework": {"stage": "script", "feedback": "hook is weak"}}
    orch, state, bus, agents = make_orch({Stage.QA: [rework]})
    job = await _run(orch, bus)
    job = await state.get(job.id)
    assert job.status == JobStatus.COMPLETED
    assert job.revision == 1
    assert job.feedback["script"] == "hook is weak"
    assert agents[Stage.RESEARCH].calls == 1  # upstream of the rework target is kept
    assert agents[Stage.SCRIPT].calls == 2 and agents[Stage.EDITING].calls == 2
    assert job.output(Stage.SCRIPT)["revision"] == 1
    await bus.close()


async def test_qa_rework_budget_exhausted_needs_review(make_orch):
    rework = {"passed": False, "rework": {"stage": "visual", "feedback": "black frames"}}
    orch, state, bus, agents = make_orch({Stage.QA: [rework] * 5}, max_qa_revisions=2)
    job = await _run(orch, bus)
    job = await state.get(job.id)
    assert job.status == JobStatus.NEEDS_REVIEW
    assert job.revision == 2
    assert agents[Stage.QA].calls == 3
    assert agents[Stage.PUBLISHING].calls == 0
    await bus.close()


async def test_stale_revision_message_is_dropped(make_orch):
    orch, state, bus, agents = make_orch()
    job = await orch.submit("t")
    await bus.drain(timeout=10)

    def _bump(j):  # a QA rework happened after this message was published
        j.revision, j.status = 1, JobStatus.RUNNING
        j.stages["visual"].status = StageStatus.PENDING
    await state.mutate(job.id, _bump)
    before = agents[Stage.VISUAL].calls
    await orch.handle(StageMessage(job_id=job.id, stage=Stage.VISUAL, revision=0))
    assert agents[Stage.VISUAL].calls == before
    await bus.close()


async def test_live_lease_blocks_second_worker(make_orch):
    orch, state, bus, agents = make_orch()
    job = await orch.submit("t")
    await bus.drain(timeout=10)
    # simulate a stage currently leased by another worker
    def _lease(j):
        st = j.stage(Stage.PUBLISHING)
        st.status, st.lease_owner, st.lease_expires_at = StageStatus.RUNNING, "other", now() + 60
        j.status = JobStatus.RUNNING
    await state.mutate(job.id, _lease)
    with pytest.raises(LeaseBusy):
        await orch.handle(StageMessage(job_id=job.id, stage=Stage.PUBLISHING))
    await bus.close()


async def test_expired_lease_is_taken_over(make_orch):
    orch, state, bus, agents = make_orch()
    job = await orch.submit("t")
    await bus.drain(timeout=10)

    def _crashed(j):
        st = j.stage(Stage.PUBLISHING)
        st.status, st.lease_owner, st.lease_expires_at = StageStatus.RUNNING, "dead-worker", now() - 1
        j.status = JobStatus.RUNNING
    await state.mutate(job.id, _crashed)
    await orch.handle(StageMessage(job_id=job.id, stage=Stage.PUBLISHING))
    job = await state.get(job.id)
    assert job.status == JobStatus.COMPLETED
    assert any(e["kind"] == "lease_takeover" for e in job.events)
    await bus.close()


async def test_unknown_job_message_is_acked(make_orch):
    orch, *_ , bus, _agents = make_orch()
    await orch.handle(StageMessage(job_id="nope", stage=Stage.RESEARCH))  # must not raise
    await bus.close()
