"""Composition root: wires settings -> backends -> providers -> agents -> orchestrator."""

from __future__ import annotations

from dataclasses import dataclass

from .. import log
from ..agents import all_agents
from ..config import Settings
from ..core.artifacts import ArtifactStore, LocalArtifactStore
from ..core.bus import Bus, InMemoryBus
from ..core.state import SQLiteStateStore, StateStore
from ..core.worker import Orchestrator
from ..models import Stage
from ..providers.registry import Providers


@dataclass
class Runtime:
    settings: Settings
    state: StateStore
    bus: Bus
    artifacts: ArtifactStore
    providers: Providers
    orchestrator: Orchestrator

    async def close(self) -> None:
        await self.bus.close()


def build(settings: Settings | None = None, *, bus: Bus | None = None) -> Runtime:
    log.setup()
    s = settings or Settings.from_env()
    if s.backend == "gcp":
        from ..gcp.adapters import FirestoreStateStore, GCSArtifactStore, PubSubBus

        if not (s.gcp_project and s.gcs_bucket):
            raise SystemExit("SF_BACKEND=gcp needs SF_GCP_PROJECT and SF_GCS_BUCKET")
        state: StateStore = FirestoreStateStore(s.gcp_project, s.firestore_collection)
        artifacts: ArtifactStore = GCSArtifactStore(s.gcs_bucket, s.gcp_project)
        bus = bus or PubSubBus(s.gcp_project, s.pubsub_topic_prefix)
    else:
        state = SQLiteStateStore(s.data_dir / "state.db")
        artifacts = LocalArtifactStore(s.data_dir / "artifacts")
        bus = bus or InMemoryBus(max_deliveries=s.max_stage_attempts + 2)

    providers = Providers.from_settings(s)
    agents = all_agents()
    if s.service_role != "all":  # a Cloud Run service may own only some stages ("none" = API only)
        wanted = {Stage(x.strip()) for x in s.service_role.split(",") if x.strip() and x.strip() != "none"}
        agents = {k: v for k, v in agents.items() if k in wanted}
    orch = Orchestrator(s, state, bus, artifacts, providers, agents)

    if isinstance(bus, InMemoryBus):
        for stage in agents:
            bus.subscribe(stage, orch.handle)
    return Runtime(s, state, bus, artifacts, providers, orch)
