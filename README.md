# shortforge

**Event-driven multi-agent platform that turns a topic into a production-ready vertical short (1080×1920, ≤ 59 s) — using free / open-source AI models, with automatic fallbacks so it never hard-stops.**

```
topic ─▶ Research ─▶ Script ─▶ Voiceover ─▶ Visual ─▶ Editing ─▶ QA ─▶ Publishing ─▶ MP4 + thumbnail + metadata
                        ▲                                          │
                        └──────────── rework with feedback ◀───────┘   (bounded, routed to the stage that can fix it)
```

```bash
uv sync                                            # needs ffmpeg on PATH (+ espeak-ng for a voice)
uv run shortforge run "the lighthouse keeper who never left"
# or fully containerised:
make image run-container                           # Podman by default, Docker as fallback
```

Runs with **zero API keys**: offline fallbacks (template writer, procedural images, espeak/silent narration) keep every stage working, and each stage reports when it was `degraded`. Add free keys to upgrade quality.

---

## What it does

| Agent | Job | Providers (priority order — free / open first) |
|---|---|---|
| **Research** | Retrieve grounding material, distil a creative brief | Wikipedia API → offline · LLM chain for the brief |
| **Script** | 6–9 narrated beats + one image prompt per beat; schema-validated, word-budgeted, content blocklist | Groq (Llama 3.3 70B) → OpenRouter `:free` → Gemini → Ollama (local) → offline writer |
| **Voiceover** | Per-beat TTS → exact beat timings; speeds up ≤ 1.25× to fit the 60 s cap | Piper (neural, GPL-3.0) → espeak-ng → timed silence |
| **Visual** | One 9:16 image per beat, generated concurrently (bounded), validated | Pollinations FLUX (no key) → HF Inference FLUX.1-schnell → Pexels → procedural renderer |
| **Editing** | FFmpeg: per-beat Ken Burns clips (parallel), burned captions, loudness-normalised narration + generated ambient bed, thumbnail | FFmpeg / libass |
| **QA** | Release gate: ffprobe stream/resolution/codec, duration limits, A/V sync, black-frame ratio, loudness, optional LLM-as-judge; failures **route back** to the stage that can fix them | ffmpeg `blackdetect` / `volumedetect`, LLM chain |
| **Publishing** | Idempotent fan-out with per-target receipts | Local folder, YouTube Data API (private by default) |

## Architecture

```mermaid
flowchart LR
  API[sf-api<br/>POST /jobs] -->|publish| T1((sf-research))
  T1 --> R[sf-research<br/>Cloud Run] --> T2((sf-script)) --> R
  R --> T3((sf-voiceover)) --> M[sf-media<br/>Cloud Run] --> T4((sf-visual)) --> M
  M --> T5((sf-editing)) --> E[sf-editor<br/>4 vCPU / 8 GiB] --> T6((sf-qa)) --> G[sf-gate<br/>Cloud Run]
  G -->|rework| T2
  G --> T7((sf-publishing)) --> G
  R & M & E & G <-->|transactions| FS[(Firestore<br/>job state)]
  R & M & E & G <--> GCS[(GCS<br/>artifacts)]
  T1 & T2 & T3 & T4 & T5 & T6 & T7 -.max 6 deliveries.-> DLQ((sf-dead-letter))
```

* **Event-driven orchestration** — one Pub/Sub topic per stage with *push* subscriptions to Cloud Run. A handler returning 2xx acks; 5xx nacks and Pub/Sub redelivers with exponential backoff (10 s → 600 s), then dead-letters. Stages are grouped into services sized for their workload (the FFmpeg editor gets 4 vCPU / 8 GiB at concurrency 1; research runs on 1 vCPU) — independent scaling and failure isolation.
* **Persistent workflow state** — each job is a Firestore document with per-stage status, attempts, lease, timings, outputs, and an event log. Every transition is a transactional read-modify-write.
* **Exactly-once *effects* on at-least-once delivery** — stage leases (owner + expiry) claimed in a transaction; completed stages are never re-executed (duplicates just re-emit downstream); messages carry the job `revision`, so stale messages from before a QA rework are dropped; expired leases from crashed workers are taken over.
* **Resumability** — `shortforge resume <job>` / `POST /jobs/{id}/resume` restarts from the first incomplete stage, reusing all completed work. Artifacts are namespaced by revision (`jobs/<id>/r<rev>/<stage>/…`) so reworks never overwrite files an older revision might read.
* **Provider fallback chains + circuit breakers** — each capability is a priority list. Unconfigured providers are skipped; failures fall through; after N consecutive failures a provider's breaker opens for a cooldown (free tiers usually fail via rate-limit windows, so fail over fast instead of hammering). Long `Retry-After` → fail over immediately; short → wait once.
* **LLM output is untrusted input** — JSON extraction tolerant of fences/preambles, schema + constraint validation, one repair round-trip per provider with the validation error fed back, then fall through.
* **Same code locally and on GCP** — `Bus`, `StateStore` and `ArtifactStore` interfaces with local (in-memory bus with identical ack/nack/backoff/DLQ semantics, SQLite `BEGIN IMMEDIATE`, filesystem) and GCP (Pub/Sub, Firestore, GCS) implementations.

Deep dive: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Quick start (uv)

Dependencies are managed with [uv](https://docs.astral.sh/uv/) and pinned in `uv.lock`.

```bash
# system deps: ffmpeg (required), espeak-ng (voice), DejaVu fonts (captions)
brew install uv ffmpeg espeak-ng                               # macOS
# sudo apt-get install ffmpeg espeak-ng fonts-dejavu-core      # Debian/Ubuntu (uv: curl -LsSf https://astral.sh/uv/install.sh | sh)

uv sync                         # creates .venv from uv.lock (Python 3.12 from .python-version)
cp .env.example .env            # optional: add free keys

uv run shortforge providers     # which providers are configured
uv run shortforge run "the cursed elevator that stops at floor 13"
uv run shortforge status               # list jobs
uv run shortforge status <job_id>      # per-stage status, attempts, timings, provider used, QA verdict
uv run shortforge resume <job_id>      # continue a failed job from its last good stage
```

Output lands in `output/<job_id>/` — `video.mp4`, `thumbnail.jpg`, `metadata.json` (title, description, hashtags, sources, `ai_generated`, degraded stages).

Optional extras: `--extra tts` (Piper neural voice), `--extra gcp` (Firestore/Pub/Sub/GCS), `--extra youtube` (uploads) — pass them to `uv run` too (e.g. `uv run --extra tts shortforge run ...`), because a plain `uv run` re-syncs the environment to the default set. Adding a dependency: `uv add <pkg>` (updates `pyproject.toml` and `uv.lock`; commit both).

**Free keys worth adding** (all optional): `GROQ_API_KEY` (best free LLM quality/latency), `HF_API_TOKEN` (FLUX images if Pollinations is down), `PEXELS_API_KEY`. For a natural voice locally: `uv run --extra tts python -m piper.download_voices en_US-ryan-medium`, set `SF_PIPER_MODEL=$PWD/en_US-ryan-medium.onnx`, and run with `uv run --extra tts shortforge run ...` (the container image bakes this voice in). Or `ollama pull llama3.1:8b` for a fully local LLM.

## Containers (Podman first, Docker works too)

The image is plain OCI — fully-qualified base images (`docker.io/library/...`, `ghcr.io/astral-sh/uv`), no BuildKit-only syntax, a numeric non-root user (UID 10001) — so it builds and runs identically under **rootless Podman** and Docker. CI builds and runs it with both.

```bash
make image                       # podman build --format docker -t localhost/shortforge:latest .
make image-slim                  # without the Piper voice (espeak-ng only), smaller
make serve-container             # HTTP API on :8080, state in the `shortforge-data` volume
make run-container TOPIC="the well behind the school"   # one-shot render into ./output
podman compose up --build        # same as serve-container, via compose.yaml (also: docker compose)
```

Without make:

```bash
podman build --format docker -t localhost/shortforge .
podman run --rm -p 8080:8080 --env-file .env -v shortforge-data:/data localhost/shortforge
curl -X POST localhost:8080/jobs -H 'Content-Type: application/json' -d '{"topic":"the well behind the school"}'
curl localhost:8080/jobs/<job_id>

# one-shot render with the video written to ./output on the host
mkdir -p output
podman run --rm --env-file .env --userns=keep-id:uid=10001,gid=10001 \
  -v "$PWD/output:/data/output:Z" localhost/shortforge shortforge run "the radio station that broadcasts at 3am"
```

Podman notes:

* **`--format docker`** keeps the `HEALTHCHECK` (Podman's default OCI format drops it with a warning).
* **Bind mounts, rootless:** the container runs as UID 10001; `--userns=keep-id:uid=10001,gid=10001` maps that UID to *your* host user so files in `./output` are owned by you and writable. Named volumes (`-v shortforge-data:/data`) need nothing extra.
* **`:Z`** relabels the bind mount for SELinux hosts (Fedora/RHEL). On macOS (`podman machine`) leave it off — the Makefile only adds it on Linux.
* **macOS (Apple Silicon):** `podman machine init --cpus 4 --memory 8192 && podman machine start`. The image builds natively for `linux/arm64` (uv, Piper and onnxruntime all ship aarch64 wheels). Give the machine ≥ 4 CPUs — FFmpeg rendering is the hot path.
* **Docker:** every command above works with `docker` substituted; `--userns=keep-id` is Podman-only (use `--user $(id -u)` or a writable directory instead).

### Deploy to GCP

```bash
export PROJECT=my-project REGION=asia-south1
./deploy/gcp/deploy.sh   # idempotent
```

Builds remotely with Cloud Build (no local container engine needed), then provisions Firestore, a GCS bucket (30-day lifecycle), Artifact Registry, Secret Manager entries for any keys in `.env`, 5 Cloud Run services (private, IAM-invoked), 7 topics with OIDC push subscriptions, retry policy and a dead-letter topic. The final MP4 stays in GCS; YouTube upload is enabled automatically when OAuth credentials are present.

## Observability

* Structured JSON logs on Cloud Run (`severity`, `job_id`, `stage`, `trace_id`, `worker`, `latency_ms`, `provider`) — queryable in Cloud Logging, pretty-printed locally.
* Each stage output carries `_metrics`: provider used, model, token usage, fallback attempts with per-provider latency and error.
* Job event log: `stage_started / completed / failed`, `lease_takeover`, `rework`, `needs_review`.
* QA output: every check with value + expectation + routing target, and which stages ran degraded.

## Tests

```bash
uv sync
make lint test        # uv run ruff check / uv run pytest -m "not e2e"
make e2e              # full pipeline: real FFmpeg render + espeak-ng, offline providers
```

The orchestration suite covers: in-order execution, duplicate delivery idempotency, transient retry, retry exhaustion → fail → resume without recomputing upstream, fatal errors, QA rework loop, rework budget → `needs_review`, stale-revision drop, live-lease contention, expired-lease takeover. CI (`uv sync --locked`) runs all of it plus a CLI smoke render, then builds the image with **both Podman and Docker** and, in each, checks the Piper voice synthesises, the API is healthy, and a full containerised render writes a video to a bind-mounted directory. The sample video is uploaded as a build artifact.

## Configuration

All via environment (see [`.env.example`](.env.example)): provider order (`SF_LLM_PROVIDERS`, `SF_IMAGE_PROVIDERS`, `SF_TTS_PROVIDERS`), reliability (`SF_MAX_STAGE_ATTEMPTS`, `SF_MAX_QA_REVISIONS`, `SF_STAGE_LEASE_SECONDS`, `SF_BREAKER_THRESHOLD`), content (`SF_WORDS_MIN/MAX`, `SF_MAX_VIDEO_SECONDS`, resolution, `SF_RENDER_PRESET`), and `SF_OFFLINE=1` to force offline providers.

## Responsible use

Uploads default to **private**, and YouTube's `containsSyntheticMedia` flag is set. Scripts pass a content blocklist and the prompts forbid real private individuals, gore and sexual content. Check each provider's terms before commercial use.

## License

MIT
