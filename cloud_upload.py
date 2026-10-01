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
from ddgs import DDGS
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image, ImageDraw, ImageEnhance, ImageFont


WORK = Path(os.getenv("WORK_DIR", "work"))
WORK.mkdir(parents=True, exist_ok=True)

WIDTH = 1280
HEIGHT = 720
FPS = 30

MIN_SECONDS = 480
TARGET_SECONDS = 540
MAX_SECONDS = 600

MIN_CUT_SECONDS = 5.0
MAX_CUT_SECONDS = 8.0

VOICE = os.getenv("EDGE_VOICE", "ja-JP-NanamiNeural")
VOICE_RATE = os.getenv("EDGE_VOICE_RATE", "+0%")
PRIVACY_STATUS = os.getenv("PRIVACY_STATUS", "private")

DRY_RUN = os.getenv(
    "DRY_RUN",
    "true",
).strip().lower() == "true"

UPLOAD_TO_YOUTUBE = os.getenv(
    "UPLOAD_TO_YOUTUBE",
    "true",
).strip().lower() == "true"

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.1-flash-lite",
)

OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openrouter/free",
)

PIXABAY_API_KEY = os.getenv(
    "PIXABAY_API_KEY",
    "",
).strip()

PIXABAY_RESULTS = int(
    os.getenv(
        "PIXABAY_RESULTS_PER_QUERY",
        "20",
    )
)

BGM_PATH = Path(
    os.getenv(
        "BGM_PATH",
        "assets/background_music.mp3",
    )
)

USED_VIDEO_IDS = set()
USED_VIDEO_URLS = set()
USED_PAGE_URLS = set()
PIXABAY_CACHE = {}

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("youtube-auto")


def run_command(command, timeout=1800):
    log.info("コマンド実行: %s", command[0])

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "{}の実行に失敗しました。\n{}".format(
                command[0],
                result.stderr[-5000:],
            )
        )

    return result


def get_font_path():
    for font_path in FONT_CANDIDATES:
        if Path(font_path).exists():
            return font_path

    raise RuntimeError(
        "日本語フォントがありません。"
        "fonts-noto-cjkをインストールしてください。"
    )


def check_runtime():
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(
                "{}がありません。".format(command)
            )

    get_font_path()

    if not PIXABAY_API_KEY:
        raise RuntimeError(
            "PIXABAY_API_KEYが未設定です。"
        )


def clean_text(text):
    value = html.unescape(str(text))
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def parse_json_response(response_text):
    cleaned = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        str(response_text).strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )

    try:
        return json.loads(cleaned)

    except json.JSONDecodeError:
        match = re.search(
            r"\{.*\}",
            cleaned,
            flags=re.DOTALL,
        )

        if not match:
            raise RuntimeError(
                "LLM応答にJSONがありません。"
            )

        return json.loads(match.group(0))


def get_json_secret(name):
    raw_value = os.getenv(name, "").strip()

    if not raw_value:
        raise RuntimeError(
            "{}が未設定です。".format(name)
        )

    return json.loads(raw_value)


def probe_duration(media_path):
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(media_path),
        ],
        timeout=60,
    )

    return float(result.stdout.strip())


def ass_time(seconds):
    centiseconds = max(
        0,
        int(round(float(seconds) * 100)),
    )

    hours, centiseconds = divmod(
        centiseconds,
        360000,
    )

    minutes, centiseconds = divmod(
        centiseconds,
        6000,
    )

    seconds_value, centiseconds = divmod(
        centiseconds,
        100,
    )

    return "{}:{:02}:{:02}.{:02}".format(
        hours,
        minutes,
        seconds_value,
        centiseconds,
    )


def escape_ass_text(text):
    value = str(text)
    value = value.replace("\\", r"\\")
    value = value.replace("{", r"\{")
    value = value.replace("}", r"\}")
    return value


def search_tokens(text):
    ignored_words = {
        "the", "and", "with", "from", "into",
        "inside", "outside", "close", "view",
        "footage", "video", "scene", "real",
        "documentary", "showing", "during",
        "while", "near", "over", "under",
    }

    words = re.findall(
        r"[a-z0-9]+",
        str(text).lower(),
    )

    return {
        word
        for word in words
        if len(word) >= 3
        and word not in ignored_words
    }


class LLMClient:

    def __init__(self):
        self.openrouter_key = os.getenv(
            "OPENROUTER_API_KEY",
            "",
        ).strip()

        self.gemini_key = os.getenv(
            "GEMINI_API_KEY",
            "",
        ).strip()

        if not self.openrouter_key and not self.gemini_key:
            raise RuntimeError(
                "GEMINI_API_KEYまたは"
                "OPENROUTER_API_KEYが必要です。"
            )

    def post(self, url, headers, payload):
        last_error = None

        for attempt in range(5):
            wait_seconds = (
                min(90, 5 * (2 ** attempt))
                + random.uniform(0, 3)
            )

            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=180,
                )

                if response.status_code < 400:
                    return response.json()

                last_error = RuntimeError(
                    "HTTP {}: {}".format(
                        response.status_code,
                        response.text[:700],
                    )
                )

                if response.status_code not in {
                    408, 429, 500, 502, 503, 504
                }:
                    raise last_error

            except requests.RequestException as error:
                last_error = error

            log.warning(
                "API再試行まで%.1f秒",
                wait_seconds,
            )

            time.sleep(wait_seconds)

        raise RuntimeError(
            "API呼び出し失敗: {}".format(
                last_error
            )
        )

    def ask(
        self,
        prompt,
        max_tokens=16000,
        temperature=0.35,
    ):
        errors = []

        if self.openrouter_key:
            try:
                result = self.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    {
                        "Authorization": (
                            "Bearer " + self.openrouter_key
                        ),
                        "Content-Type": "application/json",
                    },
                    {
                        "model": OPENROUTER_MODEL,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "日本語の映像ディレクターです。"
                                    "有効なJSONだけを返してください。"
                                ),
                            },
                            {
                                "role": "user",
                                "content": prompt,
                            },
                        ],
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                )

                return parse_json_response(
                    result["choices"][0]
                    ["message"]["content"]
                )

            except Exception as error:
                errors.append(
                    "OpenRouter失敗: {}".format(error)
                )

        if self.gemini_key:
            try:
                url = (
                    "https://generativelanguage.googleapis.com/"
                    "v1beta/models/{}:generateContent?key={}"
                ).format(
                    GEMINI_MODEL,
                    self.gemini_key,
                )

                result = self.post(
                    url,
                    {"Content-Type": "application/json"},
                    {
                        "contents": [
                            {
                                "parts": [
                                    {"text": prompt}
                                ]
                            }
                        ],
                        "generationConfig": {
                            "temperature": temperature,
                            "maxOutputTokens": max_tokens,
                            "responseMimeType": (
                                "application/json"
                            ),
                        },
                    },
                )

                return parse_json_response(
                    result["candidates"][0]
                    ["content"]["parts"][0]["text"]
                )

            except Exception as error:
                errors.append(
                    "Gemini失敗: {}".format(error)
                )

        raise RuntimeError(
            "すべてのLLM処理に失敗しました。\n"
            + "\n".join(errors)
        )


def research_theme(theme):
    search_results = []
    seen_urls = set()

    queries = [
        theme,
        "{} 仕組み 理由".format(theme),
        "{} 意外な事実".format(theme),
    ]

    for query in queries:
        log.info("Web検索: %s", query)

        try:
            results = DDGS(
                timeout=15
            ).text(
                query,
                region="jp-jp",
                max_results=5,
            )

            for result in results:
                url = str(
                    result.get("href", "")
                )

                if url and url not in seen_urls:
                    seen_urls.add(url)
                    search_results.append(result)

        except Exception as error:
            log.warning(
                "Web検索失敗: %s",
                error,
            )

    sources = []

    for result_index, result in enumerate(
        search_results
    ):
        if result_index >= 10:
            break

        url = str(result.get("href", ""))

        try:
            page = trafilatura.fetch_url(url)

            if page:
                extracted = (
                    trafilatura.extract(
                        page,
                        favor_precision=True,
                    )
                    or ""
                )
            else:
                extracted = ""

        except Exception:
            extracted = ""

        page_text = clean_text(
            extracted
            or result.get("body", "")
        )

        if len(page_text) >= 200:
            sources.append(
                {
                    "title": str(
                        result.get("title", "")
                    ),
                    "url": url,
                    "text": page_text[:5000],
                }
            )

        if len(sources) >= 5:
            break

    (WORK / "research.json").write_text(
        json.dumps(
            sources,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return sources


def normalize_scene_count(script_data):
    if not isinstance(script_data, dict):
        raise RuntimeError(
            "台本がJSONオブジェクトではありません。"
        )

    scenes = script_data.get("scenes", [])

    if not isinstance(scenes, list):
        raise RuntimeError(
            "scenesが配列ではありません。"
        )

    if len(scenes) < 9:
        raise RuntimeError(
            "シーン数が不足しています: {}".format(
                len(scenes)
            )
        )

    while len(scenes) > 11:
        extra_scene = scenes.pop()
        target_scene = scenes[-1]

        target_scene["narration"] = (
            clean_text(
                target_scene.get("narration", "")
            )
            + " "
            + clean_text(
                extra_scene.get("narration", "")
            )
        ).strip()

        target_cuts = target_scene.get(
            "visual_cuts",
            [],
        )

        extra_cuts = extra_scene.get(
            "visual_cuts",
            [],
        )

        if not isinstance(target_cuts, list):
            target_cuts = []

        if isinstance(extra_cuts, list):
            target_cuts.extend(extra_cuts)

        target_scene["visual_cuts"] = (
            target_cuts
        )

    script_data["scenes"] = scenes
    return script_data


def validate_script(script_data, theme):
    script_data = normalize_scene_count(
        script_data
    )

    scenes = script_data["scenes"]

    for scene_number, scene in enumerate(
        scenes,
        start=1,
    ):
        if not isinstance(scene, dict):
            raise RuntimeError(
                "シーン{}が辞書ではありません。".format(
                    scene_number
                )
            )

        scene["heading"] = clean_text(
            scene.get("heading")
            or "シーン{}".format(scene_number)
        )

        scene["narration"] = clean_text(
            scene.get("narration", "")
        )

        if not scene["narration"\]:
            raise RuntimeError(
                "シーン{}の原稿が空です。".format(
                    scene_number
                )
            )

        raw_cuts = scene.get(
            "visual_cuts",
            [],
        )

        if not isinstance(raw_cuts, list):
            raw_cuts = []

        valid_cuts = []

        for raw_cut in raw_cuts:
            if not isinstance(raw_cut, dict):
                continue

            query = clean_text(
                raw_cut.get("query", "")
            )[:100]

            if not query:
                continue

            fallback_query = clean_text(
                raw_cut.get(
                    "fallback_query",
                    "",
                )
            )[:100]

            if not fallback_query:
                fallback_query = (
                    "{} documentary footage"
                ).format(theme)[:100]

            points = raw_cut.get(
                "card_points",
                [],
            )

            if not isinstance(points, list):
                points = []

            valid_cuts.append(
                {
                    "narration_excerpt": clean_text(
                        raw_cut.get(
                            "narration_excerpt",
                            "",
                        )
                    ),
                    "query": query,
                    "fallback_query": (
                        fallback_query
                    ),
                    "card_title": clean_text(
                        raw_cut.get(
                            "card_title",
                            "",
                        )
                    )[:28],
                    "card_points": [
                        clean_text(point)[:44]
                        for point in points
                        if clean_text(point)
                    ][:3],
                }
            )

        if not valid_cuts:
            valid_cuts = [
                {
                    "narration_excerpt": (
                        scene["narration"][:80]
                    ),
                    "query": (
                        "{} documentary footage"
                    ).format(theme)[:100],
                    "fallback_query": (
                        "{} close up"
                    ).format(theme)[:100],
                    "card_title": (
                        scene["heading"][:28]
                    ),
                    "card_points": [],
                }
            ]

        scene["visual_cuts"] = valid_cuts

    return script_data


def repair_script_structure(
    llm,
    script_data,
):
    prompt = """
次のJSON台本を必ず9〜11シーンへ再構成してください。

・内容と事実関係を維持
・目標時間540秒
・480〜600秒にする
・各シーンにheading、narration、visual_cutsを入れる
・各visual_cutにnarration_excerpt、query、
  fallback_query、card_title、card_pointsを入れる
・JSON以外は禁止

台本:
{}
""".format(
        json.dumps(
            script_data,
            ensure_ascii=False,
        )
    )

    return llm.ask(
        prompt,
        max_tokens=16000,
        temperature=0.25,
    )


def create_script(
    llm,
    theme,
    genre,
    tone,
    sources,
):
    source_text = "\n\n".join(
        (
            "[{}] {}\n"
            "URL: {}\n"
            "{}"
        ).format(
            index + 1,
            source["title"],
            source["url"],
            source["text"],
        )
        for index, source in enumerate(
            sources
        )
    )

    if not source_text:
        source_text = (
            "資料不足のため、断定的な数値を避け、"
            "慎重に説明してください。"
        )

    prompt = """
テーマ「{}」のYouTube動画台本をJSONで作成してください。

ジャンル: {}
トーン: {}

必須条件:
・実測480〜600秒、目標540秒
・約4700〜5500文字
・必ず9〜11シーン
・冒頭15秒に強い疑問と意外性
・各シーンにvisual_cutsを8〜12個
・映像検索語は対応する原稿と深く関連
・queryとfallback_queryは具体的な英語
・主役、動作、場所、状況を含める
・同じ説明を繰り返さない
・最終シーンは要点のまとめ
・JSON以外は禁止

参考資料:
{}

JSON形式:
{{
  "title": "",
  "description": "",
  "genre": "",
  "tags": [""],
  "scenes": [
    {{
      "heading": "",
      "narration": "",
      "visual_cuts": [
        {{
          "narration_excerpt": "",
          "query": "",
          "fallback_query": "",
          "card_title": "",
          "card_points": ["", ""]
        }}
      ]
    }}
  ]
}}
""".format(
        theme,
        genre,
        tone,
        source_text,
    )

    script_data = llm.ask(prompt)

    for repair_attempt in range(3):
        try:
            script_data = validate_script(
                script_data,
                theme,
            )
            break

        except RuntimeError as error:
            if repair_attempt >= 2:
                raise

            log.warning(
                "台本構造を再生成します: %s",
                error,
            )

            script_data = repair_script_structure(
                llm,
                script_data,
            )

    description = str(
        script_data.get("description", "")
    ).strip()

    source_urls = "\n".join(
        "- " + source["url"]
        for source in sources
    )

    if source_urls:
        description += (
            "\n\n参考資料\n"
            + source_urls
        )

    description += (
        "\n\n※AI・自動生成技術を"
        "構成、音声、字幕、映像に使用しています。"
    )

    script_data["description"] = description
    return script_data


def revise_script_duration(
    llm,
    script_data,
    actual_seconds,
    theme,
):
    if actual_seconds < MIN_SECONDS:
        instruction = "原稿を十分に増やす"
    else:
        instruction = "原稿を十分に短くする"

    prompt = """
実測読み上げ時間は{:.1f}秒です。

次の台本について、{}ことで、
実測480〜600秒、目標540秒へ調整してください。

・9〜11シーンを維持
・事実関係を維持
・visual_cutsも原稿に合わせて更新
・JSON以外は禁止

{}
""".format(
        actual_seconds,
        instruction,
        json.dumps(
            script_data,
            ensure_ascii=False,
        ),
    )

    revised = llm.ask(
        prompt,
        max_tokens=16000,
        temperature=0.25,
    )

    try:
        return validate_script(
            revised,
            theme,
        )

    except RuntimeError:
        repaired = repair_script_structure(
            llm,
            revised,
        )

        return validate_script(
            repaired,
            theme,
        )


def split_subtitles(
    text,
    minimum=9,
    maximum=24,
):
    chunks = []

    sentences = re.split(
        r"(?<=[。！？!?])",
        clean_text(text),
    )

    for sentence in sentences:
        sentence = sentence.strip()

        while len(sentence) > maximum:
            positions = [
                sentence.rfind(
                    mark,
                    minimum,
                    maximum + 1,
                )
                for mark in (
                    "、",
                    "，",
                    " ",
                )
            ]

            cut = max(positions)

            if cut < minimum:
                cut = maximum
                chunks.append(
                    sentence[:cut].strip()
                )
                sentence = sentence[cut:].strip()

            else:
                chunks.append(
                    sentence[:cut + 1].strip()
                )
                sentence = sentence[cut + 1:].strip()

        if sentence:
            chunks.append(sentence)

    return chunks or [clean_text(text)]


async def synthesize_audio(
    text,
    output_path,
):
    last_error = None

    for attempt in range(4):
        try:
            await edge_tts.Communicate(
                text,
                VOICE,
                rate=VOICE_RATE,
            ).save(
                str(output_path)
            )

            if (
                output_path.exists()
                and output_path.stat().st_size > 1000
            ):
                return

        except Exception as error:
            last_error = error

        if attempt < 3:
            await asyncio.sleep(2 ** attempt)

    raise RuntimeError(
        "音声生成失敗: {}".format(
            last_error
        )
    )


def create_ass(
    events,
    scenes,
    timeline,
    output_path,
):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK JP,48,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,4,1,2,90,90,58,1
Style: Heading,Noto Sans CJK JP,38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H88000000,-1,0,0,0,100,100,0,0,3,3,1,7,45,45,38,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]

    for index, scene in enumerate(scenes):
        start = timeline[index]["start"]
        end = min(
            timeline[index]["end"],
            start + 2.2,
        )

        lines.append(
            "Dialogue: 1,{},{},Heading,,0,0,0,,{}\n".format(
                ass_time(start),
                ass_time(end),
                escape_ass_text(
                    scene["heading"]
                ),
            )
        )

    for event in events:
        text = escape_ass_text(
            event["text"]
        )

        if len(text) > 17:
            center = len(text) // 2
            text = (
                text[:center]
                + r"\N"
                + text[center:]
            )

        lines.append(
            "Dialogue: 0,{},{},Default,,0,0,0,,{}\n".format(
                ass_time(event["start"]),
                ass_time(event["end"]),
                text,
            )
        )

    output_path.write_text(
        "".join(lines),
        encoding="utf-8-sig",
    )


def create_narration(scenes):
    for old_file in WORK.glob(
        "voice_*.mp3"
    ):
        old_file.unlink(missing_ok=True)

    async def generate_all():
        for index, scene in enumerate(scenes):
            log.info(
                "音声生成 %d/%d",
                index + 1,
                len(scenes),
            )

            await synthesize_audio(
                scene["narration"],
                WORK / "voice_{:02d}.mp3".format(
                    index
                ),
            )

    asyncio.run(generate_all())

    audio_paths = []
    timeline = []
    subtitle_events = []
    cursor = 0.35

    for index, scene in enumerate(scenes):
        audio_path = (
            WORK
            / "voice_{:02d}.mp3".format(index)
        )

        scene_duration = probe_duration(
            audio_path
        )

        start = cursor
        end = start + scene_duration

        timeline.append(
            {
                "scene": index,
                "start": start,
                "end": end,
                "duration": scene_duration,
            }
        )

        chunks = split_subtitles(
            scene["narration"]
        )

        weights = [
            max(1, len(chunk))
            for chunk in chunks
        ]

        total_weight = sum(weights)
        subtitle_cursor = start

        for chunk_index, chunk in enumerate(chunks):
            if chunk_index == len(chunks) - 1:
                chunk_end = end
            else:
                chunk_duration = (
                    scene_duration
                    * weights[chunk_index]
                    / total_weight
                )

                chunk_duration = max(
                    1.5,
                    min(3.8, chunk_duration),
                )

                chunk_end = min(
                    end,
                    subtitle_cursor
                    + chunk_duration,
                )

            subtitle_events.append(
                {
                    "start": subtitle_cursor,
                    "end": chunk_end,
                    "text": chunk,
                }
            )

            subtitle_cursor = chunk_end

        cursor = end + 0.18
        audio_paths.append(audio_path)

    concat_path = WORK / "audio_concat.txt"

    concat_path.write_text(
        "".join(
            "file '{}'\n".format(
                path.resolve()
            )
            for path in audio_paths
        ),
        encoding="utf-8",
    )

    narration_path = WORK / "narration.m4a"

    run_command(
        [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_path),
            "-c:a", "aac",
            "-b:a", "160k",
            str(narration_path),
        ]
    )

    subtitle_path = WORK / "subtitles.ass"

    create_ass(
        subtitle_events,
        scenes,
        timeline,
        subtitle_path,
    )

    return (
        narration_path,
        timeline,
        probe_duration(narration_path),
        subtitle_path,
    )


def search_pixabay(query):
    normalized = clean_text(
        query
    ).lower()[:100]

    if not normalized:
        return []

    if normalized in PIXABAY_CACHE:
        return PIXABAY_CACHE[normalized]

    try:
        response = requests.get(
            "https://pixabay.com/api/videos/",
            params={
                "key": PIXABAY_API_KEY,
                "q": normalized,
                "lang": "en",
                "video_type": "film",
                "min_width": 1280,
                "min_height": 720,
                "safesearch": "true",
                "order": "popular",
                "per_page": PIXABAY_RESULTS,
            },
            timeout=45,
        )

        response.raise_for_status()

        results = response.json().get(
            "hits",
            [],
        )

        PIXABAY_CACHE[normalized] = results
        return results

    except Exception as error:
        log.warning(
            "Pixabay検索失敗: %s",
            error,
        )
        return []


def choose_video_file(video_data):
    candidates = []

    for quality, penalty in (
        ("medium", 0),
        ("large", 100),
        ("small", 10000),
        ("tiny", 20000),
    ):
        file_data = (
            video_data
            .get("videos", {})
            .get(quality, {})
        )

        url = str(file_data.get("url", ""))
        width = int(file_data.get("width", 0) or 0)
        height = int(file_data.get("height", 0) or 0)

        if (
            url
            and width > height
            and width >= 640
            and height >= 360
        ):
            score = (
                abs(width / height - 16 / 9)
                * 10000
                + penalty
                + (0 if width >= 1280 else 5000)
            )

            candidates.append(
                (score, url)
            )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[0]
    )

    return candidates[0][1]


def relevance_score(query, video_data):
    query_words = search_tokens(query)

    tag_words = search_tokens(
        video_data.get("tags", "")
    )

    if not query_words:
        return 0

    return (
        len(query_words & tag_words)
        / max(1, min(len(query_words), 4))
    )


def download_video(
    video_data,
    query,
    scene_index,
    cut_index,
):
    video_id = int(
        video_data.get("id", 0) or 0
    )

    page_url = str(
        video_data.get("pageURL", "")
    )

    video_url = choose_video_file(
        video_data
    )

    if not video_id or not video_url:
        return None, None

    if (
        video_id in USED_VIDEO_IDS
        or video_url in USED_VIDEO_URLS
        or page_url in USED_PAGE_URLS
    ):
        return None, None

    score = relevance_score(
        query,
        video_data,
    )

    if score <= 0:
        return None, None

    output_path = (
        WORK
        / "pixabay_{:02d}_{:03d}.mp4".format(
            scene_index,
            cut_index,
        )
    )

    try:
        response = requests.get(
            video_url,
            stream=True,
            timeout=120,
        )

        response.raise_for_status()

        with output_path.open("wb") as output:
            for chunk in response.iter_content(
                1024 * 1024
            ):
                if chunk:
                    output.write(chunk)

        if (
            output_path.stat().st_size < 100000
            or probe_duration(output_path)
            < MIN_CUT_SECONDS
        ):
            output_path.unlink(
                missing_ok=True
            )
            return None, None

        USED_VIDEO_IDS.add(video_id)
        USED_VIDEO_URLS.add(video_url)

        if page_url:
            USED_PAGE_URLS.add(page_url)

        credit = {
            "video_id": video_id,
            "page_url": page_url,
            "creator_name": str(
                video_data.get("user", "")
            ),
            "query": query,
        }

        return output_path, credit

    except Exception as error:
        log.warning(
            "Pixabay動画取得失敗: %s",
            error,
        )

        output_path.unlink(
            missing_ok=True
        )

        return None, None


def acquire_visual_assets(
    scenes,
    timeline,
):
    all_assets = []
    credits = []

    for scene_index, scene in enumerate(scenes):
        desired_count = max(
            len(scene["visual_cuts"]),
            math.ceil(
                timeline[scene_index]["duration"]
                / MAX_CUT_SECONDS
            ),
        )

        specifications = list(
            scene["visual_cuts"]
        )

        while len(specifications) < desired_count:
            index = (
                len(specifications)
                % len(scene["visual_cuts"])
            )

            specifications.append(
                scene["visual_cuts"][index].copy()
            )

        scene_assets = []

        for cut_index, specification in enumerate(
            specifications
        ):
            selected = None

            for query in (
                pecification["query"],
                specification["fallback_query"],
            ):
                results = sorted(
                    search_pixabay(query),
                    key=lambda video: relevance_score(
                        query,
                        video,
                    ),
                    reverse=True,
                )

                for video in results:
                    path, credit = download_video(
                        video,
                        query,
                        scene_index,
                        cut_index,
                    )

                    if path:
                        selected = {
                            "type": "video",
                            "path": path,
                            "specification": specification,
                        }

                        credits.append(credit)
                        break

                if selected:
                    break

            if selected:
                scene_assets.append(selected)
            else:
                scene_assets.append(
                    {
                        "type": "card",
                        "path": None,
                        "specification": specification,
                    }
                )

        all_assets.append(
            scene_assets[:desired_count]
        )

        log.info(
            "シーン%d: 動画%d本",
            scene_index + 1,
            sum(
                asset["type"] == "video"
                for asset in scene_assets
            ),
        )

    (WORK / "pixabay_credits.json").write_text(
        json.dumps(
            credits,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return all_assets, credits


def wrap_japanese(text, width):
    value = clean_text(text)

    return "\n".join(
        value[index:index + width]
        for index in range(
            0,
            len(value),
            width,
        )
    )


def create_information_card(
    scene,
    specification,
    card_index,
):
    output_path = (
        WORK
        / "card_{:03d}.jpg".format(
            card_index
        )
    )

    image = Image.new(
        "RGB",
        (WIDTH, HEIGHT),
        (18, 27, 47),
    )

    draw = ImageDraw.Draw(image)

    title_font = ImageFont.truetype(
        get_font_path(),
        54,
    )

    point_font = ImageFont.truetype(
        get_font_path(),
        34,
    )

    draw.rounded_rectangle(
        (55, 48, 1225, 672),
        radius=30,
        fill=(25, 38, 66),
        outline=(97, 177, 255),
        width=3,
    )

    title = (
        specification.get("card_title")
        or scene["heading"]
    )

    draw.multiline_text(
        (85, 120),
        wrap_japanese(title, 18),
        font=title_font,
        fill=(255, 228, 92),
        stroke_width=2,
        stroke_fill="black",
        spacing=12,
    )

    points = specification.get(
        "card_points",
        [],
    )

    if not points:
        points = [
            specification.get(
                "narration_excerpt"
            )
            or scene["narration"][:70]
        ]

    y_position = 310

    for point_index, point in enumerate(points):
        if point_index >= 3:
            break

        draw.ellipse(
            (
                92,
                y_position + 8,
                108,
                y_position + 24,
            ),
            fill=(97, 177, 255),
        )

        draw.multiline_text(
            (130, y_position),
            wrap_japanese(point, 26),
            font=point_font,
            fill="white",
            spacing=10,
        )

        y_position += 105

    image.save(
        output_path,
        quality=94,
    )

    return output_path


def create_image_clip(
    image_path,
    output_path,
    clip_duration,
):
    frame_count = max(
        1,
        int(clip_duration * FPS),
    )

    fade_out = max(
        0,
        clip_duration - 0.22,
    )

    video_filter = (
        "scale=1600:900:"
        "force_original_aspect_ratio=increase,"
        "crop=1600:900,"
        "zoompan="
        "z='min(zoom+0.0005,1.10)':"
        "d={}:s=1280x720:fps=30,"
        "fade=t=in:st=0:d=0.22,"
        "fade=t=out:st={:.3f}:d=0.22,"
        "format=yuv420p"
    ).format(
        frame_count,
        fade_out,
    )

    run_command(
        [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", str(image_path),
            "-an",
            "-vf", video_filter,
            "-t", "{:.3f}".format(
                clip_duration
            ),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            str(output_path),
        ]
    )


def create_video_clip(
    source_path,
    output_path,
    clip_duration,
    cut_index,
):
    source_duration = probe_duration(
        source_path
    )

    maximum_start = max(
        0.0,
        source_duration
        - clip_duration
        - 0.1,
    )

    position_factor = (
        (cut_index * 37) % 100
    ) / 100.0

    source_start = (
        maximum_start
        * (0.25 + 0.5 * position_factor)
    )

    fade_out = max(
        0,
        clip_duration - 0.18,
    )

    video_filter = (
        "scale=1280:720:"
        "force_original_aspect_ratio=increase,"
        "crop=1280:720,"
        "fps=30,"
        "fade=t=in:st=0:d=0.18,"
        "fade=t=out:st={:.3f}:d=0.18,"
        "format=yuv420p"
    ).format(fade_out)

    run_command(
        [
            "ffmpeg", "-y",
            "-ss", "{:.3f}".format(
                source_start
            ),
            "-i", str(source_path),
            "-an",
            "-t", "{:.3f}".format(
                clip_duration
            ),
            "-vf", video_filter,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            str(output_path),
        ]
    )


def build_final_video(
    scenes,
    timeline,
    assets,
    narration,
    subtitles,
):
    clip_paths = []
    card_index = 0

    for scene_index, scene in enumerate(scenes):
        scene_duration = (
            timeline[scene_index]["duration"]
        )

        count = max(
            len(assets[scene_index]),
            math.ceil(
                scene_duration
                / MAX_CUT_SECONDS
            ),
        )

        clip_duration = (
            scene_duration / count
        )

        for cut_index in range(count):
            output_path = (
                WORK
                / "clip_{:02d}_{:03d}.mp4".format(
                    scene_index,
                    cut_index,
                )
            )

            if cut_index < len(
                assets[scene_index]
            ):
                asset = assets[scene_index][
                    cut_index
                ]
            else:
                specification = (
                    scene["visual_cuts"][
                        cut_index
                        % len(scene["visual_cuts"])
                    ]
                )

                asset = {
                    "type": "card",
                    "specification": specification,
                }

            if asset["type"] == "video":
                source_duration = probe_duration(
                    asset["path"]
                )

                if source_duration >= clip_duration:
                    create_video_clip(
                        asset["path"],
                        output_path,
                        clip_duration,
                        cut_index,
                    )
                else:
                    card = create_information_card(
                        scene,
                        asset["specification"],
                        card_index,
                    )

                    card_index += 1

                    create_image_clip(
                        card,
                        output_path,
                        clip_duration,
                    )
            else:
                card = create_information_card(
                    scene,
                    asset["specification"],
                    card_index,
                )

                card_index += 1

                create_image_clip(
                    card,
                    output_path,
                    clip_duration,
                )

            clip_paths.append(output_path)

    concat_path = WORK / "video_concat.txt"

    concat_path.write_text(
        "".join(
            "file '{}'\n".format(
                path.resolve()
            )
            for path in clip_paths
        ),
        encoding="utf-8",
    )

    video_only = WORK / "video_only.mp4"

    run_command(
        [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_path),
            "-c:v", "copy",
            str(video_only),
        ]
    )

    raw_video = WORK / "raw_video.mp4"

    run_command(
        [
            "ffmpeg", "-y",
            "-i", str(video_only),
            "-i", str(narration),
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "160k",
            "-shortest",
            str(raw_video),
        ]
    )

    mixed_video = WORK / "mixed_video.mp4"

    if BGM_PATH.exists():
        narration_duration = probe_duration(
            narration
        )

        audio_filter = (
            "[1:a]volume=0.055,"
            "atrim=0:{:.3f}[bg];"
            "[0:a][bg]amix="
            "inputs=2:"
            "duration=first[mixed]"
        ).format(narration_duration)

        run_command(
            [
                "ffmpeg", "-y",
                "-i", str(raw_video),
                "-stream_loop", "-1",
                "-i", str(BGM_PATH),
                "-filter_complex", audio_filter,
                "-map", "0:v",
                "-map", "[mixed]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                str(mixed_video),
            ]
        )
    else:
        shutil.copy2(
            raw_video,
            mixed_video,
        )

    final_video = WORK / "final.mp4"

    escaped_subtitles = str(
        subtitles.resolve()
    ).replace("\\", "/").replace(
        ":",
        r"\:",
    )

    run_command(
        [
            "ffmpeg", "-y",
            "-i", str(mixed_video),
            "-vf",
            "ass='{}'".format(
                escaped_subtitles
            ),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(final_video),
        ]
    )

    return final_video


def create_thumbnail(title, scene):
    specification = (
        scene["visual_cuts"][0]
    )

    source = create_information_card(
        scene,
        specification,
        999,
    )

    output_path = WORK / "thumbnail.jpg"

    image = Image.open(
        source
    ).convert("RGB")

    image = ImageEnhance.Brightness(
        image
    ).enhance(0.7)

    draw = ImageDraw.Draw(image)

    title_font = ImageFont.truetype(
        get_font_path(),
        68,
    )

    title_text = wrap_japanese(
        title,
        14,
    )

    text_box = draw.multiline_textbbox(
        (0, 0),
        title_text,
        font=title_font,
        spacing=14,
        align="center",
        stroke_width=6,
    )

    x_position = (
        WIDTH
        - (text_box[2] - text_box[0])
    ) / 2

    y_position = (
        HEIGHT
        - (text_box[3] - text_box[1])
    ) / 2

    draw.multiline_text(
        (x_position, y_position),
        title_text,
        font=title_font,
        fill="#FFE45C",
        stroke_width=7,
        stroke_fill="black",
        spacing=14,
        align="center",
    )

    image.save(
        output_path,
        quality=95,
    )

    return output_path


def add_credits(description, credits):
    if not credits:
        return description

    lines = [
        "",
        "",
        "映像素材: Videos provided by Pixabay",
        "https://pixabay.com/",
    ]

    for credit in credits:
        if credit.get("creator_name"):
            lines.append(
                "Video by {} on Pixabay".format(
                    credit["creator_name"]
                )
            )

        if credit.get("page_url"):
            lines.append(
                credit["page_url"]
            )

    return description + "\n".join(lines)


def get_youtube_credentials():
    credentials = (
        Credentials
        .from_authorized_user_info(
            get_json_secret("TOKEN_JSON")
        )
    )

    if (
        credentials.expired
        and credentials.refresh_token
    ):
        credentials.refresh(Request())

    return credentials


def upload_to_youtube(
    video_path,
    thumbnail_path,
    script_data,
):
    if DRY_RUN or not UPLOAD_TO_YOUTUBE:
        log.info(
            "YouTube投稿をスキップします。"
        )
        return None

    video_duration = probe_duration(
        video_path
    )

    if not (
        MIN_SECONDS
        <= video_duration
        <= MAX_SECONDS
    ):
        raise RuntimeError(
            "8〜10分外のため投稿中止: {:.1f}秒".format(
                video_duration
            )
        )

    youtube = build(
        "youtube",
        "v3",
        credentials=get_youtube_credentials(),
        cache_discovery=False,
    )

    body = {
        "snippet": {
            "title": str(
                script_data.get(
                    "title",
                    "自動生成動画",
                )
            )[:100],
            "description": str(
                script_data.get(
                    "description",
                    "",
                )
            )[:5000],
            "tags": script_data.get(
                "tags",
                [],
            )[:20],
            "categoryId": "27",
            "defaultLanguage": "ja",
        },
        "status": {
            "privacyStatus": PRIVACY_STATUS,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(
            str(video_path),
            mimetype="video/mp4",
            chunksize=8 * 1024 * 1024,
            resumable=True,
        ),
        notifySubscribers=False,
    )

    response = None

    while response is None:
        status, response = request.next_chunk()

        if status:
            log.info(
                "アップロード %d%%",
                int(status.progress() * 100),
            )

    video_id = str(response["id"])

    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(
                str(thumbnail_path),
                mimetype="image/jpeg",
            ),
        ).execute()

    except Exception as error:
        log.warning(
            "サムネイル設定失敗: %s",
            error,
        )

    return video_id


def main():
    check_runtime()

    theme = os.getenv(
        "INPUT_VIDEO_TITLE",
        "",
    ).strip()

    genre = os.getenv(
        "INPUT_VIDEO_GENRE",
        "自動判定",
    ).strip()

    tone = os.getenv(
        "INPUT_VIDEO_TONE",
        "わかりやすく、最後まで見たくなる雰囲気",
    ).strip()

    if not theme:
        raise RuntimeError(
            "INPUT_VIDEO_TITLEが空です。"
        )

    log.info("=" * 60)
    log.info("高品質動画生成を開始します。")
    log.info("テーマ: %s", theme)
    log.info("DRY_RUN: %s", DRY_RUN)
    log.info("=" * 60)

    llm = LLMClient()
    sources = research_theme(theme)

    script_data = create_script(
        llm,
        theme,
        genre,
        tone,
        sources,
    )

    narration = None
    timeline = None
    narration_duration = 0
    subtitles = None

    for attempt in range(3):
        (
            narration,
            timeline,
            narration_duration,
            subtitles,
        ) = create_narration(
            script_data["scenes"]
        )

        log.info(
            "実測時間: %.1f秒",
            narration_duration,
        )

        if (
            MIN_SECONDS
            <= narration_duration
            <= MAX_SECONDS
        ):
            break

        if attempt < 2:
            script_data = revise_script_duration(
                llm,
                script_data,
                narration_duration,
                theme,
            )

    if not (
        MIN_SECONDS
        <= narration_duration
        <= MAX_SECONDS
    ):
        raise RuntimeError(
            "8〜10分に調整できませんでした。"
        )

    (WORK / "script.json").write_text(
        json.dumps(
            script_data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    assets, credits = acquire_visual_assets(
        script_data["scenes"],
        timeline,
    )

    script_data["description"] = add_credits(
        script_data.get("description", ""),
        credits,
    )

    final_video = build_final_video(
        script_data["scenes"],
        timeline,
        assets,
        narration,
        subtitles,
    )

    final_duration = probe_duration(
        final_video
    )

    if not (
        MIN_SECONDS
        <= final_duration
        <= MAX_SECONDS
    ):
        raise RuntimeError(
            "最終動画が8〜10分外です: {:.1f}秒".format(
                final_duration
            )
        )

    thumbnail = create_thumbnail(
        script_data.get("title", theme),
        script_data["scenes"][0],
    )

    video_id = upload_to_youtube(
        final_video,
        thumbnail,
        script_data,
    )

    youtube_url = (
        "https://youtu.be/{}".format(video_id)
        if video_id
        else ""
    )

    metadata = dict(script_data)

    metadata.update(
        {
            "theme": theme,
            "duration_seconds": final_duration,
            "youtube_url": youtube_url,
            "dry_run": DRY_RUN,
            "unique_video_count": len(
                USED_VIDEO_IDS
            ),
            "used_video_ids": sorted(
                USED_VIDEO_IDS
            ),
            "pixabay_credits": credits,
            "bgm_used": BGM_PATH.exists(),
        }
    )

    (WORK / "metadata.json").write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log.info(
        "処理完了: %s",
        youtube_url or final_video,
    )


if __name__ == "__main__":
    main()
