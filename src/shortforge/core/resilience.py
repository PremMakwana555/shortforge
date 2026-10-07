"""Fallback chains, circuit breakers and retry helpers shared by every provider family."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

import httpx

from .. import log
from .errors import AllProvidersFailed, ProviderError, ValidationFailed

T = TypeVar("T")
P = TypeVar("P", bound="Provider")
_log = log.get("shortforge.resilience")


class Provider(Protocol):
    name: str

    def available(self) -> bool:
        """False when the provider is not configured (e.g. missing API key). Skipped silently."""
        ...


@dataclass
class CircuitBreaker:
    """Classic closed -> open -> half-open breaker. Prevents hammering a provider that is down,
    which on a free tier usually means a rate-limit window - so fail over fast instead."""

    threshold: int = 2
    cooldown: float = 300.0
    failures: int = 0
    opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown:
            return True  # half-open: let one probe through
        return False

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


_breakers: dict[str, CircuitBreaker] = {}
_breaker_cfg = {"threshold": 2, "cooldown": 300.0}


def configure_breakers(threshold: int, cooldown: float) -> None:
    _breaker_cfg.update(threshold=threshold, cooldown=cooldown)


def breaker(name: str) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(**_breaker_cfg)  # type: ignore[arg-type]
    return _breakers[name]


def reset_breakers() -> None:
    _breakers.clear()


@dataclass
class ChainResult(Generic[T]):
    value: T
    provider: str
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """True when a non-primary provider produced the result."""
        return any(a["outcome"] == "error" or a.get("reason") == "circuit open" for a in self.attempts[:-1])


class ProviderChain(Generic[P]):
    """Try providers in priority order; the first one that returns a *valid* result wins."""

    def __init__(self, kind: str, providers: Sequence[P]):
        self.kind = kind
        self.providers = list(providers)

    async def run(
        self,
        call: Callable[[P], Awaitable[T]],
        validate: Callable[[T], None] | None = None,
    ) -> ChainResult[T]:
        attempts: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        for p in self.providers:
            if not p.available():
                attempts.append({"provider": p.name, "outcome": "skipped", "reason": "not configured"})
                continue
            br = breaker(f"{self.kind}:{p.name}")
            if not br.allow():
                attempts.append({"provider": p.name, "outcome": "skipped", "reason": "circuit open"})
                errors[p.name] = "circuit open"
                continue
            t0 = time.perf_counter()
            try:
                value = await call(p)
                if validate:
                    validate(value)
            except (TimeoutError, ProviderError, ValidationFailed, httpx.HTTPError, OSError) as e:
                br.failure()
                ms = int((time.perf_counter() - t0) * 1000)
                attempts.append({"provider": p.name, "outcome": "error", "ms": ms, "error": str(e)[:300]})
                errors[p.name] = f"{type(e).__name__}: {str(e)[:200]}"
                log.kv(_log, 30, f"{self.kind} provider failed, falling back", provider=p.name,
                       latency_ms=ms, error=str(e)[:200])
                continue
            br.success()
            ms = int((time.perf_counter() - t0) * 1000)
            attempts.append({"provider": p.name, "outcome": "ok", "ms": ms})
            return ChainResult(value=value, provider=p.name, attempts=attempts)
        raise AllProvidersFailed(self.kind, errors or {"none": "no provider available"})


def http_client(timeout: float, connect_timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=connect_timeout),
        follow_redirects=True,
        headers={"User-Agent": "shortforge/0.1 (+https://github.com/PremMakwana555/shortforge)"},
    )


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retries: int = 2,
    base_delay: float = 1.0,
    **kwargs: Any,
) -> httpx.Response:
    """In-provider retry for transient HTTP failures (429 / 5xx / network), honouring Retry-After.

    Kept small on purpose: the chain falls over to the next provider, and the stage itself is
    retried by the bus, so we avoid multiplicative retry storms.
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = await client.request(method, url, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = e
            if isinstance(e, httpx.ConnectError | httpx.ConnectTimeout) and attempt == 0:
                # Unreachable host: retrying immediately rarely helps - fail over instead.
                raise ProviderError(f"{type(e).__name__}: {e}") from e
        else:
            if r.status_code == 429 or r.status_code >= 500:
                last = ProviderError(f"HTTP {r.status_code}: {r.text[:200]}")
                retry_after = r.headers.get("retry-after")
                if retry_after and retry_after.isdigit() and int(retry_after) > 20:
                    raise last  # long rate-limit window: fall over to the next provider now
                if retry_after and retry_after.isdigit():
                    await asyncio.sleep(int(retry_after))
                    continue
            elif r.status_code >= 400:
                raise ProviderError(f"HTTP {r.status_code}: {r.text[:300]}")
            else:
                return r
        if attempt < retries:
            await asyncio.sleep(base_delay * 2**attempt * (0.8 + random.random() * 0.4))
    raise ProviderError(f"exhausted retries: {last}")
