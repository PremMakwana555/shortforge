"""Artifact (blob) storage. Stage outputs in the state store only hold *keys*; the bytes live here.

Agents always work on local paths (FFmpeg needs files), so the API is:
``local_path(key)`` -> a readable local file (downloaded/cached for remote stores) and
``put_file(local, key)`` -> persist a local file under a key.
Keys are namespaced ``jobs/<job_id>/<stage>/...`` so a stage rerun overwrites only its own outputs.
"""

from __future__ import annotations

import abc
import asyncio
import shutil
import tempfile
from pathlib import Path


class ArtifactStore(abc.ABC):
    @abc.abstractmethod
    async def put_file(self, local: Path, key: str) -> str: ...

    @abc.abstractmethod
    async def local_path(self, key: str) -> Path: ...

    @abc.abstractmethod
    async def exists(self, key: str) -> bool: ...

    def scratch_dir(self, job_id: str, stage: str) -> Path:
        d = Path(tempfile.gettempdir()) / "shortforge" / job_id / stage
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def put_bytes(self, data: bytes, key: str, scratch: Path) -> str:
        tmp = scratch / Path(key).name
        tmp.write_bytes(data)
        return await self.put_file(tmp, key)

    async def put_text(self, text: str, key: str, scratch: Path) -> str:
        return await self.put_bytes(text.encode(), key, scratch)


class LocalArtifactStore(ArtifactStore):
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents and p != self.root:
            raise ValueError(f"artifact key escapes root: {key}")
        return p

    def scratch_dir(self, job_id: str, stage: str) -> Path:
        d = self.root / "jobs" / job_id / stage / "_scratch"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def put_file(self, local: Path, key: str) -> str:
        dest = self._p(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if local.resolve() != dest:
            tmp = dest.with_suffix(dest.suffix + ".part")
            await asyncio.to_thread(shutil.copyfile, local, tmp)
            tmp.replace(dest)  # atomic publish: readers never see half-written files
        return key

    async def local_path(self, key: str) -> Path:
        p = self._p(key)
        if not p.exists():
            raise FileNotFoundError(key)
        return p

    async def exists(self, key: str) -> bool:
        return self._p(key).exists()
