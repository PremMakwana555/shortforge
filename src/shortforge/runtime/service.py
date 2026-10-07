"""HTTP service - the Cloud Run entrypoint.

* Control plane: ``POST /jobs``, ``GET /jobs``, ``GET /jobs/{id}``, ``POST /jobs/{id}/resume``
* Data plane:    ``POST /pubsub/{stage}`` - Pub/Sub *push* endpoint. 2xx = ack; 5xx = nack, and
  Pub/Sub redelivers with the subscription's exponential backoff, eventually dead-lettering.

Each Cloud Run service runs the same image; ``SF_SERVICE_ROLE`` decides which stages it accepts, so
the heavy FFmpeg editor can get 4 vCPU / 8 GiB while research runs on 1 vCPU - independent scaling.
Service-to-service auth is Cloud Run IAM (push subscriptions carry an OIDC token); ``SF_API_TOKEN``
optionally adds a bearer check on the control plane.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from .. import log
from ..core.bus import InMemoryBus
from ..core.errors import FatalError, LeaseBusy
from ..core.state import JobNotFound
from ..models import Stage, StageMessage
from .app import Runtime, build

_log = log.get("shortforge.service")


class CreateJob(BaseModel):
    topic: str = Field(min_length=3, max_length=200)


def create_app(runtime: Runtime | None = None) -> FastAPI:
    rt_holder: dict[str, Runtime] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        rt = runtime or build()
        rt_holder["rt"] = rt
        if isinstance(rt.bus, InMemoryBus):  # local docker run: process the pipeline in-process
            rt.bus.start()
        yield
        await rt.close()

    app = FastAPI(title="shortforge", version="0.1.0", lifespan=lifespan)
    api_token = os.environ.get("SF_API_TOKEN", "")

    def rt() -> Runtime:
        return rt_holder["rt"]

    def auth(authorization: str | None) -> None:
        if api_token and authorization != f"Bearer {api_token}":
            raise HTTPException(401, "invalid token")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        r = rt()
        return {"ok": True, "backend": r.settings.backend, "stages": [s.value for s in r.orchestrator.agents]}

    @app.post("/jobs", status_code=202)
    async def create_job(body: CreateJob, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        auth(authorization)
        try:
            job = await rt().orchestrator.submit(body.topic)
        except FatalError as e:
            raise HTTPException(400, str(e)) from e
        return {"job_id": job.id, "status": job.status.value}

    @app.get("/jobs")
    async def list_jobs(limit: int = 20, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        auth(authorization)
        return [{"id": j.id, "topic": j.topic, "status": j.status.value, "revision": j.revision,
                 "updated_at": j.updated_at} for j in await rt().state.list(min(limit, 100))]

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        auth(authorization)
        try:
            return (await rt().state.get(job_id)).to_doc()
        except JobNotFound as e:
            raise HTTPException(404, "job not found") from e

    @app.post("/jobs/{job_id}/resume", status_code=202)
    async def resume(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        auth(authorization)
        try:
            job = await rt().orchestrator.resume(job_id)
        except JobNotFound as e:
            raise HTTPException(404, "job not found") from e
        return {"job_id": job.id, "status": job.status.value}

    @app.post("/pubsub/{stage}")
    async def pubsub_push(stage: str, request: Request) -> Response:
        try:
            envelope = await request.json()
            data = base64.b64decode(envelope["message"]["data"])
            msg = StageMessage.model_validate(json.loads(data))
            if msg.stage.value != stage:
                raise ValueError(f"message for {msg.stage} delivered to /pubsub/{stage}")
        except (KeyError, ValueError, TypeError, binascii.Error, ValidationError) as e:
            # Poison message: ack it (204) so it doesn't redeliver forever; it's logged for forensics.
            log.kv(_log, 40, "malformed push message dropped", error=str(e)[:300])
            return Response(status_code=204)
        if Stage(stage) not in rt().orchestrator.agents:
            log.kv(_log, 40, "stage not served by this service", stage=stage)
            return Response(status_code=204)
        try:
            await rt().orchestrator.handle(msg)
        except LeaseBusy:
            return Response(status_code=429)  # nack: another instance is on it, try later
        except Exception as e:
            log.kv(_log, 30, "handler nack", job_id=msg.job_id, stage=stage, error=str(e)[:300])
            return Response(status_code=503)  # nack -> Pub/Sub backoff redelivery
        return Response(status_code=204)

    return app
