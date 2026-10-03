#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import html
import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path

import edge_tts
import requests
import trafilatura
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from json_repair import repair_json
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

VERSION = "2026.10.02-groq-json-fix-v2"
WORK = Path(os.getenv("WORK_DIR", "work"))
WORK.mkdir(parents=True, exist_ok=True)
W, H, FPS = 1280, 720, 30
MIN_SEC, TARGET_SEC, MAX_SEC = 280, 360, 600
MIN_CUT, MAX_CUT = 3.2, 7.0
VOICE = os.getenv("EDGE_VOICE", "ja-JP-NanamiNeural")
RATE = os.getenv("EDGE_VOICE_RATE", "+0%")
DRY = os.getenv("DRY_RUN", "true").lower() == "true"
UPLOAD = os.getenv("UPLOAD_TO_YOUTUBE", "true").lower() == "true"
PRIVACY = os.getenv("PRIVACY_STATUS", "private")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_MAX_TOKENS = int(os.getenv("GROQ_MAX_TOKENS", "4300"))
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
RESULTS = max(3, min(50, int(os.getenv("PIXABAY_RESULTS_PER_QUERY", "20"))))
BGM = Path(os.getenv("BGM_PATH", "assets/background_music.mp3"))
SOURCE_LIMIT = max(1, min(4, int(os.getenv("RESEARCH_SOURCE_LIMIT", "3"))))
SOURCE_CHARS = max(300, min(1000, int(os.getenv("RESEARCH_CHARS_PER_SOURCE", "600"))))
USED_IDS, USED_URLS, USED_PAGES, CACHE = set(), set(), set(), {}
FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("youtube-auto")


def run(cmd, timeout=1800):
    log.info("コマンド実行: %s", cmd[0])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("{}失敗\n{}".format(cmd[0], result.stderr[-5000:]))
    return result


def font_path():
    for path in FONTS:
        if Path(path).exists():
            return path
    raise RuntimeError("日本語フォントがありません。fonts-noto-cjkを導入してください。")


def clean(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"https?://\S+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def duration(path):
    result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], 60)
    raw = result.stdout.strip()
    if not raw:
        raise RuntimeError("再生時間を取得できません: {}".format(path))
    return float(raw)


def secret(name):
    raw = os.getenv(name, "").strip()
    if not raw:
        raise RuntimeError(name + "が未設定です。")
    return json.loads(raw)


def ass_time(seconds):
    cs = max(0, int(round(seconds * 100)))
    hours, cs = divmod(cs, 360000)
    minutes, cs = divmod(cs, 6000)
    seconds, cs = divmod(cs, 100)
    return "{}:{:02d}:{:02d}.{:02d}".format(hours, minutes, seconds, cs)


def ass_escape(value):
    return str(value).replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def wrap(value, length):
    value = clean(value)
    return "\n".join(value[i:i + length] for i in range(0, len(value), length)) if value else ""


def word_tokens(value):
    ignored = {"the", "and", "with", "from", "into", "inside", "outside", "close", "view", "footage", "video", "scene", "real", "documentary", "showing", "during", "while", "near", "over", "under", "high", "quality", "hyper", "realistic"}
    return {w for w in re.findall(r"[a-z0-9]+", str(value).lower()) if len(w) >= 3 and w not in ignored}


def parse_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip(), flags=re.I | re.S)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = json.loads(repair_json(text))
    if isinstance(data, list):
        data = {"title": "", "description": "", "genre": "", "tags": [], "scenes": data}
    if not isinstance(data, dict):
        raise RuntimeError("LLM応答がJSONオブジェクトではありません。")
    return data


class LLM:
    def __init__(self):
        self.key = os.getenv("GROQ_API_KEY", "").strip()
        if not self.key:
            raise RuntimeError("GROQ_API_KEYがありません。")

    def post(self, payload):
        last_error = None
        for attempt in range(5):
            try:
                response = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=180,
                )
                if response.status_code < 400:
                    return response.json()
                last_error = RuntimeError("HTTP {}: {}".format(response.status_code, response.text[:1500]))
                if response.status_code == 413 or response.status_code not in {408, 429, 500, 502, 503, 504}:
                    raise last_error
                wait = float(response.headers.get("retry-after", min(60, 5 * 2 ** attempt))) + random.random()
            except requests.RequestException as error:
                last_error = error
                wait = min(60, 5 * 2 ** attempt) + random.random()
            log.warning("Groq API再試行まで%.1f秒", wait)
            time.sleep(wait)
        raise RuntimeError("Groq API呼び出し失敗: {}".format(last_error))

    def request(self, prompt, max_tokens):
        payload = {
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": "JSONオブジェクトだけを返してください。前置き、Markdown、コードフェンスは禁止です。出力が長すぎる場合は説明を短くしても、必ず閉じ括弧まで完成させてください。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.35,
            "max_tokens": max_tokens,
        }
        if GROQ_MODEL.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = "low"
        result = self.post(payload)
        choices = result.get("choices", [])
        if not choices or not choices[0].get("message", {}).get("content"):
            raise RuntimeError("Groq応答本文が空です。")
        usage = result.get("usage", {})
        log.info("Groq使用量 input=%s output=%s total=%s", usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"), usage.get("total_tokens", "?"))
        return parse_json(choices[0]["message"]["content"])

    def ask(self, prompt):
        try:
            return self.request(prompt, GROQ_MAX_TOKENS)
        except Exception as first:
            text = str(first)
            if not any(x in text for x in ("HTTP 400", "json_validate_failed", "HTTP 413", "Request too large", "tokens per minute", "rate_limit_exceeded")):
                raise
            log.warning("GroqのJSON生成またはトークン上限で失敗。参考資料と出力量を圧縮して再試行します。")
            compact = re.sub(r"参考資料:\s*.*?\n\s*出力形式:", "参考資料:\n容量制限のため省略。一般に確認されている事実のみ使用。\n\n出力形式:", prompt, flags=re.S)
            return self.request(compact, min(GROQ_MAX_TOKENS, 3800))


def research(theme):
    results, seen = [], set()
    for query in (theme, theme + " 仕組み 理由", theme + " 意外な事実"):
        log.info("Web検索: %s", query)
        try:
            with DDGS() as ddgs:
                for item in ddgs.text(query, region="jp-jp", max_results=5):
                    url = str(item.get("href", "")).strip()
                    if url and url not in seen:
                        seen.add(url)
                        results.append(item)
        except Exception as error:
            log.warning("Web検索失敗: %s", error)
    sources = []
    for item in results[:10]:
        url = str(item.get("href", "")).strip()
        body = ""
        try:
            page = trafilatura.fetch_url(url)
            if page:
                body = trafilatura.extract(page, favor_precision=True, include_comments=False, include_tables=False) or ""
        except Exception as error:
            log.warning("本文取得失敗: %s", error)
        text = clean(body or item.get("body", ""))
        if len(text) >= 120:
            sources.append({"title": clean(item.get("title", ""))[:150], "url": url, "text": text[:SOURCE_CHARS]})
        if len(sources) >= SOURCE_LIMIT:
            break
    (WORK / "research.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    return sources


def split_scene(scene, theme):
    text = clean(scene.get("narration", "")) or "{}について詳しく見ていきます。".format(theme)
    middle = len(text) // 2
    cut = max(text.rfind(mark, 0, min(len(text), middle + 100)) for mark in "。！？")
    cut = middle if cut < max(40, middle - 140) else cut + 1
    cuts = scene.get("visual_cuts", []) if isinstance(scene.get("visual_cuts", []), list) else []
    split = max(1, len(cuts) // 2)
    first, second = dict(scene), dict(scene)
    first["narration"] = text[:cut].strip() or text
    second["narration"] = text[cut:].strip() or text
    second["heading"] = clean(scene.get("heading", theme)) + " 続き"
    first["visual_cuts"] = cuts[:split] or cuts
    second["visual_cuts"] = cuts[split:] or cuts
    return first, second


def normalize(data, theme):
    if isinstance(data, list):
        data = {"title": theme, "description": "", "genre": "", "tags": [], "scenes": data}
    if not isinstance(data, dict):
        raise RuntimeError("台本データを復元できません。")
    scenes = data.get("scenes", [])
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("台本にシーンがありません。")
    while len(scenes) < 9:
        index = max(range(len(scenes)), key=lambda i: len(clean(scenes[i].get("narration", ""))) if isinstance(scenes[i], dict) else 0)
        raw = scenes.pop(index)
        if not isinstance(raw, dict):
            raw = {"heading": theme, "narration": str(raw), "visual_cuts": []}
        first, second = split_scene(raw, theme)
        scenes[index:index] = [first, second]
    while len(scenes) > 11:
        extra = scenes.pop()
        if not isinstance(extra, dict):
            extra = {"narration": str(extra), "visual_cuts": []}
        target = scenes[-1]
        if not isinstance(target, dict):
            target = {"heading": theme, "narration": str(target), "visual_cuts": []}
            scenes[-1] = target
        target["narration"] = (clean(target.get("narration", "")) + " " + clean(extra.get("narration", ""))).strip()
        a = target.get("visual_cuts", []) if isinstance(target.get("visual_cuts", []), list) else []
        b = extra.get("visual_cuts", []) if isinstance(extra.get("visual_cuts", []), list) else []
        target["visual_cuts"] = a + b
    for number, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            scene = {"heading": "シーン{}".format(number), "narration": str(scene), "visual_cuts": []}
            scenes[number - 1] = scene
        scene["heading"] = clean(scene.get("heading", "")) or "シーン{}".format(number)
        scene["narration"] = clean(scene.get("narration", "")) or "{}について順番に整理します。".format(theme)
        raw_cuts = scene.get("visual_cuts", []) if isinstance(scene.get("visual_cuts", []), list) else []
        valid = []
        for cut in raw_cuts:
            if not isinstance(cut, dict):
                continue
            points = cut.get("card_points", []) if isinstance(cut.get("card_points", []), list) else []
            valid.append({
                "narration_excerpt": clean(cut.get("narration_excerpt", ""))[:120],
                "query": (clean(cut.get("query", "")) or "{} subject".format(theme))[:100],
                "fallback_query": (clean(cut.get("fallback_query", "")) or "{} close up".format(theme))[:100],
                "card_title": (clean(cut.get("card_title", "")) or scene["heading"])[:28],
                "card_points": [clean(x)[:44] for x in points if clean(x)][:3],
            })
        if not valid:
            valid = [{"narration_excerpt": scene["narration"][:90], "query": "{} subject".format(theme)[:100], "fallback_query": "{} close up".format(theme)[:100], "card_title": scene["heading"][:28], "card_points": []}]
        scene["visual_cuts"] = valid[:6]
    data["scenes"] = scenes
    data["title"] = clean(data.get("title", ""))[:100] or theme[:100]
    data["description"] = str(data.get("description", "") or "").strip()
    tags = data.get("tags", []) if isinstance(data.get("tags", []), list) else []
    data["tags"] = [clean(x)[:30] for x in tags if clean(x)][:20]
    return data


def create_script(llm, theme, genre, tone, sources):
    refs = "\n\n".join("[{}] {}\nURL: {}\n{}".format(i + 1, s["title"][:120], s["url"], s["text"][:SOURCE_CHARS]) for i, s in enumerate(sources[:SOURCE_LIMIT]))
    refs = refs or "資料不足。一般に確認されている事実のみ使用し、不確かな数値は断定しないこと。"
    prompt = """テーマ「{theme}」の日本語YouTube動画をJSONで設計。
ジャンル: {genre}
トーン: {tone}
条件:
- 8〜10分、ナレーション約3800〜4600文字、9〜11シーン
- 冒頭15秒は強い疑問か意外な事実
- 重複や冗長な前置きを避ける
- 各シーンに原稿と直接対応するvisual_cutsを2個
- queryとfallback_queryは具体的な英語検索語
- 原稿にない著名人、企業、商品を追加しない
- 最後は要点のまとめ
- JSON以外は禁止
参考資料:
{refs}
出力形式:
{{"title":"","description":"","genre":"","tags":[""],"scenes":[{{"heading":"","narration":"","visual_cuts":[{{"narration_excerpt":"","query":"","fallback_query":"","card_title":"","card_points":[""]}}]}}]}}
""".format(theme=theme, genre=genre, tone=tone, refs=refs)
    data = normalize(llm.ask(prompt), theme)
    urls = "\n".join("- " + s["url"] for s in sources[:SOURCE_LIMIT] if s.get("url"))
    data["description"] = data["description"] + (("\n\n参考資料\n" + urls) if urls else "") + "\n\n※AI・自動生成技術を使用しています。"
    return data


def adjust_duration(llm, data, seconds, theme):
    ratio = TARGET_SEC / max(seconds, 1)
    if seconds < MIN_SEC:
        log.info("動画時間が不足しています(%.1f秒)。LLMでナレーションを加筆します。", seconds)
        for i, scene in enumerate(data["scenes"]):
            text = clean(scene.get("narration", ""))
            target = int(len(text) * min(1.8, ratio))
            if len(text) < target:
                prompt = """以下のナレーションを、同じ言い回しの繰り返しを避け、具体例や詳細な解説を加えて {target}文字程度 に加筆してください。
元のトーンを維持し、JSON以外の前置きは一切禁止です。
元のナレーション: {text}

出力形式: {{"narration": "加筆されたテキスト"}}""".format(target=target, text=text)
                try:
                    res = llm.request(prompt, 2000)
                    if "narration" in res and res["narration"]:
                        scene["narration"] = clean(res["narration"])
                except Exception as e:
                    log.warning("シーン%dの加筆処理失敗: %s", i + 1, e)
    else:
        log.info("動画時間が長すぎるため、テキストを短縮します。")
        for scene in data["scenes"]:
            text = clean(scene.get("narration", ""))
            shortened = text[:max(120, int(len(text) * max(0.68, ratio)))]
            stop = max(shortened.rfind("。"), shortened.rfind("！"), shortened.rfind("？"))
            scene["narration"] = shortened[:stop + 1] if stop > len(shortened) * 0.6 else shortened
    return normalize(data, theme)

def subtitle_chunks(text):
    output = []
    for sentence in re.split(r"(?<=[。！？!?])", clean(text)):
        sentence = sentence.strip()
        while len(sentence) > 24:
            position = max(sentence.rfind(mark, 9, 25) for mark in ("、", "，", " "))
            position = 24 if position < 9 else position + 1
            output.append(sentence[:position].strip())
            sentence = sentence[position:].strip()
        if sentence:
            output.append(sentence)
    return output or [clean(text)]


async def synthesize(text, path):
    last_error = None
    for attempt in range(4):
        try:
            await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))
            if path.exists() and path.stat().st_size > 1000:
                return
        except Exception as error:
            last_error = error
        await asyncio.sleep(2 ** attempt)
    raise RuntimeError("音声生成失敗: {}".format(last_error))


def make_ass(events, scenes, timeline, path):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK JP,46,&H00FFFFFF,&H00FFFFFF,&H00141414,&H96000000,-1,0,0,0,100,100,0,0,3,4,1,2,85,85,55,1
Style: Heading,Noto Sans CJK JP,38,&H00FFFFFF,&H00FFFFFF,&H00141414,&H99000000,-1,0,0,0,100,100,0,0,3,3,1,7,42,42,35,1
Style: Emphasis,Noto Sans CJK JP,50,&H005CE4FF,&H005CE4FF,&H00141414,&H99000000,-1,0,0,0,100,100,0,0,3,4,1,2,80,80,55,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for i, scene in enumerate(scenes):
        start = timeline[i]["start"]
        lines.append("Dialogue: 1,{},{},Heading,,0,0,0,,{{\\fad(140,180)}}{}\n".format(ass_time(start), ass_time(min(timeline[i]["end"], start + 2.4)), ass_escape(scene["heading"])))
    emphasis = re.compile(r"(\d+(?:\.\d+)?(?:年|秒|分|倍|個|種類|％|%|歳)?|重要|結論|理由|ポイント|実は|つまり)")
    for event in events:
        source = event["text"]
        style = "Emphasis" if emphasis.search(source) else "Default"
        if len(source) > 17:
            middle = len(source) // 2
            pos = max(source.rfind(mark, 6, middle + 5) for mark in ("、", "。", " "))
            pos = middle if pos < 6 else pos + 1
            text = ass_escape(source[:pos].strip()) + r"\N" + ass_escape(source[pos:].strip())
        else:
            text = ass_escape(source)
        lines.append("Dialogue: 0,{},{},{},,0,0,0,,{{\\fad(70,90)}}{}\n".format(ass_time(event["start"]), ass_time(event["end"]), style, text))
    path.write_text("".join(lines), encoding="utf-8-sig")


def create_narration(scenes):
    for path in WORK.glob("voice_*.mp3"):
        path.unlink(missing_ok=True)
    async def create_all():
        for i, scene in enumerate(scenes):
            log.info("音声生成 %d/%d", i + 1, len(scenes))
            await synthesize(scene["narration"], WORK / "voice_{:02d}.mp3".format(i))
    asyncio.run(create_all())
    paths, timeline, events, cursor = [], [], [], 0.0
    for i, scene in enumerate(scenes):
        path = WORK / "voice_{:02d}.mp3".format(i)
        sec = duration(path)
        start, end = cursor, cursor + sec
        chunks = subtitle_chunks(scene["narration"])
        total = sum(max(1, len(x)) for x in chunks)
        sub_cursor = start
        scene_events = []
        for n, chunk in enumerate(chunks):
            chunk_end = end if n == len(chunks) - 1 else min(end, sub_cursor + max(1.2, min(3.4, sec * max(1, len(chunk)) / total)))
            event = {"scene_index": i, "chunk_index": n, "start": sub_cursor, "end": chunk_end, "text": chunk}
            events.append(event)
            scene_events.append(event)
            sub_cursor = chunk_end
        timeline.append({"scene_index": i, "start": start, "end": end, "duration": sec, "events": scene_events})
        cursor, paths = end, paths + [path]
    listing = WORK / "audio.txt"
    listing.write_text("".join("file '{}'\n".format(str(p.resolve()).replace("'", "'\\''")) for p in paths), encoding="utf-8")
    output = WORK / "narration.m4a"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", str(output)])
    subtitles = WORK / "subtitles.ass"
    make_ass(events, scenes, timeline, subtitles)
    return output, timeline, duration(output), subtitles


def pixabay_search(query, media_type):
    normalized = clean(query).lower()[:100]
    key = media_type + ":" + normalized
    if not normalized:
        return []
    if key in CACHE:
        return CACHE[key]
    try:
        url = "https://pixabay.com/api/videos/" if media_type == "video" else "https://pixabay.com/api/"
        params = {"key": PIXABAY_KEY, "q": normalized, "lang": "en", "safesearch": "true", "order": "popular", "per_page": RESULTS, "min_width": 1280, "min_height": 720}
        if media_type == "video":
            params["video_type"] = "film"
        else:
            params.update({"image_type": "photo", "orientation": "horizontal"})
        response = requests.get(url, params=params, timeout=45)
        response.raise_for_status()
        hits = response.json().get("hits", [])
        CACHE[key] = hits if isinstance(hits, list) else []
    except Exception as error:
        log.warning("Pixabay%s検索失敗 [%s]: %s", media_type, normalized, error)
        CACHE[key] = []
    return CACHE[key]


def relevance(query, item):
    q = word_tokens(query)
    words = word_tokens(str(item.get("tags", "")) + " " + str(item.get("pageURL", "")))
    return len(q & words) / max(1, min(len(q), 5)) if q else 0.0


def select_video_url(item):
    choices = []
    for quality, penalty in (("large", 0), ("medium", 100), ("small", 10000), ("tiny", 20000)):
        info = item.get("videos", {}).get(quality, {})
        url = str(info.get("url", ""))
        width, height = int(info.get("width", 0) or 0), int(info.get("height", 0) or 0)
        if url and width > height and width >= 640 and height >= 360:
            choices.append((abs(width / height - 16 / 9) * 10000 + penalty - min(width, 1920) / 100, url))
    return sorted(choices)[0][1] if choices else None


def download_asset(item, query, media_type, scene_index, cut_index):
    item_id = int(item.get("id", 0) or 0)
    unique_id = (media_type, item_id)
    page_url = str(item.get("pageURL", ""))
    if media_type == "video":
        url = select_video_url(item)
        suffix = ".mp4"
    else:
        url = str(item.get("largeImageURL") or item.get("fullHDURL") or item.get("webformatURL") or "")
        suffix = ".png" if ".png" in url.lower() else ".jpg"
    if not item_id or not url or unique_id in USED_IDS or url in USED_URLS or (page_url and page_url in USED_PAGES) or relevance(query, item) <= 0:
        return None, None
    path = WORK / "pixabay_{}_{:02d}_{:03d}{}".format(media_type, scene_index, cut_index, suffix)
    try:
        response = requests.get(url, stream=media_type == "video", timeout=120)
        response.raise_for_status()
        if media_type == "video":
            with path.open("wb") as out:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        out.write(chunk)
            if path.stat().st_size < 100000 or duration(path) < MIN_CUT:
                path.unlink(missing_ok=True)
                return None, None
        else:
            path.write_bytes(response.content)
            if path.stat().st_size < 50000:
                path.unlink(missing_ok=True)
                return None, None
            with Image.open(path) as image:
                image.verify()
        USED_IDS.add(unique_id)
        USED_URLS.add(url)
        if page_url:
            USED_PAGES.add(page_url)
        return path, {"media_type": media_type, "id": item_id, "page_url": page_url, "creator_name": str(item.get("user", "")), "query": query}
    except Exception as error:
        log.warning("%s取得失敗 [%s]: %s", media_type, query, error)
        path.unlink(missing_ok=True)
        return None, None


def expand_specs(scene, count):
    cuts = scene.get("visual_cuts", []) or [{"narration_excerpt": scene["narration"][:90], "query": scene["heading"], "fallback_query": scene["heading"], "card_title": scene["heading"], "card_points": []}]
    suffixes = ("", " close up", " wide shot", " natural environment", " detailed view", " slow motion")
    specs = []
    for i in range(count):
        spec = dict(cuts[i % len(cuts)])
        cycle = i // len(cuts)
        if cycle:
            spec["query"] = (clean(spec.get("query", "")) + suffixes[cycle % len(suffixes)])[:100]
        specs.append(spec)
    return specs


def acquire_assets(scenes, timeline):
    all_assets, credits = [], []
    for si, scene in enumerate(scenes):
        count = max(len(scene["visual_cuts"]), math.ceil(timeline[si]["duration"] / MAX_CUT))
        selected = []
        for ci, spec in enumerate(expand_specs(scene, count)):
            queries = []
            for q in (spec.get("query", ""), spec.get("fallback_query", "")):
                q = clean(q)
                if q and q not in queries:
                    queries.append(q)
            asset = None
            for media_type in ("video", "image"):
                for query in queries:
                    results = sorted(pixabay_search(query, media_type), key=lambda x: relevance(query, x), reverse=True)
                    for item in results:
                        path, credit = download_asset(item, query, media_type, si, ci)
                        if path:
                            asset = {"type": media_type, "path": path, "spec": spec}
                            credits.append(credit)
                            break
                    if asset:
                        break
                if asset:
                    break
            selected.append(asset or {"type": "card", "path": None, "spec": spec})
        all_assets.append(selected)
        log.info("シーン%d: 動画%d 画像%d カード%d", si + 1, sum(x["type"] == "video" for x in selected), sum(x["type"] == "image" for x in selected), sum(x["type"] == "card" for x in selected))
    (WORK / "pixabay_credits.json").write_text(json.dumps(credits, ensure_ascii=False, indent=2), encoding="utf-8")
    return all_assets, credits


def fit_font(draw, text, max_size, min_size, max_width, max_height, spacing=10):
    selected = ImageFont.truetype(font_path(), min_size)
    for size in range(max_size, min_size - 1, -2):
        candidate = ImageFont.truetype(font_path(), size)
        box = draw.multiline_textbbox((0, 0), text, font=candidate, spacing=spacing)
        selected = candidate
        if box[2] - box[0] <= max_width and box[3] - box[1] <= max_height:
            break
    return selected


def create_card(scene, spec, index):
    path = WORK / "card_{:03d}.jpg".format(index)
    image = Image.new("RGB", (W, H), (13, 22, 39))
    draw = ImageDraw.Draw(image)
    for y in range(H):
        ratio = y / (H - 1)
        draw.line((0, y, W, y), fill=(int(13 + 13 * ratio), int(22 + 20 * ratio), int(39 + 35 * ratio)))
    draw.rounded_rectangle((42, 38, 1238, 682), radius=28, fill=(20, 31, 53), outline=(79, 163, 230), width=3)
    draw.rounded_rectangle((75, 78, 1205, 225), radius=22, fill=(29, 46, 76))
    title = wrap(clean(spec.get("card_title", "")) or scene["heading"], 17)
    title_font = fit_font(draw, title, 54, 38, 1040, 125, 8)
    box = draw.multiline_textbbox((0, 0), title, font=title_font, spacing=8, align="center", stroke_width=2)
    draw.multiline_text(((W - (box[2] - box[0])) / 2, 151 - (box[3] - box[1]) / 2), title, font=title_font, fill=(255, 228, 92), stroke_width=2, stroke_fill="black", spacing=8, align="center")
    points = spec.get("card_points", []) if isinstance(spec.get("card_points", []), list) else []
    points = [clean(x) for x in points if clean(x)][:3] or [clean(spec.get("narration_excerpt", "")) or scene["narration"][:90]]
    height = 330 // len(points)
    for i, point in enumerate(points):
        y = 285 + i * height
        draw.ellipse((86, y + 13, 106, y + 33), fill=(79, 177, 255))
        text = wrap(point, 27)
        draw.multiline_text((132, y), text, font=fit_font(draw, text, 34, 26, 1000, height - 15, 8), fill=(245, 248, 255), spacing=8)
    image.save(path, quality=94, optimize=True)
    return path


def image_clip(source, output, seconds, index):
    frames = max(1, int(seconds * FPS))
    modes = [
        ("min(zoom+0.00045,1.10)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        ("min(zoom+0.00035,1.08)", "min(iw-iw/zoom,(on/{})*(iw-iw/zoom))".format(frames), "ih/2-(ih/zoom/2)"),
        ("1.08", "max(0,(1-on/{})*(iw-iw/zoom))".format(frames), "ih/2-(ih/zoom/2)"),
        ("min(zoom+0.0004,1.09)", "iw/2-(iw/zoom/2)", "min(ih-ih/zoom,(on/{})*(ih-ih/zoom))".format(frames)),
    ]
    z, x, y = modes[index % len(modes)]
    vf = "scale=1600:900:force_original_aspect_ratio=increase,crop=1600:900,zoompan=z='{}':x='{}':y='{}':d={}:s=1280x720:fps=30,eq=contrast=1.03:saturation=1.04,format=yuv420p".format(z, x, y, frames)
    run(["ffmpeg", "-y", "-loop", "1", "-i", str(source), "-an", "-vf", vf, "-t", "{:.3f}".format(seconds), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-r", str(FPS), str(output)])


def video_clip(source, output, seconds, index):
    source_sec = duration(source)
    start = max(0.0, source_sec - seconds - 0.1) * (0.15 + 0.7 * (((index * 37) % 100) / 100))
    vf = "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,fps=30,eq=contrast=1.025:saturation=1.04,format=yuv420p"
    run(["ffmpeg", "-y", "-ss", "{:.3f}".format(start), "-i", str(source), "-an", "-t", "{:.3f}".format(seconds), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-r", str(FPS), str(output)])


def cut_durations(total, count):
    pattern = (-0.65, 0.35, -0.25, 0.55, 0.0)
    values = [max(2.8, min(7.2, total / count + pattern[i % len(pattern)])) for i in range(count)]
    scale = total / sum(values)
    values = [x * scale for x in values]
    values[-1] += total - sum(values)
    return values


def create_final(scenes, timeline, all_assets, narr, subtitles):
    for pattern in ("clip_*.mp4", "card_*.jpg", "visual.mp4", "raw.mp4", "mixed.mp4", "final.mp4", "video.txt"):
        for path in WORK.glob(pattern):
            path.unlink(missing_ok=True)
    clips, card_no, global_no = [], 0, 0
    for si, scene in enumerate(scenes):
        assets = list(all_assets[si]) or [{"type": "card", "path": None, "spec": scene["visual_cuts"][0]}]
        while len(assets) < math.ceil(timeline[si]["duration"] / MAX_CUT):
            assets.append({"type": "card", "path": None, "spec": scene["visual_cuts"][len(assets) % len(scene["visual_cuts"])]})
        seconds_list = cut_durations(timeline[si]["duration"], len(assets))
        for ci, asset in enumerate(assets):
            output = WORK / "clip_{:02d}_{:03d}.mp4".format(si, ci)
            seconds = seconds_list[ci]
            try:
                if asset["type"] == "video" and asset.get("path") and duration(asset["path"]) >= seconds:
                    video_clip(asset["path"], output, seconds, global_no)
                elif asset["type"] == "image" and asset.get("path"):
                    image_clip(asset["path"], output, seconds, global_no)
                else:
                    image_clip(create_card(scene, asset["spec"], card_no), output, seconds, global_no)
                    card_no += 1
            except Exception as error:
                log.warning("カット生成失敗。カードへ切替: %s", error)
                image_clip(create_card(scene, asset["spec"], card_no), output, seconds, global_no)
                card_no += 1
            clips.append(output)
            global_no += 1
    listing = WORK / "video.txt"
    listing.write_text("".join("file '{}'\n".format(str(p.resolve()).replace("'", "'\\''")) for p in clips), encoding="utf-8")
    visual, raw, mixed, final = WORK / "visual.mp4", WORK / "raw.mp4", WORK / "mixed.mp4", WORK / "final.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-an", "-c:v", "copy", str(visual)])
    run(["ffmpeg", "-y", "-i", str(visual), "-i", str(narr), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(raw)])
    if BGM.exists():
        filt = "[1:a]volume=0.05,atrim=0:{:.3f}[music];[0:a][music]amix=inputs=2:duration=first:dropout_transition=2,alimiter=limit=0.95[audio]".format(duration(narr))
        run(["ffmpeg", "-y", "-i", str(raw), "-stream_loop", "-1", "-i", str(BGM), "-filter_complex", filt, "-map", "0:v:0", "-map", "[audio]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(mixed)])
    else:
        shutil.copy2(raw, mixed)
    sub = str(subtitles.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    run(["ffmpeg", "-y", "-i", str(mixed), "-vf", "ass='{}'".format(sub), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(final)])
    return final


def create_thumbnail(title, scene, assets):
    source = next((a.get("path") for a in assets[0] if a.get("type") == "image" and a.get("path")), None) if assets else None
    image = Image.new("RGB", (W, H), (15, 23, 38))
    if source and Path(source).exists():
        with Image.open(source) as src:
            src = src.convert("RGB")
            ratio = max(W / src.width, H / src.height)
            src = src.resize((int(src.width * ratio), int(src.height * ratio)), Image.Resampling.LANCZOS)
            image.paste(src.crop(((src.width - W) // 2, (src.height - H) // 2, (src.width + W) // 2, (src.height + H) // 2)))
    image = ImageEnhance.Brightness(image).enhance(0.55)
    draw = ImageDraw.Draw(image)
    text = wrap(clean(title) or scene["heading"], 11)
    title_font = fit_font(draw, text, 76, 52, 1080, 430, 12)
    draw.multiline_text((65, 150), text, font=title_font, fill=(255, 231, 76), stroke_width=8, stroke_fill="black", spacing=12)
    output = WORK / "thumbnail.jpg"
    image.save(output, quality=95, optimize=True)
    return output


def add_credits(description, credits):
    if not credits:
        return description
    lines, seen = ["", "", "素材提供: Pixabay", "https://pixabay.com/"], set()
    for credit in credits:
        key = (credit["media_type"], credit["id"])
        if key in seen:
            continue
        seen.add(key)
        if clean(credit.get("creator_name", "")):
            lines.append("{} by {} on Pixabay".format(credit["media_type"].title(), clean(credit["creator_name"])))
        if credit.get("page_url"):
            lines.append(credit["page_url"])
    return str(description or "").rstrip() + "\n".join(lines)


def youtube_credentials():
    value = Credentials.from_authorized_user_info(secret("TOKEN_JSON"))
    if value.expired and value.refresh_token:
        value.refresh(Request())
    return value


def upload_video(video, thumbnail, data):
    if DRY or not UPLOAD:
        log.info("YouTube投稿をスキップします。")
        return None
    if not MIN_SEC <= duration(video) <= MAX_SEC:
        raise RuntimeError("8〜10分外のため投稿中止")
    youtube = build("youtube", "v3", credentials=youtube_credentials(), cache_discovery=False)
    body = {
        "snippet": {"title": str(data.get("title", "自動生成動画"))[:100], "description": str(data.get("description", ""))[:5000], "tags": data.get("tags", [])[:20], "categoryId": "27", "defaultLanguage": "ja"},
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True},
    }
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True), notifySubscribers=False)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("アップロード %d%%", int(status.progress() * 100))
    video_id = str(response["id"])
    try:
        youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg")).execute()
    except Exception as error:
        log.warning("サムネイル設定失敗: %s", error)
    return video_id


def validate_environment():
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(command + "がありません。")
    font_path()
    if not PIXABAY_KEY:
        raise RuntimeError("PIXABAY_API_KEYが未設定です。")
    if not os.getenv("GROQ_API_KEY", "").strip():
        raise RuntimeError("GROQ_API_KEYが未設定です。")


def main():
    validate_environment()
    theme = os.getenv("INPUT_VIDEO_TITLE", "").strip()
    genre = os.getenv("INPUT_VIDEO_GENRE", "自動判定").strip()
    tone = os.getenv("INPUT_VIDEO_TONE", "わかりやすく、最後まで見たくなる雰囲気").strip()
    if not theme:
        raise RuntimeError("INPUT_VIDEO_TITLEが空です。")
    log.info("=" * 60)
    log.info("PIPELINE_VERSION: %s", VERSION)
    log.info("テーマ: %s", theme)
    log.info("DRY_RUN: %s", DRY)
    log.info("=" * 60)
    
    # 【修正】ここでインスタンスを変数 'llm' に格納する
    llm = LLM()
    
    # 生成関数に 'llm' 変数を渡す
    data = create_script(llm, theme, genre, tone, research(theme))
    
    for attempt in range(3):
        narr, timeline, seconds, subtitles = create_narration(data["scenes"])
        log.info("音声実測時間: %.1f秒", seconds)
        if MIN_SEC <= seconds <= MAX_SEC:
            break
        if attempt < 2:
            # こちらも 'llm' を渡す
            data = adjust_duration(llm, data, seconds, theme)
    for attempt in range(3):
        narr, timeline, seconds, subtitles = create_narration(data["scenes"])
        log.info("音声実測時間: %.1f秒", seconds)
        if MIN_SEC <= seconds <= MAX_SEC:
            break
        if attempt < 2:
            data = adjust_duration(llm, data, seconds, theme)
    if not MIN_SEC <= seconds <= MAX_SEC:
        raise RuntimeError("8〜10分に調整できませんでした: {:.1f}秒".format(seconds))
    (WORK / "script.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    assets, credits = acquire_assets(data["scenes"], timeline)
    data["description"] = add_credits(data.get("description", ""), credits)
    final = create_final(data["scenes"], timeline, assets, narr, subtitles)
    final_seconds = duration(final)
    if not MIN_SEC <= final_seconds <= MAX_SEC:
        raise RuntimeError("最終動画が8〜10分外です: {:.1f}秒".format(final_seconds))
    thumbnail = create_thumbnail(data.get("title", theme), data["scenes"][0], assets)
    video_id = upload_video(final, thumbnail, data)
    youtube_url = "https://youtu.be/" + video_id if video_id else ""
    metadata = dict(data)
    metadata.update({"pipeline_version": VERSION, "theme": theme, "duration_seconds": final_seconds, "youtube_url": youtube_url, "dry_run": DRY, "unique_media_count": len(USED_IDS), "used_media_ids": sorted("{}:{}".format(x[0], x[1]) for x in USED_IDS), "pixabay_credits": credits, "bgm_used": BGM.exists(), "groq_model": GROQ_MODEL})
    (WORK / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("処理完了: %s", youtube_url or final)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("動画生成処理に失敗しました。")
        raise
