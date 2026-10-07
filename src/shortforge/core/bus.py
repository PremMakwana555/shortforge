"""Message bus abstraction.

Semantics deliberately mirror Google Cloud Pub/Sub so local runs exercise the same failure paths:

* at-least-once delivery (handlers must be idempotent - the worker guarantees this),
* a handler that returns normally == ack; a handler that raises == nack -> redelivery with
  exponential backoff,
* after ``max_deliveries`` the message goes to a dead-letter list instead of looping forever.
"""

from __future__ import annotations

import abc
import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .. import log
from ..models import Stage, StageMessage

Handler = Callable[[StageMessage], Awaitable[None]]
_log = log.get("shortforge.bus")


class Bus(abc.ABC):
    @abc.abstractmethod
    async def publish(self, msg: StageMessage) -> None: ...

    async def close(self) -> None:  # pragma: no cover - optional
        return None


@dataclass
class DeadLetter:
    msg: StageMessage
    deliveries: int
    error: str


@dataclass
class _Envelope:
    msg: StageMessage
    deliveries: int = 0


@dataclass
class InMemoryBus(Bus):
    max_deliveries: int = 5
    backoff_base: float = 0.5
    backoff_cap: float = 20.0
    concurrency_per_stage: int = 2
    dead_letters: list[DeadLetter] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._queues: dict[Stage, asyncio.Queue[_Envelope]] = {}
        self._handlers: dict[Stage, Handler] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def subscribe(self, stage: Stage, handler: Handler) -> None:
        self._handlers[stage] = handler
        self._queues.setdefault(stage, asyncio.Queue())

    async def publish(self, msg: StageMessage) -> None:
        self._inflight += 1
        self._idle.clear()
        await self._queues.setdefault(msg.stage, asyncio.Queue()).put(_Envelope(msg))

    def start(self) -> None:
        for stage in self._handlers:
            for _ in range(self.concurrency_per_stage):
                self._tasks.append(asyncio.create_task(self._consume(stage), name=f"bus-{stage}"))

    async def _consume(self, stage: Stage) -> None:
        q = self._queues[stage]
        handler = self._handlers[stage]
        while True:
            env = await q.get()
            env.deliveries += 1
            try:
                await handler(env.msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # nack
                if env.deliveries >= self.max_deliveries:
                    self.dead_letters.append(DeadLetter(env.msg, env.deliveries, repr(e)))
                    log.kv(_log, 40, "dead-lettered", job_id=env.msg.job_id, stage=stage.value,
                           deliveries=env.deliveries, error=repr(e)[:300])
                    self._done()
                else:
                    delay = min(self.backoff_cap, self.backoff_base * 2 ** (env.deliveries - 1))
                    delay *= 0.75 + random.random() * 0.5  # jitter
                    log.kv(_log, 30, "nack -> redeliver", job_id=env.msg.job_id, stage=stage.value,
                           delivery=env.deliveries, backoff_s=round(delay, 2), error=repr(e)[:200])
                    asyncio.get_running_loop().call_later(delay, q.put_nowait, env)
            else:
                self._done()
            finally:
                q.task_done()

    def _done(self) -> None:
        self._inflight -= 1
        if self._inflight <= 0:
            self._inflight = 0
            self._idle.set()

    async def drain(self, timeout: float | None = None) -> None:
        """Wait until every published message has been acked or dead-lettered."""
        await asyncio.wait_for(self._idle.wait(), timeout)

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
