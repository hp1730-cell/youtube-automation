#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import html
import json
import logging
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

WORK_DIR = Path(
    os.getenv(
        "WORK_DIR",
        "work",
    )
)

WORK_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

WIDTH = 1280
HEIGHT = 720
FPS = 30

EDGE_VOICE = os.getenv(
    "EDGE_VOICE",
    "ja-JP-NanamiNeural",
)

NARRATION_SPEED = float(
    os.getenv(
        "NARRATION_SPEED",
        "1.10",
    )
)

PRIVACY_STATUS = os.getenv(
    "PRIVACY_STATUS",
    "private",
)

DRY_RUN = (
    os.getenv(
        "DRY_RUN",
        "false",
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
    "gemini-2.5-flash",
)

OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openrouter/free",
)

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
            f"{command[0]}の実行に失敗しました。\n"
            f"{result.stderr[-3000:]}"
        )

    return result


def get_font_path():
    """
    日本語フォントのパスを返す。
    """

    for font_path in FONT_CANDIDATES:
        if Path(font_path).exists():
            return font_path

    raise RuntimeError(
        "日本語フォントがありません。"
        "Workflowでfonts-noto-cjkを"
        "インストールしてください。"
    )


def check_runtime():
    """
    FFmpeg、FFprobe、フォントを確認する。
    """

    for command in (
        "ffmpeg",
        "ffprobe",
    ):
        if not shutil.which(command):
            raise RuntimeError(
                f"{command}がありません。"
            )

    get_font_path()


def get_json_secret(
    secret_name,
):
    """
    JSON形式の環境変数を読み取る。
    """

    raw_value = os.getenv(
        secret_name,
        "",
    ).strip()

    if not raw_value:
        raise RuntimeError(
            f"{secret_name}が未設定です。"
        )

    try:
        return json.loads(
            raw_value
        )

    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{secret_name}のJSON形式が"
            "正しくありません。"
        ) from error


def parse_llm_json(
    response_text,
):
    """
    LLMの回答をJSONへ変換する。
    """

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
            flags=re.DOTALL,
        )

        if not json_match:
            raise RuntimeError(
                "LLMの回答にJSONがありません。"
            )

        try:
            return json.loads(
                json_match.group(0)
            )

        except json.JSONDecodeError as error:
            raise RuntimeError(
                "LLMのJSONを解析できません。"
            ) from error


def clean_text(
    text,
):
    """
    URL、HTML、余分な空白を除去する。
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


def wrap_text(
    text,
    characters_per_line,
):
    """
    指定文字数ごとに改行する。
    """

    return "\n".join(
        text[index:index + characters_per_line]
        for index in range(
            0,
            len(text),
            characters_per_line,
        )
    )


# ================================================================
# LLM
# ================================================================

class FreeLLMClient:
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
                "GEMINI_API_KEYまたは"
                "OPENROUTER_API_KEYが必要です。"
            )

    def post_with_retry(
        self,
        url,
        headers,
        payload,
        max_attempts=6,
    ):
        """
        APIを呼び出し、
        一時エラー時は再試行する。
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
            wait_seconds = min(
                180,
                10 * (2 ** attempt),
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
                    f"HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )

                if (
                    response.status_code
                    not in retryable_statuses
                ):
                    raise last_error

                retry_after = (
                    response.headers.get(
                        "Retry-After",
                        "",
                    )
                )

                if retry_after.isdigit():
                    wait_seconds = float(
                        retry_after
                    )

            except requests.RequestException as error:
                last_error = error

            wait_seconds += random.uniform(
                0,
                3,
            )

            log.warning(
                "API再試行まで%.1f秒待機します。",
                wait_seconds,
            )

            time.sleep(
                wait_seconds
            )

        raise RuntimeError(
            f"API呼び出しに失敗しました: "
            f"{last_error}"
        )

    def ask(
        self,
        prompt,
        max_tokens=11000,
        temperature=0.4,
    ):
        """
        OpenRouter、Geminiの順に問い合わせる。
        """

        errors = []

        if self.openrouter_api_key:
            try:
                log.info(
                    "OpenRouterを使用します。"
                )

                return self.ask_openrouter(
                    prompt,
                    max_tokens,
                    temperature,
                )

            except Exception as error:
                error_message = (
                    f"OpenRouter失敗: {error}"
                )

                errors.append(
                    error_message
                )

                log.warning(
                    error_message
                )

        if self.gemini_api_key:
            try:
                log.info(
                    "Geminiを使用します。"
                )

                return self.ask_gemini(
                    prompt,
                    max_tokens,
                    temperature,
                )

            except Exception as error:
                error_message = (
                    f"Gemini失敗: {error}"
                )

                errors.append(
                    error_message
                )

                log.warning(
                    error_message
                )

        raise RuntimeError(
            "すべてのLLM処理に失敗しました。\n"
            + "\n".join(errors)
        )

    def ask_openrouter(
        self,
        prompt,
        max_tokens,
        temperature,
    ):
        """
        OpenRouter APIを呼び出す。
        """

        response_data = self.post_with_retry(
            (
                "https://openrouter.ai/api/v1/"
                "chat/completions"
            ),
            {
                "Authorization": (
                    f"Bearer "
                    f"{self.openrouter_api_key}"
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
                            "YouTube動画編集者です。"
                            "必ず有効なJSONだけを"
                            "返してください。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )

        response_text = (
            response_data["choices"][0]
            ["message"]["content"]
        )

        return parse_llm_json(
            response_text
        )

    def ask_gemini(
        self,
        prompt,
        max_tokens,
        temperature,
    ):
        """
        Gemini APIを呼び出す。
        """

        url = (
            "https://generativelanguage.googleapis.com/"
            f"v1beta/models/{GEMINI_MODEL}:"
            "generateContent"
            f"?key={self.gemini_api_key}"
        )

        response_data = self.post_with_retry(
            url,
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

        response_text = (
            response_data["candidates"][0]
            ["content"]["parts"][0]["text"]
        )

        return parse_llm_json(
            response_text
        )


# ================================================================
# Web調査
# ================================================================

def research_theme(
    theme,
):
    """
    DDGSで検索し、
    上位ページの本文を取得する。
    """

    search_queries = [
        theme,
        f"{theme} 仕組み 理由",
        f"{theme} 意外な事実",
    ]

    search_results = []
    seen_urls = set()

    for query in search_queries:
        log.info(
            "Web検索: %s",
            query,
        )

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
                    result.get(
                        "href",
                        "",
                    )
                ).strip()

                if not url:
                    continue

                if url in seen_urls:
                    continue

                seen_urls.add(
                    url
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
        url = str(
            result.get(
                "href",
                "",
            )
        )

        title = str(
            result.get(
                "title",
                "",
            )
        )

        snippet = str(
            result.get(
                "body",
                "",
            )
        )

        extracted_text = ""

        try:
            downloaded_page = (
                trafilatura.fetch_url(
                    url
                )
            )

            if downloaded_page:
                extracted_text = (
                    trafilatura.extract(
                        downloaded_page,
                        favor_precision=True,
                        include_links=False,
                        include_images=False,
                    )
                    or ""
                )

        except Exception as error:
            log.warning(
                "本文取得失敗: %s",
                error,
            )

        page_text = clean_text(
            extracted_text
            or snippet
        )

        if len(page_text) < 200:
            continue

        sources.append(
            {
                "title": title,
                "url": url,
                "text": (
                    page_text[:6500]
                ),
            }
        )

        if len(sources) >= 5:
            break

    research_path = (
        WORK_DIR / "research.json"
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
# 台本
# ================================================================

def generate_script(
    llm,
    theme,
    genre,
    tone,
    sources,
):
    """
    動画台本を生成する。
    """

    source_text = "\n\n".join(
        (
            f"[{index + 1}] "
            f"{source['title']}\n"
            f"URL: {source['url']}\n"
            f"{source['text']}"
        )
        for index, source in enumerate(
            sources
        )
    )

    if not source_text:
        source_text = (
            "資料不足のため、"
            "具体的数値を断定せず、"
            "一般知識として慎重に"
            "説明してください。"
        )

    prompt = f"""
テーマ「{theme}」について、YouTube向けの日本語動画台本を作成してください。

ジャンル:
{genre or "自動判定"}

ナレーションの雰囲気:
{tone or "わかりやすく最後まで見たくなる雰囲気"}

条件:
・雑学、科学、歴史、動物、生活知識などテーマに合う形式
・AIドラマに固定しない
・8〜10分を目標にする
・4200〜5200日本語文字を目安にする
・9〜11シーン
・冒頭15秒に疑問か意外性を置く
・資料で確認できる事実だけを断定する
・同じ内容を繰り返さない
・narrationは読み上げ本文だけ
・image_queryはWikimedia Commons向け英語名詞句
・titleは45文字以内
・JSON以外は禁止

参考資料:
{source_text}

出力形式:
{{
  "title": "",
  "description": "",
  "genre": "",
  "tags": [""],
  "scenes": [
    {{
      "heading": "",
      "narration": "",
      "image_query": ""
    }}
  ]
}}
"""

    script_data = llm.ask(
        prompt,
        max_tokens=11000,
        temperature=0.4,
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

    if not 7 <= len(scenes) <= 13:
        raise RuntimeError(
            f"シーン数が不正です: "
            f"{len(scenes)}"
        )

    for scene_index, scene in enumerate(
        scenes,
        start=1,
    ):
        narration = clean_text(
            scene.get(
                "narration",
                "",
            )
        )

        if not narration:
            raise RuntimeError(
                f"シーン{scene_index}の"
                "本文が空です。"
            )

        scene["narration"] = (
            narration
        )

        if not scene.get(
            "heading"
        ):
            scene["heading"] = (
                f"シーン{scene_index}"
            )

        if not scene.get(
            "image_query"
        ):
            scene["image_query"] = (
                theme
            )

    source_urls = "\n".join(
        f"- {source['url']}"
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
            f"{source_urls}"
        )

    description += (
        "\n\n"
        "※構成、音声、字幕、映像の一部に"
        "AI・自動生成技術を使用しています。"
    )

    script_data["description"] = (
        description
    )

    script_data["scenes"] = scenes

    return script_data


def review_script(
    llm,
    script_data,
):
    """
    台本を校閲する。
    """

    prompt = (
        "次の台本の重複、不自然な日本語、"
        "前後矛盾、強すぎる断定を修正してください。"
        "同じJSON構造を維持し、"
        "完成版JSONだけを返してください。"
        "新しい具体的数値は追加しないでください。\n"
        + json.dumps(
            script_data,
            ensure_ascii=False,
        )
    )

    try:
        reviewed_data = llm.ask(
            prompt,
            max_tokens=12000,
            temperature=0.2,
        )

        if (
            reviewed_data.get(
                "title"
            )
            and reviewed_data.get(
                "scenes"
            )
        ):
            return reviewed_data

    except Exception as error:
        log.warning(
            "校閲を省略します: %s",
            error,
        )

    return script_data


# ================================================================
# 画像
# ================================================================

def download_commons_image(
    query,
    image_index,
):
    """
    Wikimedia Commonsから画像を取得する。
    """

    parameters = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": 6,
        "gsrlimit": 10,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "iiurlwidth": 1600,
        "format": "json",
        "origin": "*",
    }

    headers = {
        "User-Agent": (
            "YouTubeAutoPipeline/1.0"
        )
    }

    try:
        response = requests.get(
            (
                "https://commons.wikimedia.org/"
                "w/api.php"
            ),
            params=parameters,
            headers=headers,
            timeout=30,
        )

        response.raise_for_status()

        pages = list(
            response.json().get(
                "query",
                {},
            ).get(
                "pages",
                {},
            ).values()
        )

        for page in pages:
            image_info = (
                page.get(
                    "imageinfo"
                )
                or [{}]
            )[0]

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

            if file_suffix not in {
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
            }:
                file_suffix = ".jpg"

            output_path = (
                WORK_DIR
                / (
                    f"scene_"
                    f"{image_index:02d}"
                    f"{file_suffix}"
                )
            )

            image_response = requests.get(
                image_url,
                headers=headers,
                timeout=60,
            )

            image_response.raise_for_status()

            output_path.write_bytes(
                image_response.content
            )

            try:
                with Image.open(
                    output_path
                ) as image:
                    image.verify()

                with Image.open(
                    output_path
                ) as image:
                    if (
                        image.width < 400
                        or image.height < 300
                    ):
                        output_path.unlink(
                            missing_ok=True
                        )

                        continue

            except Exception:
                output_path.unlink(
                    missing_ok=True
                )

                continue

            metadata = image_info.get(
                "extmetadata",
                {},
            )

            artist_html = str(
                metadata.get(
                    "Artist",
                    {},
                ).get(
                    "value",
                    "",
                )
            )

            artist = re.sub(
                r"<[^>]+>",
                "",
                artist_html,
            )

            image_credit = {
                "title": str(
                    page.get(
                        "title",
                        "",
                    )
                ),
                "source": str(
                    image_info.get(
                        "descriptionurl",
                        image_url,
                    )
                ),
                "license": str(
                    metadata.get(
                        "LicenseShortName",
                        {},
                    ).get(
                        "value",
                        "",
                    )
                ),
                "artist": (
                    clean_text(
                        artist
                    )[:200]
                ),
            }

            return (
                output_path,
                image_credit,
            )

    except Exception as error:
        log.warning(
            "画像取得失敗: %s",
            error,
        )

    return None, None


def create_fallback_image(
    heading,
    image_index,
):
    """
    画像がない場合の背景を生成する。
    """

    output_path = (
        WORK_DIR
        / f"fallback_{image_index:02d}.jpg"
    )

    image = Image.new(
        "RGB",
        (
            WIDTH,
            HEIGHT,
        ),
        (
            25 + image_index * 13 % 55,
            35,
            75 + image_index * 19 % 90,
        ),
    )

    draw = ImageDraw.Draw(
        image
    )

    heading_font = ImageFont.truetype(
        get_font_path(),
        56,
    )

    wrapped_heading = wrap_text(
        str(heading),
        16,
    )

    text_box = draw.multiline_textbbox(
        (0, 0),
        wrapped_heading,
        font=heading_font,
        spacing=14,
        align="center",
        stroke_width=3,
    )

    text_width = (
        text_box[2] - text_box[0]
    )

    text_height = (
        text_box[3] - text_box[1]
    )

    x = (
        WIDTH - text_width
    ) / 2

    y = (
        HEIGHT - text_height
    ) / 2

    draw.multiline_text(
        (x, y),
        wrapped_heading,
        font=heading_font,
        fill="white",
        spacing=14,
        align="center",
        stroke_width=4,
        stroke_fill="black",
    )

    image.save(
        output_path,
        quality=92,
    )

    return output_path


def acquire_scene_images(
    scenes,
):
    """
    全シーンの画像を取得する。
    """

    image_paths = []
    image_credits = []

    for image_index, scene in enumerate(
        scenes
    ):
        search_query = str(
            scene.get(
                "image_query",
                scene.get(
                    "heading",
                    "",
                ),
            )
        )

        log.info(
            "画像取得 %d/%d: %s",
            image_index + 1,
            len(scenes),
            search_query,
        )

        (
            image_path,
            image_credit,
        ) = download_commons_image(
            search_query,
            image_index,
        )

        if image_path is None:
            image_path = create_fallback_image(
                scene.get(
                    "heading",
                    "",
                ),
                image_index,
            )

        if image_credit:
            image_credits.append(
                image_credit
            )

        image_paths.append(
            image_path
        )

    return (
        image_paths,
        image_credits,
    )


# ================================================================
# ナレーション
# ================================================================

def split_narration(
    text,
    max_characters=70,
):
    """
    ナレーションを短く分割する。
    """

    clips = []

    sentences = re.split(
        r"(?<=[。！？!?])",
        str(text),
    )

    for sentence in sentences:
        sentence = sentence.strip()

        if not sentence:
            continue

        while (
            len(sentence)
            > max_characters
        ):
            split_position = max(
                sentence.rfind(
                    "、",
                    0,
                    max_characters,
                ),
                sentence.rfind(
                    "，",
                    0,
                    max_characters,
                ),
            )

            if split_position < 20:
                split_position = (
                    max_characters - 1
                )

            clip_text = sentence[
                :split_position + 1
            ].strip()

            sentence = sentence[
                split_position + 1:
            ].strip()

            if clip_text:
                clips.append(
                    clip_text
                )

        if sentence:
            clips.append(
                sentence
            )

    return clips


async def synthesize_audio(
    text,
    output_path,
):
    """
    Edge TTSで音声を生成する。
    """

    speed_percent = int(
        (NARRATION_SPEED - 1)
        * 100
    )

    last_error = None

    for attempt in range(4):
        try:
            communicator = (
                edge_tts.Communicate(
                    text,
                    EDGE_VOICE,
                    rate=(
                        f"{speed_percent:+d}%"
                    ),
                )
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
        f"音声生成に失敗しました: "
        f"{last_error}"
    )


def probe_duration(
    media_path,
):
    """
    FFprobeで長さを取得する。
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


def seconds_to_srt_time(
    seconds,
):
    """
    秒数をSRT形式へ変換する。
    """

    milliseconds = int(
        round(
            seconds * 1000
        )
    )

    hours, milliseconds = divmod(
        milliseconds,
        3600000,
    )

    minutes, milliseconds = divmod(
        milliseconds,
        60000,
    )

    seconds_value, milliseconds = divmod(
        milliseconds,
        1000,
    )

    return (
        f"{hours:02}:"
        f"{minutes:02}:"
        f"{seconds_value:02},"
        f"{milliseconds:03}"
    )


def create_narration(
    scenes,
):
    """
    音声と字幕を生成する。
    """

    clip_items = []

    for scene_index, scene in enumerate(
        scenes
    ):
        narration_text = clean_text(
            scene["narration"]
        )

        for clip_text in split_narration(
            narration_text
        ):
            clip_items.append(
                (
                    scene_index,
                    clip_text,
                )
            )

    if not clip_items:
        raise RuntimeError(
            "読み上げ本文がありません。"
        )

    async def generate_all_audio():
        for clip_index, (
            _,
            clip_text,
        ) in enumerate(clip_items):
            output_path = (
                WORK_DIR
                / (
                    f"voice_"
                    f"{clip_index:03d}.mp3"
                )
            )

            log.info(
                "音声生成 %d/%d",
                clip_index + 1,
                len(clip_items),
            )

            await synthesize_audio(
                clip_text,
                output_path,
            )

    asyncio.run(
        generate_all_audio()
    )

    timeline = []
    audio_paths = []
    cursor = 0.5

    for clip_index, (
        scene_index,
        clip_text,
    ) in enumerate(clip_items):
        audio_path = (
            WORK_DIR
            / (
                f"voice_"
                f"{clip_index:03d}.mp3"
            )
        )

        clip_duration = probe_duration(
            audio_path
        )

        timeline.append(
            {
                "scene": scene_index,
                "text": clip_text,
                "start": cursor,
                "end": (
                    cursor
                    + clip_duration
                ),
            }
        )

        cursor += (
            clip_duration
            + 0.15
        )

        audio_paths.append(
            audio_path
        )

    concat_file = (
        WORK_DIR / "audio_concat.txt"
    )

    concat_file.write_text(
        "".join(
            f"file '{path.resolve()}'\n"
            or path in audio_paths
        ),
        encoding="utf-8",
    )

    narration_path = (
        WORK_DIR / "narration.m4a"
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
            str(concat_file),
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(narration_path),
        ]
    )

    subtitle_path = (
        WORK_DIR / "subtitles.srt"
    )

    subtitle_blocks = []

    for subtitle_index, item in enumerate(
        timeline,
        start=1,
    ):
        subtitle_text = wrap_text(
            item["text"],
            24,
        )

        subtitle_blocks.append(
            f"{subtitle_index}\n"
            f"{seconds_to_srt_time(item['start'])}"
            " --> "
            f"{seconds_to_srt_time(item['end'])}\n"
            f"{subtitle_text}"
        )

    subtitle_path.write_text(
        "\n\n".join(
            subtitle_blocks
        ),
        encoding="utf-8",
    )

    return (
        narration_path,
        timeline,
        cursor,
        subtitle_path,
    )


# ================================================================
# 動画生成
# ================================================================

def create_video(
    scenes,
    image_paths,
    narration_path,
    timeline,
    total_duration,
    subtitle_path,
):
    """
    画像、音声、字幕を合成する。
    """

    scene_start_times = {
        0: 0.0,
    }

    for item in timeline:
        scene_start_times.setdefault(
            int(
                item["scene"]
            ),
            float(
                item["start"]
            ),
        )

    boundaries = []
    previous_start = 0.0

    for scene_index in range(
        len(scenes)
    ):
        previous_start = (
            scene_start_times.get(
                scene_index,
                previous_start,
            )
        )

        boundaries.append(
            previous_start
        )

    boundaries.append(
        total_duration + 1.0
    )

    segment_paths = []

    for scene_index, image_path in enumerate(
        image_paths
    ):
        segment_duration = max(
            0.5,
            boundaries[scene_index + 1]
            - boundaries[scene_index],
        )

        segment_path = (
            WORK_DIR
            / (
                f"segment_"
                f"{scene_index:02d}.mp4"
            )
        )

        frame_count = max(
            1,
            int(
                segment_duration
                * FPS
            ),
        )

        if scene_index % 2 == 0:
            zoom_expression = (
                "min(zoom+0.0003,1.12)"
            )
        else:
            zoom_expression = (
                "if(eq(on,1),1.12,"
                "max(zoom-0.0003,1.0))"
            )

        fade_out_start = max(
            0,
            segment_duration - 0.35,
        )

        video_filter = (
            "scale=2560:1440:"
            "force_original_aspect_ratio=increase,"
            "crop=2560:1440,"
            "zoompan="
            f"z='{zoom_expression}':"
            f"d={frame_count}:"
            "s=1280x720:"
            "fps=30,"
            "fade=t=in:st=0:d=0.35,"
            f"fade=t=out:st={fade_out_start:.3f}:"
            "d=0.35"
        )

        log.info(
            "動画シーン生成 %d/%d",
            scene_index + 1,
            len(image_paths),
        )

        run_command(
            [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-i",
                str(image_path),
                "-vf",
                video_filter,
                "-t",
                f"{segment_duration:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "21",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(FPS),
                str(segment_path),
            ]
        )

        segment_paths.append(
            segment_path
        )

    segments_file = (
        WORK_DIR / "segments.txt"
    )

    segments_file.write_text(
        "".join(
            f"file '{path.resolve()}'\n"
            for path in segment_paths
        ),
        encoding="utf-8",
    )

    raw_video_path = (
        WORK_DIR / "raw_video.mp4"
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
            str(segments_file),
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
        WORK_DIR / "final.mp4"
    )

    subtitle_style = (
        "FontName=Noto Sans CJK JP,"
        "FontSize=22,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "Outline=2,"
        "Shadow=1,"
        "Alignment=2,"
        "MarginV=45"
    )

    resolved_subtitle_path = str(
        subtitle_path.resolve()
    ).replace(
        "\\",
        "/",
    ).replace(
        ":",
        r"\:",
    )

    subtitle_filter = (
        f"subtitles='{resolved_subtitle_path}':"
        f"force_style='{subtitle_style}'"
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(raw_video_path),
            "-vf",
            subtitle_filter,
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
    background_path,
):
    """
    サムネイルを生成する。
    """

    output_path = (
        WORK_DIR / "thumbnail.jpg"
    )

    image = Image.open(
        background_path
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

    left = (
        image.width - WIDTH
    ) // 2

    top = (
        image.height - HEIGHT
    ) // 2

    image = image.crop(
        (
            left,
            top,
            left + WIDTH,
            top + HEIGHT,
        )
    )

    image = ImageEnhance.Brightness(
        image
    ).enhance(
        0.52
    )

    image = ImageEnhance.Contrast(
        image
    ).enhance(
        1.2
    )

    draw = ImageDraw.Draw(
        image
    )

    title_font = ImageFont.truetype(
        get_font_path(),
        72,
    )

    wrapped_title = wrap_text(
        str(title),
        14,
    )

    text_box = draw.multiline_textbbox(
        (0, 0),
        wrapped_title,
        font=title_font,
        spacing=14,
        align="center",
        stroke_width=5,
    )

    text_width = (
        text_box[2] - text_box[0]
    )

    text_height = (
        text_box[3] - text_box[1]
    )

    x = (
        WIDTH - text_width
    ) / 2

    y = (
        HEIGHT - text_height
    ) / 2

    draw.multiline_text(
        (x, y),
        wrapped_title,
        font=title_font,
        fill="#FFE45C",
        stroke_width=6,
        stroke_fill="black",
        spacing=14,
        align="center",
    )

    image.save(
        output_path,
        quality=95,
    )

    return output_path


# ================================================================
# YouTube投稿
# ================================================================

def get_youtube_credentials():
    """
    TOKEN_JSONから認証情報を作る。
    """

    token_information = get_json_secret(
        "TOKEN_JSON"
    )

    credentials = (
        Credentials.from_authorized_user_info(
            token_information
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
    image_credits,
):
    """
    YouTubeへ動画を投稿する。
    """

    if (
        DRY_RUN
        or not UPLOAD_TO_YOUTUBE
    ):
        log.info(
            "YouTube投稿をスキップします。"
        )

        return None

    youtube = build(
        "youtube",
        "v3",
        credentials=(
            get_youtube_credentials()
        ),
        cache_discovery=False,
    )

    credit_lines = []

    for credit in image_credits:
        credit_parts = [
            credit.get(
                "title",
                "",
            ),
            credit.get(
                "artist",
                "",
            ),
            credit.get(
                "license",
                "",
            ),
            credit.get(
                "source",
                "",
            ),
        ]

        credit_line = " | ".join(
            part
            for part in credit_parts
            if part
        )

        if credit_line:
            credit_lines.append(
                f"- {credit_line}"
            )

    description = str(
        script_data.get(
            "description",
            "",
        )
    )

    if credit_lines:
        description += (
            "\n\n画像クレジット\n"
            + "\n".join(
                credit_lines
            )
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
            "description": (
                description[:5000]
            ),
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

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=(
            8 * 1024 * 1024
        ),
        resumable=True,
    )

    upload_request = (
        youtube.videos().insert(
            part="snippet,status",
            body=request_body,
            media_body=media,
            notifySubscribers=False,
        )
    )

    upload_response = None

    while upload_response is None:
        (
            upload_status,
            upload_response,
        ) = upload_request.next_chunk()

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
    動画生成処理を開始する。
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
    log.info("動画生成を開始します。")
    log.info("テーマ: %s", theme)
    log.info("ジャンル: %s", genre)
    log.info("トーン: %s", tone)
    log.info("DRY_RUN: %s", DRY_RUN)
    log.info("=" * 60)

    sources = research_theme(
        theme
    )

    log.info(
        "参考資料取得数: %d",
        len(sources),
    )

    llm = FreeLLMClient()

    script_data = generate_script(
        llm,
        theme,
        genre,
        tone,
        sources,
    )

    script_data = review_script(
        llm,
        script_data,
    )

    script_path = (
        WORK_DIR / "script.json"
    )

    script_path.write_text(
        json.dumps(
            script_data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    scenes = script_data.get(
        "scenes",
        [],
    )

    if not scenes:
        raise RuntimeError(
            "台本にシーンがありません。"
        )

    (
        image_paths,
        image_credits,
    ) = acquire_scene_images(
        scenes
    )

    (
        narration_path,
        timeline,
        total_duration,
        subtitle_path,
    ) = create_narration(
        scenes
    )

    log.info(
        "ナレーション時間: %.1f分",
        total_duration / 60,
    )

    final_video_path = create_video(
        scenes,
        image_paths,
        narration_path,
        timeline,
        total_duration,
        subtitle_path,
    )

    thumbnail_path = create_thumbnail(
        script_data.get(
            "title",
            theme,
        ),
        image_paths[0],
    )

    video_id = upload_to_youtube(
        final_video_path,
        thumbnail_path,
        script_data,
        image_credits,
    )

    if video_id:
        youtube_url = (
            f"https://youtu.be/{video_id}"
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
                total_duration
            ),
            "youtube_url": (
                youtube_url
            ),
            "dry_run": DRY_RUN,
            "sources": [
                {
                    "title": (
                        source["title"]
                    ),
                    "url": (
                        source["url"]
                    ),
                }
                for source in sources
            ],
            "image_credits": (
                image_credits
            ),
        }
    )

    metadata_path = (
        WORK_DIR / "metadata.json"
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
