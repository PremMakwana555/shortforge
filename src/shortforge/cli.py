"""Command line entry point.

    shortforge run "the lighthouse keeper who never left"     # full pipeline, local backends
    shortforge run "..." --offline                            # no network: offline fallbacks only
    shortforge status [JOB_ID]                                # job list / stage-level detail
    shortforge resume JOB_ID                                  # continue a failed job from its last good stage
    shortforge providers                                      # which providers are configured
    shortforge serve --port 8080                              # HTTP service (Cloud Run)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from .core.bus import InMemoryBus
from .models import Job, JobStatus, Stage


def _fmt_job(job: Job) -> str:
    lines = [f"job {job.id}  status={job.status.value}  revision={job.revision}  topic={job.topic!r}"]
    for s in Stage:
        st = job.stage(s)
        m = st.output.get("_metrics", {}) if st.output else {}
        who = m.get("llm_provider") or m.get("tts_provider") or ",".join(m.get("image_providers", [])) or ""
        dur = f"{st.duration_ms / 1000:6.1f}s" if st.duration_ms is not None else "      -"
        flag = " degraded" if st.output.get("degraded") else ""
        err = f"  err={st.error[:90]}" if st.error and st.status != "completed" else ""
        lines.append(f"  {s.value:<11} {st.status.value:<10} attempts={st.attempts} {dur}  {who}{flag}{err}")
    qa = job.output(Stage.QA)
    if qa.get("checks"):
        bad = [c["name"] for c in qa["checks"] if not c["passed"]]
        lines.append(f"  QA: {'PASS' if qa.get('passed') else 'FAIL ' + ', '.join(bad)}"
                     + (f"  (degraded: {', '.join(qa['degraded_stages'])})" if qa.get("degraded_stages") else ""))
    pub = job.output(Stage.PUBLISHING).get("targets", {})
    for name, r in pub.items():
        lines.append(f"  published[{name}]: {r.get('url') or r.get('path') or r}")
    if job.error:
        lines.append(f"  error: {job.error}")
    return "\n".join(lines)


async def _run_local(args: argparse.Namespace) -> int:
    from .runtime.app import build

    rt = build()
    if not isinstance(rt.bus, InMemoryBus):
        print("`run` drives the pipeline in-process; with SF_BACKEND=gcp use the HTTP API instead.")
        return 2
    rt.bus.start()
    t0 = time.time()
    try:
        if args.cmd == "run":
            job = await rt.orchestrator.submit(args.topic)
        else:
            job = await rt.orchestrator.resume(args.job_id)
        await rt.bus.drain(timeout=args.timeout)
    finally:
        await rt.close()
    job = await rt.state.get(job.id)
    print("\n" + _fmt_job(job))
    print(f"\nwall time: {time.time() - t0:.1f}s")
    if rt.bus.dead_letters:  # type: ignore[attr-defined]
        print(f"dead letters: {len(rt.bus.dead_letters)}")  # type: ignore[attr-defined]
    return 0 if job.status == JobStatus.COMPLETED else 1


async def _status(args: argparse.Namespace) -> int:
    from .runtime.app import build

    rt = build()
    if args.job_id:
        job = await rt.state.get(args.job_id)
        print(json.dumps(job.to_doc(), indent=2) if args.json else _fmt_job(job))
    else:
        for j in await rt.state.list(args.limit):
            print(f"{j.id}  {j.status.value:<12} rev={j.revision}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(j.created_at))}  {j.topic}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shortforge", description="Multi-agent short-form video pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the full pipeline for a topic")
    r.add_argument("topic")
    r.add_argument("--offline", action="store_true", help="use offline fallback providers only")
    r.add_argument("--timeout", type=float, default=3600)
    rs = sub.add_parser("resume", help="resume a failed/stuck job from its first incomplete stage")
    rs.add_argument("job_id")
    rs.add_argument("--timeout", type=float, default=3600)
    st = sub.add_parser("status", help="list jobs or show one job")
    st.add_argument("job_id", nargs="?")
    st.add_argument("--json", action="store_true")
    st.add_argument("--limit", type=int, default=20)
    sub.add_parser("providers", help="show provider chains and which are configured")
    sv = sub.add_parser("serve", help="run the HTTP service (Cloud Run entrypoint)")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = p.parse_args(argv)

    if getattr(args, "offline", False):
        os.environ["SF_OFFLINE"] = "1"
    if args.cmd in ("run", "resume"):
        return asyncio.run(_run_local(args))
    if args.cmd == "status":
        return asyncio.run(_status(args))
    if args.cmd == "providers":
        from .config import Settings
        from .providers.registry import Providers

        print(json.dumps(Providers.from_settings(Settings.from_env()).describe(), indent=2))
        return 0
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("shortforge.runtime.service:create_app", factory=True, host=args.host, port=args.port,
                    log_config=None)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
