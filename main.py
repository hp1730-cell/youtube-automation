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

PEXELS_API_KEY = os.getenv(
    "PEXELS_API_KEY",
    "",
).strip()

PEXELS_VIDEOS_PER_SCENE = int(
    os.getenv(
        "PEXELS_VIDEOS_PER_SCENE",
        "2",
    )
)

PEXELS_RESULTS_PER_QUERY = int(
    os.getenv(
        "PEXELS_RESULTS_PER_QUERY",
        "15",
    )
)

USED_PEXELS_VIDEO_IDS = set()

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

    for font_file in FONT_FILES:
        if Path(font_file).exists():
            return font_file

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
                "{}がインストールされていません。".format(
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

    lines = []
    position = 0

    while position < len(text):
        lines.append(
            text[
                position:
                position + width
            ]
        )

        position += width

    return "\n".join(
        lines
    )


def parse_llm_json(
    response_text,
):
    """
    LLM応答をJSONへ変換する。
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


def probe_duration(
    media_path,
):
    """
    FFprobeでメディア時間を取得する。
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
        max_attempts=5,
    ):
        """
        APIを呼び出し、
        一時エラーの場合は再試行する。
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

        response_data = self.post_with_retry(
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
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )

        choices = response_data.get(
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

        candidates = response_data.get(
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
    Web検索し、上位ページの本文を取得する。
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
    checked_count = 0

    for result in search_results:
        if checked_count >= 10:
            break

        checked_count += 1

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
    台本とPexels用の英語検索語を生成する。
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
            "具体的な数値を断定せず、"
            "一般知識として慎重に"
            "説明してください。"
        )

    if not genre:
        genre = "自動判定"

    if not tone:
        tone = (
            "わかりやすく、"
            "最後まで見たくなる雰囲気"
        )

    prompt = """
テーマ「{}」について、YouTube向けの日本語動画台本を作成してください。

ジャンル:
{}

ナレーションの雰囲気:
{}

条件:
・8〜10分を目標にする
・3800〜4800日本語文字を目安にする
・9〜11シーンで構成する
・冒頭15秒に疑問か意外性を置く
・資料で確認できる事実だけを断定する
・同じ説明を繰り返さない
・narrationには読み上げ本文だけを入れる
・titleは45文字以内にする
・JSON以外の文章を返さない

Pexels動画検索語の条件:
・各シーンにvideo_queriesを必ず3個付ける
・video_queriesは英語のみ
・主役、行動、場所が分かる検索語にする
・実写の横長動画が見つかりやすい表現にする
・企業ロゴ、商品名、映画名、著名人名を入れない
・3個の検索語は異なる映像になるようにする

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
        genre,
        tone,
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

        if not scene.get(
            "heading"
        ):
            scene["heading"] = (
                "シーン{}".format(
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

        cleaned_queries = []

        for query in video_queries:
            query = str(
                query
            ).strip()

            if query:
                cleaned_queries.append(
                    query
                )

        fallback_query = (
            "{} documentary footage"
        ).format(
            theme
        )

        while len(cleaned_queries) < 3:
            cleaned_queries.append(
                fallback_query
            )

        scene["video_queries"] = (
            cleaned_queries[:3]
        )

    source_urls = []

    for source in sources:
        source_url = source.get(
            "url",
            "",
        )

        if source_url:
            source_urls.append(
                "- {}".format(
                    source_url
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
            + "\n".join(
                source_urls
            )
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
        *User-Agent": (
            "YouTub*AutoPipeline/1.0"
        )
    }
*    try:
        response = reques*s.get(
            (
             *  "https://commons.wikimedia.org/"*                "w/api.php"
      *     ),
            params=paramet*rs,
            headers=headers,
 *          timeout=30,
        )

 *      response.raise_for_status()
*        pages = list(
            *esponse.json().get(
              * "query",
                {},
    *       ).get(
                "pag*s",
                {},
          * ).values()
        )

        for*page in pages:
            image_i*fo_list = page.get(
              * "imageinfo",
                [],
*           )

            if not i*age_info_list:
                con*inue

            image_info = (
 *              image_info_list[0]
 *          )

            image_url*= (
                image_info.get*
                    "thumburl"
  *             )
                or *mage_info.get(
                   *"url"
                )
          * )

            if not image_url:
*               continue

         *  file_suffix = Path(
            *   urlparse(
                    i*age_url
                ).path
   *        ).suffix.lower()

        *   allowed_suffixes = (
          *     ".jpg",
                ".jpe*",
                ".png",
       *        ".webp",
            )

  *         if (
                file*suffix
                not in allo*ed_suffixes
            ):
       *        file_suffix = ".jpg"

    *       output_path = (
           *    WORK
                / (
     *              "scene_{:02d}{}"
   *            ).format(
            *       image_index,
              *     file_suffix,
                *
            )

            image_*esponse = requests.get(
          *     image_url,
                he*ders=headers,
                time*ut=60,
            )

            *mage_response.raise_for_status()

*           output_path.write_bytes*
                image_response.co*tent
            )

            tr*:
                with Image.open(*                    output_path
  *             ) as image:
         *          image.verify()

        *       with Image.open(
          *         output_path
             *  ) as image:
                    *f (
                        image.*idth < 400
                       *or image.height < 300
            *       ):
                        *utput_path.unlink(
               *            missing_ok=True
      *                 )

              *         continue

            exc*pt Exception:
                outp*t_path.unlink(
                   *missing_ok=True
                )
*                continue

        *   metadata = image_info.get(
    *           "extmetadata",
        *       {},
            )

        *   artist_html = str(
            *   metadata.get(
                 *  "Artist",
                    {}*
                ).get(
          *         "value",
                *   "",
                )
         *  )

            artist = re.sub(
*               r"<[^>]+>",
       *        "",
                artist*html,
            )

            i*age_credit = {
                "ti*le": str(
                    page*get(
                        "titl*",
                        "",
   *                )
                *,
                "source": str(
 *                  image_info.get(
*                       "descriptio*url",
                        imag*_url,
                    )
      *         ),
                "licen*e": str(
                    metad*ta.get(
                        "L*censeShortName",
                 *      {},
                    ).ge*(
                        "value",*                        "",
      *             )
                ),
*               "artist": (
       *            clean_text(
          *             artist
              *     )[:200]
                ),
  *         }

            return (
 *              output_path,
       *        image_credit,
            *

    except Exception as error:
 *      log.warning(
            "Wi*imedia画像取得失敗: %s",
            err*r,
        )

    return None, Non*


def create_fallback_image(
    *eading,
    image_index,
):
    ""*
    Pillowでフォールバック背景を生成する。
    ""*

    output_path = (
        WORK*        / (
            "fallback_*:02d}.jpg"
        ).format(
     *      image_index
        )
    )
*    image = Image.new(
        "RG*",
        (
            WIDTH,
  *         HEIGHT,
        ),
      * (
            25 + image_index * *3 % 55,
            35,
          * 75 + image_index * 19 % 90,
     *  ),
    )

    draw = ImageDraw.D*aw(
        image
    )

    headi*g_font = ImageFont.truetype(
     *  get_font_path(),
        56,
   *)

    wrapped_heading = wrap_text*
        str(heading),
        16,*    )

    text_box = draw.multili*e_textbbox(
        (0, 0),
      * wrapped_heading,
        font=hea*ing_font,
        spacing=14,
    *   align="center",
        stroke_*idth=3,
    )

    text_width = (
*       text_box[2]
        - text_*ox[0]
    )

    text_height = (
 *      text_box[3]
        - text_b*x[1]
    )

    x_position = (
   *    WIDTH - text_width
    ) / 2

*   y_position = (
        HEIGHT -*text_height
    ) / 2

    draw.mu*tiline_text(
        (
           *x_position,
            y_position*
        ),
        wrapped_headin*,
        font=heading_font,
     *  fill="white",
        spacing=14*
        align="center",
        s*roke_width=4,
        stroke_fill=*black",
    )

    image.save(
   *    output_path,
        quality=9*,
    )

    return output_path


*ef acquire_fallback_images(
    sc*nes,
):
    """
    Pexels取得失敗時に使用*る画像を用意する。
    """

    image_paths*= []
    image_credits = []

    f*r image_index, scene in enumerate(*        scenes
    ):
        vide*_queries = scene.get(
            *video_queries",
            [],
  *     )

        if video_queries:
*           search_query = str(
   *            video_queries[0]
     *      )
        else:
            *earch_query = str(
               *scene.get(
                    "he*ding",
                    "",
   *            )
            )

     *  image_path, image_credit = (
   *        download_commons_image(
  *             search_query,
       *        image_index,
            )*        )

        if image_path i* None:
            image_path = (
*               create_fallback_ima*e(
                    scene.get(
*                       "heading",
*                       "",
       *            ),
                   *image_index,
                )
   *        )

        image_paths.app*nd(
            image_path
       *)

        if image_credit:
      *     image_credits.append(
       *        image_credit
            )*
    return (
        image_paths,*        image_credits,
    )


# =*==================================*===========================
# Pexe*s動画取得
# ==========================*==================================*==

def choose_pexels_video_file(
*   video_files,
):
    """
    Pex*ls動画の中から横長HD素材を選ぶ。
    """

    ca*didates = []

    for video_file i* video_files:
        width = int(*            video_file.get(
      *         "width",
                *,
            )
            or 0
 *      )

        height = int(
   *        video_file.get(
          *     "height",
                0,
*           )
            or 0
    *   )

        file_type = str(
   *        video_file.get(
          *     "file_type",
                *",
            )
        ).lower()*
        file_link = str(
        *   video_file.get(
               *"link",
                "",
      *     )
        )

        if not f*le_link:
            continue

   *    if width <= height:
          * continue

        if width < 1280*
            continue

        if *eight < 720:
            continue
*        if "mp4" not in file_type:*            continue

        aspe*t_difference = abs(
            (w*dth / height)
            - (16 / *)
        )

        resolution_di*ference = abs(
            width -*1920
        )

        score = (
*           aspect_difference * 100*0
            + resolution_differe*ce
        )

        candidates.a*pend(
            (
              * score,
                file_link,*                width,
           *    height,
            )
        *

    if not candidates:
        r*turn None

    candidates.sort(
  *     key=lambda item: item[0]
    *

    selected = candidates[0]

  * return {
        "link": selected*1],
        "width": selected[2],
*       "height": selected[3],
    *


def search_pexels_videos(
    q*ery,
):
    """
    Pexels APIで横長動*を検索する。
    """

    if not PEXELS_*PI_KEY:
        log.warning(
     *      "PEXELS_API_KEYが未設定のため、"
   *        "Pexels検索をスキップします。"
      * )

        return []

    try:
  *     response = requests.get(
    *       (
                "https://*pi.pexels.com/"
                "v*/videos/search"
            ),
   *        headers={
                *Authorization": (
                *   PEXELS_API_KEY
                *
            },
            params*{
                "query": query,
*               "orientation": "lan*scape",
                "size": "m*dium",
                "per_page":*(
                    PEXELS_RESUL*S_PER_QUERY
                ),
   *            "page": 1,
           *},
            timeout=45,
       *)

        if response.status_code*== 429:
            log.warning(
 *              "Pexels APIの上限に達しました*"
            )

            retur* []

        response.raise_for_st*tus()

        remaining = respons*.headers.get(
            "X-Ratel*mit-Remaining",
            "不明",
*       )

        log.info(
      *     "Pexels API残りリクエスト数: %s",
   *        remaining,
        )

    *   return response.json().get(
   *        "videos",
            [],
*       )

    except Exception as *rror:
        log.warning(
       *    "Pexels検索失敗: %s",
            *rror,
        )

        return []*


*ef download_pexels_video(
    vide*,
    scene_index,
    video_index*
):
    """
    選択したPexels動画をダウンロー*する。
    """

    video_id = int(
 *      video.get(
            "id",*            0,
        )
        o* 0
    )

    if not video_id:
   *    return None, None

    if (
  *     video_id
        in USED_PEXE*S_VIDEO_IDS
    ):
        return *one, None

    selected_file = (
 *      choose_pexels_video_file(
  *         video.get(
              * "video_files",
                []*
            )
        )
    )

  * if not selected_file:
        ret*rn None, None

    output_path = (*        WORK
        / (
         *  "pexels_"
            "{:02d}_"
*           "{:02d}.mp4"
        ).*ormat(
            scene_index,
  *         video_index,
        )
  * )

    try:
        response = re*uests.get(
            selected_fi*e["link"],
            stream=True*
            timeout=120,
        *

        response.raise_for_statu*()

        with output_path.open(*            "wb"
        ) as outp*t_file:
            for chunk in r*sponse.iter_content(
             *  chunk_size=1024 * 1024,
        *   ):
                if chunk:
  *                 output_file.write*
                        chunk
   *                )

        if (
  *         not output_path.exists()
*           or output_path.stat().s*_size
            < 100000
       *):
            output_path.unlink(*                missing_ok=True
  *         )

            return Non*, None

        video_duration = p*obe_duration(
            output_p*th
        )

        if video_dur*tion < 1:
            output_path.*nlink(
                missing_ok=*rue
            )

            ret*rn None, None

        USED_PEXELS*VIDEO_IDS.add(
            video_i*
        )

        user_data = vi*eo.get(
            "user",
      *     {},
        )

        credit*= {
            "provider": "Pexel*",
            "video_id": video_i*,
            "video_url": str(
  *             video.get(
          *         "url",
                  * "",
                )
           *),
            "creator_name": str*
                user_data.get(
  *                 "name",
         *          "",
                )
  *         ),
            "creator_u*l": str(
                user_data*get(
                    "url",
  *                 "",
             *  )
            ),
            "wi*th": selected_file["width"],
     *      "height": selected_file["hei*ht"],
        }

        return (
*           output_path,
          * credit,
        )

    except Exc*ption as error:
        log.warnin*(
            "Pexels動画取得失敗: %s",
*           error,
        )

     *  output_path.unlink(
            *issing_ok=True
        )

        *eturn None, None


def acquire_pex*ls_scene_videos(
    scenes,
):
  * """
    各シーンにつき最大2本のPexels動画を取得する*
    """

    all_scene_videos = [*
    all_credits = []

    for sce*e_index, scene in enumerate(
     *  scenes
    ):
        scene_vide*s = []

        video_queries = sc*ne.get(
            "video_queries*,
            [],
        )

     *  if not isinstance(
            v*deo_queries,
            list,
   *    ):
            video_queries =*[]

        log.info(
            *Pexels動画取得 %d/%d",
            sce*e_index + 1,
            len(scene*),
        )

        for query in*video_queries:
            query =*str(
                query
       *    ).strip()

            if not *uery:
                continue

  *         search_results = (
      *         search_pexels_videos(
   *                query
            *   )
            )

            fo* video in search_results:
        *       video_path, credit = (
    *               download_pexels_vid*o(
                        video,
*                       scene_index*
                        len(scene*videos),
                    )
   *            )

                if *ideo_path:
                    sce*e_videos.append(
                 *      video_path
                 *  )

                    if credit*
                        credit["q*ery"] = query

                   *    all_credits.append(
          *                 credit
          *             )

                if*(
                    len(scene_vi*eos)
                    >= PEXELS*VIDEOS_PER_SCENE
                )*
                    break

      *     if (
                len(scen*_videos)
                >= PEXELS*VIDEOS_PER_SCENE
            ):
  *             break

        all_sc*ne_videos.append(
            scen*_videos
        )

        log.inf*(
            "シーン%dのPexels素材: %d本*,
            scene_index + 1,
   *        len(scene_videos),
       *)

    credits_path = (
        WO*K
        / "pexels_credits.json"
*   )

    credits_path.write_text(*        json.dumps(
            al*_credits,
            ensure_ascii*False,
            indent=2,
     *  ),
        encoding="utf-8",
   *)

    return (
        all_scene_*ideos,
        all_credits,
    )
*
# ===============================*================================
#*ナレーション
# =========================*==================================*===

def split_narration(
    text*
    max_characters=70,
):
    """*    読み上げ文章を短い単位へ分割する。
    """

   *clips = []

    sentences = re.spl*t(
        r"(?<=[。！？!?])",
      * str(text),
    )

    for sentenc* in sentences:
        sentence = *entence.strip()

        if not se*tence:
            continue

     *  while (
            len(sentence*
            > max_characters
    *   ):
            split_position =*max(
                sentence.rfin*(
                    "、",
       *            0,
                   *max_characters,
                ),*                sentence.rfind(
  *                 "，",
            *       0,
                    max_*haracters,
                ),
    *       )

            if split_pos*tion < 20:
                split_p*sition = (
                    max*characters - 1
                )

*           clip_text = sentence[
 *              :split_position + 1
*           ].strip()

            *entence = sentence[
              * split_position + 1:
            ]*strip()

            if clip_text:*                clips.append(
    *               clip_text
         *      )

        if sentence:
    *       clips.append(
             *  sentence
            )

    retu*n clips


async def synthesize_aud*o(
    text,
    output_path,
):
 *  """
    Edge TTSで音声を生成する。
    ""*

    speed_percent = int(
       *(NARRATION_SPEED - 1)
        * 10*
    )

    last_error = None

   *for attempt in range(4):
        t*y:
            communicator = (
  *             edge_tts.Communicate(*                    text,
        *           VOICE,
                *   rate=(
                        *{:+d}%".format(
                  *         speed_percent
           *            )
                    *,
                )
            )
*            await communicator.sav*(
                str(output_path)*            )

            if (
  *             output_path.exists()
*               and output_path.sta*().st_size
                > 1000
*           ):
                retu*n

            raise RuntimeError(*                "生成された音声が空です。"
   *        )

        except Exceptio* as error:
            last_error * error

            if attempt < 3*
                await asyncio.sle*p(
                    2 ** attemp*
                )

    raise Runt*meError(
        "音声生成に失敗しました: {}"*format(
            last_error
   *    )
    )


def create_narration*
    scenes,
):
    """
    ナレーション*声と字幕を生成する。
    """

    clip_items*= []

    for scene_index, scene i* enumerate(
        scenes
    ):
*       narration_text = clean_text*
            scene.get(
          *     "narration",
                *",
            )
        )

      * for clip_text in split_narration(*            narration_text
       *):
            clip_items.append(
*               (
                 *  scene_index,
                   *clip_text,
                )
     *      )

    if not clip_items:
  *     raise RuntimeError(
         *  "読み上げ本文がありません。"
        )

    a*ync def generate_all_audio():
    *   for clip_index, clip_item in en*merate(
            clip_items
   *    ):
            output_path = (*                WORK
             *  / (
                    "voice_{*03d}.mp3"
                ).format*
                    clip_index
  *             )
            )

    *       log.info(
                "*声生成 %d/%d",
                clip_i*dex + 1,
                len(clip_*tems),
            )

            *wait synthesize_audio(
           *    clip_item[1],
                *utput_path,
            )

    asy*cio.run(
        generate_all_audi*()
    )

    timeline = []
    au*io_paths = []
    cursor = 0.5

  * for clip_index, clip_item in enum*rate(
        clip_items
    ):
  *     scene_index = clip_item[0]
  *     clip_text = clip_item[1]

   *    audio_path = (
            WOR*
            / (
                "*oice_{:03d}.mp3"
            ).for*at(
                clip_index
   *        )
        )

        clip_*uration = probe_duration(
        *   audio_path
        )

        t*meline.append(
            {
     *          "scene": scene_index,
  *             "text": clip_text,
  *             "start": cursor,
    *           "end": (
              *     cursor
                    + *lip_duration
                ),
  *         }
        )

        curs*r += (
            clip_duration
 *          + 0.15
        )

      * audio_paths.append(
            a*dio_path
        )

    concat_fil* = (
        WORK
        / "audio*concat.txt"
    )

    concat_line* = []

    for audio_path in audio*paths:
        concat_lines.append*
            "file '{}'\n".format(*                audio_path.resolve*)
            )
        )

    con*at_file.write_text(
        "".joi*(
            concat_lines
       *),
        encoding="utf-8",
    )*
    narration_path = (
        WO*K
        / "narration.m4a"
    )
*    run_command(
        [
       *    "ffmpeg",
            "-y",
  *         "-f",
            "concat*,
            "-safe",
           *"0",
            "-i",
           *str(concat_file),
            "-c:*",
            "aac",
            *-b:a",
            "160k",
       *    str(narration_path),
        ]*    )

    subtitle_path = (
     *  WORK
        / "subtitles.srt"
 *  )

    subtitle_blocks = []

   *for subtitle_index, item in enumer*te(
        timeline,
        star*=1,
    ):
        subtitle_blocks*append(
            (
            *   "{}\n"
                "{} --> *}\n"
                "{}"
        *   ).format(
                subti*le_index,
                seconds_*o_srt_time(
                    it*m["start"]
                ),
    *           seconds_to_srt_time(
  *                 item["end"]
     *          ),
                wrap_*ext(
                    item["tex*"],
                    24,
      *         ),
            )
        *

    subtitle_path.write_text(
  *     "\n\n".join(
            subt*tle_blocks
        ),
        enco*ing="utf-8",
    )

    return (
 *      narration_path,
        time*ine,
        cursor,
        subti*le_path,
    )


# ===============*==================================*=============
# Pexels動画の正規化
# ===*==================================*=========================

def nor*alize_pexels_video(
    input_path*
    output_path,
    required_dur*tion,
):
    """
    Pexels動画を1280*720、30fps、H.264へ統一する。
    """

   *run_command(
        [
           *"ffmpeg",
            "-y",
      *     "-stream_loop",
            "*1",
            "-i",
            *tr(input_path),
            "-an",*            "-vf",
            (
 *              "scale=1280:720:"
  *             "force_original_aspec*_ratio=increase,"
                *crop=1280:720,"
                "f*s=30,"
                "format=yuv*20p"
            ),
            "-*",
            "{:.3f}".format(
  *             required_duration
   *        ),
            "-c:v",
   *        "libx264",
            "-p*eset",
            "veryfast",
   *        "-crf",
            "22",
*           "-pix_fmt",
           *"yuv420p",
            "-r",
     *      "30",
            "-movflags*,
            "+faststart",
      *     str(output_path),
        ]
 *  )

    if not output_path.exists*):
        raise RuntimeError(
   *        "Pexels動画の正規化に失敗しました。"
   *    )

    return output_path
    

# ===*==================================*=========================
# 最終動画生成*# ================================*===============================

d*f create_video_from_pexels(
    sc*nes,
    scene_videos,
    fallbac*_images,
    narration_path,
    t*meline,
    total_duration,
    su*title_path,
):
    """
    Pexels動*を中心に最終動画を生成する。
    """

    scene_*tart_times = {
        0: 0.0,
   *}

    for item in timeline:
     *  scene_number = int(
            *tem.get(
                "scene",
*               0,
            )
  *     )

        if (
            s*ene_number
            not in scen*_start_times
        ):
          * scene_start_times[
              * scene_number
            ] = floa*(
                item.get(
      *             "start",
            *       0,
                )
      *     )

    boundaries = []
    pr*vious_start = 0.0

    for scene_i*dex in range(
        len(scenes)
*   ):
        previous_start = (
 *          scene_start_times.get(
 *              scene_index,
       *        previous_start,
          * )
        )

        boundaries.a*pend(
            previous_start
 *      )

    boundaries.append(
  *     total_duration + 1
    )

   *normalized_clips = []

    for sce*e_index in range(
        len(scen*s)
    ):
        scene_duration =*max(
            0.5,
            *oundaries[scene_index + 1]
       *    - boundaries[scene_index],
   *    )

        available_videos = *]

        if scene_index < len(
 *          scene_videos
        ):
*           available_videos = (
  *             scene_videos[
       *            scene_index
          *     ]
            )

        if a*ailable_videos:
            durati*n_per_video = (
                sc*ne_duration
                / len(*vailable_videos)
            )

  *         for video_index, video_pa*h in enumerate(
                av*ilable_videos
            ):
     *          normalized_path = (
    *               WORK
              *     / (
                        "*ormalized_"
                      * "{:02d}_"
                       *"{:02d}.mp4"
                    )*format(
                        sc*ne_index,
                        *ideo_index,
                    )
*               )

                *ormalize_pexels_video(
           *        video_path,
              *     normalized_path,
            *       duration_per_video,
       *        )

                normali*ed_clips.append(
                 *  normalized_path
                *

        else:
            fallba*k_video_path = (
                W*RK
                / (
           *        "fallback_video_"
        *           "{:02d}.mp4"
          *     ).format(
                   *scene_index
                )
    *       )

            run_command(*                [
                *   "ffmpeg",
                    "*y",
                    "-loop",
 *                  "1",
           *        "-i",
                    *tr(
                        fallba*k_images[
                        *   scene_index
                   *    ]
                    ),
     *              "-an",
             *      "-vf",
                    (*                        "scale=1280:720:"
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
                    str(
                        fallback_video_path
                    ),
                ]
            )

            normalized_clips.append(
                fallback_video_path
            )

    if not normalized_clips:
        raise RuntimeError(
            "動画クリップが生成されませんでした。"
        )

    concat_file = (
        WORK
        / "pexels_concat.txt"
    )

    concat_lines = []

    for clip_path in normalized_clips:
        concat_lines.append(
            "file '{}'\n".format(
                clip_path.resolve()
            )
        )

    concat_file.write_text(
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
            str(concat_file),
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
        "subtitles='{}':"
        "force_style='{}'"
    ).format(
        resolved_subtitle_path,
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
    YouTubeサムネイルを生成する。
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


def add_pexels_credits(
    description,
    pexels_credits,
):
    """
    Pexelsクレジットを概要欄へ追加する。
    """

    if not pexels_credits:
        return description

    lines = [
        "",
        "",
        "映像素材:",
        "Videos provided by Pexels",
        "https://www.pexels.com/",
    ]

    used_video_ids = set()

    for credit in pexels_credits:
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

        video_url = credit.get(
            "video_url",
            "",
        )

        if creator_name:
            lines.append(
                "Video by {} on Pexels".format(
                    creator_name
                )
            )

        if video_url:
            lines.append(
                video_url
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
    TOKEN_JSONから認証情報を作る。
    """

    token_information = (
        get_json_secret(
            "TOKEN_JSON"
        )
    )

    credentials = (
        Credentials
        .from_authorized_user_info(
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
# メイン
# ================================================================

def main():
    """
    Pexels動画を使用して動画を生成する。
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

    script_data = create_script(
        llm,
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

    (
        scene_videos,
        pexels_credits,
    ) = acquire_pexels_scene_videos(
        scenes
    )

    (
        fallback_images,
        image_credits,
    ) = acquire_fallback_images(
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

    final_video_path = (
        create_video_from_pexels(
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
        add_pexels_credits(
            str(
                script_data.get(
                    "description",
                    "",
                )
            ),
            pexels_credits,
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
            "requested_genre": genre,
            "requested_tone": tone,
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
            "pexels_credits": (
                pexels_credits
            ),
            "image_credits": (
                image_credits
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
