"""GCP implementations of Bus / StateStore / ArtifactStore. Imported lazily (``uv sync --extra gcp``).

Mapping to the local implementations:
  InMemoryBus        -> Pub/Sub topic per stage, push subscription -> Cloud Run ``/pubsub/{stage}``
  SQLiteStateStore   -> Firestore document per job, transactions for every mutate()
  LocalArtifactStore -> GCS bucket, files cached in the container's /tmp for FFmpeg
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from ..core.artifacts import ArtifactStore
from ..core.bus import Bus
from ..core.state import JobNotFound, StateStore
from ..models import Job, StageMessage, now

T = TypeVar("T")


def topic_name(prefix: str, stage: str) -> str:
    return f"{prefix}-{stage}"


class PubSubBus(Bus):
    def __init__(self, project: str, prefix: str):
        from google.cloud import pubsub_v1

        self.project, self.prefix = project, prefix
        self.client = pubsub_v1.PublisherClient()

    async def publish(self, msg: StageMessage) -> None:
        topic = self.client.topic_path(self.project, topic_name(self.prefix, msg.stage.value))
        fut = self.client.publish(topic, msg.model_dump_json().encode(), job_id=msg.job_id,
                                  stage=msg.stage.value, revision=str(msg.revision))
        await asyncio.to_thread(fut.result, timeout=30)


class FirestoreStateStore(StateStore):
    def __init__(self, project: str, collection: str):
        from google.cloud import firestore

        self._fs = firestore
        self.db = firestore.AsyncClient(project=project)
        self.col = self.db.collection(collection)

    async def create(self, job: Job) -> Job:
        await self.col.document(job.id).create(job.to_doc())  # fails if it exists -> no silent overwrite
        return job

    async def get(self, job_id: str) -> Job:
        snap = await self.col.document(job_id).get()
        if not snap.exists:
            raise JobNotFound(job_id)
        return Job.from_doc(snap.to_dict())

    async def list(self, limit: int = 20) -> list[Job]:
        q = self.col.order_by("updated_at", direction=self._fs.Query.DESCENDING).limit(limit)
        return [Job.from_doc(d.to_dict()) async for d in q.stream()]

    async def mutate(self, job_id: str, fn: Callable[[Job], T]) -> tuple[Job, T]:
        ref = self.col.document(job_id)
        transaction = self.db.transaction(max_attempts=10)

        @self._fs.async_transactional
        async def _txn(tx) -> tuple[Job, T]:  # type: ignore[no-untyped-def]
            snap = await ref.get(transaction=tx)
            if not snap.exists:
                raise JobNotFound(job_id)
            job = Job.from_doc(snap.to_dict())
            result = fn(job)  # must be pure w.r.t. external side effects: Firestore may re-run it
            job.updated_at = now()
            tx.set(ref, job.to_doc())
            return job, result

        return await _txn(transaction)


class GCSArtifactStore(ArtifactStore):
    def __init__(self, bucket: str, project: str | None = None):
        from google.cloud import storage

        self.bucket = storage.Client(project=project).bucket(bucket)
        self.cache = Path(tempfile.gettempdir()) / "shortforge-cache"

    async def put_file(self, local: Path, key: str) -> str:
        blob = self.bucket.blob(key)
        await asyncio.to_thread(blob.upload_from_filename, str(local))
        cached = self.cache / key  # write-through so this instance never reads its own stale copy
        cached.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, local, cached)
        return key

    async def local_path(self, key: str) -> Path:
        dest = self.cache / key
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        blob = self.bucket.blob(key)
        if not await asyncio.to_thread(blob.exists):
            raise FileNotFoundError(key)
        tmp = dest.with_suffix(dest.suffix + ".part")
        await asyncio.to_thread(blob.download_to_filename, str(tmp))
        tmp.replace(dest)
        return dest

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self.bucket.blob(key).exists)
