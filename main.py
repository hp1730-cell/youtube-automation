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
from PIL import Image
from PIL import ImageDraw
from PIL import ImageEnhance
from PIL import ImageFont


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
    """
    外部コマンドを実行する。
    """

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
    """
    日本語フォントのパスを取得する。
    """

    for font_path in FONT_CANDIDATES:
        if Path(font_path).exists():
            return font_path

    raise RuntimeError(
        "日本語フォントがありません。"
        "fonts-noto-cjkを導入してください。"
    )


def check_runtime():
    """
    実行環境とAPIキーを確認する。
    """

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
    """
    HTML、URL、余分な空白を削除する。
    """

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
    """
    LLMの回答をJSONへ変換する。
    """

    cleaned_text = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        response_text.strip(),
        flags=re.IGNORECASE | re.DOTALL,
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
    """
    JSON形式の環境変数を取得する。
    """

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
    """
    FFprobeでメディアの長さを取得する。
    """

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
    """
    秒数をASS字幕の時刻形式へ変換する。
    """

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
    """
    ASS字幕用に文字をエスケープする。
    """

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
    """
    英語検索語から関連度判定用の単語を取得する。
    """

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
    """
    OpenRouterを優先し、
    失敗した場合はGeminiを使用する。
    """

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
        """
        APIを呼び出し、一時エラー時は再試行する。
        """

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
        """
        OpenRouter、Geminiの順に問い合わせる。
        """

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
                                    "あなたは日本語の"
                                    "映像ディレクターです。"
                                    "有効なJSONだけを"
                                    "返してください。"
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
    """
    テーマについてWeb検索し、参考資料を取得する。
    """

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
# 台本生成・検証
# ================================================================

def validate_script(
    script_data,
    theme,
):
    """
    台本JSONを検証し、欠落項目を補う。
    """

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

            narration_excerpt = clean_text(
                visual_cut.get(
                    "narration_excerpt",
                    "",
                )
            )

            search_query = clean_text(
                visual_cut.get(
                    "query",
                    "",
                )
            )[:100]

            fallback_query = clean_text(
                visual_cut.get(
                    "fallback_query",
                    "",
                )
            )[:100]

            if not search_query:
                continue

            if not fallback_query:
                fallback_query = (
                    "{} documentary footage"
                ).format(
                    theme
                )[:100]

            valid_cuts.append(
                {
                    "narration_excerpt": (
                        narration_excerpt
                    ),
                    "query": search_query,
                    "fallback_query": (
                        fallback_query
                    ),
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
    """
    8〜10分を目標とした台本と映像指示を生成する。
    """

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
テーマ「{}」について、8〜10分のYouTube動画を設計してください。

ジャンル:
{}

トーン:
{}

目標:
・読み上げ時間は約540秒
・許容範囲は480〜600秒
・日本語原稿は約4200〜5200文字
・9〜11シーン

映像設計の重要条件:
1. 各シーンの原稿を意味のまとまりに分ける
2. 各シーンにvisual_cutsを6〜10個作る
3. 各visual_cutは、必ず原稿の特定部分と深く関連させる
4. narration_excerptには対応する原稿部分を書く
5. queryにはPixabay向けの英語検索語を書く
6. fallback_queryには別の英語検索語を書く
7. 検索語には主役、動作、場所、状況を含める
8. 原稿にない要素を検索語へ追加しない
9. 著名人、企業ロゴ、商品名を入れない
10. 抽象概念は視覚化できる具体的な場面へ置き換える
11. 後段で1カット5〜8秒として使用する
12. 事実は参考資料で裏付けられる範囲にする
13. 誇張や重複を避ける
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
          "fallback_query": ""
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

    script_data = llm.ask(
        prompt,
        max_tokens=16000,
        temperature=0.35,
    )

    script_data = validate_script(
        script_data,
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
    """
    実測時間に応じて原稿を増減する。
    """

    if actual_seconds < MIN_SECONDS:
        revision_direction = "増やし"
    else:
        revision_direction = "短くし"

    prompt = """
次の動画台本の実測読み上げ時間は{:.1f}秒です。

原稿を{}、再生成後の実測時間が480〜600秒、
目標540秒になるよう調整してください。

条件:
・事実関係を維持する
・タイトルを維持する
・シーン数を9〜11の範囲で維持する
・JSON構造を維持する
・visual_cutsと原稿の深い対応関係を維持する
・原稿の変更に合わせてvisual_cutsも更新する
・必要量を確実に調整する
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
# 字幕・音声
# ================================================================

def split_subtitles(
    text,
    min_characters=10,
    max_characters=26,
):
    """
    読みやすい長さへ字幕を分割する。
    """

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
                chunk = sentence[
                    :cut_position
                ].strip()

                sentence = sentence[
                    cut_position:
                ].strip()

            else:
                chunk = sentence[
                    :cut_position + 1
                ].strip()

                sentence = sentence[
                    cut_position + 1:
                ].strip()

            if chunk:
                subtitle_chunks.append(
                    chunk
                )

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

    if not subtitle_chunks:
        subtitle_chunks = [
            clean_text(
                text
            )
        ]

    return subtitle_chunks


async def synthesize_scene_audio(
    text,
    output_path,
):
    """
    1シーン分の音声を生成する。
    """

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
                "生成された音声が空です。"
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
    output_path,
):
    """
    読みやすいASS字幕を生成する。
    """

    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK JP,44,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,4,1,2,78,78,48,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    output_lines = [
        ass_header
    ]

    for subtitle_event in subtitle_events:
        subtitle_text = escape_ass_text(
            subtitle_event["text"]
        )

        if len(subtitle_text) > 18:
            center_position = (
                len(subtitle_text) // 2
            )

            search_start = max(
                8,
                center_position - 5,
            )

            search_end = min(
                len(subtitle_text) - 7,
                center_position + 6,
            )

            split_position = center_position

            for separator in (
                "、",
                "，",
                " ",
            ):
                nearby_position = (
                    subtitle_text.rfind(
                        separator,
                        search_start,
                        search_end,
                    )
                )

                if nearby_position >= 0:
                    split_position = (
                        nearby_position + 1
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
    """
    シーン単位の音声とASS字幕を生成する。
    """

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

        scene_start = timeline_cursor
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

        subtitle_cursor = scene_start

        for subtitle_index, subtitle_chunk in enumerate(
            subtitle_chunks
        ):
            if (
                subtitle_index
                == len(subtitle_chunks) - 1
            ):
                subtitle_end = scene_end

            else:
                subtitle_duration = (
                    scene_duration
                    * subtitle_weights[
                        subtitle_index
                    ]
                    / total_weight
                )

                subtitle_duration = max(
                    1.4,
                    min(
                        4.0,
                        subtitle_duration,
                    ),
                )

                subtitle_end = min(
                    scene_end,
                    subtitle_cursor
                    + subtitle_duration,
                )

            if (
                subtitle_end
                - subtitle_cursor
                < 1.0
            ):
                subtitle_end = min(
                    scene_end,
                    subtitle_cursor + 1.0,
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
        subtitle_path,
    )

    narration_duration = probe_duration(
        narration_path
    )

    return (
        narration_path,
        scene_timeline,
        narration_duration,
        subtitle_path,
    )


# ================================================================
# Pixabay検索・動画取得
# ================================================================

def search_pixabay(
    search_query,
):
    """
    Pixabay APIで動画を検索する。
    """

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
                "per_page": PIXABAY_RESULTS,
            },
            timeout=45,
        )

        if response.status_code == 429:
            log.warning(
                "Pixabay APIの上限に達しました。"
            )

            return []

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


def select_pixabay_file(
    video_data,
):
    """
    Pixabay動画の適切な画質を選ぶ。
    """

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

        aspect_penalty = abs(
            (width / height)
            - (16 / 9)
        ) * 10000

        if width >= 1280:
            resolution_penalty = 0
        else:
            resolution_penalty = 5000

        score = (
            aspect_penalty
            + quality_penalty
            + resolution_penalty
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
    """
    検索語とPixabayタグの関連度を計算する。
    """

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

    overlap_count = len(
        query_tokens
        & tag_tokens
    )

    return (
        overlap_count
        / max(
            1,
            min(
                len(query_tokens),
                4,
            ),
        )
    )


defownload_pixabay_video(
    video_data,
    search_query,
    scene_index,
    cut_index,
):
    """
    未使用で関連性のあるPixabay動画を取得する。
    """

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

    selected_file = select_pixabay_file(
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
            "pixabay_{:02d}_{:02d}.mp4"
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

        source_duration = probe_duration(
            output_path
        )

        if source_duration < MIN_CUT_SECONDS:
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
    """
    原稿の意味単位ごとに動画素材を取得する。
    """

    all_scene_assets = []
    all_credits = []

    for scene_index, scene in enumerate(
        scenes
    ):
        required_duration = (
            scene_timeline[
                scene_index
            ]["duration"]
        )

        desired_asset_count = max(
            len(
                scene["visual_cuts"]
            ),
            math.ceil(
                required_duration
                / MAX_CUT_SECONDS
            ),
        )

        cut_specifications = list(
            scene["visual_cuts"]
        )

        while (
            len(cut_specifications)
            < desired_asset_count
        ):
            source_cut = (
                scene["visual_cuts"][
                    len(cut_specifications)
                    % len(
                        scene["visual_cuts"]
                    )
                ]
            )

            cut_specifications.append(
                source_cut.copy()
            )

        scene_assets = []

        for cut_index, visual_cut in enumerate(
            cut_specifications
        ):
            selected_asset = None

            search_queries = (
                visual_cut["query"],
                visual_cut["fallback_query"],
            )

            for search_query in search_queries:
                search_results = search_pixabay(
                    search_query
                )

                ranked_results = sorted(
                    search_results,
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
                        download_pixabay_video(
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
                            "query": search_query,
                            "excerpt": visual_cut.get(
                                "narration_excerpt",
                                "",
                            ),
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

            if (
                len(scene_assets)
                >= desired_asset_count
            ):
                break

        all_scene_assets.append(
            scene_assets
        )

        log.info(
            "シーン%d: 関連動画%d本",
            scene_index + 1,
            len(scene_assets),
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
# フォールバック画像
# ================================================================

def download_commons_image(
    search_query,
    image_index,
):
    """
    Wikimedia Commonsから関連画像を取得する。
    """

    try:
        response = requests.get(
            (
                "https://commons.wikimedia.org/"
                "w/api.php"
            ),
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": search_query,
                "gsrnamespace": 6,
                "gsrlimit": 8,
                "prop": "imageinfo",
                "iiprop": "url|extmetadata",
                "iiurlwidth": 1600,
                "format": "json",
                "origin": "*",
            },
            headers={
                "User-Agent": (
                    "YouTubeAutoPipeline/2.0"
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

            image_info = image_info_list[0]

            image_url = (
                image_info.get(
                    "thumburl"
                )
                or image_info.get(
                    "url"
                )
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
                    "fallback_{:03d}{}"
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


def create_generated_fallback(
    heading,
    image_index,
):
    """
    画像がない場合の背景を生成する。
    """

    output_path = (
        WORK
        / (
            "generated_{:03d}.jpg"
        ).format(
            image_index
        )
    )

    image = Image.new(
        "RGB",
        (
            WIDTH,
            HEIGHT,
        ),
        (
            24 + image_index * 9 % 50,
            32,
            72 + image_index * 13 % 90,
        ),
    )

    draw = ImageDraw.Draw(
        image
    )

    title_font = ImageFont.truetype(
        get_font_path(),
        56,
    )

    title_lines = "\n".join(
        str(heading)[index:index + 15]
        for index in range(
            0,
            len(str(heading)),
            15,
        )
    )

    text_box = draw.multiline_textbbox(
        (0, 0),
        title_lines,
        font=title_font,
        spacing=14,
        align="center",
        stroke_width=4,
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
        title_lines,
        font=title_font,
        fill="white",
        stroke_width=5,
        stroke_fill="black",
        spacing=14,
        align="center",
    )

    image.save(
        output_path,
        quality=92,
    )

    return output_path


# ================================================================
# 動画生成
# ================================================================

def create_video_clip(
    source_path,
    output_path,
    clip_duration,
    source_start=0.0,
):
    """
    動画素材から5〜8秒のカットを生成する。
    ループは行わない。
    """

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
            (
                "scale=1280:720:"
                "force_original_aspect_ratio=increase,"
                "crop=1280:720,"
                "fps=30,"
                "format=yuv420p"
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


def create_image_clip(
    image_path,
    output_path,
    clip_duration,
):
    """
    フォールバック画像から動画カットを生成する。
    """

    run_command(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-an",
            "-t",
            "{:.3f}".format(
                clip_duration
            ),
            "-vf",
            (
                "scale=1280:720:"
                "force_original_aspect_ratio=increase,"
                "crop=1280:720,"
                "fps=30,"
                "format=yuv420p"
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


def build_final_video(
    scenes,
    scene_timeline,
    scene_assets,
    narration_path,
    subtitle_path,
):
    """
    映像、音声、ASS字幕を結合する。
    """

    clip_paths = []
    fallback_image_counter = 0

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

        desired_clip_count = max(
            1,
            math.ceil(
                scene_duration
                / MAX_CUT_SECONDS
            ),
        )

        clip_durations = []

        remaining_duration = (
            scene_duration
        )

        for clip_index in range(
            desired_clip_count
        ):
            remaining_clips = (
                desired_clip_count
                - clip_index
            )

            clip_duration = (
                remaining_duration
                / remaining_clips
            )

            clip_duration = min(
                MAX_CUT_SECONDS,
                max(
                    MIN_CUT_SECONDS,
                    clip_duration,
                ),
            )

            if clip_index == desired_clip_count - 1:
                clip_duration = (
                    remaining_duration
                )

            clip_durations.append(
                clip_duration
            )

            remaining_duration -= (
                clip_duration
            )

        if abs(
            sum(clip_durations)
            - scene_duration
        ) > 0.05:
            clip_durations[-1] += (
                scene_duration
                - sum(clip_durations)
            )

        fallback_image_path = None

        if len(available_assets) < len(
            clip_durations
        ):
            first_query = (
                scene["visual_cuts"][0]
                ["query"]
            )

            fallback_image_path = (
                download_commons_image(
                    first_query,
                    fallback_image_counter,
                )
            )

            if fallback_image_path is None:
                fallback_image_path = (
                    create_generated_fallback(
                        scene["heading"],
                        fallback_image_counter,
                    )
                )

            fallback_image_counter += 1

        for clip_index, clip_duration in enumerate(
            clip_durations
        ):
            output_path = (
                WORK
                / (
                    "clip_{:02d}_{:03d}.mp4"
                ).format(
                    scene_index,
                    clip_index,
                )
            )

            if clip_index < len(
                available_assets
            ):
                asset = available_assets[
                    clip_index
                ]

                source_duration = probe_duration(
                    asset["path"]
                )

                usable_duration = min(
                    clip_duration,
                    source_duration,
                )

                create_video_clip(
                    asset["path"],
                    output_path,
                    usable_duration,
                    source_start=0.0,
                )

                if (
                    usable_duration
                    < clip_duration - 0.05
                ):
                    image_output_path = (
                        WORK
                        / (
                            "clip_{:02d}_{:03d}_fill.mp4"
                        ).format(
                            scene_index,
                            clip_index,
                        )
                    )

                    create_image_clip(
                        fallback_image_path,
                        image_output_path,
                        clip_duration
                        - usable_duration,
                    )

                    clip_paths.append(
                        output_path
                    )

                    clip_paths.append(
                        image_output_path
                    )

                else:
                    clip_paths.append(
                        output_path
                    )

            else:
                create_image_clip(
                    fallback_image_path,
                    output_path,
                    clip_duration,
                )

                clip_paths.append(
                    output_path
                )

    if not clip_paths:
        raise RuntimeError(
            "動画カットが生成されませんでした。"
        )

    video_concat_path = (
        WORK
        / "video_concat.txt"
    )

    concat_lines = []

    for clip_path in clip_paths:
        concat_lines.append(
            "file '{}'\n".format(
                clip_path.resolve()
            )
        )

    video_concat_path.write_text(
        "".join(
            concat_lines
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
            str(raw_video_path),
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
# サムネイル
# ================================================================

def create_thumbnail(
    title,
    image_path,
):
    """
    YouTubeサムネイルを生成する。
    """

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
        0.48
    )

    image = ImageEnhance.Contrast(
        image
    ).enhance(
        1.25
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
    """
    Pixabayクレジットを概要欄へ追加する。
    """

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
    """
    TOKEN_JSONから認証情報を作成する。
    """

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
    """
    8〜10分の動画だけをYouTubeへ投稿する。
    """

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
    """
    動画生成パイプラインを実行する。
    """

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

    log.info(
        "参考資料取得数: %d",
        len(sources),
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
            log.info(
                "原稿の長さを再調整します。"
            )

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

    final_video_path = build_final_video(
        script_data["scenes"],
        scene_timeline,
        scene_assets,
        narration_path,
        subtitle_path,
    )

    final_duration = probe_duration(
        final_video_path
    )

    log.info(
        "最終動画時間: %.1f秒",
        final_duration,
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
        thumbnail_source = (
            create_generated_fallback(
                script_data.get(
                    "title",
                    theme,
                ),
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
            "requested_genre": genre,
            "requested_tone": tone,
            "duration_seconds": (
                final_duration
            ),
            "youtube_url": youtube_url,
            "dry_run": DRY_RUN,
            "minimum_duration_seconds": (
                MIN_SECONDS
            ),
            "maximum_duration_seconds": (
                MAX_SECONDS
            ),
            "used_pixabay_video_ids": (
                sorted(
                    USED_VIDEO_IDS
                )
            ),
            "unique_video_count": (
                len(
                    USED_VIDEO_IDS
                )
            ),
            "pixabay_credits": (
                pixabay_credits
            ),
            "sources": [
                {
                    "title": source.get(
                        "title",
                        "",
                    ),
                    "url": source.get(
                        "url",
                        "",
                    ),
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
