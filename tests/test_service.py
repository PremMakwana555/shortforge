from __future__ import annotations

import base64
import dataclasses

from fastapi.testclient import TestClient

from shortforge.core.bus import Bus
from shortforge.models import StageMessage
from shortforge.runtime.app import build
from shortforge.runtime.service import create_app


class RecordingBus(Bus):
    """Stands in for Pub/Sub: records publishes instead of delivering them."""

    def __init__(self) -> None:
        self.sent: list[StageMessage] = []

    async def publish(self, msg: StageMessage) -> None:
        self.sent.append(msg)


def _client(settings):
    s = dataclasses.replace(settings, llm_providers=["offline"], research_providers=["offline"])
    bus = RecordingBus()
    rt = build(s, bus=bus)
    return TestClient(create_app(rt)), bus


def _envelope(msg: StageMessage) -> dict:
    return {"message": {"data": base64.b64encode(msg.model_dump_json().encode()).decode(), "messageId": "1"},
            "subscription": "projects/p/subscriptions/s"}


def test_create_job_then_push_delivery_advances_pipeline(settings):
    client, bus = _client(settings)
    with client:
        r = client.post("/jobs", json={"topic": "the cursed elevator"})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        assert bus.sent[-1].stage.value == "research"

        r = client.post("/pubsub/research", json=_envelope(bus.sent[-1]))
        assert r.status_code == 204
        assert bus.sent[-1].stage.value == "script"  # completion published the next stage

        job = client.get(f"/jobs/{job_id}").json()
        assert job["stages"]["research"]["status"] == "completed"
        assert client.get("/healthz").json()["ok"] is True


def test_malformed_push_is_acked_not_retried(settings):
    client, _ = _client(settings)
    with client:
        assert client.post("/pubsub/research", json={"message": {"data": "!!notbase64"}}).status_code == 204
        assert client.post("/pubsub/research", json={"nope": 1}).status_code == 204


def test_wrong_stage_route_is_rejected(settings):
    client, _ = _client(settings)
    with client:
        msg = StageMessage(job_id="x", stage="script")
        assert client.post("/pubsub/research", json=_envelope(msg)).status_code == 204


def test_unknown_job_404(settings):
    client, _ = _client(settings)
    with client:
        assert client.get("/jobs/doesnotexist").status_code == 404
        assert client.post("/jobs", json={"topic": "x"}).status_code == 422
