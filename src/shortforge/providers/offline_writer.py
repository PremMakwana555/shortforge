"""Deterministic, seeded template writer used when no LLM is reachable.

It is intentionally simple but produces *schema-valid*, readable output so that downstream
stages (TTS, visuals, render, QA) are always exercised. Output is flagged ``degraded`` upstream.
"""

from __future__ import annotations

import hashlib
import random
import re
from typing import Any

_SETTINGS = {
    "lighthouse": ("an abandoned lighthouse", "the lamp room", "salt-stained stairs", "a foghorn"),
    "forest": ("a pine forest", "a clearing", "a ring of dead trees", "snapping branches"),
    "hospital": ("a closed psychiatric hospital", "ward C", "a flickering corridor", "a wheelchair rolling"),
    "mirror": ("an old apartment", "the bathroom mirror", "a fogged-up glass", "tapping from inside the glass"),
    "doll": ("a grandmother's attic", "a cracked porcelain doll", "a dusty cradle", "a music box"),
    "radio": ("a night-shift radio station", "the broadcast booth", "a static-filled frequency", "a voice in the static"),
    "elevator": ("an office tower after midnight", "the elevator", "a floor that should not exist", "the chime"),
    "cabin": ("a cabin by a frozen lake", "the cellar door", "footprints in the snow", "knocking"),
    "well": ("a village well", "the stone well", "a frayed rope", "a voice from below"),
    "train": ("the last train of the night", "an empty carriage", "a station missing from the map", "the brakes"),
}
_DEFAULT = ("a quiet town", "the house at the end of the street", "a hallway with no light", "slow footsteps")


def _rng(*parts: str) -> random.Random:
    seed = int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:12], 16)
    return random.Random(seed)


def _setting(topic: str) -> tuple[str, str, str, str]:
    t = topic.lower()
    for key, val in _SETTINGS.items():
        if key in t:
            return val
    return _DEFAULT


def _clean_topic(topic: str) -> str:
    return re.sub(r"\s+", " ", topic).strip().rstrip(".?!")


def research_brief(ctx: dict[str, Any]) -> dict[str, Any]:
    topic = _clean_topic(ctx["topic"])
    place, focus, detail, sound = _setting(topic)
    snippets = [s for s in ctx.get("snippets", []) if s][:3]
    facts = [
        f"Stories about {topic.lower()} tend to centre on {place}.",
        f"Witness accounts repeatedly mention {sound}.",
        f"The most unsettling detail is usually {detail}.",
        "Most reports happen between 2 and 4 a.m., when the place is supposed to be empty.",
    ]
    for s in snippets:
        facts.append(s[:240])
    return {
        "summary": f"A short horror story inspired by '{topic}', set in {place}, building dread through "
                   f"{sound} and ending on a twist.",
        "angle": f"First-person account: someone returns to {place} and realises {focus} was waiting for them.",
        "facts": facts[:6],
        "keywords": [w for w in re.findall(r"[a-zA-Z]{4,}", topic.lower())][:5] or ["horror"],
    }


def script(ctx: dict[str, Any]) -> dict[str, Any]:
    topic = _clean_topic(ctx["topic"])
    rng = _rng(topic, str(ctx.get("revision", 0)))
    place, focus, detail, sound = _setting(topic)
    hook = rng.choice([
        f"Nobody warned me about {place}. I wish someone had.",
        f"There is a reason no one goes near {place} after dark.",
        f"I heard {sound} three nights in a row. On the fourth night, I went to look.",
    ])
    beats = [
        (hook, f"wide establishing shot of {place} at night, fog, moonlight"),
        (f"It started with {sound}. Faint at first, always at the same minute, always from {focus}.",
         f"{focus}, dim light, long shadows"),
        ("I told myself it was the wind. Wind does not knock in threes. Wind does not wait for you to answer.",
         f"close-up of {detail}, cold blue light"),
        ("So I took a flashlight and followed the sound. The batteries were new. The beam still shook.",
         "flashlight beam cutting through darkness, dust in the air"),
        (f"At {focus} I found {detail}, and beside it, fresh footprints. Small ones. Pointing toward me.",
         f"{detail} with footprints on the floor, eerie"),
        ("Then the sound stopped. And something behind me took a slow, deliberate breath.",
         "dark silhouette standing in a doorway, backlit"),
        ("I did not turn around. I ran. I still have not gone back.",
         "empty road at night, distant figure under a streetlight"),
        ("But every night since, at the same minute, I hear it again. Closer.",
         f"{place} seen through a rain-streaked window, a light flickers on"),
    ]
    feedback = (ctx.get("feedback") or "").lower()
    if m := re.search(r"cut to about (\d+) words", feedback):
        limit = int(m.group(1))
        while len(beats) > 5 and sum(len(t.split()) for t, _ in beats) > limit:
            beats.pop(len(beats) // 2)  # keep hook and ending, drop from the middle
    elif "short" in feedback or "too few" in feedback:
        beats.insert(3, ("The house was silent in a way that felt rehearsed, like it was holding its breath "
                         "and listening for mine.", "dark interior, a single candle"))
    return {
        "title": f"{topic.title()} | Scary Story"[:90],
        "beats": [{"text": t, "visual": f"cinematic horror photo, {v}, film grain, vertical"} for t, v in beats],
        "description": f"A short horror story inspired by {topic}. Would you have turned around?",
        "hashtags": ["#horror", "#scarystories", "#shorts", "#creepy"],
    }


TASKS = {"research_brief": research_brief, "script": script}
