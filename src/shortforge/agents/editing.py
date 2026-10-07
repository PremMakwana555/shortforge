from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..core.worker import Agent, AgentContext
from ..models import Stage
from ..providers import media

TAIL_SECONDS = 0.8  # hold the last frame after narration ends
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def build_captions(beats: list[dict[str, Any]], timeline: list[dict[str, Any]], width: int, height: int,
                   words_per_chunk: int = 3) -> str:
    """Short-form style captions: 2-4 word chunks, timed proportionally to characters within each
    beat's exact audio window (no forced alignment model needed, drift is bounded per beat)."""
    font_size = int(width * 0.072)
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,DejaVu Sans,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,"
        f"{max(4, font_size // 12)},2,2,80,80,{int(height * 0.30)},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for beat, tl in zip(beats, timeline, strict=True):
        words = beat["text"].split()
        chunks = [" ".join(words[i:i + words_per_chunk]) for i in range(0, len(words), words_per_chunk)]
        speech_end = tl["end"] - 0.3  # exclude the inter-beat breath
        span = max(0.4, speech_end - tl["start"])
        total_chars = sum(len(c) for c in chunks) or 1
        t = tl["start"]
        for c in chunks:
            d = span * len(c) / total_chars
            text = _ass_escape(c.upper())
            # red accent on the last chunk of each beat - tiny touch of channel identity
            if c is chunks[-1] and re.search(r"[.!?]$", c):
                text = r"{\c&H3030E0&}" + text
            lines.append(f"Dialogue: 0,{_ass_time(t)},{_ass_time(t + d)},Cap,,0,0,0,,{text}")
            t += d
    return header + "\n".join(lines) + "\n"


def make_thumbnail(src: Path, title: str, dest: Path) -> None:
    img = Image.open(src).convert("RGB")
    img.thumbnail((720, 1280))
    d = ImageDraw.Draw(img)
    font_path = next((f for f in FONT_CANDIDATES if Path(f).exists()), None)
    size = int(img.width * 0.09)
    font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
    words, lines, cur = title.upper().split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > 14:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    y = int(img.height * 0.12)
    for ln in lines[:4]:
        tw = d.textlength(ln, font=font)
        d.text(((img.width - tw) / 2, y), ln, font=font, fill=(240, 240, 240), stroke_width=6,
               stroke_fill=(0, 0, 0))
        y += int(size * 1.15)
    img.save(dest, "JPEG", quality=88)


class EditingAgent(Agent):
    """FFmpeg render: per-beat Ken Burns clips (rendered in parallel) -> concat -> captions +
    loudness-normalised narration + generated ambient bed -> H.264/AAC 1080x1920 MP4."""

    stage = Stage.EDITING

    async def run(self, ctx: AgentContext) -> dict[str, Any]:
        s = ctx.settings
        W, H, fps = s.video_width, s.video_height, s.fps
        script, vo = ctx.job.output(Stage.SCRIPT), ctx.job.output(Stage.VOICEOVER)
        frames = ctx.job.output(Stage.VISUAL)["frames"]
        timeline = vo["timeline"]
        work = ctx.scratch
        if len(frames) != len(timeline):
            raise ValueError(f"frame/timeline mismatch: {len(frames)} vs {len(timeline)}")

        sem = asyncio.Semaphore(2)

        async def clip(i: int) -> Path:
            img = await ctx.artifacts.local_path(frames[i]["key"])
            dur = timeline[i]["duration"] + (TAIL_SECONDS if i == len(frames) - 1 else 0)
            n = max(1, int(round(dur * fps)))
            zin = i % 2 == 0  # alternate push-in / pull-out so cuts feel intentional
            z = f"1.0+0.12*on/{n}" if zin else f"1.12-0.12*on/{n}"
            vf = (f"scale={int(W * 1.5)}:-2,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':"
                  f"y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps},"
                  f"fade=t=in:st=0:d=0.25,format=yuv420p")
            out = work / f"clip_{i:02d}.mp4"
            async with sem:
                await media.ffmpeg("-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", str(img),
                                   "-vf", vf, "-frames:v", str(n), "-c:v", "libx264", "-preset", s.render_preset,
                                   "-crf", "20", "-pix_fmt", "yuv420p", "-an", str(out), timeout=600)
            return out

        clips = await asyncio.gather(*(clip(i) for i in range(len(frames))))
        listfile = work / "clips.txt"
        listfile.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips))
        video_only = work / "video_only.mp4"
        await media.ffmpeg("-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(video_only))

        ass = work / "captions.ass"
        ass.write_text(build_captions(script["beats"], timeline, W, H), encoding="utf-8")
        narration = await ctx.artifacts.local_path(vo["audio_key"])
        total = round(vo["duration"] + TAIL_SECONDS, 3)

        final = work / "final.mp4"
        ass_arg = ass.as_posix().replace(":", r"\:").replace("'", r"\'")
        fc = (
            f"[0:v]subtitles='{ass_arg}'[v];"
            "[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,apad[n];"
            "[2:a]lowpass=f=420,highpass=f=30,volume=0.35[amb0];"
            "[3:a]volume=0.10[drone];"
            "[amb0][drone]amix=inputs=2:normalize=0,afade=t=in:d=1.5"
            f",afade=t=out:st={max(0.0, total - 1.2):.2f}:d=1.2[amb];"
            "[n][amb]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]"
        )
        await media.ffmpeg(
            "-i", str(video_only), "-i", str(narration),
            "-f", "lavfi", "-i", "anoisesrc=color=brown:amplitude=0.15:sample_rate=48000",
            "-f", "lavfi", "-i", "sine=frequency=48:sample_rate=48000",
            "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", s.render_preset, "-crf", "21", "-pix_fmt", "yuv420p",
            "-r", str(fps), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart",
            str(final), timeout=900,
        )

        thumb = work / "thumbnail.jpg"
        make_thumbnail(await ctx.artifacts.local_path(frames[0]["key"]), script["title"], thumb)
        video_key = await ctx.artifacts.put_file(final, ctx.key(Stage.EDITING, "final.mp4"))
        thumb_key = await ctx.artifacts.put_file(thumb, ctx.key(Stage.EDITING, "thumbnail.jpg"))
        await ctx.artifacts.put_file(ass, ctx.key(Stage.EDITING, "captions.ass"))
        ctx.metrics.update(clips=len(clips), size_bytes=final.stat().st_size)
        return {"video_key": video_key, "thumbnail_key": thumb_key, "expected_duration": total,
                "size_bytes": final.stat().st_size}
