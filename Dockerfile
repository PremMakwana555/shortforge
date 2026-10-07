# Builds with Podman/Buildah or Docker - no BuildKit-only syntax, fully-qualified image names
# (Podman's short-name resolution can otherwise prompt or pick a different registry).
#
#   podman build -t shortforge .            # or: docker build -t shortforge .
#   podman build --build-arg WITH_PIPER=0 -t shortforge .   # skip the neural voice (smaller image)
#
# One image, many roles: SF_SERVICE_ROLE picks which pipeline stages a Cloud Run service handles.

ARG PYTHON_IMAGE=docker.io/library/python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11

# --------------------------------------------------------------------------- uv binary
FROM ${UV_IMAGE} AS uv

# --------------------------------------------------------------------------- dependencies (uv, locked)
FROM ${PYTHON_IMAGE} AS builder
ARG WITH_PIPER=1
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first (cached layer), project second. --frozen = fail if uv.lock is out of date.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN EXTRAS="--extra service --extra gcp --extra youtube"; \
    if [ "$WITH_PIPER" = "1" ]; then EXTRAS="$EXTRAS --extra tts"; fi; \
    uv sync --frozen --no-dev --no-install-project $EXTRAS
COPY src ./src
RUN EXTRAS="--extra service --extra gcp --extra youtube"; \
    if [ "$WITH_PIPER" = "1" ]; then EXTRAS="$EXTRAS --extra tts"; fi; \
    uv sync --frozen --no-dev --no-editable $EXTRAS

# --------------------------------------------------------------------------- Piper voice (open-source)
FROM ${PYTHON_IMAGE} AS voice
ARG WITH_PIPER=1
ARG PIPER_VOICE=en_US-ryan-medium
RUN mkdir -p /opt/piper && if [ "$WITH_PIPER" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
      V="$PIPER_VOICE"; L=$(echo "$V" | cut -d_ -f1); LC=$(echo "$V" | cut -d- -f1); \
      N=$(echo "$V" | cut -d- -f2); Q=$(echo "$V" | cut -d- -f3); \
      BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/$L/$LC/$N/$Q"; \
      curl -fsSL "$BASE/$V.onnx" -o /opt/piper/voice.onnx && \
      curl -fsSL "$BASE/$V.onnx.json" -o /opt/piper/voice.onnx.json; \
    fi

# --------------------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg espeak-ng fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

# Numeric non-root UID: works rootless under Podman (maps into the user namespace) and on Cloud Run.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 10001:10001 /data

COPY --from=builder /app/.venv /app/.venv
COPY --from=voice /opt/piper /opt/piper

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SF_LOG_FORMAT=json \
    SF_DATA_DIR=/data/state \
    SF_OUTPUT_DIR=/data/output \
    SF_PIPER_MODEL=/opt/piper/voice.onnx

USER 10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/healthz', timeout=4)"
CMD ["sh", "-c", "exec shortforge serve --port ${PORT:-8080}"]
