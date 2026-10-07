from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shortforge.core.bus import InMemoryBus
from shortforge.core.errors import AllProvidersFailed, ProviderError
from shortforge.core.resilience import CircuitBreaker, ProviderChain
from shortforge.core.state import JobNotFound, SQLiteStateStore
from shortforge.models import Job, Stage, StageMessage


async def test_bus_redelivers_then_acks():
    bus = InMemoryBus(backoff_base=0.01, backoff_cap=0.02)
    seen: list[int] = []

    async def handler(msg: StageMessage) -> None:
        seen.append(1)
        if len(seen) < 3:
            raise RuntimeError("transient")

    bus.subscribe(Stage.RESEARCH, handler)
    bus.start()
    await bus.publish(StageMessage(job_id="j", stage=Stage.RESEARCH))
    await bus.drain(timeout=5)
    assert len(seen) == 3 and not bus.dead_letters
    await bus.close()


async def test_bus_dead_letters_poison_message():
    bus = InMemoryBus(max_deliveries=3, backoff_base=0.01, backoff_cap=0.02)

    async def handler(msg: StageMessage) -> None:
        raise RuntimeError("always")

    bus.subscribe(Stage.RESEARCH, handler)
    bus.start()
    await bus.publish(StageMessage(job_id="j", stage=Stage.RESEARCH))
    await bus.drain(timeout=5)
    assert len(bus.dead_letters) == 1 and bus.dead_letters[0].deliveries == 3
    await bus.close()


async def test_sqlite_mutate_is_atomic_under_concurrency(tmp_path: Path):
    store = SQLiteStateStore(tmp_path / "s.db")
    job = await store.create(Job(topic="t"))

    def bump(j: Job) -> int:
        j.revision += 1
        return j.revision

    await asyncio.gather(*(store.mutate(job.id, bump) for _ in range(25)))
    assert (await store.get(job.id)).revision == 25


async def test_sqlite_mutate_rolls_back_on_error(tmp_path: Path):
    store = SQLiteStateStore(tmp_path / "s.db")
    job = await store.create(Job(topic="t"))

    def bad(j: Job) -> None:
        j.revision = 99
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await store.mutate(job.id, bad)
    assert (await store.get(job.id)).revision == 0
    with pytest.raises(JobNotFound):
        await store.get("missing")


class _P:
    def __init__(self, name: str, fail: bool = False, configured: bool = True):
        self.name, self.fail, self.configured, self.calls = name, fail, configured, 0

    def available(self) -> bool:
        return self.configured

    async def go(self) -> str:
        self.calls += 1
        if self.fail:
            raise ProviderError(f"{self.name} down")
        return self.name


async def test_chain_falls_back_and_breaker_opens():
    a, b, c = _P("a", fail=True), _P("b", configured=False), _P("c")
    chain = ProviderChain("t", [a, b, c])
    r1 = await chain.run(lambda p: p.go())
    assert r1.value == "c" and r1.degraded
    await chain.run(lambda p: p.go())
    await chain.run(lambda p: p.go())
    assert a.calls == 2  # breaker opened after 2 failures: third run skipped 'a' entirely
    assert b.calls == 0


async def test_chain_all_fail():
    chain = ProviderChain("t", [_P("a", fail=True)])
    with pytest.raises(AllProvidersFailed) as ei:
        await chain.run(lambda p: p.go())
    assert "a" in ei.value.errors


def test_breaker_half_opens_after_cooldown():
    br = CircuitBreaker(threshold=1, cooldown=0.0)
    br.failure()
    assert br.allow()  # cooldown 0 -> immediately half-open
