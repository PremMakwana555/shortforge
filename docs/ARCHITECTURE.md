# Architecture

## 1. Job state machine

A job is one document (`models.Job`). Each of the 7 stages has a `StageState`:

```
pending ──claim──▶ running ──ok──▶ completed
   ▲                  │
   │                  ├─retryable error──▶ retrying ──next delivery──▶ running   (≤ SF_MAX_STAGE_ATTEMPTS)
   │                  └─fatal / exhausted─▶ failed  ──resume()──▶ pending
   └──────── QA rework resets target stage + everything downstream (revision += 1)
```

Job status: `queued → running → completed | failed | needs_review` (QA rework budget exhausted; `resume` grants one more cycle).

The only write primitive is `StateStore.mutate(job_id, fn)` — a transactional read-modify-write (SQLite `BEGIN IMMEDIATE`, Firestore transaction). Claim, complete, fail, rework and resume are all pure functions over the job passed to `mutate`, so they are atomic and backend-agnostic. Firestore may re-run the function on contention, which is why those functions have no side effects; publishing happens *after* the transaction commits.

## 2. Delivery semantics

Pub/Sub is at-least-once and can reorder. The worker (`core/worker.py`) makes the *effects* exactly-once:

| Hazard | Defence |
|---|---|
| Duplicate delivery of a finished stage | Claim sees `completed` → no re-execution; re-emits the downstream message (covers a crash between commit and publish; downstream dedupes the same way). |
| Two instances receive the same message | Lease claimed in a transaction; the loser gets `LeaseBusy` → HTTP 429 → Pub/Sub retries later. |
| Worker dies mid-stage | Lease expires (`SF_STAGE_LEASE_SECONDS`); the next delivery takes over and logs `lease_takeover`. Agent runtime is capped below the lease so a slow-but-alive worker can't overlap a takeover. |
| Slow worker finishes after a takeover | `_complete` checks it still owns the lease, otherwise the result is discarded. |
| Message from before a QA rework | Messages carry `revision`; anything older than the job's revision is dropped. |
| Out-of-order message (stage N before N-1 is done) | Dropped; the correct sequence is re-driven by the predecessor or by `resume`. |
| Poison message | Malformed payloads are acked (204) and logged; handler failures nack until `max-delivery-attempts`, then go to the dead-letter topic. |
| Non-idempotent side effect (upload) | Publishing writes a receipt per target before moving on; retries skip targets with receipts. |

Retry layering is deliberate to avoid retry storms: **in-provider** (≤ 2 quick HTTP retries, honouring short `Retry-After`) → **chain** (fall through to the next provider; circuit breaker skips known-bad ones) → **stage** (bus redelivery with exponential backoff, bounded attempts) → **job** (manual/automated `resume`).

## 3. Provider chains

`core/resilience.ProviderChain` tries providers in priority order and returns the first *validated* result plus an attempt trace (`provider`, `outcome`, `ms`, `error`) that's stored in the stage's `_metrics`. A result from a non-primary provider is marked `degraded`, and QA surfaces degraded stages in the final metadata.

| Capability | Chain | Validation |
|---|---|---|
| LLM | groq → openrouter → gemini → ollama → offline | JSON extraction → schema/constraint validator → one repair round-trip |
| Research | wikipedia → offline | non-empty results |
| TTS | piper → espeak → silent | non-empty audio; engine pinned after beat 0 for voice consistency |
| Images | pollinations → huggingface → pexels → procedural | decodes, ≥ 256 px, not blank (luma stddev) |

## 4. Media pipeline

1. **Voiceover** synthesises each beat separately, trims leading silence, adds a 0.35 s breath, and records exact `start/end` per beat. If total narration exceeds the Shorts budget it applies `atempo` up to 1.25×; beyond that it's a script problem and QA sends it back.
2. **Visual** generates one 768×1344 image per beat (3 in flight), crops/scales to 1080×1920.
3. **Editing** renders per-beat clips in parallel (`zoompan`, alternating push-in/pull-out, 0.25 s fade-in), concatenates with stream copy, then a single final pass burns ASS captions (3-word chunks timed proportionally inside each beat's audio window — drift is bounded per beat without a forced-alignment model), loudness-normalises narration to −16 LUFS, mixes a generated brown-noise + 48 Hz drone bed, and encodes H.264/AAC with `+faststart`.
4. **QA** probes the output and routes failures to the earliest stage that can fix them:

| Check | Threshold | Routed to |
|---|---|---|
| streams / resolution / codec | 1 video + 1 audio, 1080×1920, h264 | editing |
| max / min duration | ≤ 59 s, ≥ 15 s | script (with "cut to ~N words") |
| A/V sync | render vs narration ≤ 0.5 s | editing |
| black frames | ≤ 15 % (`blackdetect`) | visual (prompts get exposure hints) |
| audio level | −35…−8 dB mean (skipped for silent fallback) | voiceover |
| LLM judge (when a real LLM is configured) | hook / coherence / payoff ≥ 5 | script (issues fed back) |

## 5. Deployment topology (GCP)

| Service | Stages | Sizing | Why |
|---|---|---|---|
| `sf-api` | — (control plane) | 1 vCPU, concurrency 40 | cheap, request/response |
| `sf-research` | research, script | 1 vCPU, concurrency 8 | network-bound LLM calls |
| `sf-media` | voiceover, visual | 2 vCPU, concurrency 2 | TTS CPU + image I/O |
| `sf-editor` | editing | 4 vCPU / 8 GiB, concurrency 1 | FFmpeg is CPU-bound; isolate it |
| `sf-gate` | qa, publishing | 2 vCPU | ffprobe/blackdetect + uploads |

Push subscriptions: ack deadline 600 s, retry backoff 10–600 s, 6 max deliveries → `sf-dead-letter` (held 7 days). Services are private; Pub/Sub authenticates with an OIDC token for `sf-invoker`. Keys live in Secret Manager.

## 6. Known limits / next steps

* **Fan-out inside a stage** (images per beat) is in-process concurrency, not separate messages — simpler and fine at 6–9 images; at higher volume, split to per-beat messages with a join counter in the job doc.
* **Captions** are proportionally timed, not forced-aligned. Whisper-based alignment (open source) would tighten word timing.
* **Firestore document size** — the event log is capped at 300 entries to stay far below 1 MiB.
* **Cost / quota guard** — add per-day job budgets and per-provider token accounting dashboards (the data is already in `_metrics`).
* **Eval harness** — persist QA scores + judge outputs per `prompt_version` to compare prompt changes offline.
