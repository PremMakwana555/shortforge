"""Workflow state store.

All state transitions go through ``mutate(job_id, fn)``: a transactional read-modify-write.
That single primitive is enough to implement lease claims, completion and QA rework atomically
on any backend that supports compare-and-set (SQLite ``BEGIN IMMEDIATE``, Firestore transactions).
"""

from __future__ import annotations

import abc
import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from ..models import Job, now

T = TypeVar("T")


class JobNotFound(KeyError):
    pass


class StateStore(abc.ABC):
    @abc.abstractmethod
    async def create(self, job: Job) -> Job: ...

    @abc.abstractmethod
    async def get(self, job_id: str) -> Job: ...

    @abc.abstractmethod
    async def list(self, limit: int = 20) -> list[Job]: ...

    @abc.abstractmethod
    async def mutate(self, job_id: str, fn: Callable[[Job], T]) -> tuple[Job, T]:
        """Atomically load job, apply ``fn`` (which mutates it in place), persist, return (job, fn result).

        If ``fn`` raises, nothing is persisted and the exception propagates.
        """


class SQLiteStateStore(StateStore):
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                " id TEXT PRIMARY KEY, topic TEXT NOT NULL, status TEXT NOT NULL,"
                " data TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS ix_jobs_updated ON jobs(updated_at)")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _write(self, c: sqlite3.Connection, job: Job) -> None:
        job.updated_at = now()
        c.execute(
            "INSERT INTO jobs(id, topic, status, data, created_at, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data, "
            "updated_at=excluded.updated_at",
            (job.id, job.topic, job.status.value, json.dumps(job.to_doc()), job.created_at, job.updated_at),
        )

    async def create(self, job: Job) -> Job:
        def _do() -> Job:
            with self._lock, self._conn() as c:
                if c.execute("SELECT 1 FROM jobs WHERE id=?", (job.id,)).fetchone():
                    raise ValueError(f"job {job.id} already exists")
                self._write(c, job)
            return job

        return await asyncio.to_thread(_do)

    async def get(self, job_id: str) -> Job:
        def _do() -> Job:
            with self._conn() as c:
                row = c.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise JobNotFound(job_id)
            return Job.from_doc(json.loads(row[0]))

        return await asyncio.to_thread(_do)

    async def list(self, limit: int = 20) -> list[Job]:
        def _do() -> list[Job]:
            with self._conn() as c:
                rows = c.execute("SELECT data FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            return [Job.from_doc(json.loads(r[0])) for r in rows]

        return await asyncio.to_thread(_do)

    async def mutate(self, job_id: str, fn: Callable[[Job], T]) -> tuple[Job, T]:
        def _do() -> tuple[Job, T]:
            with self._lock:
                c = self._conn()
                try:
                    c.execute("BEGIN IMMEDIATE")  # take the write lock before reading -> serialisable CAS
                    row = c.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
                    if not row:
                        raise JobNotFound(job_id)
                    job = Job.from_doc(json.loads(row[0]))
                    result = fn(job)
                    self._write(c, job)
                    c.execute("COMMIT")
                    return job, result
                except BaseException:
                    c.execute("ROLLBACK")
                    raise
                finally:
                    c.close()

        return await asyncio.to_thread(_do)


class MemoryStateStore(StateStore):
    """For unit tests."""

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def create(self, job: Job) -> Job:
        async with self._lock:
            if job.id in self._docs:
                raise ValueError(f"job {job.id} already exists")
            self._docs[job.id] = job.to_doc()
        return job

    async def get(self, job_id: str) -> Job:
        if job_id not in self._docs:
            raise JobNotFound(job_id)
        return Job.from_doc(json.loads(json.dumps(self._docs[job_id])))

    async def list(self, limit: int = 20) -> list[Job]:
        jobs = [Job.from_doc(d) for d in self._docs.values()]
        return sorted(jobs, key=lambda j: j.updated_at, reverse=True)[:limit]

    async def mutate(self, job_id: str, fn: Callable[[Job], T]) -> tuple[Job, T]:
        async with self._lock:
            job = await self.get(job_id)
            result = fn(job)
            job.updated_at = now()
            self._docs[job_id] = job.to_doc()
            return job, result
