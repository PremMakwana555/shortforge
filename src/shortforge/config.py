"""Runtime configuration, read from environment variables (prefix ``SF_``) and an optional ``.env`` file.

Everything has a sane default so the pipeline runs end-to-end with zero keys (offline fallbacks).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader (no dependency). Existing env vars win."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _list(name: str, default: str) -> list[str]:
    return [x.strip().lower() for x in _env(name, default).split(",") if x.strip()]


def _bool(name: str, default: bool = False) -> bool:
    v = _env(name, "")
    if not v:
        return default
    return v.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- runtime backends -------------------------------------------------
    backend: str = "local"  # local | gcp
    data_dir: Path = Path("./.shortforge")
    output_dir: Path = Path("./output")
    offline: bool = False  # force offline providers only (CI / air-gapped)

    # --- GCP ----------------------------------------------------------------
    gcp_project: str = ""
    gcs_bucket: str = ""
    pubsub_topic_prefix: str = "sf"
    firestore_collection: str = "sf_jobs"
    service_role: str = "all"  # which stages a Cloud Run service handles ("all" or comma list)

    # --- reliability --------------------------------------------------------
    max_stage_attempts: int = 3
    max_qa_revisions: int = 2
    stage_lease_seconds: int = 900
    http_timeout_seconds: float = 60.0
    http_connect_timeout_seconds: float = 6.0
    breaker_failure_threshold: int = 2
    breaker_cooldown_seconds: float = 300.0

    # --- providers (order = priority; providers missing a key are skipped) --
    llm_providers: list[str] = field(default_factory=list)
    image_providers: list[str] = field(default_factory=list)
    tts_providers: list[str] = field(default_factory=list)
    research_providers: list[str] = field(default_factory=list)

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"

    hf_api_token: str = ""
    hf_image_model: str = "black-forest-labs/FLUX.1-schnell"
    pexels_api_key: str = ""
    pollinations_model: str = "flux"

    piper_model_path: str = ""  # explicit .onnx; default: <cache_dir>/voices/<piper_voice>.onnx
    piper_voice: str = "en_US-ryan-medium"
    cache_dir: Path = Path("~/.cache/shortforge").expanduser()
    espeak_voice: str = "en-us+m3"
    espeak_speed_wpm: int = 160

    # --- content ------------------------------------------------------------
    niche: str = "horror"
    target_words_min: int = 85
    target_words_max: int = 165
    video_width: int = 1080
    video_height: int = 1920
    fps: int = 30
    render_preset: str = "veryfast"
    max_video_seconds: float = 59.0

    # --- publishing ---------------------------------------------------------
    publishers: list[str] = field(default_factory=list)
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_refresh_token: str = ""
    youtube_privacy: str = "private"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(_env("SF_ENV_FILE", ".env"))
        offline = _bool("SF_OFFLINE")
        return cls(
            backend=_env("SF_BACKEND", "local"),
            data_dir=Path(_env("SF_DATA_DIR", "./.shortforge")),
            output_dir=Path(_env("SF_OUTPUT_DIR", "./output")),
            offline=offline,
            gcp_project=_env("SF_GCP_PROJECT", _env("GOOGLE_CLOUD_PROJECT")),
            gcs_bucket=_env("SF_GCS_BUCKET"),
            pubsub_topic_prefix=_env("SF_PUBSUB_TOPIC_PREFIX", "sf"),
            firestore_collection=_env("SF_FIRESTORE_COLLECTION", "sf_jobs"),
            service_role=_env("SF_SERVICE_ROLE", "all"),
            max_stage_attempts=int(_env("SF_MAX_STAGE_ATTEMPTS", "3")),
            max_qa_revisions=int(_env("SF_MAX_QA_REVISIONS", "2")),
            stage_lease_seconds=int(_env("SF_STAGE_LEASE_SECONDS", "900")),
            http_timeout_seconds=float(_env("SF_HTTP_TIMEOUT", "60")),
            http_connect_timeout_seconds=float(_env("SF_HTTP_CONNECT_TIMEOUT", "6")),
            breaker_failure_threshold=int(_env("SF_BREAKER_THRESHOLD", "2")),
            breaker_cooldown_seconds=float(_env("SF_BREAKER_COOLDOWN", "300")),
            llm_providers=["offline"] if offline else _list(
                "SF_LLM_PROVIDERS", "groq,openrouter,gemini,ollama,offline"),
            image_providers=["procedural"] if offline else _list(
                "SF_IMAGE_PROVIDERS", "pollinations,huggingface,pexels,procedural"),
            tts_providers=_list("SF_TTS_PROVIDERS", "piper,espeak,silent"),
            research_providers=["offline"] if offline else _list(
                "SF_RESEARCH_PROVIDERS", "wikipedia,offline"),
            groq_api_key=_env("GROQ_API_KEY"),
            groq_model=_env("SF_GROQ_MODEL", "llama-3.3-70b-versatile"),
            openrouter_api_key=_env("OPENROUTER_API_KEY"),
            openrouter_model=_env("SF_OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
            gemini_api_key=_env("GEMINI_API_KEY"),
            gemini_model=_env("SF_GEMINI_MODEL", "gemini-2.5-flash"),
            ollama_base_url=_env("OLLAMA_BASE_URL", "http://localhost:11434"),
            ollama_model=_env("SF_OLLAMA_MODEL", "llama3.1:8b"),
            hf_api_token=_env("HF_API_TOKEN"),
            hf_image_model=_env("SF_HF_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell"),
            pexels_api_key=_env("PEXELS_API_KEY"),
            pollinations_model=_env("SF_POLLINATIONS_MODEL", "flux"),
            piper_model_path=_env("SF_PIPER_MODEL"),
            piper_voice=_env("SF_PIPER_VOICE", "en_US-ryan-medium"),
            cache_dir=Path(_env("SF_CACHE_DIR", "~/.cache/shortforge")).expanduser(),
            espeak_voice=_env("SF_ESPEAK_VOICE", "en-us+m3"),
            espeak_speed_wpm=int(_env("SF_ESPEAK_WPM", "160")),
            niche=_env("SF_NICHE", "horror"),
            target_words_min=int(_env("SF_WORDS_MIN", "85")),
            target_words_max=int(_env("SF_WORDS_MAX", "165")),
            video_width=int(_env("SF_VIDEO_WIDTH", "1080")),
            video_height=int(_env("SF_VIDEO_HEIGHT", "1920")),
            fps=int(_env("SF_FPS", "30")),
            render_preset=_env("SF_RENDER_PRESET", "veryfast"),
            max_video_seconds=float(_env("SF_MAX_VIDEO_SECONDS", "59")),
            publishers=_list("SF_PUBLISHERS", "local"),
            youtube_client_id=_env("YOUTUBE_CLIENT_ID"),
            youtube_client_secret=_env("YOUTUBE_CLIENT_SECRET"),
            youtube_refresh_token=_env("YOUTUBE_REFRESH_TOKEN"),
            youtube_privacy=_env("SF_YOUTUBE_PRIVACY", "private"),
        )
