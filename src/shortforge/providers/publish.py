"""Publishing targets.

* ``local``   - copies the final video, thumbnail and metadata into ``SF_OUTPUT_DIR/<job_id>/``.
* ``youtube`` - YouTube Data API v3 resumable upload (OAuth refresh token).
                Uploads default to ``private`` so a human can review before going public.

Publishing is the one non-idempotent side effect in the pipeline, so the agent records each
target's result in the stage output and skips targets that already succeeded on a retry.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from ..config import Settings
from ..core.errors import FatalError, ProviderError


class Publisher:
    name = "base"

    def available(self) -> bool:
        return True

    async def publish(self, video: Path, thumbnail: Path | None, meta: dict[str, Any], job_id: str) -> dict[str, Any]:
        raise NotImplementedError


class LocalPublisher(Publisher):
    name = "local"

    def __init__(self, settings: Settings):
        self.out = settings.output_dir

    async def publish(self, video: Path, thumbnail: Path | None, meta: dict[str, Any], job_id: str) -> dict[str, Any]:
        dest = (self.out / job_id).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(video, dest / "video.mp4")
        if thumbnail and thumbnail.exists():
            shutil.copyfile(thumbnail, dest / "thumbnail.jpg")
        (dest / "metadata.json").write_text(json.dumps(meta, indent=2))
        return {"path": str(dest / "video.mp4"), "dir": str(dest)}


class YouTubePublisher(Publisher):
    name = "youtube"

    def __init__(self, settings: Settings):
        self.s = settings

    def available(self) -> bool:
        return bool(self.s.youtube_client_id and self.s.youtube_client_secret and self.s.youtube_refresh_token)

    async def publish(self, video: Path, thumbnail: Path | None, meta: dict[str, Any], job_id: str) -> dict[str, Any]:
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.errors import HttpError
            from googleapiclient.http import MediaFileUpload
        except ImportError as e:
            raise FatalError("YouTube client libraries missing - run `uv sync`") from e

        def _upload() -> dict[str, Any]:
            creds = Credentials(
                token=None, refresh_token=self.s.youtube_refresh_token, client_id=self.s.youtube_client_id,
                client_secret=self.s.youtube_client_secret, token_uri="https://oauth2.googleapis.com/token",
                scopes=["https://www.googleapis.com/auth/youtube.upload"],
            )
            yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
            body = {
                "snippet": {"title": meta["title"][:100], "description": meta["description"][:4900],
                            "tags": [h.lstrip("#") for h in meta.get("hashtags", [])][:15], "categoryId": "24"},
                "status": {"privacyStatus": self.s.youtube_privacy, "selfDeclaredMadeForKids": False,
                           "containsSyntheticMedia": True},
            }
            req = yt.videos().insert(part="snippet,status", body=body,
                                     media_body=MediaFileUpload(str(video), chunksize=8 * 1024 * 1024,
                                                                resumable=True, mimetype="video/mp4"))
            try:
                resp = None
                while resp is None:
                    _, resp = req.next_chunk(num_retries=3)
            except HttpError as e:
                raise ProviderError(f"youtube upload failed: {e}") from e
            vid = resp["id"]
            return {"video_id": vid, "url": f"https://youtube.com/shorts/{vid}", "privacy": self.s.youtube_privacy}

        return await asyncio.to_thread(_upload)


def build_publishers(settings: Settings) -> list[Publisher]:
    catalog = {"local": lambda: LocalPublisher(settings), "youtube": lambda: YouTubePublisher(settings)}
    return [catalog[n]() for n in settings.publishers if n in catalog]
