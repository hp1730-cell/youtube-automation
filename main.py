#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.1-flash-lite",
)

OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openrouter/free",
)

FONT_FILES = [
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
                result.stderr[-3000:],
            )
        )

    return result


def get_font_path():
    """
    日本語フォントのパスを取得する。
    """

    for filename in FONT_FILES:
        if Path(filename).exists():
            return filename

    raise RuntimeError(
        "日本語フォントがありません。"
        "Workflowでfonts-noto-cjkを"
        "インストールしてください。"
    )


def check_runtime():
    """
    FFmpegなどの実行環境を確認する。
    """

    for command in (
        "ffmpeg",
        "ffprobe",
    ):
        if not shutil.which(command):
            raise RuntimeError(
                "{}がインストールされていません。".format(
                    command
                )
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
            "{}のJSON形式が正しくありません。".format(
                secret_name
            )
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


def wrap_text(
    text,
    characters_per_line,
):
    """
    指定文字数ごとに改行する。
    """

    lines = []
    position = 0

    while position < len(text):
        lines.append(
            text[
                position:
                position + characters_per_line
            ]
        )

        position += characters_per_line

    return "\n".join(
        lines
    )


# ================================================================
# LLM処理
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
        max_attempts=5,
    ):
        """
        APIを呼び出して、
        一時的なエラーの場合は再試行する。
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
                90,
                5 * (2 ** attempt),
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
                        response.text[:500],
                    )
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

            time.sleep(
                wait_seconds
            )

        raise RuntimeError(
            "API呼び出しに失敗しました: {}".format(
                last_error
            )
        )

    def ask(
        self,
        prompt,
        max_tokens=8192,
        temperature=0.4,
    ):
        """
        OpenRouter、Geminiの順に試す。
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

        url = (
            "https://openrouter.ai/api/v1/"
            "chat/completions"
        )

        headers = {
            "Authorization": (
                "Bearer {}".format(
                    self.openrouter_api_key
                )
            ),
            "Content-Type": (
                "application/json"
            ),
        }

        payload = {
            "model": OPENROUTER_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "あなたは日本語の"
                        "YouTube動画編集者です。"
                        "必ずJSONだけを"
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
        }

        result = self.post_with_retry(
            url,
            headers,
            payload,
        )

        choices = result.get(
            "choices",
            [],
        )

        if not choices:
            raise RuntimeError(
                "OpenRouterの回答が空です。"
            )

        response_text = (
            choices[0]
            .get("message", {})
            .get("content", "")
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
            "v1beta/models/{}:"
            "generateContent"
            "?key={}"
        ).format(
            GEMINI_MODEL,
            self.gemini_api_key,
        )

        headers = {
            "Content-Type": (
                "application/json"
            ),
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
                "maxOutputTokens": (
                    max_tokens
                ),
                "responseMimeType": (
                    "application/json"
                ),
            },
        }

        result = self.post_with_retry(
            url,
            headers,
            payload,
        )

        candidates = result.get(
            "candidates",
            [],
        )

        if not candidates:
            raise RuntimeError(
                "Geminiの回答が空です。"
            )

        parts = (
            candidates[0]
            .get("content", {})
            .get("parts", [])
        )

        if not parts:
            raise RuntimeError(
                "Geminiの本文が空です。"
            )

        response_text = parts[0].get(
            "text",
            "",
        )

        return parse_llm_json(
            response_text
        )


# ================================================================
# Web検索
# ================================================================

def research_theme(
    theme,
):
    """
    Web検索して本文を取得する。
    """

    search_queries = [
        theme,
        "{} 仕組み 理由".format(
            theme
        ),
        "{} 意外な事実".format(
            theme
        ),
    ]

    search_results = []
    seen_urls = set()

    for query in search_queries:
        log
