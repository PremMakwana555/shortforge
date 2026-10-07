"""Structured JSON logging. On Cloud Run, stdout JSON lines are parsed by Cloud Logging, so
``severity`` and arbitrary fields (job_id, stage, provider, latency_ms) become queryable."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextvars import ContextVar
from typing import Any

_ctx: ContextVar[dict[str, Any] | None] = ContextVar("sf_log_ctx", default=None)


def bind(**kv: Any) -> None:
    _ctx.set({**(_ctx.get() or {}), **kv})


def clear() -> None:
    _ctx.set({})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **(_ctx.get() or {}),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class PrettyFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = {**(_ctx.get() or {}), **(getattr(record, "fields", None) or {})}
        tail = " ".join(f"{k}={v}" for k, v in ctx.items())
        msg = f"{time.strftime('%H:%M:%S')} {record.levelname[:4]} {record.name}: {record.getMessage()}"
        if tail:
            msg += f"  [{tail}]"
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return msg


def setup(level: str | None = None) -> None:
    root = logging.getLogger()
    if getattr(root, "_sf_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    fmt = os.environ.get("SF_LOG_FORMAT") or ("json" if os.environ.get("K_SERVICE") else "pretty")
    handler.setFormatter(JsonFormatter() if fmt == "json" else PrettyFormatter())
    root.handlers = [handler]
    root.setLevel(level or os.environ.get("SF_LOG_LEVEL", "INFO"))
    for noisy in ("httpx", "httpcore", "urllib3", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._sf_configured = True  # type: ignore[attr-defined]


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


def kv(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})
