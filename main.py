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

USED_PIXABAY_VIDEO_IDS = set()

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
        "日本語フォントが見つかりません。"
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
    台本とPixabay向け英語検索語を生成する。
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

Pixabay動画検索語の条件:
・各シーンにvideo_queriesを必ず3個付ける
・video_queriesは英語のみ
・主役、行動、場所が分かる検索語にする
・実写の横長動画が見つかりやすい表現にする
・企業ロゴ、商品名、映画名、著名人名を入れない
・3個の検索語は異なる映像になるようにする
・検索語は100文字以内にする

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
                    query[:100]
                )

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

        for fallback_query in fallback_queries:
            if len(cleaned_queries) >= 3:
                break

            cleaned_queries.append(
                fallback_query[:100]
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

            allowed_suffixes = (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
            )

            if (
                file_suffix
                not in allowed_suffixes
            ):
                file_suffix = ".jpg"

            output_path = (
                WORK_DIR
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
            "Wikimedia画像取得失敗: %s",
            error,
        )

    return None, None


def create_fallback_image(
    heading,
    image_index,
):
    """
    Pillowでフォールバック背景を生成する。
    """

    output_path = (
        WORK_DIR
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
        text_box[2]
        - text_box[0]
    )

    text_height = (
        text_box[3]
        - text_box[1]
    )

    x_position = (
        WIDTH - text_width
    ) / 2

    y_position = (
        HEIGHT - text_height
    ) / 2

    draw.multiline_text(
        (
            x_position,
            y_position,
        ),
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
# Pixabay動画検索
# ================================================================

def choose_pixabay_video_file(
    video_data,
):
    """
    Pixabay検索結果から横長HD素材を選ぶ。
    """

    videos = video_data.get(
        "videos",
        {},
    )

    candidates = []

    for quality_name in (
        "medium",
        "large",
        "small",
        "tiny",
    ):
        video_file = videos.get(
            quality_name,
            {},
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

        aspect_difference = abs(
            (width / height)
            - (16 / 9)
        )

        if (
            width >= 1280
            and height >= 720
        ):
            resolution_penalty = 0
        else:
            resolution_penalty = 10000

        if quality_name == "medium":
            quality_penalty = 0
        elif quality_name == "large":
            quality_penalty = 100
        elif quality_name == "small":
            quality_penalty = 20000
        else:
            quality_penalty = 30000

        score = (
            aspect_difference * 10000
            + resolution_penalty
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
    同一実行中の同じ検索語はキャッシュする。
    """

    normalized_query = str(
        query
    ).strip().lower()

    if not normalized_query:
        return []

    if normalized_query in PIXABAY_SEARCH_CACHE:
        log.info(
            "Pixabay検索キャッシュを使用: %s",
            normalized_query,
        )

        return PIXABAY_SEARCH_CACHE[
            normalized_query
        ]

    if not PIXABAY_API_KEY:
        log.warning(
            "PIXABAY_API_KEYが未設定のため、"
            "Pixabay検索をスキップします。"
        )

        return []

    try:
        response = requests.get(
            (
                "https://pixabay.com/"
                "api/videos/"
            ),
            params={
                "key": PIXABAY_API_KEY,
                "q": normalized_query[:100],
                "lang": "en",
                "video_type": "film",
                "min_width": 1280,
                "min_height": 720,
                "safesearch": "true",
                "order": "popular",
                "per_page": (
                    PIXABAY_RESULTS_PER_QUERY
                ),
                "page": 1,
            },
            timeout=45,
        )

        if response.status_code == 429:
            log.warning(
                "Pixabay APIの"
                "レート上限に達しました。"
            )

            return []

        response.raise_for_status()

        remaining = response.headers.get(
            "X-RateLimit-Remaining",
            "不明",
        )

        log.info(
            "Pixabay API残りリクエスト数: %s",
            remaining,
        )

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

    if (
        video_id
        in USED_PIXABAY_VIDEO_IDS
    ):
        return None, None

    selected_file = (
        choose_pixabay_video_file(
            video_data
        )
    )

    if not selected_file:
        return None, None

    output_path = (
        WORK_DIR
        / (
            "pixabay_"
            "{:02d}_"
            "{:02d}.mp4"
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

        video_duration = probe_duration(
            output_path
        )

        if video_duration < 1:
            output_path.unlink(
                missing_ok=True
            )

            return None, None

        USED_PIXABAY_VIDEO_IDS.add(
            video_id
        )

        user_name = str(
            video_data.get(
                "user",
                "",
            )
        )

        user_id = str(
            video_data.get(
                "user_id",
                "",
            )
        )

        if user_name and user_id:
            user_url = (
                "https://pixabay.com/users/"
                "{}-{}/"
            ).format(
                user_name,
                user_id,
            )
        else:
            user_url = ""

        credit = {
            "provider": "Pixabay",
            "video_id": video_id,
            "page_url": str(
                video_data.get(
                    "pageURL",
                    "",
                )
            ),
            "creator_name": user_name,
            "creator_url": user_url,
            "tags": str(
                video_data.get(
                    "tags",
                    "",
                )
            ),
            "width": selected_file["width"],
            "height": selected_file["height"],
            "quality": (
                selected_file["quality"]
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


def acquire_pixabay_scene_videos(
    scenes,
):
    """
    各シーンにつき最大2本のPixabay動画を取得する。
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

        log.info(
            "Pixabay動画取得 %d/%d",
            scene_index + 1,
            len(scenes),
        )

        for query in video_queries:
            query = str(
                query
            ).strip()

            if not query:
                continue

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
        WORK_DIR
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
                        "{:+d}%".format(
                            speed_percent
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
                "生成された音声が空です。"
            )

        except Exception as error:
            last_error = error

            if attempt < 3:
                await asyncio.sleep(
                    2 ** attempt
                )

    raise RuntimeError(
        "音声生成に失敗しました: {}".format(
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
        narration_text = clean_text(
            scene.get(
                "narration",
                "",
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

    async def generate_all_audio():
        for clip_index, clip_item in enumerate(
            clip_items
        ):
            output_path = (
                WORK_DIR
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
            WORK_DIR
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

    concat_file = (
        WORK_DIR
        / "audio_concat.txt"
    )

    concat_lines = []

    for audio_path in audio_paths:
        concat_lines.append(
            "file '{}'\n".format(
                audio_path.resolve()
            )
        )

    concat_file.write_text(
        "".join(
            concat_lines
        ),
        encoding="utf-8",
    )

    narration_path = (
        WORK_DIR
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
            str(concat_file),
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(narration_path),
        ]
    )

    subtitle_path = (
        WORK_DIR
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
# Pixabay動画の正規化
# ================================================================

def normalize_pixabay_video(
    input_path,
    output_path,
    required_duration,
):
    """
    Pixabay動画を1280×720、30fps、H.264へ統一する。
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
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )

    if not output_path.exists():
        raise RuntimeError(
            "Pixabay動画の正規化に失敗しました。"
        )

    return output_path


# ================================================================
# 最終動画生成
# ================================================================

def create_video_from_pixabay(
    scenes,
    scene_videos,
    fallback_images,
    narration_path,
    timeline,
    total_duration,
    subtitle_path,
):
    """
    Pixabay動画を中心に最終動画を生成する。
    """

    scene_start_times = {
        0: 0.0,
    }

    for item in timeline:
        scene_number = int(
            item.get(
                "scene",
                0,
            )
        )

        if (
            scene_number
            not in scene_start_times
        ):
            scene_start_times[
                scene_number
            ] = float(
                item.get(
                    "start",
                    0,
                )
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
        total_duration + 1
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

        available_videos = []

        if scene_index < len(
            scene_videos
        ):
            available_videos = (
                scene_videos[
                    scene_index
                ]
            )

        if available_videos:
            duration_per_video = (
                scene_duration
                / len(available_videos)
            )

            for video_index, video_path in enumerate(
                available_videos
            ):
                normalized_path = (
                    WORK_DIR
                    / (
                        "normalized_"
                        "{:02d}_"
                        "{:02d}.mp4"
                    ).format(
                        scene_index,
                        video_index,
                    )
                )

                normalize_pixabay_video(
                    video_path,
                    normalized_path,
                    duration_per_video,
                )

                normalized_clips.append(
                    normalized_path
                )

        else:
            fallback_video_path = (
                WORK_DIR
                / (
                    "fallback_video_"
                    "{:02d}.mp4"
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
        WORK_DIR
        / "pixabay_concat.txt"
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
        WORK_DIR
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
        WORK_DIR
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
        WORK_DIR
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
        WORK_DIR
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


def add_pixabay_credits(
    description,
    pixabay_credits,
):
    """
    Pixabayクレジットを概要欄へ追加する。
    """

    if not pixabay_credits:
        return description

    lines = [
        "",
        "",
        "映像素材:",
        "Videos provided by Pixabay",
        "https://pixabay.com/",
    ]

    used_video_ids = set()

    for credit in pixabay_credits:
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

    script_data = create_script(
        llm,
        theme,
        genre,
        tone,
        sources,
    )

    script_path = (
        WORK_DIR
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
        pixabay_credits,
    ) = acquire_pixabay_scene_videos(
        scenes
    )

    scenes_with_video = sum(
        1
        for videos in scene_videos
        if videos
    )

    log.info(
        "Pixabay動画取得成功シーン: %d/%d",
        scenes_with_video,
        len(scenes),
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
        create_video_from_pixabay(
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
            "pixabay_credits": (
                pixabay_credits
            ),
            "image_credits": (
                image_credits
            ),
            "pixabay_scene_count": (
                scenes_with_video
            ),
            "total_scene_count": (
                len(scenes)
            ),
        }
    )

    metadata_path = (
        WORK_DIR
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
