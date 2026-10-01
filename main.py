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
from typing import Any
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
    command: list[str],
    timeout: int = 1800,
) -> subprocess.CompletedProcess:
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


def get_font_path() -> str:
    """
    日本語フォントの場所を取得する。
    """

    for font_path in FONT_CANDIDATES:
        if Path(font_path).exists():
            return font_path

    raise RuntimeError(
        "日本語フォントがありません。"
        "Workflowでfonts-noto-cjkを"
        "インストールしてください。"
    )


def check_runtime() -> None:
    """
    FFmpegなどの実行環境を確認する。
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


def parse_json_response(
    response_text: str,
) -> dict[str, Any\]:
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


def get_json_secret(
    secret_name: str,
) -> dict[str, Any\]:
    """
    JSON形式の環境変数を取得する。
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


def clean_text(
    text: str,
) -> str:
    """
    URL、HTMLエンティティ、余分な空白を除去する。
    """

    text = html.unescape(
        text
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
    text: str,
    characters_per_line: int,
) -> str:
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

    def __init__(self) -> None:
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
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        max_attempts: int = 6,
    ) -> dict[str, Any\]:
        """
        APIを呼び出し、
        一時的なエラーの場合は再試行する。
        """

        last_error: Exception | None = None

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
        prompt: str,
        max_tokens: int = 11000,
        temperature: float = 0.4,
    ) -> dict[str, Any\]:
        """
        LLMへ問い合わせる。
        """

        errors: list[str] = []

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
        prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any\]:
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

        return parse_json_response(
            response_text
        )

    def ask_gemini(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any\]:
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

        return parse_json_response(
            response_text
        )


# ================================================================
# Web調査
# ================================================================

def research_theme(
    theme: str,
) -> list[dict[str, str]\]:
    """
    DDGSで検索し、
    検索先の本文を抽出する。
    """

    search_queries = [
        theme,
        f"{theme} 仕組み 理由",
        f"{theme} 意外な事実",
    ]

    search_results: list[
        dict[str, Any]
    ] = []

    seen_urls: set[str] = set()

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

                seen_urls.add(url)

                search_results.append(
                    result
                )

        except Exception as error:
            log.warning(
                "Web検索失敗: %s",
                error,
            )

    sources: list[
        dict[str, str]
    ] = []

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
            extracted_text or snippet
        )

        if len(page_text) < 200:
            continue

        sources.append(
            {
                "title": title,
                "url": url,
                "text": page_text[:6500],
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
# 台本生成
# ================================================================

def generate_script(
    llm: FreeLLMClient,
    theme: str,
    genre: str,
    tone: str,
    sources: list[dict[str, str]],
) -> dict[str, Any\]:
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

【条件】

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

【資料】

{source_text}

【出力形式】

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
            str(
                scene.get(
                    "narration",
                    "",
                )
            )
        )

        if not narration:
            raise RuntimeError(
                f"シーン{scene_index}の"
                "本文が空です。"
            )

        scene["narration"] = narration

        if not scene.get("heading"):
            scene["heading"] = (
                f"シーン{scene_index}"
            )

        if not scene.get(
            "image_query"
        ):
            scene["image_query"] = theme

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
    llm: FreeLLMClient,
    script_data: dict[str, Any],
) -> dict[str, Any\]:
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
# Wikimedia Commons画像
# ================================================================

def download_commons_image(
    query: str,
    image_index: int,
) -> tuple[
    Path | None,
    dict[str, str] | None,
\]:
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
                    "imageinfo",
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

            credit = {
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
                credit,
            )

    except Exception as error:
        log.warning(
            "画像取得失敗: %s",
            error,
        )

    return None, None


def create_fallback_image(
    heading: str,
    image_index: int,
) -> Path:
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
        heading,
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
    scenes: list[dict[str, Any]],
) -> tuple[
    list[Path],
    list[dict[str, str]],
\]:
    """
    全シーンの画像を取得する。
    """

    image_paths: list[Path] = []

    image_credits: list[
        dict[str, str]
    ] = []

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
                str(
                    scene.get(
                        "heading",
                        "",
                    )
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
    text: str,
    max_characters: int = 70,
) -> list[str\]:
    """
    ナレーションを短い単位に分割する。
    """

    clips: list[str] = []

    sentences = re.split(
        r"(?<=[。！？!?])",
        text,
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
    text: str,
    output_path: Path,
) -> None:
    """
    Edge TTSで音声を生成する。
    """

    speed_percent = int(
        (NARRATION_SPEED - 1)
        * 100
    )

    last_error: Exception | None = None

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
    media_path: Path,
) -> float:
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
    seconds: float,
) -> str:
    """
    秒数をSRT時刻に変換する。
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
    scenes: list[dict[str, Any]],
) -> tuple[
    Path,
    list[dict[str, Any]],
    float,
    Path,
\]:
    """
    音声と字幕を生成する。
    """

    clip_items: list[
        tuple[int, str]
    ] = []

    for scene_index, scene in enumerate(
        scenes
    ):
        narration_text = clean_text(
            str(
                scene["narration"]
            )
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

    async def generate_all_audio() -> None:
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

    timeline: list[
        dict[str, Any]
    ] = []

    audio_paths: list[Path] = []

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
            for path in audio_paths
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

    subtitle_blocks: list[str] = []

    for subtitle_index, item in enumerate(
        timeline
