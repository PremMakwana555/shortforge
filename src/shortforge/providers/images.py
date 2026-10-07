"""Image generation. Free / open models first:

* ``pollinations`` - FLUX / open diffusion models via pollinations.ai - no key, no signup
* ``huggingface``  - HF Inference (FLUX.1-schnell, SDXL ...) with a free HF token
* ``pexels``       - free stock photos (key) - real footage when diffusion is unavailable
* ``procedural``   - local PIL renderer (fog, silhouettes, vignette, grain) - always works

Every result is validated (decodes, minimum size, not a blank / single-colour frame) before it is
accepted, so a provider returning an error page as ``image/jpeg`` falls through to the next one.
"""

from __future__ import annotations

import hashlib
import io
import random
from urllib.parse import quote

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

from ..config import Settings
from ..core.errors import ProviderError, ValidationFailed
from ..core.resilience import ProviderChain, http_client, request_with_retry

NEGATIVE = "text, watermark, logo, gore, blood, nudity, deformed hands, low quality"


class ImageProvider:
    name = "base"

    def available(self) -> bool:
        return True

    async def generate(self, prompt: str, width: int, height: int, seed: int) -> bytes:
        raise NotImplementedError


class Pollinations(ImageProvider):
    name = "pollinations"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def generate(self, prompt: str, width: int, height: int, seed: int) -> bytes:
        url = f"https://image.pollinations.ai/prompt/{quote(prompt[:900], safe='')}"
        params = {"width": width, "height": height, "seed": seed, "model": self.settings.pollinations_model,
                  "nologo": "true", "safe": "true", "negative": NEGATIVE}
        async with http_client(120, self.settings.http_connect_timeout_seconds) as c:
            r = await request_with_retry(c, "GET", url, params=params, retries=1)
        if not r.headers.get("content-type", "").startswith("image/"):
            raise ProviderError(f"non-image response: {r.headers.get('content-type')}")
        return r.content


class HuggingFace(ImageProvider):
    name = "huggingface"

    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> bool:
        return bool(self.settings.hf_api_token)

    async def generate(self, prompt: str, width: int, height: int, seed: int) -> bytes:
        url = f"https://router.huggingface.co/hf-inference/models/{self.settings.hf_image_model}"
        body = {"inputs": prompt, "parameters": {"width": width, "height": height, "seed": seed,
                                                 "negative_prompt": NEGATIVE}}
        async with http_client(180, self.settings.http_connect_timeout_seconds) as c:
            r = await request_with_retry(c, "POST", url, json=body, retries=1, headers={
                "Authorization": f"Bearer {self.settings.hf_api_token}", "Accept": "image/png"})
        if not r.headers.get("content-type", "").startswith("image/"):
            raise ProviderError(f"non-image response: {r.text[:200]}")
        return r.content


class Pexels(ImageProvider):
    name = "pexels"

    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> bool:
        return bool(self.settings.pexels_api_key)

    async def generate(self, prompt: str, width: int, height: int, seed: int) -> bytes:
        # Stock search wants keywords, not a diffusion prompt: keep the subject part only.
        query = " ".join(prompt.replace("cinematic horror photo,", "").split(",")[0].split()[:5]) or "dark night"
        async with http_client(60, self.settings.http_connect_timeout_seconds) as c:
            r = await request_with_retry(c, "GET", "https://api.pexels.com/v1/search", params={
                "query": query, "orientation": "portrait", "per_page": 10},
                headers={"Authorization": self.settings.pexels_api_key})
            photos = r.json().get("photos", [])
            if not photos:
                raise ProviderError(f"no pexels results for '{query}'")
            photo = photos[seed % len(photos)]
            img = await request_with_retry(c, "GET", photo["src"].get("portrait") or photo["src"]["large2x"])
        return img.content


class Procedural(ImageProvider):
    """Offline renderer producing moody, on-theme frames from the prompt's keywords."""

    name = "procedural"

    async def generate(self, prompt: str, width: int, height: int, seed: int) -> bytes:
        img = render_procedural(prompt, width, height, seed)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue()


def validate_image(data: bytes, min_side: int = 256) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ValidationFailed(f"undecodable image: {e}") from e
    if min(img.size) < min_side:
        raise ValidationFailed(f"image too small: {img.size}")
    stat = ImageStat.Stat(img.convert("L").resize((64, 64)))
    if stat.stddev[0] < 3:
        raise ValidationFailed("image is blank / single colour")
    return img.convert("RGB")


def build_image_chain(settings: Settings) -> ProviderChain[ImageProvider]:
    catalog = {"pollinations": lambda: Pollinations(settings), "huggingface": lambda: HuggingFace(settings),
               "pexels": lambda: Pexels(settings), "procedural": Procedural}
    return ProviderChain("image", [catalog[n]() for n in settings.image_providers if n in catalog])


# --------------------------------------------------------------------------- procedural renderer
_PALETTES = [
    ((6, 8, 18), (28, 38, 62), (150, 170, 200)),     # cold moonlight
    ((10, 4, 6), (60, 14, 18), (200, 120, 110)),     # blood dusk
    ((4, 10, 8), (22, 48, 40), (140, 190, 170)),     # sickly green
    ((8, 8, 10), (40, 40, 46), (190, 190, 180)),     # grey fog
]


def _lerp(a: tuple[int, ...], b: tuple[int, ...], t: float) -> tuple[int, ...]:
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b, strict=False))


def render_procedural(prompt: str, width: int, height: int, seed: int) -> Image.Image:
    p = prompt.lower()
    rng = random.Random(int(hashlib.md5(f"{prompt}|{seed}".encode()).hexdigest()[:8], 16))
    dark, mid, light = _PALETTES[rng.randrange(len(_PALETTES))]
    if any(k in p for k in ("blood", "red", "fire", "candle")):
        dark, mid, light = _PALETTES[1]

    # sky / wall gradient
    img = Image.new("RGB", (width, height), dark)
    d = ImageDraw.Draw(img)
    horizon = int(height * rng.uniform(0.55, 0.7))
    for y in range(height):
        t = 1 - abs(y - horizon) / max(horizon, height - horizon)
        d.line([(0, y), (width, y)], fill=_lerp(dark, mid, max(0.0, t) ** 1.6))

    # light source (moon / lamp / window)
    lx, ly = int(width * rng.uniform(0.25, 0.75)), int(height * rng.uniform(0.12, 0.35))
    glow = Image.new("L", (width, height), 0)
    gd = ImageDraw.Draw(glow)
    r = int(width * rng.uniform(0.07, 0.12))
    for i in range(12, 0, -1):
        gd.ellipse([lx - r * i / 3, ly - r * i / 3, lx + r * i / 3, ly + r * i / 3], fill=int(255 / (i * 1.3)))
    glow = glow.filter(ImageFilter.GaussianBlur(width // 18))
    img = Image.composite(Image.new("RGB", img.size, light), img, glow)
    d = ImageDraw.Draw(img)

    sil = (2, 2, 4)
    ground = [(0, horizon)] + [(x, horizon + int(rng.uniform(-18, 18))) for x in range(0, width + 60, 60)] + \
        [(width, height), (0, height)]
    d.polygon(ground, fill=_lerp(dark, sil, 0.5))

    if any(k in p for k in ("forest", "tree", "woods", "clearing", "pine")) or rng.random() < 0.35:
        for _ in range(rng.randint(5, 9)):
            x = rng.randint(-40, width + 40)
            h = rng.randint(int(height * 0.25), int(height * 0.55))
            w = rng.randint(width // 14, width // 7)
            d.rectangle([x - w // 10, horizon - h // 3, x + w // 10, horizon + 40], fill=sil)
            for k in range(5):
                ty = horizon - h + k * h // 7
                d.polygon([(x, ty), (x - w // 2 - k * 6, ty + h // 4), (x + w // 2 + k * 6, ty + h // 4)], fill=sil)
    if any(k in p for k in ("house", "cabin", "lighthouse", "hospital", "building", "window", "station")):
        bw, bh = int(width * rng.uniform(0.35, 0.55)), int(height * rng.uniform(0.18, 0.28))
        bx = rng.randint(0, width - bw)
        if "lighthouse" in p:
            bw = width // 6
            bx = width // 2 - bw // 2
            bh = int(height * 0.45)
            d.polygon([(bx, horizon), (bx + bw, horizon), (bx + bw * 0.8, horizon - bh),
                       (bx + bw * 0.2, horizon - bh)], fill=sil)
            d.rectangle([bx + bw * 0.25, horizon - bh - 60, bx + bw * 0.75, horizon - bh], fill=_lerp(light, (255, 240, 200), .5))
        else:
            d.rectangle([bx, horizon - bh, bx + bw, horizon + 20], fill=sil)
            d.polygon([(bx - 20, horizon - bh), (bx + bw // 2, horizon - bh - bh // 2), (bx + bw + 20, horizon - bh)],
                      fill=sil)
            for _ in range(rng.randint(1, 3)):
                wx = rng.randint(bx + 20, bx + bw - 60)
                wy = rng.randint(horizon - bh + 20, horizon - 60)
                d.rectangle([wx, wy, wx + 40, wy + 55], fill=(230, 190, 110) if rng.random() < .6 else (60, 60, 70))
    if any(k in p for k in ("corridor", "hallway", "elevator", "ward", "carriage", "booth")):
        cx, cy = width // 2, int(height * 0.45)
        for i in range(9):
            t = i / 9
            col = _lerp(mid, dark, t)
            hw, hh = int(width * (0.6 - 0.55 * t)), int(height * (0.5 - 0.45 * t))
            d.rectangle([cx - hw, cy - hh, cx + hw, cy + hh], outline=col, width=6)
        d.rectangle([cx - 14, cy - 20, cx + 14, cy + 20], fill=light)
    if any(k in p for k in ("road", "streetlight", "street")):
        d.polygon([(width * 0.42, horizon), (width * 0.58, horizon), (width, height), (0, height)], fill=(14, 14, 18))
        sx = int(width * 0.7)
        d.line([(sx, horizon + 40), (sx, horizon - int(height * 0.2))], fill=sil, width=10)

    if any(k in p for k in ("figure", "silhouette", "someone", "man", "woman", "child", "doorway", "behind")):
        fx = int(width * rng.uniform(0.35, 0.65))
        fh = int(height * rng.uniform(0.22, 0.32))
        fy = horizon + int(height * 0.05)
        d.ellipse([fx - fh * 0.07, fy - fh, fx + fh * 0.07, fy - fh * 0.82], fill=(0, 0, 0))
        d.polygon([(fx - fh * 0.13, fy), (fx - fh * 0.1, fy - fh * 0.8), (fx + fh * 0.1, fy - fh * 0.8),
                   (fx + fh * 0.13, fy)], fill=(0, 0, 0))
    # fog layers
    for _ in range(3):
        fog = Image.effect_noise((width // 8, height // 8), 70).resize((width, height), Image.BICUBIC)
        fog = fog.filter(ImageFilter.GaussianBlur(width // 30))
        mask = Image.linear_gradient("L").resize((width, height))
        mask = ImageChops.multiply(mask.point(lambda v: int(v * 0.5)), fog)
        img = Image.composite(Image.new("RGB", img.size, _lerp(mid, light, 0.35)), img, mask)

    # vignette + grain
    vig = Image.radial_gradient("L").resize((width, height)).point(lambda v: int(min(200, v * 0.85)))
    img = Image.composite(Image.new("RGB", img.size, (0, 0, 0)), img, vig)
    img = img.point(lambda v: min(255, int(v * 1.25 + 6)))  # lift shadows so encoders keep detail
    grain = Image.effect_noise((width, height), 22).convert("RGB")
    img = Image.blend(img, ImageChops.overlay(img, grain), 0.35)
    return img.filter(ImageFilter.GaussianBlur(0.6))


def stable_seed(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16) % 2_000_000_000


