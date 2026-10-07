"""Builds every provider chain once per process from Settings."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..core.resilience import ProviderChain, configure_breakers
from .images import ImageProvider, build_image_chain
from .llm import LLMProvider, build_llm_chain
from .publish import Publisher, build_publishers
from .research import ResearchProvider, build_research_chain
from .tts import TTSProvider, build_tts_chain


@dataclass
class Providers:
    llm: ProviderChain[LLMProvider]
    research: ProviderChain[ResearchProvider]
    tts: ProviderChain[TTSProvider]
    images: ProviderChain[ImageProvider]
    publishers: list[Publisher]

    @classmethod
    def from_settings(cls, s: Settings) -> Providers:
        configure_breakers(s.breaker_failure_threshold, s.breaker_cooldown_seconds)
        return cls(llm=build_llm_chain(s), research=build_research_chain(s), tts=build_tts_chain(s),
                   images=build_image_chain(s), publishers=build_publishers(s))

    def describe(self) -> dict[str, list[str]]:
        def names(chain: ProviderChain) -> list[str]:  # type: ignore[type-arg]
            return [f"{p.name}{'' if p.available() else ' (not configured)'}" for p in chain.providers]

        return {"llm": names(self.llm), "research": names(self.research), "tts": names(self.tts),
                "images": names(self.images), "publishers": [p.name for p in self.publishers]}
