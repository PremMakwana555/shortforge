"""Research sources. Wikipedia (free, no key) for grounding; an empty offline source as last resort.
Grounding facts are passed into the script prompt to reduce hallucinated "true story" claims."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from ..config import Settings
from ..core.errors import ProviderError
from ..core.resilience import ProviderChain, http_client, request_with_retry


@dataclass
class Source:
    title: str
    url: str
    extract: str


class ResearchProvider:
    name = "base"

    def available(self) -> bool:
        return True

    async def search(self, query: str, limit: int = 3) -> list[Source]:
        raise NotImplementedError


class Wikipedia(ResearchProvider):
    name = "wikipedia"

    def __init__(self, settings: Settings, lang: str = "en"):
        self.settings, self.base = settings, f"https://{lang}.wikipedia.org"

    async def search(self, query: str, limit: int = 3) -> list[Source]:
        async with http_client(20, self.settings.http_connect_timeout_seconds) as c:
            r = await request_with_retry(c, "GET", f"{self.base}/w/api.php", params={
                "action": "query", "list": "search", "srsearch": query, "srlimit": limit,
                "format": "json", "srnamespace": 0,
            })
            hits = r.json().get("query", {}).get("search", [])
            out: list[Source] = []
            for h in hits[:limit]:
                title = h["title"]
                s = await request_with_retry(c, "GET",
                                             f"{self.base}/api/rest_v1/page/summary/{quote(title, safe='')}")
                d = s.json()
                extract = (d.get("extract") or "").strip()
                if extract:
                    url = (d.get("content_urls") or {}).get("desktop", {}).get("page") or \
                        f"{self.base}/wiki/{quote(title.replace(' ', '_'))}"
                    out.append(Source(title=title, url=url, extract=extract[:1200]))
        if not out:
            raise ProviderError(f"no wikipedia results for '{query}'")
        return out


class OfflineResearch(ResearchProvider):
    name = "offline"

    async def search(self, query: str, limit: int = 3) -> list[Source]:
        return []


def build_research_chain(settings: Settings) -> ProviderChain[ResearchProvider]:
    catalog = {"wikipedia": lambda: Wikipedia(settings), "offline": OfflineResearch}
    return ProviderChain("research", [catalog[n]() for n in settings.research_providers if n in catalog])
