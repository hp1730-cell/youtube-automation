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

PIXABAY_VIDEOS_PER_SCENE = int(
    os.getenv(
        "PIXABAY_VIDEOS_PER_SCENE",
        "2",
    )
)

PIXABAY_RESULTS_PER_QUERY = int(
    os.getenv(
        "PIXABAY_RESULTS_PER_QUERY",
        "20",
    )
)

USED_PIXABAY_IDS = set()
PIXABAY_CACHE = {}

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
            "{}の実行に失敗しました。\n{}".format(
                command[0],
                result.stderr[-4000:],
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
        "Workflowでfonts-noto-cjkを"
        "インストールしてください。"
    )


def check_runtime():
    """
    FFmpeg、FFprobe、日本語フォントを確認する。
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
    width,
):
    """
    指定文字数ごとに改行する。
    """

    return "\n".join(
        text[index:index + width]
        for index in range(
            0,
            len(text),
            width,
        )
    )


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
            "{}のJSON形式が不正です。".format(
                secret_name
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


def seconds_to_srt_time(
    seconds,
):
    """
    秒数をSRT字幕形式へ変換する。
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
        "{:02}:{:02}:{:02},{:03}"
    ).format(
        hours,
        minutes,
        seconds_value,
        milliseconds,
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
        APIを呼び出し、一時エラーの場合は再試行する。
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
                        response.text[:700],
                    )
                )

                if (
                    response.status_code
                    not in retryable_statuses
                ):
                    raise last_error

                retry_after = response.headers.get(
                    "Retry-After",
                    "",
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
            "API呼び出し失敗: {}".format(
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
        OpenRouter、Geminiの順に問い合わせる。
        """

        errors = []

        if self.openrouter_api_key:
            try:
                log.info(
                    "OpenRouterを使用します。"
                )

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
                                    "日本語の動画編集者です。"
                                    "JSONだけを返してください。"
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

                return parse_llm_json(
                    result["choices"][0]
                    ["message"]["content"]
                )

            except Exception as error:
                error_message = (
                    "OpenRouter: {}".format(
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

                url = (
                    "https://generativelanguage.googleapis.com/"
                    "v1beta/models/{}:"
                    "generateContent"
                    "?key={}"
                ).format(
                    GEMINI_MODEL,
                    self.gemini_api_key,
                )

                result = self.post_with_retry(
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

                return parse_llm_json(
                    result["candidates"][0]
                    ["content"]["parts"][0]["text"]
                )

            except Exception as error:
                error_message = (
                    "Gemini: {}".format(
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


# ================================================================
# Web調査
# ================================================================

def research_theme(
    theme,
):
    """
    Web検索し、上位ページの本文を取得する。
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
                )

                if (
                    url
                    and url not in seen_urls
                ):
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

    for result in search_results[:10]:
        url = str(
            result.get(
                "href",
                "",
            )
        )

        try:
            downloaded_page = trafilatura.fetch_url(
                url
            )

            if downloaded_page:
                page_body = (
                    trafilatura.extract(
                        downloaded_page,
                        favor_precision=True,
                    )
                    or ""
                )
            else:
                page_body = ""

        except Exception:
            page_body = ""

        page_text = clean_text(
            page_body
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
                    "url": url,
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

def create_script(
    llm,
    theme,
    genre,
    tone,
    sources,
):
    """
    台本とPixabay用英語検索語を生成する。
    """

    source_parts = []

    for index, source in enumerate(
        sources
    ):
        source_parts.append(
            (
                "[{}] {}\n"
                "URL: {}\n"
                "{}"
            ).format(
                index + 1,
                source.get(
                    "title",
                    "",
                ),
                source.get(
                    "url",
                    "",
                ),
                source.get(
                    "text",
                    "",
                ),
            )
        )

    source_text = "\n\n".join(
        source_parts
    )

    if not source_text:
        source_text = (
            "資料不足のため、"
            "具体的数値を断定せず慎重に説明すること。"
        )

    prompt = """
テーマ「{}」のYouTube向け日本語動画台本を作成してください。

ジャンル:
{}

トーン:
{}

条件:
- 8〜10分を目標にする
- 3800〜4800日本語文字
- 9〜11シーン
- 冒頭15秒に疑問か意外性
- 資料で確認できる事実だけを断定
- narrationは読み上げ本文だけ
- 各シーンに英語のvideo_queriesを3個付ける
- video_queriesは主役、行動、場所が分かる検索句
- 実写映像が検索しやすい表現にする
- 商品名、企業ロゴ、著名人名を入れない
- titleは45文字以内
- JSON以外は禁止

参考資料:
{}

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
      "video_queries": [
        "",
        "",
        ""
      ]
    }}
  ]
}}
""".format(
        theme,
        genre or "自動判定",
        tone or (
            "わかりやすく、"
            "最後まで見たくなる雰囲気"
        ),
        source_text,
    )

    script_data = llm.ask(
        prompt,
        max_tokens=8192,
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
            "シーン数が不正です。"
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
                "シーン{}の本文が空です。".format(
                    scene_index
                )
            )

        scene["narration"] = narration

        scene["heading"] = (
            scene.get(
                "heading"
            )
            or "シーン{}".format(
                scene_index
            )
        )

        video_queries = scene.get(
            "video_queries",
            [],
        )

        if not isinstance(
            video_queries,
            list,
        ):
            video_queries = []

        cleaned_queries = [
            str(query).strip()[:100]
            for query in video_queries
            if str(query).strip()
        ]

        fallback_queries = [
            "{} documentary footage".format(
                theme
            ),
            "{} close up".format(
                theme
            ),
            "{} nature footage".format(
                theme
            ),
        ]

        while len(cleaned_queries) < 3:
            cleaned_queries.append(
                fallback_queries[
                    len(cleaned_queries)
                ]
            )

        scene["video_queries"] = (
            cleaned_queries[:3]
        )

    source_urls = "\n".join(
        "- " + source.get(
            "url",
            "",
        )
        for source in sources
        if source.get(
            "url"
        )
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

    script_data["scenes"] = scenes

    return script_data


# ================================================================
# Pixabay動画検索
# ================================================================

def choose_pixabay_file(
    video_data,
):
    """
    Pixabay検索結果から適切な動画ファイルを選ぶ。
    """

    candidates = []

    for quality_name in (
        "medium",
        "large",
        "small",
        "tiny",
    ):
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

        if (
            width >= 1280
            and height >= 720
        ):
            hd_penalty = 0
        else:
            hd_penalty = 10000

        quality_penalty = {
            "medium": 0,
            "large": 100,
            "small": 20000,
            "tiny": 30000,
        }[quality_name]

        aspect_penalty = abs(
            (width / height)
            - (16 / 9)
        ) * 10000

        score = (
            aspect_penalty
            + hd_penalty
            + quality_penalty
        )

        candidates.append(
            (
                score,
                video_url,
                width,
                height,
                quality_name,
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
        "quality": selected[4],
    }


def search_pixabay_videos(
    query,
):
    """
    Pixabay APIで動画を検索する。
    """

    normalized_query = str(
        query
    ).strip().lower()[:100]

    if not normalized_query:
        return []

    if not PIXABAY_API_KEY:
        log.warning(
            "PIXABAY_API_KEYが未設定です。"
        )

        return []

    if normalized_query in PIXABAY_CACHE:
        log.info(
            "Pixabay検索キャッシュを使用: %s",
            normalized_query,
        )

        return PIXABAY_CACHE[
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
                "Pixabay APIの制限に達しました。"
            )

            return []

        response.raise_for_status()

        remaining = response.headers.get(
            "X-RateLimit-Remaining",
            "不明",
        )

        log.info(
            "Pixabay API残数: %s",
            remaining,
        )

        search_results = response.json().get(
            "hits",
            [],
        )

        PIXABAY_CACHE[
            normalized_query
        ] = search_results

        return search_results

    except Exception as error:
        log.warning(
            "Pixabay検索失敗: %s",
            error,
        )

        return []


def download_pixabay_video(
    video_data,
    scene_index,
    video_index,
):
    """
    Pixabay動画をダウンロードする。
    """

    video_id = int(
        video_data.get(
            "id",
            0,
        )
        or 0
    )

    if not video_id:
        return None, None

    if video_id in USED_PIXABAY_IDS:
        return None, None

    selected_file = choose_pixabay_file(
        video_data
    )

    if not selected_file:
        return None, None

    output_path = (
        WORK
        / (
            "pixabay_{:02d}_{:02d}.mp4"
        ).format(
            scene_index,
            video_index,
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
        ) < 1:
            output_path.unlink(
                missing_ok=True
            )

            return None, None

        USED_PIXABAY_IDS.add(
            video_id
        )

        creator_name = str(
            video_data.get(
                "user",
                "",
            )
        )

        creator_id = str(
            video_data.get(
                "user_id",
                "",
            )
        )

        if (
            creator_name
            and creator_id
        ):
            creator_url = (
                "https://pixabay.com/users/"
                "{}-{}/"
            ).format(
                creator_name,
                creator_id,
            )
        else:
            creator_url = ""

        credit = {
            "provider": "Pixabay",
            "video_id": video_id,
            "page_url": str(
                video_data.get(
                    "pageURL",
                    "",
                )
            ),
            "creator_name": creator_name,
            "creator_url": creator_url,
            "tags": str(
                video_data.get(
                    "tags",
                    "",
                )
            ),
            "width": selected_file["width"],
            "height": selected_file["height"],
            "quality": selected_file["quality"],
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


def acquire_pixabay_scene_videos(
    scenes,
):
    """
    各シーンにつき最大2本の動画を取得する。
    """

    all_scene_videos = []
    all_credits = []

    for scene_index, scene in enumerate(
        scenes
    ):
        scene_videos = []

        video_queries = scene.get(
            "video_queries",
            [],
        )

        if not isinstance(
            video_queries,
            list,
        ):
            video_queries = []

        for query in video_queries:
            search_results = (
                search_pixabay_videos(
                    query
                )
            )

            for video_data in search_results:
                video_path, credit = (
                    download_pixabay_video(
                        video_data,
                        scene_index,
                        len(scene_videos),
                    )
                )

                if video_path:
                    scene_videos.append(
                        video_path
                    )

                    if credit:
                        credit["query"] = query
                        all_credits.append(
                            credit
                        )

                if (
                    len(scene_videos)
                    >= PIXABAY_VIDEOS_PER_SCENE
                ):
                    break

            if (
                len(scene_videos)
                >= PIXABAY_VIDEOS_PER_SCENE
            ):
                break

        all_scene_videos.append(
            scene_videos
        )

        log.info(
            "シーン%dのPixabay素材: %d本",
            scene_index + 1,
            len(scene_videos),
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
        all_scene_videos,
        all_credits,
    )


# ================================================================
# Wikimedia Commonsフォールバック
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
            image_info_list = page.get(
                "imageinfo",
                [],
            )

            if not image_info_list:
                continue

            image_info = (
                image_info_list[0]
            )

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
                    "scene_{:02d}{}"
                ).format(
                    image_index,
                    file_suffix,
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

            metadata = image_info.get(
                "extmetadata",
                {},
            )

            artist = re.sub(
                r"<[^>]+>",
                "",
                metadata.get(
                    "Artist",
                    {},
                ).get(
                    "value",
                    "",
                ),
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
            "Wikimedia画像取得失敗: %s",
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
        WORK
        / (
            "fallback_{:02d}.jpg"
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
            30 + image_index * 11 % 50,
            35,
            80 + image_index * 17 % 80,
        ),
    )

    draw = ImageDraw.Draw(
        image
    )

    heading_font = ImageFont.truetype(
        get_font_path(),
        56,
    )

    text = wrap_text(
        str(heading),
        16,
    )

    text_box = draw.multiline_textbbox(
        (0, 0),
        text,
        font=heading_font,
        spacing=14,
        align="center",
        stroke_width=3,
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
        text,
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


def acquire_fallback_images(
    scenes,
):
    """
    Pixabay動画がない場合に使う画像を用意する。
    """

    image_paths = []
    image_credits = []

    for image_index, scene in enumerate(
        scenes
    ):
        video_queries = scene.get(
            "video_queries",
            [],
        )

        if video_queries:
            search_query = str(
                video_queries[0]
            )
        else:
            search_query = str(
                scene.get(
                    "heading",
                    "",
                )
            )

        image_path, image_credit = (
            download_commons_image(
                search_query,
                image_index,
            )
        )

        if image_path is None:
            image_path = (
                create_fallback_image(
                    scene.get(
                        "heading",
                        "",
                    ),
                    image_index,
                )
            )

        image_paths.append(
            image_path
        )

        if image_credit:
            image_credits.append(
                image_credit
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
    読み上げ文章を短い単位へ分割する。
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

            clips.append(
                sentence[
                    :split_position + 1
                ].strip()
            )

            sentence = sentence[
                split_position + 1:
            ].strip()

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

    rate_percent = int(
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
                        "{:+d}%".format(
                            rate_percent
                        )
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
                "空の音声です。"
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


def create_narration(
    scenes,
):
    """
    ナレーション音声と字幕を生成する。
    """

    clip_items = []

    for scene_index, scene in enumerate(
        scenes
    ):
        narration_text = scene.get(
            "narration",
            "",
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
        for clip_index, clip_item in enumerate(
            clip_items
        ):
            output_path = (
                WORK
                / (
                    "voice_{:03d}.mp3"
                ).format(
                    clip_index
                )
            )

            log.info(
                "音声生成 %d/%d",
                clip_index + 1,
                len(clip_items),
            )

            await synthesize_audio(
                clip_item[1],
                output_path,
            )

    asyncio.run(
        generate_all_audio()
    )

    timeline = []
    audio_paths = []
    cursor = 0.5

    for clip_index, clip_item in enumerate(
        clip_items
    ):
        scene_index = clip_item[0]
        clip_text = clip_item[1]

        audio_path = (
            WORK
            / (
                "voice_{:03d}.mp3"
            ).format(
                clip_index
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
        / "subtitles.srt"
    )

    subtitle_blocks = []

    for subtitle_index, item in enumerate(
        timeline,
        start=1,
    ):
        subtitle_blocks.append(
            (
                "{}\n"
                "{} --> {}\n"
                "{}"
            ).format(
                subtitle_index,
                seconds_to_srt_time(
                    item["start"]
                ),
                seconds_to_srt_time(
                    item["end"]
                ),
                wrap_text(
                    item["text"],
                    24,
                ),
            )
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
# 動画正規化・結合
# ================================================================

def normalize_video(
    input_path,
    output_path,
    required_duration,
):
    """
    動画を1280×720、30fps、H.264へ正規化する。
    """

    run_command(
        [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(input_path),
            "-an",
            "-vf",
            (
                "scale=1280:720:"
                "force_original_aspect_ratio=increase,"
                "crop=1280:720,"
                "fps=30,"
                "format=yuv420p"
            ),
            "-t",
            "{:.3f}".format(
                required_duration
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

    return output_path


def create_final_video(
    scenes,
    scene_videos,
    fallback_images,
    narration_path,
    timeline,
    total_duration,
    subtitle_path,
):
    """
    Pixabay動画、音声、字幕を結合する。
    """

    scene_start_times = {
        0: 0.0,
    }

    for item in timeline:
        scene_number = int(
            item["scene"]
        )

        if (
            scene_number
            not in scene_start_times
        ):
            scene_start_times[
                scene_number
            ] = float(
                item["start"]
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

    normalized_clips = []

    for scene_index in range(
        len(scenes)
    ):
        scene_duration = max(
            0.5,
            boundaries[scene_index + 1]
            - boundaries[scene_index],
        )

        if scene_index < len(
            scene_videos
        ):
            available_videos = (
                scene_videos[
                    scene_index
                ]
            )
        else:
            available_videos = []

        if available_videos:
            duration_per_video = (
                scene_duration
                / len(available_videos)
            )

            for video_index, video_path in enumerate(
                available_videos
            ):
                output_path = (
                    WORK
                    / (
                        "normalized_{:02d}_{:02d}.mp4"
                    ).format(
                        scene_index,
                        video_index,
                    )
                )

                normalize_video(
                    video_path,
                    output_path,
                    duration_per_video,
                )

                normalized_clips.append(
                    output_path
                )

        else:
            output_path = (
                WORK
                / (
                    "fallback_video_{:02d}.mp4"
                ).format(
                    scene_index
                )
            )

            run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-loop",
                    "1",
                    "-i",
                    str(
                        fallback_images[
                            scene_index
                        ]
                    ),
                    "-an",
                    "-vf",
                    (
                        "scale=1280:720:"
                        "force_original_aspect_ratio=increase,"
                        "crop=1280:720,"
                        "fps=30,"
                        "format=yuv420p"
                    ),
                    "-t",
                    "{:.3f}".format(
                        scene_duration
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

            normalized_clips.append(
                output_path
            )

    if not normalized_clips:
        raise RuntimeError(
            "動画クリップが生成されませんでした。"
        )

    concat_path = (
        WORK
        / "pixabay_concat.txt"
    )

    concat_lines = []

    for clip_path in normalized_clips:
        concat_lines.append(
            "file '{}'\n".format(
                clip_path.resolve()
            )
        )

    concat_path.write_text(
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
            str(concat_path),
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

    subtitle_style = (
        "FontName=Noto Sans CJK JP,"
        "FontSize=22,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BackColour=&H64000000,"
        "BorderStyle=1,"
        "Outline=2,"
        "Shadow=1,"
        "Alignment=2,"
        "MarginV=45"
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

    subtitle_filter = (
        "subtitles='{}':"
        "force_style='{}'"
    ).format(
        escaped_subtitle_path,
        subtitle_style,
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
        WORK
        / "thumbnail.jpg"
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

    text = wrap_text(
        str(title),
        14,
    )

    text_box = draw.multiline_textbbox(
        (0, 0),
        text,
        font=title_font,
        spacing=14,
        align="center",
        stroke_width=5,
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
        text,
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


def add_pixabay_credits(
    description,
    credits,
):
    """
    Pixabayクレジットを説明欄へ追加する。
    """

    if not credits:
        return description

    lines = [
        "",
        "",
        "映像素材:",
        "Videos provided by Pixabay",
        "https://pixabay.com/",
    ]

    used_video_ids = set()

    for credit in credits:
        video_id = credit.get(
            "video_id"
        )

        if video_id in used_video_ids:
            continue

        used_video_ids.add(
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
            lines.append(
                "Video by {} on Pixabay".format(
                    creator_name
                )
            )

        if page_url:
            lines.append(
                page_url
            )

    return (
        description
        + "\n".join(lines)
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

    description = str(
        script_data.get(
            "description",
            "",
        )
    )

    image_credit_lines = []

    for credit in image_credits:
        credit_parts = []

        for key in (
            "title",
            "artist",
            "license",
            "source",
        ):
            value = credit.get(
                key,
                "",
            )

            if value:
                credit_parts.append(
                    str(value)
                )

        if credit_parts:
            image_credit_lines.append(
                "- "
                + " | ".join(
                    credit_parts
                )
            )

    if image_credit_lines:
        description += (
            "\n\n画像クレジット\n"
            + "\n".join(
                image_credit_lines
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
    Pixabay動画を使用して動画を生成する。
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

    log.info(
        "Pixabay対応動画生成を開始します。"
    )

    log.info(
        "テーマ: %s",
        theme,
    )

    log.info(
        "ジャンル: %s",
        genre,
    )

    log.info(
        "トーン: %s",
        tone,
    )

    log.info(
        "DRY_RUN: %s",
        DRY_RUN,
    )

    log.info("=" * 60)

    sources = research_theme(
        theme
    )

    log.info(
        "参考資料取得数: %d",
        len(sources),
    )

    script_data = create_script(
        FreeLLMClient(),
        theme,
        genre,
        tone,
        sources,
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

    scenes = script_data.get(
        "scenes",
        [],
    )

    if not scenes:
        raise RuntimeError(
            "台本にシーンがありません。"
        )

    scene_videos, pixabay_credits = (
        acquire_pixabay_scene_videos(
            scenes
        )
    )

    fallback_images, image_credits = (
        acquire_fallback_images(
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

    final_video_path = (
        create_final_video(
            scenes,
            scene_videos,
            fallback_images,
            narration_path,
            timeline,
            total_duration,
            subtitle_path,
        )
    )

    thumbnail_path = (
        create_thumbnail(
            script_data.get(
                "title",
                theme,
            ),
            fallback_images[0],
        )
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

    video_id = upload_to_youtube(
        final_video_path,
        thumbnail_path,
        script_data,
        image_credits,
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
                total_duration
            ),
            "youtube_url": youtube_url,
            "dry_run": DRY_RUN,
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
            "pixabay_credits": (
                pixabay_credits
            ),
            "image_credits": (
                image_credits
            ),
            "pixabay_scene_count": sum(
                1
                for videos in scene_videos
                if videos
            ),
            "total_scene_count": (
                len(scenes)
            ),
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
