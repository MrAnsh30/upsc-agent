#!/usr/bin/env python3
"""Script 2 — Turn output/bundle.json into 1..N vertical (1080x1920) short videos.

Per video: LLM script -> edge-tts narration per scene -> free images -> Pillow text overlays ->
MoviePy assembly (pan effect + dip-to-black transitions). Writes output/manifest.json.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import re
import sys
import time
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

from common import OUT, env, llm_json

try:  # MoviePy 2.x
    from moviepy import AudioFileClip, VideoClip, concatenate_videoclips
except ImportError:  # MoviePy 1.x
    from moviepy.editor import AudioFileClip, VideoClip, concatenate_videoclips

W, H, FPS = 1080, 1920, 24
PAN = 1.15                       # background is rendered 15% larger so it can slowly pan
NARRATION_LANG = env("NARRATION_LANG", "hi")     # "hi" = Hindi voice-over, "en" = English voice-over
VOICE = env("TTS_VOICE", "hi-IN-SwaraNeural")     # Hindi: hi-IN-SwaraNeural (female) / hi-IN-MadhurNeural (male)
FALLBACK_VOICE = "en-IN-NeerjaNeural"             # used only if the Hindi script could not be generated
RATE = env("TTS_RATE", "-4%")
ITEMS_PER_VIDEO = int(env("ITEMS_PER_VIDEO", "5"))
MAX_VIDEOS = int(env("MAX_VIDEOS", "5"))          # YouTube free quota allows ~6 uploads/day
MAX_SECONDS = 58
SCENE_PAD = 0.35
UA = {"User-Agent": "UPSC-CA-Agent/1.0 (educational project; set your contact e-mail here)"}

FONT_BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf", "DejaVuSans-Bold.ttf"]
FONT_REG = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "DejaVuSans.ttf"]


# ----------------------------------------------------------------------------- grouping & script
def group_items(items: list[dict]) -> list[list[dict]]:
    items = items[: MAX_VIDEOS * ITEMS_PER_VIDEO]           # leftovers stay in the Sheet only
    items = sorted(items, key=lambda x: (x.get("category", ""), x.get("gs_paper", "")))
    n = min(MAX_VIDEOS, max(1, math.ceil(len(items) / ITEMS_PER_VIDEO)))
    size = math.ceil(len(items) / n)
    return [items[i:i + size] for i in range(0, len(items), size)]


def write_script(group: list[dict]) -> dict:
    listing = "\n".join(f"[{i}] {g['headline']} | {g['summary']} | GS: {g['gs_paper']} | "
                        f"Thread: {g['thread_title']}" for i, g in enumerate(group))
    if NARRATION_LANG == "hi":
        lang_rule = ("NARRATION LANGUAGE: VERY SIMPLE, everyday spoken Hindi written in Devanagari - the Hindi people use in "
                     "daily talk, which a Class 8 student or a person from any state can understand at once. "
                     "RULES: (1) short sentences, at most 12 words each; (2) NO difficult, formal or Sanskritised words - use the "
                     "easy word (e.g. फैसला not निर्णय, सरकार not शासन, मदद not सहायता, पैसा not धनराशि, बढ़ाना not वृद्धि करना, "
                     "नियम not विधान, बताया not उद्घोषित किया); (3) words that Hindi speakers already use from English are GOOD - "
                     "keep them but write them in Devanagari (बजट, रिपोर्ट, पॉलिसी, एग्ज़ाम, कोर्ट, यूपीआई, इसरो, ब्रिक्स, जीएसटी); "
                     "(4) if a topic has a hard term, explain it in one easy line; (5) sound like a friendly teacher talking, "
                     "not like a news reader. ON-SCREEN TEXT (title, hook.screen, screen_title, bullets) MUST stay in ENGLISH, "
                     "exactly as an English video would show it.")
        budget = "hook <= 12 words, each scene narration <= 20 words, outro <= 12 words (counted in Hindi words)"
        outro_hint = 'e.g. "पूरे नोट्स डिस्क्रिप्शन में हैं। रोज़ की रिवीज़न के लिए फ़ॉलो करें।"'
    else:
        lang_rule = "NARRATION LANGUAGE: plain spoken English. On-screen text is English too."
        budget = "hook <= 14 words, each scene narration <= 22 words, outro <= 14 words"
        outro_hint = "telling viewers the full thread-wise notes are in the description"
    prompt = f"""Write a YouTube Shorts / Instagram Reels script (<= 55 seconds spoken) for UPSC aspirants revising today's
current affairs. Structure: a curiosity-driven hook, one scene per item below (the fact PLUS why it matters for the exam),
and an outro {outro_hint}. Word budget: {budget}. No emojis or hashtags inside narration. Do not copy source sentences.
{lang_rule}
Return JSON: {{"title":"<=70 chars, ENGLISH","hook":{{"text":"<narration>","screen":"<=8 words ENGLISH"}},
"scenes":[{{"i":0,"narration":"<narration>","screen_title":"<=12 words ENGLISH","bullets":["<=9 words ENGLISH"]}}],
"outro":"<narration>","hashtags":["#UPSC"]}}  (max 3 bullets per scene, max 8 hashtags)

ITEMS:
{listing}"""
    try:
        s = llm_json(prompt)
        assert s.get("scenes") and s.get("hook")
        if NARRATION_LANG == "hi":
            spoken = " ".join([s["hook"]["text"], s.get("outro", "")] + [x["narration"] for x in s["scenes"]])
            assert re.search(r"[\u0900-\u097F]", spoken), "narration is not Hindi"
        return s
    except Exception as e:  # noqa: BLE001 - deterministic fallback so the pipeline never stalls
        print(f"[warn] script LLM failed ({e}); using English fallback script + English voice")
        return {"_voice": FALLBACK_VOICE,
                "title": f"UPSC Current Affairs: {group[0]['headline'][:50]}",
                "hook": {"text": f"{len(group)} UPSC updates you must revise today.", "screen": "UPSC Daily Revision"},
                "scenes": [{"i": i, "narration": g["headline"] + ". " + (g["key_points"][0] if g["key_points"] else ""),
                            "screen_title": g["headline"], "bullets": g["key_points"][:3]} for i, g in enumerate(group)],
                "outro": "Full notes are in the description. Follow for daily UPSC revision.",
                "hashtags": ["#UPSC", "#CurrentAffairs", "#IAS"]}


# ----------------------------------------------------------------------------- TTS
async def _edge(text: str, path: str, rate: str, voice: str) -> None:
    import edge_tts
    await edge_tts.Communicate(text, voice, rate=rate).save(path)


def synth(text: str, path: Path, rate: str, voice: str = VOICE) -> None:
    for attempt in range(3):
        try:
            asyncio.run(_edge(text, str(path), rate, voice))
            if path.exists() and path.stat().st_size > 1000:
                return
        except Exception as e:  # noqa: BLE001
            print(f"[warn] edge-tts attempt {attempt + 1} failed: {e}")
            time.sleep(3)
    from gtts import gTTS  # free fallback if Microsoft's endpoint blocks the runner IP
    gTTS(text, lang="hi" if voice.startswith("hi-") else "en", tld="co.in").save(str(path))


def audio_len(path: Path) -> float:
    a = AudioFileClip(str(path))
    d = float(a.duration)
    a.close()
    return d


# ----------------------------------------------------------------------------- images (all free)
def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def img_wikimedia(q: str):
    r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=25, params={
        "action": "query", "generator": "search", "gsrsearch": f"{q} filetype:bitmap", "gsrnamespace": 6,
        "gsrlimit": 12, "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime", "iiurlwidth": 1400,
        "format": "json"})
    for p in (r.json().get("query", {}).get("pages", {}) or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata", {})
        lic = _strip(meta.get("LicenseShortName", {}).get("value", ""))
        if ii.get("mime") != "image/jpeg" or ii.get("width", 0) < 900:
            continue
        if not re.match(r"(CC0|CC BY|Public domain|PD)", lic, re.I) or re.search(r"\b(NC|ND)\b", lic):
            continue
        artist = _strip(meta.get("Artist", {}).get("value", "")) or "Unknown"
        return ii["thumburl"], f"{artist[:60]} ({lic}), Wikimedia Commons"
    return None


def img_pexels(q: str):
    key = env("PEXELS_API_KEY")
    if not key:
        return None
    r = requests.get("https://api.pexels.com/v1/search", headers={"Authorization": key}, timeout=25,
                     params={"query": q, "orientation": "portrait", "per_page": 5})
    ph = (r.json().get("photos") or [None])[0]
    return (ph["src"]["large2x"], f"Photo by {ph['photographer']} on Pexels") if ph else None


def img_unsplash(q: str):
    key = env("UNSPLASH_ACCESS_KEY")
    if not key:
        return None
    h = {"Authorization": f"Client-ID {key}"}
    r = requests.get("https://api.unsplash.com/search/photos", headers=h, timeout=25,
                     params={"query": q, "orientation": "portrait", "per_page": 3})
    res = (r.json().get("results") or [None])[0]
    if not res:
        return None
    requests.get(res["links"]["download_location"], headers=h, timeout=15)  # required by Unsplash API terms
    return res["urls"]["regular"], f"Photo by {res['user']['name']} on Unsplash"


def gradient_bg(seed: str) -> Image.Image:
    rng = np.random.default_rng(abs(hash(seed)) % (2**32))
    c1, c2 = rng.integers(20, 90, 3), rng.integers(60, 160, 3)
    t = np.linspace(0, 1, int(H * PAN))[:, None, None]
    arr = (c1 * (1 - t) + c2 * t) * np.ones((1, int(W * PAN), 1))
    return Image.fromarray(arr.astype(np.uint8))


def get_background(query: str, used: set[str]) -> tuple[Image.Image, str]:
    for fn in (img_wikimedia, img_pexels, img_unsplash):
        try:
            hit = fn(query)
            if not hit or hit[0] in used:
                continue
            raw = requests.get(hit[0], headers=UA, timeout=40)
            raw.raise_for_status()
            im = Image.open(io.BytesIO(raw.content)).convert("RGB")
            used.add(hit[0])
            return ImageOps.fit(im, (int(W * PAN), int(H * PAN)), Image.LANCZOS), hit[1]
        except Exception as e:  # noqa: BLE001
            print(f"[warn] image source {fn.__name__} failed for '{query}': {e}")
    return gradient_bg(query), ""


# ----------------------------------------------------------------------------- overlay rendering
def font(size: int, bold: bool = True):
    for p in (FONT_BOLD if bold else FONT_REG):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap(draw, text: str, fnt, max_w: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,; ") + "…"
    return [l for l in lines if l]


def text_with_shadow(d, xy, text, fnt, fill):
    d.text((xy[0] + 3, xy[1] + 3), text, font=fnt, fill=(0, 0, 0, 200))
    d.text(xy, text, font=fnt, fill=fill)


def build_layer(scene: dict) -> Image.Image:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    mx, maxw = 70, W - 140
    chip = scene.get("chip", "UPSC DAILY REVISION")
    cf = font(34)
    cw = int(d.textlength(chip, font=cf)) + 50
    d.rounded_rectangle((mx, 210, mx + cw, 270), 30, fill=(255, 153, 51, 235))
    d.text((mx + 25, 220), chip, font=cf, fill=(20, 20, 20, 255))

    if scene["kind"] in ("hook", "outro"):
        big = font(86)
        lines = wrap(d, scene["screen_title"], big, maxw, 5)
        y = 820
        for l in lines:
            text_with_shadow(d, (mx, y), l, big, (255, 255, 255, 255))
            y += 108
        if scene["kind"] == "outro":
            sf = font(44, False)
            for l in wrap(d, "Thread-wise notes + GS mapping: Master Sheet link in the description", sf, maxw, 3):
                text_with_shadow(d, (mx, y + 30), l, sf, (255, 214, 153, 255))
                y += 58
        return layer

    hf, bf = font(64), font(42, False)
    y = 880
    for l in wrap(d, scene["screen_title"], hf, maxw, 4):
        text_with_shadow(d, (mx, y), l, hf, (255, 255, 255, 255))
        y += 80
    y += 24
    for b in scene.get("bullets", [])[:3]:
        for j, l in enumerate(wrap(d, b, bf, maxw - 50, 2)):
            if y > 1480:
                break
            if j == 0:
                text_with_shadow(d, (mx, y), "•", bf, (255, 153, 51, 255))
            text_with_shadow(d, (mx + 50, y), l, bf, (235, 235, 235, 255))
            y += 56
        y += 14
    sf = font(30, False)
    text_with_shadow(d, (mx, 1500), f"Source: {scene.get('source', '')}  |  {scene.get('gs', '')}", sf, (200, 200, 200, 255))
    return layer


_GRAD = None


def grad() -> np.ndarray:
    global _GRAD
    if _GRAD is None:
        ys = np.linspace(0, 1, H, dtype=np.float32)
        _GRAD = (np.clip((ys - 0.30) / 0.40, 0, 1) * 0.80 + 0.12)[:, None, None]
    return _GRAD


def scene_clip(bg: Image.Image, layer: Image.Image, dur: float, flip: bool):
    bgarr = np.asarray(bg)
    BH, BW = bgarr.shape[:2]
    lay = np.asarray(layer)
    t_rgb = lay[..., :3].astype(np.float32)
    t_a = lay[..., 3:4].astype(np.float32) / 255.0
    g = grad()
    mx, my = BW - W, BH - H

    def frame(t: float):
        p = min(max(t / dur, 0.0), 1.0)
        x = int(mx * ((1 - p) if flip else p))
        y = int(my * (0.3 + 0.4 * p))
        out = bgarr[y:y + H, x:x + W].astype(np.float32) * (1 - g)
        k = t_a * min(1.0, t / 0.5)                       # text fades in
        out = out * (1 - k) + t_rgb * k
        fade = min(1.0, t / 0.25, max(0.0, (dur - t) / 0.25))  # dip-to-black transition
        return np.clip(out * fade, 0, 255).astype(np.uint8)

    return VideoClip(frame, duration=dur)


def with_audio(clip, audio):
    return clip.with_audio(audio) if hasattr(clip, "with_audio") else clip.set_audio(audio)


# ----------------------------------------------------------------------------- build one video
def build_video(idx: int, group: list[dict]) -> dict:
    script = write_script(group)
    voice = script.get("_voice", VOICE)
    tmp = OUT / "tmp" / f"v{idx}"
    tmp.mkdir(parents=True, exist_ok=True)

    first_q, last_q = group[0]["image_query"], group[-1]["image_query"]
    gs_tags = sorted({g["gs_paper"] for g in group if g.get("gs_paper")})
    chip = "UPSC DAILY • " + "/".join(gs_tags) if gs_tags else "UPSC DAILY REVISION"
    scenes = [{"kind": "hook", "narration": script["hook"]["text"], "screen_title": script["hook"].get("screen", "UPSC Daily Revision"),
               "image_query": first_q, "chip": chip}]
    for sc in script["scenes"]:
        g = group[sc["i"]] if isinstance(sc.get("i"), int) and 0 <= sc["i"] < len(group) else group[len(scenes) - 1]
        scenes.append({"kind": "item", "narration": sc["narration"], "screen_title": sc["screen_title"],
                       "bullets": sc.get("bullets", []), "image_query": g["image_query"], "source": g["source"],
                       "gs": g["gs_paper"], "chip": chip})
    scenes.append({"kind": "outro", "narration": script["outro"], "screen_title": "Revise the full notes",
                   "image_query": last_q, "chip": chip})

    def synth_all(rate: str) -> float:
        total = 0.0
        for k, s in enumerate(scenes):
            p = tmp / f"s{k}.mp3"
            synth(s["narration"], p, rate, voice)
            s["audio"], s["dur"] = p, audio_len(p) + SCENE_PAD
            total += s["dur"]
        return total

    total = synth_all(RATE)
    if total > MAX_SECONDS:
        print(f"[warn] video {idx} is {total:.0f}s; re-voicing faster to fit Shorts limit")
        total = synth_all("+15%")

    used: set[str] = set()
    credits: list[str] = []
    cache: dict[str, tuple[Image.Image, str]] = {}
    clips, audios = [], []
    for k, s in enumerate(scenes):
        q = s["image_query"]
        if q not in cache:
            cache[q] = get_background(q, used)
            if cache[q][1]:
                credits.append(cache[q][1])
        bg, _ = cache[q]
        clip = scene_clip(bg, build_layer(s), s["dur"], flip=bool(k % 2))
        a = AudioFileClip(str(s["audio"]))
        audios.append(a)
        clips.append(with_audio(clip, a))

    final = concatenate_videoclips(clips, method="chain")
    path = OUT / "videos" / f"video_{idx:02d}.mp4"
    final.write_videofile(str(path), fps=FPS, codec="libx264", audio_codec="aac", preset="veryfast",
                          threads=2, logger=None, ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"])
    final.close()
    for a in audios:
        a.close()
    return {"file": str(path), "title": script["title"][:90], "hashtags": script.get("hashtags", ["#UPSC"])[:8],
            "duration": round(total, 1), "voice": voice, "credits": sorted(set(credits)),
            "items": [{"news_id": g["news_id"], "thread_id": g["thread_id"], "headline": g["headline"],
                       "gs_paper": g["gs_paper"]} for g in group]}


def main() -> int:
    bundle = json.loads((OUT / "bundle.json").read_text())
    if not bundle["items"]:
        print("[info] empty bundle; no videos today")
        (OUT / "manifest.json").write_text(json.dumps({"sheet_url": bundle["sheet_url"], "videos": []}))
        return 0
    videos = []
    for i, grp in enumerate(group_items(bundle["items"]), start=1):
        print(f"[info] building video {i} ({len(grp)} items)")
        videos.append(build_video(i, grp))
    (OUT / "manifest.json").write_text(json.dumps(
        {"date": bundle["date"], "sheet_url": bundle["sheet_url"], "videos": videos}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
