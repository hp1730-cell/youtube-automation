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
from urllib.parse import urlparse

import edge_tts
import requests
import trafilatura
from ddgs import DDGS
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image, ImageDraw, ImageEnhance, ImageFont


# ================================================================
# 基本設定
# ================================================================

WORK = Path(
    os.getenv(
        "WORK_DIR",
        "work",
    )
)

WORK.mkdir(
    parents=True,
    exist_ok=True,
)

WIDTH = 1280
HEIGHT = 720
FPS = 30

MIN_SECONDS = 480
TARGET_SECONDS = 540
MAX_SECONDS = 600

MIN_CUT_SECONDS = 5.0
MAX_CUT_SECONDS = 8.0

VOICE = os.getenv(
    "EDGE_VOICE",
    "ja-JP-NanamiNeural",
)

VOICE_RATE = os.getenv(
    "EDGE_VOICE_RATE",
    "+0%",
)

PRIVACY = os.getenv(
    "PRIVACY_STATUS",
    "private",
)

DRY_RUN = (
    os.getenv(
        "DRY_RUN",
        "true",
    ).strip().lower()
    == "true"
)

UPLOAD = (
    os.getenv(
        "UPLOAD_TO_YOUTUBE",
        "true",
    ).strip().lower()
    == "true"
)

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.1-flash-lite",
)

OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openrouter/free",
)

PIXABAY_KEY = os.getenv(
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

SEARCH_CACHE = {}

FONT_CANDIDATES = [
    (
        "/usr/share/fonts/opentype/noto/"
        "NotoSansCJK-Regular.ttc"
    ),
    (
        "/usr/share/fonts/truetype/noto/"
        "NotoSansCJK-Regular.ttc"
    ),
]

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s "
        "%(levelname)s "
        "%(message)s"
    ),
)

log = logging.getLogger(
    "youtube-auto"
)


# ================================================================
# 共通処理
# ================================================================

def run_command(
    command,
    timeout=1800,
):
    log.info(
        "コマンド実行: %s",
        command[0],
    )

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
        "fonts-noto-cjkを導入してください。"
    )


def check_runtime():
    for command in (
        "ffmpeg",
        "ffprobe",
    ):
        if not shutil.which(command):
            raise RuntimeError(
                "{}がありません。".format(
                    command
                )
            )

    get_font_path()

    if not PIXABAY_KEY:
        raise RuntimeError(
            "PIXABAY_API_KEYが未設定です。"
        )


def clean_text(
    text,
):
    text = html.unescape(
        str(text)
    )

    text = re.sub(
        r"https?://\S+",
        "",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def parse_json_response(
    response_text,
):
    cleaned_text = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        response_text.strip(),
        flags=(
            re.IGNORECASE
            | re.DOTALL
        ),
    )

    try:
        return json.loads(
            cleaned_text
        )

    except json.JSONDecodeError:
        json_match = re.search(
            r"\{.*\}",
            cleaned_text,
            re.DOTALL,
        )

        if not json_match:
            raise RuntimeError(
                "LLM応答にJSONがありません。"
            )

        try:
            return json.loads(
                json_match.group(0)
            )

        except json.JSONDecodeError as error:
            raise RuntimeError(
                "LLMのJSONを解析できません。"
            ) from error


def get_json_secret(
    name,
):
    raw_value = os.getenv(
        name,
        "",
    ).strip()

    if not raw_value:
        raise RuntimeError(
            "{}が未設定です。".format(
                name
            )
        )

    try:
        return json.loads(
            raw_value
        )

    except json.JSONDecodeError as error:
        raise RuntimeError(
            "{}のJSON形式が不正です。".format(
                name
            )
        ) from error


def probe_duration(
    media_path,
):
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

    return float(
        result.stdout.strip()
    )


def ass_time(
    seconds,
):
    total_centiseconds = max(
        0,
        int(
            round(
                seconds * 100
            )
        ),
    )

    hours, total_centiseconds = divmod(
        total_centiseconds,
        360000,
    )

    minutes, total_centiseconds = divmod(
        total_centiseconds,
        6000,
    )

    seconds_value, centiseconds = divmod(
        total_centiseconds,
        100,
    )

    return (
        "{}:{:02}:{:02}.{:02}"
    ).format(
        hours,
        minutes,
        seconds_value,
        centiseconds,
    )


def escape_ass_text(
    text,
):
    return (
        str(text)
        .replace(
            "\\",
            r"\\",
        )
        .replace(
            "{",
            r"\{",
        )
        .replace(
            "}",
            r"\}",
        )
    )


def extract_search_tokens(
    text,
):
    ignored_words = {
        "the",
        "and",
        "with",
        "from",
        "into",
        "inside",
        "outside",
        "close",
        "view",
        "footage",
        "video",
        "scene",
        "real",
        "documentary",
        "showing",
        "during",
        "while",
        "near",
        "over",
        "under",
    }

    words = re.findall(
        r"[a-z0-9]+",
        str(text).lower(),
    )

    return {
        word
        for word in words
        if (
            len(word) >= 3
            and word not in ignored_words
        )
    }


# ================================================================
# LLM処理
# ================================================================

class LLMClient:

    def __init__(self):
        self.openrouter_api_key = os.getenv(
            "OPENROUTER_API_KEY",
            "",
        ).strip()

        self.gemini_api_key = os.getenv(
            "GEMINI_API_KEY",
            "",
        ).strip()

        if (
            not self.openrouter_api_key
            and not self.gemini_api_key
        ):
            raise RuntimeError(
                "LLMのAPIキーがありません。"
            )

    def post_with_retry(
        self,
        url,
        headers,
        payload,
        max_attempts=5,
    ):
        last_error = None

        retryable_statuses = {
            408,
            429,
            500,
            502,
            503,
            504,
        }

        for attempt in range(
            max_attempts
        ):
            wait_seconds = (
                min(
                    90,
                    5 * (2 ** attempt),
                )
                + random.uniform(
                    0,
                    3,
                )
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

                if (
                    response.status_code
                    not in retryable_statuses
                ):
                    raise last_error

                retry_after = response.headers.get(
                    "Retry-After"
                )

                if retry_after:
                    try:
                        wait_seconds = float(
                            retry_after
                        )

                    except ValueError:
                        pass

            except requests.RequestException as error:
                last_error = error

            log.warning(
                "API再試行まで%.1f秒",
                wait_seconds,
            )

            time.sleep(
                wait_seconds
            )

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

        if self.openrouter_api_key:
            try:
                result = self.post_with_retry(
                    (
                        "https://openrouter.ai/api/v1/"
                        "chat/completions"
                    ),
                    {
                        "Authorization": (
                            "Bearer {}".format(
                                self.openrouter_api_key
                            )
                        ),
                        "Content-Type": (
                            "application/json"
                        ),
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
                error_message = (
                    "OpenRouter失敗: {}".format(
                        error
                    )
                )

                errors.append(
                    error_message
                )

                log.warning(
                    error_message
                )

        if self.gemini_api_key:
            try:
                result = self.post_with_retry(
                    (
                        "https://generativelanguage.googleapis.com/"
                        "v1beta/models/{}:"
                        "generateContent"
                        "?key={}"
                    ).format(
                        GEMINI_MODEL,
                        self.gemini_api_key,
                    ),
                    {
                        "Content-Type": (
                            "application/json"
                        )
                    },
                    {
                        "contents": [
                            {
                                "parts": [
                                    {
                                        "text": prompt,
                                    }
                                ]
                            }
                        ],
                        "generationConfig": {
                            "temperature": temperature,
                            "maxOutputTokens": (
                                max_tokens
                            ),
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
                error_message = (
                    "Gemini失敗: {}".format(
                        error
                    )
                )

                errors.append(
                    error_message
                )

                log.warning(
                    error_message
                )

        raise RuntimeError(
            "すべてのLLM処理に失敗しました。\n"
            + "\n".join(
                errors
            )
        )


# ================================================================
# Web調査
# ================================================================

def research_theme(
    theme,
):
    search_results = []
    seen_urls = set()

    search_queries = [
        theme,
        "{} 仕組み 理由".format(
            theme
        ),
        "{} 意外な事実".format(
            theme
        ),
    ]

    for search_query in search_queries:
        log.info(
            "Web検索: %s",
            search_query,
        )

        try:
            results = DDGS(
                timeout=15
            ).text(
                search_query,
                region="jp-jp",
                max_results=5,
            )

            for result in results:
                result_url = str(
                    result.get(
                        "href",
                        "",
                    )
                )

                if (
                    result_url
                    and result_url not in seen_urls
                ):
                    seen_urls.add(
                        result_url
                    )

                    search_results.append(
                        result
                    )

        except Exception as error:
            log.warning(
                "Web検索失敗: %s",
                error,
            )

    sources = []

    for result in search_results[:10\]:
        result_url = str(
            result.get(
                "href",
                "",
            )
        )

        try:
            downloaded_page = (
                trafilatura.fetch_url(
                    result_url
                )
            )

            if downloaded_page:
                extracted_text = (
                    trafilatura.extract(
                        downloaded_page,
                        favor_precision=True,
                    )
                    or ""
                )
            else:
                extracted_text = ""

        except Exception:
            extracted_text = ""

        page_text = clean_text(
            extracted_text
            or result.get(
                "body",
                "",
            )
        )

        if len(page_text) >= 200:
            sources.append(
                {
                    "title": str(
                        result.get(
                            "title",
                            "",
                        )
                    ),
                    "url": result_url,
                    "text": page_text[:5000],
                }
            )

        if len(sources) >= 5:
            break

    research_path = (
        WORK
        / "research.json"
    )

    research_path.write_text(
        json.dumps(
            sources,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return sources


# ================================================================
# 台本生成
# ================================================================

def validate_script(
    script_data,
    theme,
):
    scenes = script_data.get(
        "scenes",
        [],
    )

    if not isinstance(
        scenes,
        list,
    ):
        raise RuntimeError(
            "scenesが配列ではありません。"
        )

    if not 9 <= len(scenes) <= 11:
        raise RuntimeError(
            "シーン数は9〜11である必要があります。"
        )

    for scene_number, scene in enumerate(
        scenes,
        start=1,
    ):
        scene["heading"] = clean_text(
            scene.get(
                "heading",
                "",
            )
            or "シーン{}".format(
                scene_number
            )
        )

        scene["narration"] = clean_text(
            scene.get(
                "narration",
                "",
            )
        )

        if not scene["narration"\]:
            raise RuntimeError(
                "シーン{}の原稿が空です。".format(
                    scene_number
                )
            )

        visual_cuts = scene.get(
            "visual_cuts",
            [],
        )

        if not isinstance(
            visual_cuts,
            list,
        ):
            visual_cuts = []

        valid_cuts = []

        for visual_cut in visual_cuts:
            if not isinstance(
                visual_cut,
                dict,
            ):
                continue

            search_query = clean_text(
                visual_cut.get(
                    "query",
                    "",
                )
            )[:100]

            if not search_query:
                continue

            card_points = visual_cut.get(
                "card_points",
                [],
            )

            if not isinstance(
                card_points,
                list,
            ):
                card_points = []

            valid_cuts.append(
                {
                    "narration_excerpt": clean_text(
                        visual_cut.get(
                            "narration_excerpt",
                            "",
                        )
                    ),
                    "query": search_query,
                    "fallback_query": (
                        clean_text(
                            visual_cut.get(
                                "fallback_query",
                                "",
                            )
                        )[:100]
                        or (
                            "{} documentary footage"
                        ).format(
                            theme
                        )[:100]
                    ),
                    "card_title": clean_text(
                        visual_cut.get(
                            "card_title",
                            "",
                        )
                    )[:28],
                    "card_points": [
                        clean_text(
                            point
                        )[:44]
                        for point in card_points
                        if clean_text(
                            point
                        )
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
                    ).format(
                        theme
                    )[:100],
                    "fallback_query": (
                        "{} close up"
                    ).format(
                        theme
                    )[:100],
                    "card_title": (
                        scene["heading"][:28]
                    ),
                    "card_points": [],
                }
            ]

        scene["visual_cuts"] = valid_cuts

    script_data["scenes"] = scenes

    return script_data


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
            source_index + 1,
            source["title"],
            source["url"],
            source["text"],
        )
        for source_index, source in enumerate(
            sources
        )
    )

    if not source_text:
        source_text = (
            "資料不足のため、"
            "具体的な数値を断定せず、"
            "慎重に説明してください。"
        )

    prompt = """
テーマ「{}」の8〜10分YouTube動画を設計してください。

ジャンル:
{}

トーン:
{}

目標:
・読み上げ時間は540秒
・480〜600秒を厳守
・日本語原稿は約4700〜5500文字
・9〜11シーン

必須条件:
1. 冒頭15秒は、強い疑問、意外な事実、この動画で分かることの順にする
2. 各シーンの原稿を意味単位へ分ける
3. 各シーンにvisual_cutsを8〜12個作る
4. queryとfallback_queryはnarration_excerptと深く関連させる
5. queryはPixabay向け英語検索句にする
6. 主役、動作、場所、状況を検索句に含める
7. 原稿にない要素を検索句へ追加しない
8. 著名人、企業ロゴ、商品名を追加しない
9. card_titleとcard_pointsに短い日本語要点を入れる
10. 各シーンのheadingを短く明快にする
11. 最終シーンは要点のまとめにする
12. 事実は参考資料の範囲にする
13. 重複した説明を避ける
14. JSON以外を出力しない

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
          "card_points": [
            "",
            ""
          ]
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

    script_data = validate_script(
        llm.ask(
            prompt,
        ),
        theme,
    )

    source_urls = "\n".join(
        "- " + source["url"]
        for source in sources
    )

    description = str(
        script_data.get(
            "description",
            "",
        )
    ).strip()

    if source_urls:
        description += (
            "\n\n参考資料\n"
            + source_urls
        )

    description += (
        "\n\n"
        "※構成、音声、字幕、映像の一部に"
        "AI・自動生成技術を使用しています。"
    )

    script_data["description"] = (
        description
    )

    return script_data


def revise_script_for_duration(
    llm,
    script_data,
    actual_seconds,
    theme,
):
    if actual_seconds < MIN_SECONDS:
        revision_direction = "増やし"
    else:
        revision_direction = "短くし"

    prompt = """
次の動画台本の実測読み上げ時間は{:.1f}秒です。

原稿を{}、再生成後が480〜600秒、
目標540秒になるよう十分に調整してください。

条件:
・事実関係を維持する
・タイトルを維持する
・9〜11シーンを維持する
・JSON構造を維持する
・原稿変更に合わせてvisual_cutsを更新する
・JSON以外を出力しない

台本:
{}
""".format(
        actual_seconds,
        revision_direction,
        json.dumps(
            script_data,
            ensure_ascii=False,
        ),
    )

    revised_data = llm.ask(
        prompt,
        max_tokens=16000,
        temperature=0.25,
    )

    return validate_script(
        revised_data,
        theme,
    )


# ================================================================
# 音声・ASS字幕
# ================================================================

def split_subtitles(
    text,
    min_characters=9,
    max_characters=24,
):
    subtitle_chunks = []

    sentences = re.split(
        r"(?<=[。！？!?])",
        clean_text(
            text
        ),
    )

    for sentence in sentences:
        sentence = sentence.strip()

        if not sentence:
            continue

        while len(sentence) > max_characters:
            cut_candidates = [
                sentence.rfind(
                    separator,
                    min_characters,
                    max_characters + 1,
                )
                for separator in (
                    "、",
                    "，",
                    " ",
                )
            ]

            cut_position = max(
                cut_candidates
            )

            if cut_position < min_characters:
                cut_position = max_characters

                subtitle_chunks.append(
                    sentence[
                        :cut_position
                    ].strip()
                )

                sentence = sentence[
                    cut_position:
                ].strip()

            else:
                subtitle_chunks.append(
                    sentence[
                        :cut_position + 1
                    ].strip()
                )

                sentence = sentence[
                    cut_position + 1:
                ].strip()

        if sentence:
            if (
                subtitle_chunks
                and len(sentence) < min_characters
                and (
                    len(subtitle_chunks[-1])
                    + len(sentence)
                    <= max_characters
                )
            ):
                subtitle_chunks[-1] += sentence

            else:
                subtitle_chunks.append(
                    sentence
                )

    return (
        subtitle_chunks
        or [
            clean_text(
                text
            )
        ]
    )


async def synthesize_scene_audio(
    text,
    output_path,
):
    last_error = None

    for attempt in range(4):
        try:
            communicator = edge_tts.Communicate(
                text,
                VOICE,
                rate=VOICE_RATE,
            )

            await communicator.save(
                str(output_path)
            )

            if (
                output_path.exists()
                and output_path.stat().st_size
                > 1000
            ):
                return

            raise RuntimeError(
                "生成音声が空です。"
            )

        except Exception as error:
            last_error = error

            if attempt < 3:
                await asyncio.sleep(
                    2 ** attempt
                )

    raise RuntimeError(
        "音声生成失敗: {}".format(
            last_error
        )
    )


def create_ass_subtitles(
    subtitle_events,
    scenes,
    scene_timeline,
    output_path,
):
    ass_header = """[Script Info]
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

    output_lines = [
        ass_header
    ]

    for scene_index, scene in enumerate(
        scenes
    ):
        heading_start = (
            scene_timeline[
                scene_index
            ]["start"]
        )

        heading_end = min(
            scene_timeline[
                scene_index
            ]["end"],
            heading_start + 2.2,
        )

        output_lines.append(
            (
                "Dialogue: 1,{},{},"
                "Heading,,0,0,0,,{}\n"
            ).format(
                ass_time(
                    heading_start
                ),
                ass_time(
                    heading_end
                ),
                escape_ass_text(
                    scene["heading"]
                ),
            )
        )

    for subtitle_event in subtitle_events:
        subtitle_text = escape_ass_text(
            subtitle_event["text"]
        )

        if len(subtitle_text) > 17:
            center_position = (
                len(subtitle_text) // 2
            )

            split_position = (
                center_position
            )

            for separator in (
                "、",
                "，",
                " ",
            ):
                found_position = (
                    subtitle_text.rfind(
                        separator,
                        max(
                            7,
                            center_position - 5,
                        ),
                        min(
                            len(subtitle_text) - 6,
                            center_position + 6,
                        ),
                    )
                )

                if found_position >= 0:
                    split_position = (
                        found_position + 1
                    )

                    break

            subtitle_text = (
                subtitle_text[
                    :split_position
                ]
                + r"\N"
                + subtitle_text[
                    split_position:
                ]
            )

        output_lines.append(
            (
                "Dialogue: 0,{},{},"
                "Default,,0,0,0,,{}\n"
            ).format(
                ass_time(
                    subtitle_event["start"]
                ),
                ass_time(
                    subtitle_event["end"]
                ),
                subtitle_text,
            )
        )

    output_path.write_text(
        "".join(
            output_lines
        ),
        encoding="utf-8-sig",
    )


def create_narration(
    scenes,
):
    for old_audio_path in WORK.glob(
        "voice_*.mp3"
    ):
        old_audio_path.unlink(
            missing_ok=True
        )

    async def generate_all_audio():
        for scene_index, scene in enumerate(
            scenes
        ):
            log.info(
                "シーン音声生成 %d/%d",
                scene_index + 1,
                len(scenes),
            )

            output_path = (
                WORK
                / (
                    "voice_{:02d}.mp3"
                ).format(
                    scene_index
                )
            )

            await synthesize_scene_audio(
                scene["narration"],
                output_path,
            )

    asyncio.run(
        generate_all_audio()
    )

    audio_paths = []
    scene_timeline = []
    subtitle_events = []

    timeline_cursor = 0.35

    for scene_index, scene in enumerate(
        scenes
    ):
        audio_path = (
            WORK
            / (
                "voice_{:02d}.mp3"
            ).format(
                scene_index
            )
        )

        scene_duration = probe_duration(
            audio_path
        )

        scene_start = (
            timeline_cursor
        )

        scene_end = (
            scene_start
            + scene_duration
        )

        scene_timeline.append(
            {
                "scene": scene_index,
                "start": scene_start,
                "end": scene_end,
                "duration": scene_duration,
            }
        )

        subtitle_chunks = split_subtitles(
            scene["narration"]
        )

        subtitle_weights = [
            max(
                1,
                len(
                    subtitle_chunk.replace(
                        " ",
                        "",
                    )
                ),
            )
            for subtitle_chunk in subtitle_chunks
        ]

        total_weight = sum(
            subtitle_weights
        )

        subtitle_cursor = (
            scene_start
        )

        for subtitle_index, subtitle_chunk in enumerate(
            subtitle_chunks
        ):
            remaining_chunks = (
                len(subtitle_chunks)
                - subtitle_index
            )

            remaining_time = (
                scene_end
                - subtitle_cursor
            )

            if (
                subtitle_index
                == len(subtitle_chunks) - 1
            ):
                subtitle_end = (
                    scene_end
                )

            else:
                weighted_duration = (
                    scene_duration
                    * subtitle_weights[
                        subtitle_index
                    ]
                    / total_weight
                )

                maximum_duration = max(
                    1.5,
                    remaining_time
                    - 1.5 * (
                        remaining_chunks - 1
                    ),
                )

                display_duration = min(
                    maximum_duration,
                    max(
                        1.5,
                        min(
                            3.8,
                            weighted_duration,
                        ),
                    ),
                )

                subtitle_end = min(
                    scene_end,
                    subtitle_cursor
                    + display_duration,
                )

            subtitle_events.append(
                {
                    "start": subtitle_cursor,
                    "end": subtitle_end,
                    "text": subtitle_chunk,
                }
            )

            subtitle_cursor = (
                subtitle_end
            )

        timeline_cursor = (
            scene_end + 0.18
        )

        audio_paths.append(
            audio_path
        )

    audio_concat_path = (
        WORK
        / "audio_concat.txt"
    )

    audio_concat_lines = []

    for audio_path in audio_paths:
        audio_concat_lines.append(
            "file '{}'\n".format(
                audio_path.resolve()
            )
        )

    audio_concat_path.write_text(
        "".join(
            audio_concat_lines
        ),
        encoding="utf-8",
    )

    narration_path = (
        WORK
        / "narration.m4a"
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(audio_concat_path),
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(narration_path),
        ]
    )

    subtitle_path = (
        WORK
        / "subtitles.ass"
    )

    create_ass_subtitles(
        subtitle_events,
        scenes,
        scene_timeline,
        subtitle_path,
    )

    return (
        narration_path,
        scene_timeline,
        probe_duration(
            narration_path
        ),
        subtitle_path,
    )


# ================================================================
# Pixabay動画検索
# ================================================================

def search_pixabay(
    search_query,
):
    normalized_query = clean_text(
        search_query
    ).lower()[:100]

    if not normalized_query:
        return []

    if normalized_query in SEARCH_CACHE:
        return SEARCH_CACHE[
            normalized_query
        ]

    try:
        response = requests.get(
            (
                "https://pixabay.com/"
                "api/videos/"
            ),
            params={
                "key": PIXABAY_KEY,
                "q": normalized_query,
                "lang": "en",
                "video_type": "film",
                "min_width": 1280,
                "min_height": 720,
                "safesearch": "true",
                "order": "popular",
                "per_page": (
                    PIXABAY_RESULTS
                ),
            },
            timeout=45,
        )

        response.raise_for_status()

        search_results = response.json().get(
            "hits",
            [],
        )

        SEARCH_CACHE[
            normalized_query
        ] = search_results

        return search_results

    except Exception as error:
        log.warning(
            "Pixabay検索失敗: %s",
            error,
        )

        return []


def select_video_file(
    video_data,
):
    candidates = []

    quality_settings = (
        (
            "medium",
            0,
        ),
        (
            "large",
            100,
        ),
        (
            "small",
            10000,
        ),
        (
            "tiny",
            20000,
        ),
    )

    for quality_name, quality_penalty in quality_settings:
        video_file = (
            video_data
            .get("videos", {})
            .get(quality_name, {})
        )

        video_url = str(
            video_file.get(
                "url",
                "",
            )
        )

        width = int(
            video_file.get(
                "width",
                0,
            )
            or 0
        )

        height = int(
            video_file.get(
                "height",
                0,
            )
            or 0
        )

        if not video_url:
            continue

        if width <= height:
            continue

        if width < 640:
            continue

        if height < 360:
            continue

        score = (
            abs(
                (width / height)
                - (16 / 9)
            )
            * 10000
            + quality_penalty
            + (
                0
                if width >= 1280
                else 5000
            )
        )

        candidates.append(
            (
                score,
                video_url,
                width,
                height,
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[0]
    )

    selected = candidates[0]

    return {
        "url": selected[1],
        "width": selected[2],
        "height": selected[3],
    }


def calculate_relevance_score(
    search_query,
    video_data,
):
    query_tokens = extract_search_tokens(
        search_query
    )

    tag_tokens = extract_search_tokens(
        video_data.get(
            "tags",
            "",
        )
    )

    if not query_tokens:
        return 0

    return (
        len(
            query_tokens
            & tag_tokens
        )       / max(
            1,
            min(
                len(query_tokens),
                4,
            ),
        )
    )


def download_unique_video(
    video_data,
    search_query,
    scene_index,
    cut_index,
):
    video_id = int(
        video_data.get(
            "id",
            0,
        )
        or 0
    )

    page_url = str(
        video_data.get(
            "pageURL",
            "",
        )
    )

    selected_file = select_video_file(
        video_data
    )

    if not video_id:
        return None, None

    if not selected_file:
        return None, None

    if video_id in USED_VIDEO_IDS:
        return None, None

    if (
        selected_file["url"]
        in USED_VIDEO_URLS
    ):
        return None, None

    if (
        page_url
        and page_url in USED_PAGE_URLS
    ):
        return None, None

    relevance_score = (
        calculate_relevance_score(
            search_query,
            video_data,
        )
    )

    if relevance_score <= 0:
        return None, None

    output_path = (
        WORK
        / (
            "pixabay_{:02d}_{:03d}.mp4"
        ).format(
            scene_index,
            cut_index,
        )
    )

    try:
        response = requests.get(
            selected_file["url"],
            stream=True,
            timeout=120,
        )

        response.raise_for_status()

        with output_path.open(
            "wb"
        ) as output_file:
            for chunk in response.iter_content(
                chunk_size=1024 * 1024,
            ):
                if chunk:
                    output_file.write(
                        chunk
                    )

        if (
            not output_path.exists()
            or output_path.stat().st_size
            < 100000
        ):
            output_path.unlink(
                missing_ok=True
            )

            return None, None

        if probe_duration(
            output_path
        ) < MIN_CUT_SECONDS:
            output_path.unlink(
                missing_ok=True
            )

            return None, None

        USED_VIDEO_IDS.add(
            video_id
        )

        USED_VIDEO_URLS.add(
            selected_file["url"]
        )

        if page_url:
            USED_PAGE_URLS.add(
                page_url
            )

        credit = {
            "video_id": video_id,
            "page_url": page_url,
            "creator_name": str(
                video_data.get(
                    "user",
                    "",
                )
            ),
            "query": search_query,
            "relevance_score": (
                relevance_score
            ),
        }

        return (
            output_path,
            credit,
        )

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
    scene_timeline,
):
    all_scene_assets = []
    all_credits = []

    for scene_index, scene in enumerate(
        scenes
    ):
        desired_asset_count = max(
            len(
                scene["visual_cuts"]
            ),
            math.ceil(
                scene_timeline[
                    scene_index
                ]["duration"]
                / MAX_CUT_SECONDS
            ),
        )

        specifications = list(
            scene["visual_cuts"]
        )

        while (
            len(specifications)
            < desired_asset_count
        ):
            source_specification = (
                scene["visual_cuts"][
                    len(specifications)
                    % len(
                        scene[
                            "visual_cuts"
                        ]
                    )
                ]
            )

            specifications.append(
                source_specification.copy()
            )

        scene_assets = []

        for cut_index, specification in enumerate(
            specifications
        ):
            selected_asset = None

            for search_query in (
                specification["query"],
                specification[
                    "fallback_query"
                ],
            ):
                ranked_results = sorted(
                    search_pixabay(
                        search_query
                    ),
                    key=lambda video_data: (
                        calculate_relevance_score(
                            search_query,
                            video_data,
                        )
                    ),
                    reverse=True,
                )

                for video_data in ranked_results:
                    video_path, credit = (
                        download_unique_video(
                            video_data,
                            search_query,
                            scene_index,
                            cut_index,
                        )
                    )

                    if video_path:
                        selected_asset = {
                            "type": "video",
                            "path": video_path,
                            "specification": (
                                specification
                            ),
                            "query": search_query,
                        }

                        all_credits.append(
                            credit
                        )

                        break

                if selected_asset:
                    break

            if selected_asset:
                scene_assets.append(
                    selected_asset
                )

            else:
                scene_assets.append(
                    {
                        "type": "card",
                        "path": None,
                        "specification": (
                            specification
                        ),
                        "query": specification[
                            "query"
                        ],
                    }
                )

        all_scene_assets.append(
            scene_assets[
                :desired_asset_count
            ]
        )

        video_count = sum(
            1
            for asset in scene_assets
            if asset["type"] == "video"
        )

        card_count = sum(
            1
            for asset in scene_assets
            if asset["type"] == "card"
        )

        log.info(
            (
                "シーン%d: "
                "動画%d本、補完カード%d枚"
            ),
            scene_index + 1,
            video_count,
            card_count,
        )

    credits_path = (
        WORK
        / "pixabay_credits.json"
    )

    credits_path.write_text(
        json.dumps(
            all_credits,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return (
        all_scene_assets,
        all_credits,
    )


# ================================================================
# 静止画・情報カード
# ================================================================

def download_commons_image(
    search_query,
    image_index,
):
    try:
        response = requests.get(
            (
                "https://commons.wikimedia.org/"
                "w/api.php"
            ),
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": (
                    search_query
                ),
                "gsrnamespace": 6,
                "gsrlimit": 8,
                "prop": "imageinfo",
                "iiprop": "url",
                "iiurlwidth": 1600,
                "format": "json",
                "origin": "*",
            },
            headers={
                "User-Agent": (
                    "YouTubeAutoPipeline/3.0"
                )
            },
            timeout=30,
        )

        response.raise_for_status()

        pages = (
            response.json()
            .get("query", {})
            .get("pages", {})
            .values()
        )

        for page in pages:
            image_info_list = page.get(
                "imageinfo",
                [],
            )

            if not image_info_list:
                continue

            image_url = (
                image_info_list[0]
                .get("thumburl")
                or image_info_list[0]
                .get("url")
            )

            if not image_url:
                continue

            file_suffix = Path(
                urlparse(
                    image_url
                ).path
            ).suffix.lower()

            if file_suffix not in (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
            ):
                file_suffix = ".jpg"

            output_path = (
                WORK
                / (
                    "commons_{:03d}{}"
                ).format(
                    image_index,
                    file_suffix,
                )
            )

            image_response = requests.get(
                image_url,
                timeout=60,
            )

            image_response.raise_for_status()

            output_path.write_bytes(
                image_response.content
            )

            with Image.open(
                output_path
            ) as image:
                if (
                    image.width >= 400
                    and image.height >= 300
                ):
                    return output_path

            output_path.unlink(
                missing_ok=True
            )

    except Exception as error:
        log.warning(
            "Wikimedia画像取得失敗: %s",
            error,
        )

    return None


def wrap_japanese(
    text,
    width,
):
    text = clean_text(
        text
    )

    return "\n".join(
        text[index:index + width]
        for index in range(
            0,
            len(text),
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
        / (
            "card_{:03d}.jpg"
        ).format(
            card_index
        )
    )

    image = Image.new(
        "RGB",
        (
            WIDTH,
            HEIGHT,
        ),
        (
            18,
            27,
            47,
        ),
    )

    draw = ImageDraw.Draw(
        image
    )

    title_font = ImageFont.truetype(
        get_font_path(),
        54,
    )

    point_font = ImageFont.truetype(
        get_font_path(),
        34,
    )

    label_font = ImageFont.truetype(
        get_font_path(),
        23,
    )

    draw.rounded_rectangle(
        (
            55,
            48,
            1225,
            672,
        ),
        radius=30,
        fill=(
            25,
            38,
            66,
        ),
        outline=(
            97,
            177,
            255,
        ),
        width=3,
    )

    draw.text(
        (
            85,
            75,
        ),
        "KEY POINT",
        font=label_font,
        fill=(
            97,
            177,
            255,
        ),
    )

    title_text = (
        specification.get(
            "card_title"
        )
        or scene["heading"]
    )

    draw.multiline_text(
        (
            85,
            125,
        ),
        wrap_japanese(
            title_text,
            18,
        ),
        font=title_font,
        fill=(
            255,
            228,
            92,
        ),
        spacing=12,
        stroke_width=2,
        stroke_fill=(
            0,
            0,
            0,
        ),
    )

    card_points = specification.get(
        "card_points",
        [],
    )

    if not card_points:
        excerpt = (
            specification.get(
                "narration_excerpt"
            )
            or scene["narration"][:70]
        )

        card_points = [
            excerpt
        ]

    y_position = 305

    for card_point in card_points[:3]:
        draw.ellipse(
            (
                92,
                y_position + 8,
                108,
                y_position + 24,
            ),
            fill=(
                97,
                177,
                255,
            ),
        )

        draw.multiline_text(
            (
                130,
                y_position,
            ),
            wrap_japanese(
                card_point,
                26,
            ),
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


# ================================================================
# 動画カット生成
# ================================================================

def create_image_clip(
    image_path,
    output_path,
    clip_duration,
    motion_index,
):
    frame_count = max(
        1,
        int(
            clip_duration
            * FPS
        ),
    )

    if motion_index % 2 == 0:
        zoom_expression = (
            "min(zoom+0.0005,1.10)"
        )

    else:
        zoom_expression = (
            "if(eq(on,1),1.10,"
            "max(zoom-0.0005,1.0))"
        )

    fade_out_start = max(
        0,
        clip_duration - 0.22,
    )

    video_filter = (
        "scale=1600:900:"
        "force_original_aspect_ratio=increase,"
        "crop=1600:900,"
        "zoompan="
        "z='{}':"
        "d={}:"
        "s=1280x720:"
        "fps=30,"
        "fade=t=in:st=0:d=0.22,"
        "fade=t=out:st={:.3f}:d=0.22,"
        "format=yuv420p"
    ).format(
        zoom_expression,
        frame_count,
        fade_out_start,
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-an",
            "-vf",
            video_filter,
            "-t",
            "{:.3f}".format(
                clip_duration
            ),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            str(output_path),
        ]
    )


def create_video_clip(
    source_path,
    output_path,
    clip_duration,
    motion_index,
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

    if maximum_start:
        source_start = (
            maximum_start
            * (
                0.25
                + 0.5
                * (
                    (
                        motion_index
                        * 37
                    )
                    % 100
                )
                / 100.0
            )
        )

    else:
        source_start = 0.0

    fade_out_start = max(
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
    ).format(
        fade_out_start
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "{:.3f}".format(
                source_start
            ),
            "-i",
            str(source_path),
            "-an",
            "-t",
            "{:.3f}".format(
                clip_duration
            ),
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            str(output_path),
        ]
    )


def allocate_clip_durations(
    scene_duration,
    asset_count,
):
    clip_count = max(
        asset_count,
        math.ceil(
            scene_duration
            / MAX_CUT_SECONDS
        ),
    )

    while (
        clip_count > 1
        and scene_duration
        / clip_count
        < MIN_CUT_SECONDS
    ):
        clip_count -= 1

    base_duration = (
        scene_duration
        / clip_count
    )

    clip_durations = [
        base_duration
        for _ in range(
            clip_count
        )
    ]

    clip_durations[-1] += (
        scene_duration
        - sum(
            clip_durations
        )
    )

    return clip_durations


# ================================================================
# BGMと最終動画
# ================================================================

def mix_audio(
    raw_video_path,
    narration_path,
    output_path,
):
    if BGM_PATH.exists():
        narration_duration = probe_duration(
            narration_path
        )

        run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(raw_video_path),
                "-stream_loop",
                "-1",
                "-i",
                str(BGM_PATH),
                "-filter_complex",
                (
                    "[1:a]"
                    "volume=0.055,"
                    "atrim=0:{:.3f}"
                    "[bg];"
                    "[0:a][bg]"
                    "amix="
                    "inputs=2:"
                    "duration=first:"
                    "dropout_transition=2"
                    "[mixed]"
                ).format(
                    narration_duration
                ),
                "-map",
                "0:v",
                "-map",
                "[mixed]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(output_path),
            ]
        )

    else:
        shutil.copy2(
            raw_video_path,
            output_path,
        )


def build_final_video(
    scenes,
    scene_timeline,
    scene_assets,
    narration_path,
    subtitle_path,
):
    clip_paths = []
    card_index = 0

    for scene_index, scene in enumerate(
        scenes
    ):
        scene_duration = (
            scene_timeline[
                scene_index
            ]["duration"]
        )

        available_assets = (
            scene_assets[
                scene_index
            ]
        )

        clip_durations = (
            allocate_clip_durations(
                scene_duration,
                len(
                    available_assets
                ),
            )
        )

        while (
            len(available_assets)
            < len(clip_durations)
        ):
            specification = (
                scene["visual_cuts"][
                    len(available_assets)
                    % len(
                        scene["visual_cuts"]
                    )
                ]
            )

            available_assets.append(
                {
                    "type": "card",
                    "path": None,
                    "specification": (
                        specification
                    ),
                    "query": specification[
                        "query"
                    ],
                }
            )

        for cut_index, clip_duration in enumerate(
            clip_durations
        ):
            asset = available_assets[
                cut_index
            ]

            output_path = (
                WORK
                / (
                    "clip_{:02d}_{:03d}.mp4"
                ).format(
                    scene_index,
                    cut_index,
                )
            )

            if asset["type"] == "video":
                source_duration = probe_duration(
                    asset["path"]
                )

                if (
                    source_duration
                    >= clip_duration
                ):
                    create_video_clip(
                        asset["path"],
                        output_path,
                        clip_duration,
                        cut_index,
                    )

                else:
                    card_path = (
                        create_information_card(
                            scene,
                            asset[
                                "specification"
                            ],
                            card_index,
                        )
                    )

                    card_index += 1

                    create_image_clip(
                        card_path,
                        output_path,
                        clip_duration,
                        cut_index,
                    )

            else:
                specification = asset[
                    "specification"
                ]

                related_image = (
                    download_commons_image(
                        specification[
                            "query"
                        ],
                        card_index,
                    )
                )

                if related_image:
                    create_image_clip(
                        related_image,
                        output_path,
                        clip_duration,
                        cut_index,
                    )

                else:
                    card_path = (
                        create_information_card(
                            scene,
                            specification,
                            card_index,
                        )
                    )

                    create_image_clip(
                        card_path,
                        output_path,
                        clip_duration,
                        cut_index,
                    )

                card_index += 1

            clip_paths.append(
                output_path
            )

    video_concat_path = (
        WORK
        / "video_concat.txt"
    )

    video_concat_lines = []

    for clip_path in clip_paths:
        video_concat_lines.append(
            "file '{}'\n".format(
                clip_path.resolve()
            )
        )

    video_concat_path.write_text(
        "".join(
            video_concat_lines
        ),
        encoding="utf-8",
    )

    video_only_path = (
        WORK
        / "video_only.mp4"
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(video_concat_path),
            "-c:v",
            "copy",
            str(video_only_path),
        ]
    )

    raw_video_path = (
        WORK
        / "raw_video.mp4"
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_only_path),
            "-i",
            str(narration_path),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            str(raw_video_path),
        ]
    )

    mixed_video_path = (
        WORK
        / "mixed_video.mp4"
    )

    mix_audio(
        raw_video_path,
        narration_path,
        mixed_video_path,
    )

    final_video_path = (
        WORK
        / "final.mp4"
    )

    escaped_subtitle_path = str(
        subtitle_path.resolve()
    ).replace(
        "\\",
        "/",
    ).replace(
        ":",
        r"\:",
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(mixed_video_path),
            "-vf",
            "ass='{}'".format(
                escaped_subtitle_path
            ),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(final_video_path),
        ]
    )

    return final_video_path


# ================================================================
# サムネイル・クレジット
# ================================================================

def create_thumbnail(
    title,
    image_path,
):
    output_path = (
        WORK
        / "thumbnail.jpg"
    )

    image = Image.open(
        image_path
    ).convert(
        "RGB"
    )

    resize_ratio = max(
        WIDTH / image.width,
        HEIGHT / image.height,
    )

    image = image.resize(
        (
            int(
                image.width
                * resize_ratio
            ),
            int(
                image.height
                * resize_ratio
            ),
        )
    )

    crop_left = (
        image.width - WIDTH
    ) // 2

    crop_top = (
        image.height - HEIGHT
    ) // 2

    image = image.crop(
        (
            crop_left,
            crop_top,
            crop_left + WIDTH,
            crop_top + HEIGHT,
        )
    )

    image = ImageEnhance.Brightness(
        image
    ).enhance(
        0.46
    )

    image = ImageEnhance.Contrast(
        image
    ).enhance(
        1.28
    )

    draw = ImageDraw.Draw(
        image
    )

    title_font = ImageFont.truetype(
        get_font_path(),
        72,
    )

    title_text = "\n".join(
        str(title)[index:index + 14]
        for index in range(
            0,
            len(str(title)),
            14,
        )
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
        - (
            text_box[2]
            - text_box[0]
        )
    ) / 2

    y_position = (
        HEIGHT
        - (
            text_box[3]
            - text_box[1]
        )
    ) / 2

    draw.multiline_text(
        (
            x_position,
            y_position,
        ),
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


def add_pixabay_credits(
    description,
    credits,
):
    if not credits:
        return description

    credit_lines = [
        "",
        "",
        "映像素材:",
        "Videos provided by Pixabay",
        "https://pixabay.com/",
    ]

    added_video_ids = set()

    for credit in credits:
        video_id = credit.get(
            "video_id"
        )

        if video_id in added_video_ids:
            continue

        added_video_ids.add(
            video_id
        )

        creator_name = credit.get(
            "creator_name",
            "",
        )

        page_url = credit.get(
            "page_url",
            "",
        )

        if creator_name:
            credit_lines.append(
                "Video by {} on Pixabay".format(
                    creator_name
                )
            )

        if page_url:
            credit_lines.append(
                page_url
            )

    return (
        description
        + "\n".join(
            credit_lines
        )
    )


# ================================================================
# YouTube投稿
# ================================================================

def get_youtube_credentials():
    credentials = (
        Credentials
        .from_authorized_user_info(
            get_json_secret(
                "TOKEN_JSON"
            )
        )
    )

    if (
        credentials.expired
        and credentials.refresh_token
    ):
        credentials.refresh(
            Request()
        )

    return credentials


def upload_to_youtube(
    video_path,
    thumbnail_path,
    script_data,
):
    if (
        DRY_RUN
        or not UPLOAD
    ):
        log.info(
            "YouTube投稿をスキップします。"
        )

        return None

    final_duration = probe_duration(
        video_path
    )

    if not (
        MIN_SECONDS
        <= final_duration
        <= MAX_SECONDS
    ):
        raise RuntimeError(
            (
                "最終動画が8〜10分外のため"
                "投稿を中止します: {:.1f}秒"
            ).format(
                final_duration
            )
        )

    youtube = build(
        "youtube",
        "v3",
        credentials=(
            get_youtube_credentials()
        ),
        cache_discovery=False,
    )

    tags = script_data.get(
        "tags",
        [],
    )

    if not isinstance(
        tags,
        list,
    ):
        tags = []

    request_body = {
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
            "tags": [
                str(tag)[:50]
                for tag in tags[:20]
            ],
            "categoryId": "27",
            "defaultLanguage": "ja",
            "defaultAudioLanguage": "ja",
        },
        "status": {
            "privacyStatus": PRIVACY,
            "selfDeclaredMadeForKids": (
                False
            ),
            "containsSyntheticMedia": (
                True
            ),
        },
    }

    upload_request = (
        youtube.videos().insert(
            part="snippet,status",
            body=request_body,
            media_body=MediaFileUpload(
                str(video_path),
                mimetype="video/mp4",
                chunksize=(
                    8 * 1024 * 1024
                ),
                resumable=True,
            ),
            notifySubscribers=False,
        )
    )

    upload_response = None

    while upload_response is None:
        upload_status, upload_response = (
            upload_request.next_chunk()
        )

        if upload_status:
            log.info(
                "アップロード進捗: %d%%",
                int(
                    upload_status.progress()
                    * 100
                ),
            )

    video_id = str(
        upload_response["id"]
    )

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


# ================================================================
# メイン処理
# ================================================================

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
        (
            "わかりやすく、"
            "最後まで見たくなる雰囲気"
        ),
    ).strip()

    if not theme:
        raise RuntimeError(
            "INPUT_VIDEO_TITLEが空です。"
        )

    log.info("=" * 60)
    log.info("高品質動画生成を開始します。")
    log.info("テーマ: %s", theme)
    log.info("ジャンル: %s", genre)
    log.info("トーン: %s", tone)
    log.info("DRY_RUN: %s", DRY_RUN)
    log.info("=" * 60)

    llm = LLMClient()

    sources = research_theme(
        theme
    )

    script_data = create_script(
        llm,
        theme,
        genre,
        tone,
        sources,
    )

    narration_path = None
    scene_timeline = None
    narration_duration = None
    subtitle_path = None

    for adjustment_attempt in range(3):
        (
            narration_path,
            scene_timeline,
            narration_duration,
            subtitle_path,
        ) = create_narration(
            script_data["scenes"]
        )

        log.info(
            "実測ナレーション時間: %.1f秒",
            narration_duration,
        )

        if (
            MIN_SECONDS
            <= narration_duration
            <= MAX_SECONDS
        ):
            break

        if adjustment_attempt < 2:
            script_data = (
                revise_script_for_duration(
                    llm,
                    script_data,
                    narration_duration,
                    theme,
                )
            )

    if not (
        MIN_SECONDS
        <= narration_duration
        <= MAX_SECONDS
    ):
        raise RuntimeError(
            (
                "原稿調整後も8〜10分に"
                "収まりませんでした: {:.1f}秒"
            ).format(
                narration_duration
            )
        )

    script_path = (
        WORK
        / "script.json"
    )

    script_path.write_text(
        json.dumps(
            script_data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    (
        scene_assets,
        pixabay_credits,
    ) = acquire_visual_assets(
        script_data["scenes"],
        scene_timeline,
    )

    script_data["description"] = (
        add_pixabay_credits(
            str(
                script_data.get(
                    "description",
                    "",
                )
            ),
            pixabay_credits,
        )
    )

    final_video_path = (
        build_final_video(
            script_data["scenes"],
            scene_timeline,
            scene_assets,
            narration_path,
            subtitle_path,
        )
    )

    final_duration = probe_duration(
        final_video_path
    )

    if not (
        MIN_SECONDS
        <= final_duration
        <= MAX_SECONDS
    ):
        raise RuntimeError(
            (
                "最終動画が8〜10分外です: "
                "{:.1f}秒"
            ).format(
                final_duration
            )
        )

    first_visual_query = (
        script_data["scenes"][0]
        ["visual_cuts"][0]["query"]
    )

    thumbnail_source = (
        download_commons_image(
            first_visual_query,
            999,
        )
    )

    if thumbnail_source is None:
        first_specification = (
            script_data["scenes"][0]
            ["visual_cuts"][0]
        )

        thumbnail_source = (
            create_information_card(
                script_data["scenes"][0],
                first_specification,
                999,
            )
        )

    thumbnail_path = create_thumbnail(
        script_data.get(
            "title",
            theme,
        ),
        thumbnail_source,
    )

    video_id = upload_to_youtube(
        final_video_path,
        thumbnail_path,
        script_data,
    )

    if video_id:
        youtube_url = (
            "https://youtu.be/{}"
        ).format(
            video_id
        )
    else:
        youtube_url = ""

    metadata = dict(
        script_data
    )

    metadata.update(
        {
            "theme": theme,
            "duration_seconds": (
                final_duration
            ),
            "youtube_url": (
                youtube_url
            ),
            "dry_run": DRY_RUN,
            "unique_pixabay_video_count": (
                len(
                    USED_VIDEO_IDS
                )
            ),
            "used_pixabay_video_ids": sorted(
                USED_VIDEO_IDS
            ),
            "pixabay_credits": (
                pixabay_credits
            ),
            "bgm_used": (
                BGM_PATH.exists()
            ),
            "sources": [
                {
                    "title": source[
                        "title"
                    ],
                    "url": source[
                        "url"
                    ],
                }
                for source in sources
            ],
        }
    )

    metadata_path = (
        WORK
        / "metadata.json"
    )

    metadata_path.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log.info("=" * 60)

    log.info(
        "処理完了: %s",
        youtube_url
        or final_video_path,
    )

    log.info("=" * 60)


if __name__ == "__main__":
    main()
