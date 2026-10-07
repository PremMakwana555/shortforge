# One image, many roles: SF_SERVICE_ROLE picks which pipeline stages a Cloud Run service handles.
FROM python:3.12-slim AS base

ARG WITH_PIPER=1
ARG PIPER_VOICE=en_US-ryan-medium

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    SF_LOG_FORMAT=json SF_DATA_DIR=/tmp/shortforge SF_OUTPUT_DIR=/tmp/shortforge/output

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg espeak-ng fonts-dejavu-core curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[service,gcp,youtube]"

# Optional neural TTS (Piper, MIT licensed) - open-source voice baked into the image.
RUN if [ "$WITH_PIPER" = "1" ]; then \
      pip install piper-tts && mkdir -p /opt/piper && \
      V="$PIPER_VOICE"; L=$(echo "$V" | cut -d_ -f1); LC=$(echo "$V" | cut -d- -f1); \
      N=$(echo "$V" | cut -d- -f2); Q=$(echo "$V" | cut -d- -f3); \
      BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/$L/$LC/$N/$Q"; \
      curl -fsSL "$BASE/$V.onnx" -o /opt/piper/voice.onnx && \
      curl -fsSL "$BASE/$V.onnx.json" -o /opt/piper/voice.onnx.json ; \
    fi
ENV SF_PIPER_MODEL=/opt/piper/voice.onnx

RUN useradd -m -u 10001 app && mkdir -p /tmp/shortforge && chown -R app /tmp/shortforge
USER app

EXPOSE 8080
CMD ["sh", "-c", "shortforge serve --port ${PORT:-8080}"]
