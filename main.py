#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GitHub Actions向け YouTube雑学動画自動生成

処理内容:
1. GitHub Actionsからテーマを取得
2. DDGSでWeb検索
3. 検索先の本文を抽出
4. OpenRouterまたはGeminiで台本を生成
5. 台本を校閲
6. Wikimedia Commonsから画像を取得
7. Edge TTSで日本語ナレーションを生成
8. FFmpegで字幕付き動画を生成
9. サムネイルを生成
10. YouTubeへアップロード

必要なGitHub Secrets:
- GEMINI_API_KEY
- TOKEN_JSON

任意:
- OPENROUTER_API_KEY
"""

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

VOICE = os.getenv(
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

OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openrouter/free",
)

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash",
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
    "youtube-auto-pipeline"
)


# ================================================================
# 共通処理
# ================================================================

def run_command(
    command: list[str],
    timeout: int = 1800,
) -> subprocess.CompletedProcess:
    """
    FFmpegなどの外部コマンドを実行する。
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


def check_runtime() -> None:
    """
    FFmpegとFFprobeの存在を確認する。
    """

    for command in (
        "ffmpeg",
        "ffprobe",
    ):
        if not shutil.which(command):
            raise RuntimeError(
                f"{command}がインストールされていません。"
            )

    get_font_path()


def get_font_path() -> str:
    """
    日本語フォントのパスを取得する。
    """

    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate

    raise RuntimeError(
        "日本語フォントが見つかりません。"
        "GitHub Actionsでfonts-noto-cjkを"
        "インストールしてください。"
    )


def get_json_secret(
    name: str,
) -> dict[str, Any\]:
    """
    JSON形式の環境変数を辞書へ変換する。
    """

    raw_value = os.getenv(
        name,
        "",
    ).strip()

    if not raw_value:
        raise RuntimeError(
            f"{name}が設定されていません。"
        )

    try:
        return json.loads(raw_value)

    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{name}のJSON形式が正しくありません。"
        ) from error


def parse_llm_json(
    response_text: str,
) -> dict[str, Any\]:
    """
    LLMの回答からJSONを取り出す。
    """

    cleaned = response_text.strip()

    cleaned = re.sub(
        r"^```(?:json)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    cleaned = re.sub(
        r"\s*```$",
        "",
        cleaned,
    )

    try:
        return json.loads(cleaned)

    except json.JSONDecodeError:
        json_match = re.search(
            r"\{.*\}",
            cleaned,
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


def sanitize_text(
    text: str,
) -> str:
    """
    HTML、URL、余分な空白を取り除く。
    """

    text = html.unescape(text)

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
    OpenRouterを優先し、失敗時にGeminiを使用する。
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
                "OPENROUTER_API_KEYまたは"
                "GEMINI_API_KEYが必要です。"
            )

    def post_with_retry(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        max_attempts: int = 6,
    ) -> dict[str, Any\]:
        """
        APIを呼び出し、一時エラー時に再試行する。
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

        for attempt in range(max_attempts):
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

            wait_seconds += random.uniform(
                0,
                3,
            )

            log.warning(
                "API再試行まで%.1f秒待機します。",
                wait_seconds,
            )

            time.sleep(wait_seconds)

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
        OpenRouter、Geminiの順に実行する。
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
                message = (
                    f"OpenRouter失敗: {error}"
                )

                log.warning(message)
                errors.append(message)

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
                message = (
                    f"Gemini失敗: {error}"
                )

                log.warning(message)
                errors.append(message)

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

        url = (
            "https://openrouter.ai/api/v1/"
            "chat/completions"
        )

        headers = {
            "Authorization": (
                f"Bearer {self.openrouter_api_key}"
            ),
            "Content-Type": "application/json",
        }

        payload = {
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
        }

        response_data = self.post_with_retry(
            url,
            headers,
            payload,
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

        headers = {
            "Content-Type": "application/json",
        }

        payload = {
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
                "maxOutputTokens": max_tokens,
                "responseMimeType": (
                    "application/json"
                ),
            },
        }

        response_data = self.post_with_retry(
            url,
            headers,
            payload,
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
    theme: str,
) -> list[dict[str, str]\]:
    """
    DDGSで検索し、上位ページの本文を抽出する。
    """

    search_queries = [
        theme,
        f"{theme} 仕組み 理由",
        f"{theme} 意外な事実",
    ]

    search_results: list[dict[str, Any]] = []
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
                search_results.append(result)

        except Exception as error:
            log.warning(
                "Web検索に失敗しました: %s",
                error,
            )

    sources: list[dict[str, str]] = []

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
                trafilatura.fetch_url(url)
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

        page_text = sanitize_text(
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
        for index, source in enumerate(sources)
    )

    if not source_text:
        source_text = (
            "資料不足のため、具体的数値を断定せず、"
            "一般知識として慎重に説明してください。"
        )

    selected_genre = (
        genre
        if genre
        else "自動判定"
    )

    selected_tone = (
        tone
        if tone
        else (
            "わかりやすく、"
            "最後まで見たくなる雰囲気"
        )
    )

    prompt = f"""
次のテーマについて、YouTube向けの日本語動画台本を作成してください。

テーマ:
{theme}

ジャンル:
{selected_genre}

ナレーションの雰囲気:
{selected_tone}

【条件】

・雑学、科学、歴史、動物、生活知識など、テーマに合う構成にする
・AIドラマに固定しない
・8〜10分を目標にする
・台本全体は4200〜5200日本語文字を目安にする
・9〜11シーンで構成する
・冒頭15秒に疑問、驚き、意外性を入れる
・同じ内容を繰り返さない
・各シーンを自然につなげる
・参考資料で確認できる内容だけを断定する
・諸説ある内容は断定しない
・参考資料にない数値を作らない
・専門用語には簡単な説明を付ける
・narrationには読み上げる本文だけを入れる
・narrationにURLを入れない
・image_queryはWikimedia Commonsで検索しやすい英語名詞句にする
・タイトルは45文字以内にする
・過度な煽り表現を使わない
・JSON以外の文章を返さない

【参考資料】

{source_text}

【出力JSON形式】

{{
  "title": "動画タイトル",
  "description": "動画概要",
  "genre": "動画ジャンル",
  "tags": [
    "タグ1",
    "タグ2",
    "タグ3"
  ],
  "scenes": [
    {{
      "heading": "シーン見出し",
      "narration": "日本語の読み上げ本文",
      "image_query": "English noun phrase"
    }}
  ]
}}
"""

    script_data = llm.ask(
        prompt,
        max_tokens=11000,
        temperature=0.4,
    )

    title = str(
        script_data.get(
            "title",
            "",
        )
    ).strip()

    scenes = script_data.get(
        "scenes",
        [],
    )

    if not title:
        raise RuntimeError(
            "動画タイトルが生成されませんでした。"
        )

    if not isinstance(scenes, list):
        raise RuntimeError(
            "scenesが配列ではありません。"
        )

    if len(scenes) < 7:
        raise RuntimeError(
            "シーン数が少なすぎます。"
        )

    if len(scenes) > 13:
        raise RuntimeError(
            "シーン数が多すぎます。"
        )

    for scene_index, scene in enumerate(
        scenes,
        start=1,
    ):
        narration = sanitize_text(
            str(
                scene.get(
                    "narration",
                    "",
                )
            )
        )

        if not narration:
            raise RuntimeError(
                f"シーン{scene_index}の本文が空です。"
            )

        scene["narration"] = narration

        if not scene.get("heading"):
            scene["heading"] = (
                f"シーン{scene_index}"
            )

        if not scene.get("image_query"):
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

    script_data["title"] = title[:100]
    script_data["description"] = description
    script_data["scenes"] = scenes

    return script_data


def review_script(
    llm: FreeLLMClient,
    script_data: dict[str, Any],
) -> dict[str, Any\]:
    """
    台本を校閲する。
    """

    prompt = f"""
次のYouTube動画台本を校閲してください。

【修正条件】

・事実を断定しすぎている表現を修正する
・同じ説明の繰り返しを削除する
・不自然な日本語を修正する
・シーン間の矛盾を修正する
・シーン間のつながりを自然にする
・新しい具体的数値を追加しない
・title、description、genre、tags、scenesの構造を維持する
・heading、narration、image_queryを維持する
・完成したJSONだけを返す

【台本】

{json.dumps(
    script_data,
    ensure_ascii=False,
    indent=2,
)}
"""

    try:
        reviewed_data = llm.ask(
            prompt,
            max_tokens=12000,
            temperature=0.2,
        )

        if (
            reviewed_data.get("title")
            and reviewed_data.get("scenes")
        ):
            return reviewed_data

    except Exception as error:
        log.warning(
            "台本校閲をスキップします: %s",
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

    api_url = (
        "https://commons.wikimedia.org/w/api.php"
    )

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
            api_url,
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
            image_info_list = page.get(
                "imageinfo",
                [],
            )

            if not image_info_list:
                continue

            image_info = image_info_list[0]

            image_url = (
                image_info.get("thumburl")
                or image_info.get("url")
            )

            if not image_url:
                continue

            suffix = Path(
                urlparse(image_url).path
            ).suffix.lower()

            if suffix not in {
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
            }:
                suffix = ".jpg"

            output_path = (
                WORK_DIR
                / f"scene_{image_index:02d}{suffix}"
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
                "artist": sanitize_text(
                    artist
                )[:200],
            }

            return output_path, credit

    except Exception as error:
        log.warning(
            "画像取得に失敗しました: %s",
            error,
        )

    return None, None


def create_fallback_image(
    heading: str,
    image_index: int,
) -> Path:
    """
    画像が見つからない場合の背景を生成する。
    """

    output_path = (
        WORK_DIR
        / f"fallback_{image_index:02d}.jpg"
    )

    red = 25 + (
        image_index * 13
    ) % 55

    green = 35

    blue = 75 + (
        image_index * 19
    ) % 90

    image = Image.new(
        "RGB",
        (WIDTH, HEIGHT),
        (red, green, blue),
    )

    draw = ImageDraw.Draw(image)

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
    image_credits: list[dict[str, str]] = []

    for image_index, scene in enumerate(scenes):
        search_query = str(
            scene.get(
                "image_query",
                scene.get("heading", ""),
            )
        ).strip()

        log.info(
            "画像取得 %d/%d: %s",
            image_index + 1,
            len(scenes),
            search_query,
        )

        image_path, image_credit = (
            download_commons_image(
                search_query,
                image_index,
            )
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

        elif image_credit:
            image_credits.append(
                image_credit
            )

        image_paths.append(
            image_path
        )

    return image_paths, image_credits


# ================================================================
# ナレーション
# ================================================================

def split_narration(
    text: str,
    max_characters: int = 70,
) -> list[str\]:
    """
    ナレーションを短い読み上げ単位に分割する。
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

        while len(sentence) > max_characters:
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
            clips.append(sentence)

    return clips


async def synthesize_audio(
    text: str,
    output_path: Path,
) -> None:
    """
    Edge TTSで音声を生成する。
    """

    speed_percent = int(
        (NARRATION_SPEED - 1) * 100
    )

    last_error: Exception | None = None

    for attempt in range(4):
        try:
            communicator = edge_tts.Communicate(
                text,
                VOICE,
                rate=f"{speed_percent:+d}%",
            )

            await communicator.save(
                str(output_path)
            )

            if (
                output_path.exists()
                and output_path.stat().st_size > 1000
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
    秒数をSRT形式に変換する。
    """

    milliseconds = int(
        round(seconds * 1000)
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
    ナレーションと字幕ファイルを作成する。
    """

    clip_items: list[
        tuple[int, str]
    ] = []

    for scene_index, scene in enumerate(scenes):
        narration_text = sanitize_text(
            str(scene["narration"])
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
           enumerate(clip_items):
            output_path = (
                WORK_DIR
                / f"voice_{clip_index:03d}.mp3"
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

    timeline: list[dict[str, Any]] = []
    audio_paths: list[Path] = []

    cursor = 0.5
    clip_gap = 0.15

    for clip_index, (
        scene_index,
        clip_text,
    ) in enumerate(clip_items):
        audio_path = (
            WORK_DIR
            / f"voice_{clip_index:03d}.mp3"
        )

        clip_duration = probe_duration(
            audio_path
        )

        timeline.append(
            {
                "scene": scene_index,
                "text": clip_text,
                "start": cursor,
                "end": cursor + clip_duration,
            }
        )

        cursor += (
            clip_duration
            + clip_gap
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

    for subtitle_index, timeline_item in enumerate(
        timeline,
        start=1,
    ):
        subtitle_text = wrap_text(
            timeline_item["text"],
            24,
        )

        subtitle_blocks.append(
            f"{subtitle_index}\n"
            f"{seconds_to_srt_time(timeline_item['start'])}"
            " --> "
            f"{seconds_to_srt_time(timeline_item['end'])}\n"
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
    scenes: list[dict[str, Any]],
    image_paths: list[Path],
    narration_path: Path,
    timeline: list[dict[str, Any]],
    total_duration: float,
    subtitle_path: Path,
) -> Path:
    """
    静止画、音声、字幕を合成する。
    """

    scene_start_times: dict[
        int,
        float,
    ] = {
        0: 0.0,
    }

    for timeline_item in timeline:
        scene_start_times.setdefault(
            int(timeline_item["scene"]),
            float(timeline_item["start"]),
        )

    boundaries: list[float] = []
    previous_start = 0.0

    for scene_index in range(
        len(scenes)
    ):
        previous_start = scene_start_times.get(
            scene_index,
            previous_start,
        )

        boundaries.append(
            previous_start
        )

    boundaries.append(
        total_duration + 1.0
    )

    segment_paths: list[Path] = []

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
            / f"segment_{scene_index:02d}.mp4"
        )

        frame_count = max(
            1,
            int(
                segment_duration * FPS
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
    title: str,
    background_image_path: Path,
) -> Path:
    """
    サムネイルを生成する。
    """

    output_path = (
        WORK_DIR / "thumbnail.jpg"
    )

    image = Image.open(
        background_image_path
    ).convert(
        "RGB"
    )

    resize_ratio = max(
        WIDTH / image.width,
        HEIGHT / image.height,
    )

    resized_width = int(
        image.width * resize_ratio
    )

    resized_height = int(
        image.height * resize_ratio
    )

    image = image.resize(
        (
            resized_width,
            resized_height,
        )
    )

    left = (
        resized_width - WIDTH
    ) // 2

    top = (
        resized_height - HEIGHT
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

    draw = ImageDraw.Draw(image)

    title_font = ImageFont.truetype(
        get_font_path(),
        72,
    )

    wrapped_title = wrap_text(
        title,
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

def get_youtube_credentials() -> Credentials:
    """
    TOKEN_JSONからYouTube認証情報を作る。
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
    video_path: Path,
    thumbnail_path: Path,
    script_data: dict[str, Any],
    image_credits: list[dict[str, str]],
) -> str | None:
    """
    YouTubeへ投稿する。
    """

    if DRY_RUN:
        log.info(
            "DRY_RUN=trueのため"
            "YouTube投稿をスキップします。"
        )

        return None

    if not UPLOAD_TO_YOUTUBE:
        log.info(
            "UPLOAD_TO_YOUTUBE=falseのため"
            "投稿をスキップします。"
        )

        return None

    credentials = (
        get_youtube_credentials()
    )

    youtube = build(
        "youtube",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )

    credit_lines: list[str] = []

    for credit in image_credits:
        credit_parts = [
            credit.get("title", ""),
            credit.get("artist", ""),
            credit.get("license", ""),
            credit.get("source", ""),
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
            + "\n".join(credit_lines)
        )

    tags = script_data.get(
        "tags",
        [],
    )

    if not isinstance(tags, list):
        tags = []

    request_body = {
        "snippet": {
            "title": str(
                script_data.get(
                    "title",
                    "自動生成動画",
                )
            )[:100],
            "description": description[:5000],
            "tags": [
                str(tag)[:50]
                for tag in tags[:20]
            ],
            "categoryId": "27",
            "defaultLanguage": "ja",
            "defaultAudioLanguage": "ja",
        },
        "status": {
            "privacyStatus": PRIVACY_STATUS,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=8 * 1024 * 1024,
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

    log.info(
        "YouTubeへアップロードします。"
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

    if thumbnail_path.exists():
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(
                    str(thumbnail_path),
                    mimetype="image/jpeg",
                ),
            ).execute()

            log.info(
                "サムネイルを設定しました。"
            )

        except Exception as error:
            log.warning(
                "サムネイル設定失敗: %s",
                error,
            )

    return video_id


# ================================================================
# メイン処理
# ================================================================

def main() -> None:
    """
    動画生成処理を実行する。
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
            "生成された台本にシーンがありません。"
        )

    image_paths, image_credits = (
        acquire_scene_images(
            scenes
        )
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
        str(
            script_data.get(
                "title",
                theme,
            )
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
            "youtube_url": youtube_url,
            "dry_run": DRY_RUN,
            "sources": [
                {
                    "title": source["title"],
                    "url": source["url"],
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
    log.info("動画生成が完了しました。")
    log.info(
        "動画ファイル: %s",
        final_video_path,
    )

    if youtube_url:
        log.info(
            "YouTube URL: %s",
            youtube_url,
        )
    else:
        log.info(
            "YouTube投稿は行われていません。"
        )

    log.info("=" * 60)


if __name__ == "__main__":
    main()
