"""Domain model: a Job moves through an ordered list of Stages.

The job document is the single source of truth for workflow state. It is stored as a plain
JSON dict (SQLite locally, Firestore on GCP) so every backend can do compare-and-set on it.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Stage(StrEnum):
    RESEARCH = "research"
    SCRIPT = "script"
    VOICEOVER = "voiceover"
    VISUAL = "visual"
    EDITING = "editing"
    QA = "qa"
    PUBLISHING = "publishing"


PIPELINE: list[Stage] = list(Stage)


def next_stage(stage: Stage) -> Stage | None:
    i = PIPELINE.index(stage)
    return PIPELINE[i + 1] if i + 1 < len(PIPELINE) else None


def downstream(stage: Stage) -> list[Stage]:
    """The stage itself and everything after it (used to invalidate on QA rework)."""
    return PIPELINE[PIPELINE.index(stage):]


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"  # QA exhausted its revision budget

    @property
    def terminal(self) -> bool:
        return self in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.NEEDS_REVIEW}


def now() -> float:
    return time.time()


class StageState(BaseModel):
    status: StageStatus = StageStatus.PENDING
    attempts: int = 0
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    duration_ms: int | None = None
    error: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)


class Job(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    topic: str
    niche: str = "horror"
    status: JobStatus = JobStatus.QUEUED
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    revision: int = 0  # number of QA-triggered rework loops
    feedback: dict[str, str] = Field(default_factory=dict)  # stage -> QA feedback for rework
    stages: dict[str, StageState] = Field(default_factory=lambda: {s.value: StageState() for s in PIPELINE})
    events: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None

    def stage(self, s: Stage | str) -> StageState:
        return self.stages[Stage(s).value]

    def output(self, s: Stage | str) -> dict[str, Any]:
        return self.stage(s).output

    def first_incomplete(self) -> Stage | None:
        for s in PIPELINE:
            if self.stage(s).status != StageStatus.COMPLETED:
                return s
        return None

    def log(self, kind: str, **data: Any) -> None:
        self.events.append({"ts": round(now(), 3), "kind": kind, **data})
        if len(self.events) > 300:  # keep the document bounded (Firestore 1 MiB limit)
            self.events = self.events[-300:]

    def to_doc(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Job:
        return cls.model_validate(doc)


class StageMessage(BaseModel):
    """Payload on the bus. Deliberately tiny: state lives in the store, not in messages."""

    job_id: str
    stage: Stage
    revision: int = 0
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
