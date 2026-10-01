#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import edge_tts
import gspread
import requests
import trafilatura
from ddgs import DDGS
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as SACredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image, ImageDraw, ImageEnhance, ImageFont


# ================================================================
# 基本設定
# ================================================================

WORK_DIR = Path(os.getenv("WORK_DIR", "work"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

WIDTH = 1280
HEIGHT = 720
FPS = 30

VOICE = os.getenv(
    "EDGE_VOICE",
    "ja-JP-NanamiNeural",
)

NARRATION_SPEED = float(
    os.getenv("NARRATION_SPEED", "1.10")
)

DRY_RUN = (
    os.getenv("DRY_RUN", "false").strip().lower()
    == "true"
)

UPLOAD_TO_YOUTUBE = (
    os.getenv("UPLOAD_TO_YOUTUBE", "true")
    .strip()
    .lower()
    == "true"
)

SHEET_TAB = os.getenv(
    "SHEET_TAB",
    "動画管理",
)

PRIVACY_STATUS = os.getenv(
    "PRIVACY_STATUS",
    "private",
)

OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openrouter/free",
)

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash",
)

SHEET_HEADERS = [
    "ID",
    "テーマ",
    "ジャンル",
    "トーン",
    "ステータス",
    "タイトル",
    "YouTube URL",
    "エラー",
    "更新日時",
]

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
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("youtube-video-generator")


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

    log.info("コマンド実行: %s", command[0])

    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        error_message = result.stderr[-3000:]

        raise RuntimeError(
            f"{command[0]} の実行に失敗しました。\n"
            f"{error_message}"
        )

    return result


def check_runtime() -> None:
    """
    FFmpegなど、実行に必要なソフトを確認する。
    """

    for command in ["ffmpeg", "ffprobe"\]:
        if not shutil.which(command):
            raise RuntimeError(
                f"{command} がインストールされていません。"
            )


def get_font_path() -> str:
    """
    日本語フォントのパスを取得する。
    """

    for font_path in FONT_CANDIDATES:
        if Path(font_path).exists():
            return font_path

    raise RuntimeError(
        "Noto Sans CJK日本語フォントが見つかりません。"
    )


def get_json_environment(name: str) -> dict:
    """
    GitHub Secretsなどに保存したJSON文字列を辞書へ変換する。
    """

    raw_value = os.getenv(name, "").strip()

    if not raw_value:
        raise RuntimeError(
            f"{name} が設定されていません。"
        )

    try:
        return json.loads(raw_value)

    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{name} のJSON形式が正しくありません。"
        ) from error


def parse_json_response(text: str) -> dict:
    """
    LLMから返された文字列をJSONへ変換する。
    Markdownコードブロックにも対応する。
    """

    cleaned = text.strip()

    cleaned = re.sub(
        r"^```(?:json)?\s*",
        "",
        cleaned,
  *     flags=re.IGNORECASE,
    )

    cleaned = re.sub(
        r"\s*```$",
        "",
        cleaned,
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
                "LLMの応答にJSONが含まれていません。"
            )

        try:
            return json.loads(match.group(0))

        except json.JSONDecodeError as error:
            raise RuntimeError(
                "LLMのJSON解析に失敗しました。"
            ) from error


def wrap_japanese_text(
    text: str,
    characters_per_line: int,
) -> str:
    """
    日本語文字列を指定文字数ごとに改行する。
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
# 無料LLM
# ================================================================

class FreeLLMClient:
    """
    OpenRouterの無料モデルを優先する。

    OpenRouterで失敗した場合のみ、
    Gemini API無料枠へフォールバックする。
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
        headers: dict,
        payload: dict,
        max_attempts: int = 6,
    ) -> dict:
        """
        APIへPOSTし、429や一時的障害時に再試行する。
        """

        last_error = None

        for attempt in range(max_attempts):
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

                retry_statuses = [
                    408,
                    429,
                    500,
                    502,
                    503,
                    504,
                ]

                if response.status_code not in retry_statuses:
                    raise last_error

                retry_after = response.headers.get(
                    "Retry-After"
                )

                if retry_after:
                    wait_seconds = float(retry_after)

                else:
                    wait_seconds = min(
                        180,
                        10 * (2 ** attempt),
                    )

                wait_seconds += random.uniform(0, 3)

                log.warning(
                    "LLM API制限または一時障害。"
                    "%.1f秒後に再試行します。",
                    wait_seconds,
                )

                time.sleep(wait_seconds)

            except requests.RequestException as error:
                last_error = error

                wait_seconds = min(
                    180,
                    10 * (2 ** attempt),
                )

                wait_seconds += random.uniform(0, 3)

                log.warning(
                    "LLM通信エラー。"
                    "%.1f秒後に再試行します。",
                    wait_seconds,
                )

                time.sleep(wait_seconds)

        raise RuntimeError(
            f"LLM APIへの接続に失敗しました: "
            f"{last_error}"
        )

    def ask(
        self,
        prompt: str,
        max_tokens: int = 11000,
        temperature: float = 0.4,
    ) -> dict:
        """
        OpenRouterを優先し、
        失敗時にGeminiへ切り替える。
        """

        errors = []

        if self.openrouter_api_key:
            try:
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
    ) -> dict:
        """
        OpenRouter無料ルーターを呼び出す。
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
                        "あなたは日本語のYouTube動画編集者です。"
                        "必ず有効なJSONだけを返してください。"
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

        response_text = (
            result["choices"][0]["message"]["content"]
        )

        return parse_json_response(response_text)

    def ask_gemini(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> dict:
        """
        Gemini API無料枠を呼び出す。
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

        result = self.post_with_retry(
            url,
            headers,
            payload,
        )

        response_text = (
            result["candidates"][0]
            ["content"]["parts"][0]["text"]
        )

        return parse_json_response(response_text)


# ================================================================
# Googleスプレッドシート
# ================================================================

class SpreadsheetQueue:
    """
    Googleスプレッドシートを動画生成キューとして使う。
    """

    def __init__(self) -> None:
        service_account_info = (
            get_json_environment(
                "GOOGLE_SERVICE_ACCOUNT_JSON"
            )
        )

        credentials = (
            SACredentials.from_service_account_info(
                service_account_info,
                scopes=[
                    (
                        "https://www.googleapis.com/"
                        "auth/spreadsheets"
                    )
                ],
            )
        )

        client = gspread.authorize(credentials)

        spreadsheet_id = os.getenv(
            "SPREADSHEET_ID",
            "",
        ).strip()

        if not spreadsheet_id:
            raise RuntimeError(
                "SPREADSHEET_IDが設定されていません。"
            )

        spreadsheet = client.open_by_key(
            spreadsheet_id
        )

        self.worksheet = spreadsheet.worksheet(
            SHEET_TAB
        )

        if not self.worksheet.row_values(1):
            self.worksheet.append_row(
                SHEET_HEADERS
            )

    def get_next_job(
        self,
    ) -> tuple[int, dict] | None:
        """
        未処理の先頭行を1件取得する。
        """

        records = self.worksheet.get_all_records(
            expected_headers=SHEET_HEADERS
        )

        for row_number, record in enumerate(
            records,
            start=2,
        ):
            theme = str(
                record.get("テーマ", "")
            ).strip()

            status = str(
                record.get("ステータス", "")
            ).strip()

            if (
                theme
                and status in (
                    "",
                    "未処理",
                    "待機",
                )
            ):
                return row_number, record

        return None

    def update_job(
        self,
        row_number: int,
        **values,
    ) -> None:
        """
        指定行のステータスなどを更新する。
        """

        values["更新日時"] = (
            datetime.now(timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds")
        )

        headers = self.worksheet.row_values(1)

        for column_name, value in values.items():
            if column_name not in headers:
                continue

            column_number = (
                headers.index(column_name) + 1
            )

            self.worksheet.update_cell(
                row_number,
                column_number,
                str(value)[:45000],
            )


# ================================================================
# 無料Web調査
# ================================================================

def research_theme(
    theme: str,
) -> list[dict\]:
    """
    DuckDuckGo系検索を使い、
    上位ページ本文を取得する。
    """

    search_queries = [
        theme,
        f"{theme} 仕組み 理由",
        f"{theme} 意外な事実",
    ]

    search_results = []
    seen_urls = set()

    for query in search_queries:
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
                url = result.get(
                    "href",
                    "",
                )

                if not url:
                    continue

                if url in seen_urls:
                    continue

                seen_urls.add(url)
                search_results.append(result)

        except Exception as error:
            log.warning(
                "Web検索に失敗
