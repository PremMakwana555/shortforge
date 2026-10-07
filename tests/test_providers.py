from __future__ import annotations

import dataclasses
import io

import httpx
import pytest
import respx
from PIL import Image

from shortforge.agents.script import make_script_validator
from shortforge.core.errors import ValidationFailed
from shortforge.providers import offline_writer
from shortforge.providers.images import Procedural, build_image_chain, validate_image
from shortforge.providers.llm import build_llm_chain, extract_json, generate_json

GROQ = "https://api.groq.com/openai/v1/chat/completions"


def _script(words_per_beat: int = 16, beats: int = 7) -> dict:
    return {"title": "The Last Keeper", "description": "d", "hashtags": ["horror", "#Creepy"],
            "beats": [{"text": " ".join(["word"] * words_per_beat), "visual": "fog"} for _ in range(beats)]}


@pytest.mark.parametrize("raw", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    'Sure! Here you go: {"a": 1, "b": "has } brace"} hope that helps',
])
def test_extract_json_tolerates_noise(raw):
    assert extract_json(raw)["a"] == 1


def test_extract_json_rejects_garbage():
    with pytest.raises(ValidationFailed):
        extract_json("no json here")
    with pytest.raises(ValidationFailed):
        extract_json("[1, 2]")


def test_script_validator_enforces_constraints():
    v = make_script_validator(85, 165)
    out = v(_script())
    assert out["word_count"] == 112 and "#shorts" in out["hashtags"] and "#horror" in out["hashtags"]
    with pytest.raises(ValidationFailed, match="words"):
        v(_script(words_per_beat=40))
    with pytest.raises(ValidationFailed, match="beats"):
        v(_script(beats=3, words_per_beat=30))
    bad = _script()
    bad["beats"][2]["text"] += " suicide"
    with pytest.raises(ValidationFailed, match="disallowed"):
        v(bad)


@pytest.mark.parametrize("topic", ["the lighthouse keeper", "cursed elevator", "something unknown", "a"])
def test_offline_script_always_valid(topic):
    v = make_script_validator(85, 165)
    v(offline_writer.script({"topic": topic}))
    v(offline_writer.script({"topic": topic, "feedback": "too short", "revision": 1}))


@respx.mock
async def test_llm_chain_falls_back_from_rate_limited_groq_to_offline(settings):
    s = dataclasses.replace(settings, groq_api_key="k", llm_providers=["groq", "offline"])
    respx.post(GROQ).mock(return_value=httpx.Response(429, headers={"retry-after": "60"}, text="slow down"))
    res = await generate_json(build_llm_chain(s), task="script", system="s", user="u",
                              context={"topic": "haunted well"}, validate=make_script_validator(85, 165))
    assert res.provider == "offline" and res.degraded
    assert res.attempts[0]["provider"] == "groq" and res.attempts[0]["outcome"] == "error"


@respx.mock
async def test_llm_repair_round_trip(settings):
    s = dataclasses.replace(settings, groq_api_key="k", llm_providers=["groq"])
    good = '{"summary": "a long enough summary of the story", "facts": ["f"], "angle": "x", "keywords": []}'
    route = respx.post(GROQ).mock(side_effect=[
        httpx.Response(200, json={"choices": [{"message": {"content": '{"summary": "too"}'}}],
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 5}}),
        httpx.Response(200, json={"choices": [{"message": {"content": good}}],
                                  "usage": {"prompt_tokens": 20, "completion_tokens": 9}}),
    ])
    from shortforge.agents.research import validate_brief

    res = await generate_json(build_llm_chain(s), task="research_brief", system="s", user="u",
                              context={}, validate=validate_brief)
    assert route.call_count == 2 and res.provider == "groq" and not res.degraded
    assert res.usage == {"prompt_tokens": 30, "completion_tokens": 14}
    assert "rejected" in route.calls[1].request.content.decode()


def test_validate_image_rejects_blank_and_tiny():
    def png(img: Image.Image) -> bytes:
        b = io.BytesIO()
        img.save(b, "PNG")
        return b.getvalue()

    with pytest.raises(ValidationFailed, match="blank"):
        validate_image(png(Image.new("RGB", (512, 512), (10, 10, 10))))
    with pytest.raises(ValidationFailed, match="small"):
        validate_image(png(Image.new("RGB", (64, 64))))
    with pytest.raises(ValidationFailed, match="undecodable"):
        validate_image(b"<html>error</html>")


async def test_procedural_image_is_valid():
    data = await Procedural().generate("figure in a foggy forest near a cabin", 384, 672, 1)
    assert validate_image(data).size == (384, 672)


@respx.mock
async def test_image_chain_rejects_html_error_page_and_falls_back(settings):
    s = dataclasses.replace(settings, image_providers=["pollinations", "procedural"])
    respx.get(url__startswith="https://image.pollinations.ai/").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"<html>oops</html>"))
    res = await build_image_chain(s).run(lambda p: p.generate("dark hallway", 384, 672, 3),
                                         validate=lambda d: validate_image(d) and None)
    assert res.provider == "procedural"
