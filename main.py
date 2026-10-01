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

PRIVACY_STATUS = os.getenv(
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

UPLOAD_TO_YOUTUBE = (
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

PIXABAY_API_KEY = os.getenv(
    "PIXABAY_API_KEY",
    "",
).strip()

PIXABAY_RESULTS_PER_QUERY = int(
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

PIXABAY_SEARCH_CACHE = {}

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
        "fonts-noto-cjkをインストールしてください。"
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

    if not PIXABAY_API_KEY:
        raise RuntimeError(
            "PIXABAY_API_KEYが未設定です。"
        )


def clean_text(
    text,
):
    value = html.unescape(
        str(text)
    )

    value = re.sub(
        r"https?://\S+",
        "",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def parse_json_response(
    response_text,
):
    cleaned_text = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        str(response_text).strip(),
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
            flags=re.DOTALL,
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
    secret_name,
):
    raw_value = os.getenv(
        secret_name,
        "",
    ).strip()

    if not raw_value:
        raise RuntimeError(
            "{}が未設定です。".format(
                secret_name
            )
        )

    try:
        return json.loads(
            raw_value
        )

    except json.JSONDecodeError as error:
        raise RuntimeError(
            "{}のJSON形式が不正です。".format(
                secret_name
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
                float(seconds) * 100
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
    value = str(text)

    value = value.replace(
        "\\",
        r"\\",
    )

    value = value.replace(
        "{",
        r"\{",
    )

    value = value.replace(
        "}",
        r"\}",
    )

    return value


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
# LLM
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
                "GEMINI_API_KEYまたは"
                "OPENROUTER_API_KEYが必要です。"
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

    for result_index, result in enumerate(
        search_results
    ):
        if result_index >= 10:
            break

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
# 台本生成とシーン数自動修正
# ================================================================

def normalize_scene_count(
    script_data,
):
    if not isinstance(
        script_data,
        dict,
    ):
        raise RuntimeError(
            "台本データがJSONオブジェクトではありません。"
        )

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

    if len(scenes) < 9:
        raise RuntimeError(
            "シーン数が不足しています: {}".format(
                len(scenes)
            )
        )

    while len(scenes) > 11:
        extra_scene = scenes.pop()

        target_scene = scenes[-1]

        target_narration = clean_text(
            target_scene.get(
                "narration",
                "",
            )
        )

        extra_narration = clean_text(
            extra_scene.get(
                "narration",
                "",
            )
        )

        target_scene["narration"] = (
            target_narration
            + " "
            + extra_narration
        ).strip()

        target_cuts = target_scene.get(
            "visual_cuts",
            [],
        )

        extra_cuts = extra_scene.get(
            "visual_cuts",
            [],
        )

        if not isinstance(
            target_cuts,
            list,
        ):
            target_cuts = []

        if isinstance(
            extra_cuts,
            list,
        ):
            target_cuts.extend(
                extra_cuts
            )

        target_scene["visual_cuts"] = (
            target_cuts
        )

    script_data["scenes"] = scenes

    return script_data


def validate_script(
    script_data,
    theme,
):
    script_data = normalize_scene_count(
        script_data
    )

    scenes = script_data.get(
        "scenes",
        [],
    )

    for scene_number, scene in enumerate(
        scenes,
        start=1,
    ):
        if not isinstance(
            scene,
            dict,
        ):
            raise RuntimeError(
                "シーン{}がオブジェクトではありません。".format(
                    scene_number
                )
            )

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

        if not scene.get(
            "narration"
        ):
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

            fallback_query = clean_text(
                visual_cut.get(
                    "fallback_query",
                    "",
                )
            )[:100]

            if not fallback_query:
                fallback_query = (
                    "{} documentary footage"
                ).format(
                    theme
                )[:100]

            card_points = visual_cut.get(
                "card_points",
                [],
            )

            if not isinstance(
                card_points,
                list,
            ):
                card_points = []

            cleaned_card_points = []

            for card_point in card_points:
                cleaned_point = clean_text(
                    card_point
                )[:44]

                if cleaned_point:
                    cleaned_card_points.append(
                        cleaned_point
                    )

                if len(
                    cleaned_card_points
                ) >= 3:
                    break

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
                        fallback_query
                    ),
                    "card_title": clean_text(
                        visual_cut.get(
                            "card_title",
                            "",
                        )
                    )[:28],
                    "card_points": (
                        cleaned_card_points
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
                    "card_title": (
                        scene["heading"][:28]
                    ),
                    "card_points": [],
                }
            ]

        scene["visual_cuts"] = valid_cuts

    script_data["scenes"] = scenes

    return script_data


def repair_script_structure(
    llm,
    script_data,
):
    repair_prompt = """
次のJSON台本を必ず9〜11シーンに再構成してください。

条件:
・内容と事実関係を維持する
・タイトルを維持する
・全体の読み上げ時間を480〜600秒にする
・目標は540秒
・各シーンにheadingを入れる
・各シーンにnarrationを入れる
・各シーンにvisual_cutsを入れる
・各visual_cutにnarration_excerptを入れる
・各visual_cutにqueryを入れる
・各visual_cutにfallback_queryを入れる
・各visual_cutにcard_titleを入れる
・各visual_cutにcard_pointsを入れる
・JSON以外を返さない

台本:
{}
""".format(
        json.dumps(
            script_data,
            ensure_ascii=False,
        )
    )

    return llm.ask(
        repair_prompt,
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

時間と構成:
・読み上げ時間の目標は540秒
・480〜600秒を厳守
・日本語原稿は約4700〜5500文字
・必ず9〜11シーンにする

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
10. headingを短く明快にする
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

    script_data = llm.ask(
        prompt,
        max_tokens=16000,
        temperature=0.35,
    )

    last_error = None

    for structure_attempt in range(3):
        try:
            script_data = validate_script(
                script_data,
                theme,
            )

            last_error = None
            break

        except RuntimeError as error:
            last_error = error

            if structure_attempt >= 2:
                raise

            log.warning(
                "台本構造を修正します: %s",
                error,
            )

            script_data = repair_script_structure(
                llm,
                script_data,
            )

    if last_error:
        raise last_error

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
・原稿変更に合わせてvisual_cutsも更新する
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

    try:
        return validate_script(
            revised_data,
            theme,
        )

    except RuntimeError:
        repaired_data = repair_script_structure(
            llm,
            revised_data,
        )

        return validate_script(
            repaired_data,
            theme,
        )


# ================================================================
# 字幕とナレーション
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
            cut_positions = []

            for separator in (
                "、",
                "，",
                " ",
            ):
                cut_positions.append(
                    sentence.rfind(
                        separator,
                        min_characters,
                        max_characters + 1,
                    )
                )

            cut_position = max(
                cut_positions
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
                search_start = max(
                    7,
                    center_position - 5,
                )

                search_end = min(
                    len(subtitle_text) - 6,
                    center_position + 6,
                )

                found_position = (
                    subtitle_text.rfind(
                        separator,
                        search_start,
                        search_end,
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

        subtitle_weights = []

        for subtitle_chunk in subtitle_chunks:
            subtitle_weights.append(
                max(
                    1,
                    len(
                        subtitle_chunk.replace(
                            " ",
                            "",
                        )
                    ),
                )
            )

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
                    - 1.5
                    * (
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

    if normalized_query in PIXABAY_SEARCH_CACHE:
        return PIXABAY_SEARCH_CACHE[
            normalized_query
        ]

    try:
        response = requests.get(
            (
                "https://pixabay.com/"
                "api/videos/"
            ),
            params={
                "key": PIXABAY_API_KEY,
                "q": normalized_query,
                "lang": "en",
                "video_type": "film",
                "min_width": 1280,
                "min_height": 720,
                "safesearch": "true",
                "order": "popular",
                "per_page": (
                    PIXABAY_RESULTS_PER_QUERY
                ),
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

        PIXABAY_SEARCH_CACHE[
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

        video_width = int(
            video_file.get(
                "width",
                0,
            )
            or 0
        )

        video_height = int(
            video_file.get(
                "height",
                0,
            )
            or 0
        )

        if not video_url:
            continue

        if video_width <= video_height:
            continue

        if video_width < 640:
            continue

        if video_height < 360:
            continue

        aspect_penalty = abs(
            (video_width / video_height)
            - (16 / 9)
        ) * 10000

        if video_width >= 1280:
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
                video_width,
                video_height,
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[0]
    )

    selected_candidate = candidates[0]

    return {
        "url": selected_candidate[1],
        "width": selected_candidate[2],
        "height": selected_candidate[3],
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
            source_index = (
                len(specifications)
                % len(
                    scene["visual_cuts"]
                )
            )

            specifications.append(
                scene["visual_cuts"][
                    source_index
                ].copy()
            )

        scene_assets = []

        for cut_index, specification in enumerate(
            specifications
        ):
            selected_asset = None

            search_queries = [
                specification["query"],
                specification[
                    "fallback_query"
                ],
            ]

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

        video_count = 0
        card_count = 0

        for asset in scene_assets:
            if asset["type"] == "video":
                video_count += 1
            else:
                card_count += 1

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
# 情報カード
# ================================================================

def wrap_japanese(
    text,
    width,
):
    value = clean_text(
        text
    )

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

    card_title = (
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
            card_title,
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
        card_points = [
            (
                specification.get(
                    "narration_excerpt"
                )
                or scene["narration"][:70]
            )
        ]

    y_position = 305

    point_count = 0

    for card_point in card_points:
        if point_count >= 3:
            break

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
        point_count += 1

    image.save(
        output_path,
        quality=94,
    )

    return output_path


# ================================================================
# 動画編集
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

    if maximum_start > 0:
        position_factor = (
            (
                motion_index
                * 37
            )
            % 100
        ) / 100.0

        source_start = (
            maximum_start
            * (
                0.25
                + 0.5
                * position_factor
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

    clip_durations = []

    for _ in range(
        clip_count
    ):
        clip_durations.append(
            base_duration
        )

    clip_durations[-1] += (
        scene_duration
        - sum(
            clip_durations
        )
    )

    return clip_durations


def mix_audio(
    raw_video_path,
    narration_path,
    output_path,
):
    if BGM_PATH.exists():
        narration_duration = probe_duration(
            narration_path
        )

        filter_complex = (
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
                filter_complex,
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

        available_assets = list(
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
            visual_cuts = scene[
                "visual_cuts"
            ]

            specification_index = (
                len(available_assets)
                % len(visual_cuts)
            )

            specification = visual_cuts[
                specification_index
            ]

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

                if source_duration >= clip_duration:
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
# サムネイル
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
        or not UPLOAD_TO_YOUTUBE
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
            "privacyStatus": (
                PRIVACY_STATUS
            ),
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
# メイン
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

    first_scene = (
        script_data["scenes"][0]
    )

    first_specification = (
        first_scene[
            "visual_cuts"
        ][0]
    )

    thumbnail_source = (
        create_information_card(
            first_scene,
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
