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

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from json_repair import repair_json
from PIL import Image, ImageDraw, ImageEnhance, ImageFont


VERSION = "2026.10.02-groq-tpm-fix-v2"

WORK = Path(os.getenv("WORK_DIR", "work"))
WORK.mkdir(parents=True, exist_ok=True)

W = 1280
H = 720
FPS = 30

MIN_SEC = 480
TARGET_SEC = 540
MAX_SEC = 600

MIN_CUT = 4.0
MAX_CUT = 7.0

VOICE = os.getenv(
    "EDGE_VOICE",
    "ja-JP-NanamiNeural",
)

RATE = os.getenv(
    "EDGE_VOICE_RATE",
    "+0%",
)

DRY = os.getenv(
    "DRY_RUN",
    "true",
).lower() == "true"

UPLOAD = os.getenv(
    "UPLOAD_TO_YOUTUBE",
    "true",
).lower() == "true"

PRIVACY = os.getenv(
    "PRIVACY_STATUS",
    "private",
)

GROQ_MODEL = os.getenv(
    "GROQ_MODEL",
    "openai/gpt-oss-120b",
)

GROQ_MAX_TOKENS = int(
    os.getenv(
        "GROQ_MAX_TOKENS",
        "4600",
    )
)

RESEARCH_SOURCE_LIMIT = int(
    os.getenv(
        "RESEARCH_SOURCE_LIMIT",
        "3",
    )
)

RESEARCH_CHARS_PER_SOURCE = int(
    os.getenv(
        "RESEARCH_CHARS_PER_SOURCE",
        "650",
    )
)

PIXABAY_KEY = os.getenv(
    "PIXABAY_API_KEY",
    "",
).strip()

RESULTS = int(
    os.getenv(
        "PIXABAY_RESULTS_PER_QUERY",
        "20",
    )
)

BGM = Path(
    os.getenv(
        "BGM_PATH",
        "assets/background_music.mp3",
    )
)

USED_IDS = set()
USED_URLS = set()
USED_PAGES = set()
CACHE = {}

FONTS = [
    "/usr/share/fonts/opentype/noto/"
    "NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/"
    "NotoSansCJK-Regular.ttc",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("youtube-auto")


def run(cmd, timeout=1800):
    log.info(
        "コマンド実行: %s",
        cmd[0],
    )

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode:
        raise RuntimeError(
            "{}失敗\n{}".format(
                cmd[0],
                result.stderr[-5000:],
            )
        )

    return result


def font():
    for path in FONTS:
        if Path(path).exists():
            return path

    raise RuntimeError(
        "日本語フォントがありません。"
        "fonts-noto-cjkを導入してください。"
    )


def clean(value):
    value = html.unescape(
        str(value or "")
    )

    value = re.sub(
        r"<[^>]+>",
        "",
        value,
    )

    value = re.sub(
        r"https?://\S+",
        "",
        value,
    )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def duration(path):
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        timeout=60,
    )

    output = result.stdout.strip()

    if not output:
        raise RuntimeError(
            "動画または音声の長さを取得できません: {}".format(
                path
            )
        )

    return float(output)


def secret(name):
    raw = os.getenv(
        name,
        "",
    ).strip()

    if not raw:
        raise RuntimeError(
            name + "が未設定です。"
        )

    return json.loads(raw)


def ass_time(seconds):
    centiseconds = max(
        0,
        int(round(seconds * 100)),
    )

    hours, centiseconds = divmod(
        centiseconds,
        360000,
    )

    minutes, centiseconds = divmod(
        centiseconds,
        6000,
    )

    seconds, centiseconds = divmod(
        centiseconds,
        100,
    )

    return "{}:{:02d}:{:02d}.{:02d}".format(
        hours,
        minutes,
        seconds,
        centiseconds,
    )


def esc(value):
    return (
        str(value)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )


def wrap(value, length):
    value = clean(value)

    if not value:
        return ""

    return "\n".join(
        value[index:index + length]
        for index in range(
            0,
            len(value),
            length,
        )
    )


def tokens(value):
    ignored = {
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
        "high",
        "quality",
        "hyper",
        "realistic",
    }

    return {
        word
        for word in re.findall(
            r"[a-z0-9]+",
            str(value).lower(),
        )
        if len(word) >= 3
        and word not in ignored
    }


def parse_json(text):
    text = str(
        text or ""
    ).strip()

    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.I,
    )

    text = re.sub(
        r"\s*```$",
        "",
        text,
        flags=re.I,
    )

    try:
        data = json.loads(text)

    except json.JSONDecodeError:
        try:
            repaired = repair_json(text)
            data = json.loads(repaired)

        except Exception:
            match = re.search(
                r"(\{.*\}|\[.*\])",
                text,
                flags=re.S,
            )

            if not match:
                raise RuntimeError(
                    "LLM応答にJSONがありません。"
                )

            try:
                repaired = repair_json(
                    match.group(0)
                )

                data = json.loads(
                    repaired
                )

            except Exception as error:
                raise RuntimeError(
                    "JSONのパースに失敗しました: {}\n"
                    "応答先頭: {}".format(
                        error,
                        text[:500],
                    )
                )

    if isinstance(data, list):
        data = {
            "title": "",
            "description": "",
            "genre": "",
            "tags": [],
            "scenes": data,
        }

    if not isinstance(data, dict):
        raise RuntimeError(
            "LLM応答がJSONオブジェクトではありません。"
        )

    return data


class LLM:
    def __init__(self):
        self.groq = os.getenv(
            "GROQ_API_KEY",
            "",
        ).strip()

        if not self.groq:
            raise RuntimeError(
                "GROQ_API_KEYがありません。"
            )

    def post(
        self,
        url,
        headers,
        payload,
    ):
        last_error = None

        for attempt in range(5):
            wait = (
                min(
                    90,
                    5 * (2 ** attempt),
                )
                + random.uniform(0, 3)
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

                message = response.text[:1500]

                last_error = RuntimeError(
                    "HTTP {}: {}".format(
                        response.status_code,
                        message,
                    )
                )

                if response.status_code == 413:
                    raise last_error

                if response.status_code not in {
                    408,
                    429,
                    500,
                    502,
                    503,
                    504,
                }:
                    raise last_error

                retry_after = response.headers.get(
                    "retry-after"
                )

                if retry_after:
                    try:
                        wait = max(
                            wait,
                            float(retry_after) + 1,
                        )
                    except ValueError:
                        pass

            except requests.RequestException as error:
                last_error = error

            log.warning(
                "Groq API再試行まで%.1f秒",
                wait,
            )

            time.sleep(wait)

        raise RuntimeError(
            "API呼び出し失敗: {}".format(
                last_error
            )
        )

    def request(
        self,
        prompt,
        max_tokens,
    ):
        payload = {
            "model": GROQ_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "有効なJSONオブジェクトだけを返してください。"
                        "前置き、説明、Markdown、コードフェンスは禁止です。"
                        "事実と推測を区別し、参考資料にない数値を"
                        "断定しないでください。"
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "temperature": 0.35,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_object",
            },
        }

        if GROQ_MODEL.startswith(
            "openai/gpt-oss"
        ):
            payload["reasoning_effort"] = "low"

        result = self.post(
            "https://api.groq.com/openai/v1/"
            "chat/completions",
            {
                "Authorization": (
                    "Bearer " + self.groq
                ),
                "Content-Type": "application/json",
            },
            payload,
        )

        choices = result.get(
            "choices",
            [],
        )

        if not choices:
            raise RuntimeError(
                "Groq応答にchoicesがありません。"
            )

        choice = choices[0]
        message = choice.get(
            "message",
            {},
        )

        content = message.get(
            "content",
            "",
        )

        if not content:
            raise RuntimeError(
                "Groq応答本文が空です。"
            )

        finish_reason = choice.get(
            "finish_reason",
            "",
        )

        if finish_reason == "length":
            log.warning(
                "Groq応答が最大トークン数で"
                "打ち切られました。JSON修復を試みます。"
            )

        usage = result.get(
            "usage",
            {},
        )

        log.info(
            "Groq使用量 input=%s output=%s total=%s",
            usage.get(
                "prompt_tokens",
                "?",
            ),
            usage.get(
                "completion_tokens",
                "?",
            ),
            usage.get(
                "total_tokens",
                "?",
            ),
        )

        return parse_json(content)

    def ask(self, prompt):
        try:
            return self.request(
                prompt,
                GROQ_MAX_TOKENS,
            )

        except Exception as first_error:
            error_text = str(first_error)

            size_error = any(
                marker in error_text
                for marker in (
                    "HTTP 413",
                    "Request too large",
                    "tokens per minute",
                    "rate_limit_exceeded",
                )
            )

            if not size_error:
                raise RuntimeError(
                    "Groq API処理に失敗しました: {}".format(
                        first_error
                    )
                )

            log.warning(
                "Groqのトークン上限を超えたため、"
                "参考資料を省略して再試行します。"
            )

            compact_prompt = re.sub(
                r"参考資料:\s*.*?\n\s*出力形式:",
                (
                    "参考資料:\n"
                    "容量制限のため本文を省略。"
                    "一般に確認されている事実だけを使用し、"
                    "不確かな内容や数値は断定しないこと。\n\n"
                    "出力形式:"
                ),
                prompt,
                flags=re.S,
            )

            try:
                return self.request(
                    compact_prompt,
                    min(
                        GROQ_MAX_TOKENS,
                        4400,
                    ),
                )

            except Exception as second_error:
                raise RuntimeError(
                    "Groq API処理に失敗しました。\n"
                    "初回: {}\n"
                    "圧縮後: {}".format(
                        first_error,
                        second_error,
                    )
                )


def research(theme):
    results = []
    seen = set()

    queries = (
        theme,
        theme + " 仕組み 理由",
        theme + " 意外な事実",
    )

    for query in queries:
        log.info(
            "Web検索: %s",
            query,
        )

        try:
            with DDGS() as ddgs:
                search_results = ddgs.text(
                    query,
                    region="jp-jp",
                    max_results=5,
                )

                for item in search_results:
                    url = str(
                        item.get(
                            "href",
                            "",
                        )
                    ).strip()

                    if (
                        url
                        and url not in seen
                    ):
                        seen.add(url)
                        results.append(item)

        except Exception as error:
            log.warning(
                "Web検索失敗: %s",
                error,
            )

    sources = []

    for item in results[:10]:
        url = str(
            item.get(
                "href",
                "",
            )
        ).strip()

        body = ""

        try:
            page = trafilatura.fetch_url(url)

            if page:
                body = (
                    trafilatura.extract(
                        page,
                        favor_precision=True,
                        include_comments=False,
                        include_tables=False,
                    )
                    or ""
                )

        except Exception as error:
            log.warning(
                "本文取得失敗: %s",
                error,
            )

        text = clean(
            body
            or item.get(
                "body",
                "",
            )
        )

        if len(text) >= 120:
            sources.append(
                {
                    "title": clean(
                        item.get(
                            "title",
                            "",
                        )
                    )[:150],
                    "url": url,
                    "text": text[
                        :RESEARCH_CHARS_PER_SOURCE
                    ],
                }
            )

        if len(sources) >= RESEARCH_SOURCE_LIMIT:
            break

    research_path = (
        WORK / "research.json"
    )

    research_path.write_text(
        json.dumps(
            sources,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log.info(
        "参考資料取得: %d件",
        len(sources),
    )

    return sources


def split_scene(
    scene,
    theme,
):
    text = clean(
        scene.get(
            "narration",
            "",
        )
    )

    if len(text) < 2:
        text = (
            "{}について詳しく見ていきます。"
        ).format(theme)

    middle = len(text) // 2

    positions = [
        text.rfind(
            mark,
            0,
            min(
                len(text),
                middle + 100,
            ),
        )
        for mark in "。！？"
    ]

    cut = max(positions)

    if cut < max(
        40,
        middle - 140,
    ):
        cut = middle
    else:
        cut += 1

    cuts = scene.get(
        "visual_cuts",
        [],
    )

    if not isinstance(cuts, list):
        cuts = []

    cut_middle = max(
        1,
        len(cuts) // 2,
    )

    first = dict(scene)
    second = dict(scene)

    first["narration"] = (
        text[:cut].strip()
        or text
    )

    second["narration"] = (
        text[cut:].strip()
        or text
    )

    second["heading"] = (
        clean(
            scene.get(
                "heading",
                theme,
            )
        )
        + " 続き"
    )

    first["visual_cuts"] = (
        cuts[:cut_middle]
        or cuts
    )

    second["visual_cuts"] = (
        cuts[cut_middle:]
        or cuts
    )

    return first, second


def normalize(
    data,
    theme,
):
    if isinstance(data, list):
        data = {
            "title": theme,
            "description": "",
            "genre": "",
            "tags": [],
            "scenes": data,
        }

    if not isinstance(data, dict):
        raise RuntimeError(
            "台本データを復元できません。"
        )

    scenes = data.get(
        "scenes",
        [],
    )

    if (
        not isinstance(scenes, list)
        or not scenes
    ):
        raise RuntimeError(
            "台本にシーンがありません。"
        )

    while len(scenes) < 9:
        index = max(
            range(len(scenes)),
            key=lambda number: len(
                clean(
                    scenes[number].get(
                        "narration",
                        "",
                    )
                )
            )
            if isinstance(
                scenes[number],
                dict,
            )
            else 0,
        )

        raw_scene = scenes.pop(index)

        if not isinstance(
            raw_scene,
            dict,
        ):
            raw_scene = {
                "heading": theme,
                "narration": str(raw_scene),
                "visual_cuts": [],
            }

        first, second = split_scene(
            raw_scene,
            theme,
        )

        scenes[index:index] = [
            first,
            second,
        ]

    while len(scenes) > 11:
        extra = scenes.pop()
        target = scenes[-1]

        if not isinstance(
            target,
            dict,
        ):
            target = {
                "heading": theme,
                "narration": str(target),
                "visual_cuts": [],
            }

            scenes[-1] = target

        if not isinstance(
            extra,
            dict,
        ):
            extra = {
                "narration": str(extra),
                "visual_cuts": [],
            }

        target["narration"] = (
            clean(
                target.get(
                    "narration",
                    "",
                )
            )
            + " "
            + clean(
                extra.get(
                    "narration",
                    "",
                )
            )
        ).strip()

        target_cuts = target.get(
            "visual_cuts",
            [],
        )

        extra_cuts = extra.get(
            "visual_cuts",
            [],
        )

        if not isinstance(
            target_cuts,
            list,
        ):
            target_cuts = []

        if
        def subtitle_chunks(text):
    output = []

    sentences = re.split(
        r"(?<=[。！？!?])",
        clean(text),
    )

    for sentence in sentences:
        sentence = sentence.strip()

        if not sentence:
            continue

        while len(sentence) > 24:
            candidates = [
                sentence.rfind(
                    mark,
                    9,
                    25,
                )
                for mark in (
                    "、",
                    "，",
                    " ",
                )
            ]

            position = max(candidates)

            if position < 9:
                position = 24
            else:
                position += 1

            chunk = sentence[
                :position
            ].strip()

            if chunk:
                output.append(chunk)

            sentence = sentence[
                position:
            ].strip()

        if sentence:
            output.append(sentence)

    return output or [
        clean(text)
    ]


async def synthesize(
    text,
    path,
):
    last_error = None

    for attempt in range(4):
        try:
            communicator = edge_tts.Communicate(
                text,
                VOICE,
                rate=RATE,
            )

            await communicator.save(
                str(path)
            )

            if (
                path.exists()
                and path.stat().st_size > 1000
            ):
                return

        except Exception as error:
            last_error = error

        wait = 2 ** attempt

        log.warning(
            "音声生成を%d秒後に再試行します。",
            wait,
        )

        await asyncio.sleep(wait)

    raise RuntimeError(
        "音声生成失敗: {}".format(
            last_error
        )
    )


def make_ass(
    events,
    scenes,
    timeline,
    path,
):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK JP,46,&H00FFFFFF,&H00FFFFFF,&H00141414,&H96000000,-1,0,0,0,100,100,0,0,3,4,1,2,85,85,55,1
Style: Heading,Noto Sans CJK JP,38,&H00FFFFFF,&H00FFFFFF,&H00141414,&H99000000,-1,0,0,0,100,100,0,0,3,3,1,7,42,42,35,1
Style: Emphasis,Noto Sans CJK JP,50,&H005CE4FF,&H005CE4FF,&H00141414,&H99000000,-1,0,0,0,100,100,0,0,3,4,1,2,80,80,55,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]

    for index, scene in enumerate(scenes):
        if index >= len(timeline):
            break

        start = timeline[index]["start"]

        end = min(
            timeline[index]["end"],
            start + 2.4,
        )

        heading = esc(
            scene.get(
                "heading",
                "",
            )
        )

        if heading:
            lines.append(
                "Dialogue: 1,{},{},Heading,,0,0,0,,"
                "{{\\fad(140,180)}}{}\n".format(
                    ass_time(start),
                    ass_time(end),
                    heading,
                )
            )

    emphasis_pattern = re.compile(
        r"(\d+(?:\.\d+)?(?:年|秒|分|倍|個|種類|％|%|歳)?"
        r"|重要|結論|理由|ポイント|実は|つまり)"
    )

    for event in events:
        text = esc(
            event.get(
                "text",
                "",
            )
        )

        if not text:
            continue

        style = "Default"

        if emphasis_pattern.search(
            event.get(
                "text",
                "",
            )
        ):
            style = "Emphasis"

        if len(text) > 17:
            source_text = event.get(
                "text",
                "",
            )

            middle = len(source_text) // 2

            split_candidates = [
                source_text.rfind(
                    mark,
                    6,
                    middle + 5,
                )
                for mark in (
                    "、",
                    "。",
                    " ",
                )
            ]

            split_position = max(
                split_candidates
            )

            if split_position < 6:
                split_position = middle
            else:
                split_position += 1

            first_line = esc(
                source_text[
                    :split_position
                ].strip()
            )

            second_line = esc(
                source_text[
                    split_position:
                ].strip()
            )

            if second_line:
                text = (
                    first_line
                    + r"\N"
                    + second_line
                )
            else:
                text = first_line

        lines.append(
            "Dialogue: 0,{},{},{},,0,0,0,,"
            "{{\\fad(70,90)}}{}\n".format(
                ass_time(
                    event["start"]
                ),
                ass_time(
                    event["end"]
                ),
                style,
                text,
            )
        )

    path.write_text(
        "".join(lines),
        encoding="utf-8-sig",
    )


def create_narration(scenes):
    for path in WORK.glob(
        "voice_*.mp3"
    ):
        path.unlink(
            missing_ok=True
        )

    async def create_all():
        for index, scene in enumerate(
            scenes
        ):
            log.info(
                "音声生成 %d/%d",
                index + 1,
                len(scenes),
            )

            output = (
                WORK
                / "voice_{:02d}.mp3".format(
                    index
                )
            )

            await synthesize(
                scene["narration"],
                output,
            )

    asyncio.run(create_all())

    paths = []
    timeline = []
    events = []

    cursor = 0.0

    for index, scene in enumerate(
        scenes
    ):
        path = (
            WORK
            / "voice_{:02d}.mp3".format(
                index
            )
        )

        if not path.exists():
            raise RuntimeError(
                "音声ファイルがありません: {}".format(
                    path
                )
            )

        scene_duration = duration(path)
        start = cursor
        end = start + scene_duration

        chunks = subtitle_chunks(
            scene["narration"]
        )

        total_weight = sum(
            max(
                1,
                len(chunk),
            )
            for chunk in chunks
        )

        subtitle_cursor = start
        scene_events = []

        for chunk_number, chunk in enumerate(
            chunks
        ):
            if (
                chunk_number
                == len(chunks) - 1
            ):
                chunk_end = end

            else:
                estimated = (
                    scene_duration
                    * max(
                        1,
                        len(chunk),
                    )
                    / total_weight
                )

                display_time = max(
                    1.2,
                    min(
                        3.4,
                        estimated,
                    ),
                )

                chunk_end = min(
                    end,
                    subtitle_cursor
                    + display_time,
                )

            if chunk_end <= subtitle_cursor:
                chunk_end = min(
                    end,
                    subtitle_cursor + 0.5,
                )

            event = {
                "scene_index": index,
                "chunk_index": chunk_number,
                "start": subtitle_cursor,
                "end": chunk_end,
                "text": chunk,
            }

            events.append(event)
            scene_events.append(event)

            subtitle_cursor = chunk_end

        timeline.append(
            {
                "scene_index": index,
                "start": start,
                "end": end,
                "duration": scene_duration,
                "events": scene_events,
            }
        )

        cursor = end
        paths.append(path)

    listing = WORK / "audio.txt"

    listing.write_text(
        "".join(
            "file '{}'\n".format(
                str(path.resolve()).replace(
                    "'",
                    "'\\''",
                )
            )
            for path in paths
        ),
        encoding="utf-8",
    )

    output = WORK / "narration.m4a"

    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            str(output),
        ]
    )

    subtitles = (
        WORK / "subtitles.ass"
    )

    make_ass(
        events,
        scenes,
        timeline,
        subtitles,
    )

    narration_duration = duration(
        output
    )

    return (
        output,
        timeline,
        narration_duration,
        subtitles,
    )


def pixabay(query):
    normalized = clean(
        query
    ).lower()[:100]

    if not normalized:
        return []

    cache_key = (
        "video:" + normalized
    )

    if cache_key in CACHE:
        return CACHE[cache_key]

    try:
        response = requests.get(
            "https://pixabay.com/api/videos/",
            params={
                "key": PIXABAY_KEY,
                "q": normalized,
                "lang": "en",
                "video_type": "film",
                "min_width": 1280,
                "min_height": 720,
                "safesearch": "true",
                "order": "popular",
                "per_page": RESULTS,
            },
            timeout=45,
        )

        response.raise_for_status()

        hits = response.json().get(
            "hits",
            [],
        )

        if not isinstance(hits, list):
            hits = []

        CACHE[cache_key] = hits

        return hits

    except Exception as error:
        log.warning(
            "Pixabay動画検索失敗 [%s\]: %s",
            normalized,
            error,
        )

        CACHE[cache_key] = []

        return []


def pixabay_images(query):
    normalized = clean(
        query
    ).lower()[:100]

    if not normalized:
        return []

    cache_key = (
        "image:" + normalized
    )

    if cache_key in CACHE:
        return CACHE[cache_key]

    try:
        response = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_KEY,
                "q": normalized,
                "lang": "en",
                "image_type": "photo",
                "orientation": "horizontal",
                "min_width": 1280,
                "min_height": 720,
                "safesearch": "true",
                "order": "popular",
                "per_page": RESULTS,
            },
            timeout=45,
        )

        response.raise_for_status()

        hits = response.json().get(
            "hits",
            [],
        )

        if not isinstance(hits, list):
            hits = []

        CACHE[cache_key] = hits

        return hits

    except Exception as error:
        log.warning(
            "Pixabay画像検索失敗 [%s\]: %s",
            normalized,
            error,
        )

        CACHE[cache_key] = []

        return []


def video_url(video):
    choices = []

    for quality, penalty in (
        ("large", 0),
        ("medium", 100),
        ("small", 10000),
        ("tiny", 20000),
    ):
        file_data = (
            video.get(
                "videos",
                {},
            ).get(
                quality,
                {},
            )
        )

        url = str(
            file_data.get(
                "url",
                "",
            )
        ).strip()

        width = int(
            file_data.get(
                "width",
                0,
            )
            or 0
        )

        height = int(
            file_data.get(
                "height",
                0,
            )
            or 0
        )

        if (
            url
            and width > height
            and width >= 640
            and height >= 360
        ):
            ratio_penalty = (
                abs(
                    width / height
                    - 16 / 9
                )
                * 10000
            )

            resolution_bonus = min(
                width,
                1920,
            ) / 100

            score = (
                ratio_penalty
                + penalty
                - resolution_bonus
            )

            choices.append(
                (
                    score,
                    url,
                )
            )

    if not choices:
        return None

    choices.sort(
        key=lambda item: item[0]
    )

    return choices[0][1]


def image_url(image):
    candidates = [
        image.get(
            "largeImageURL",
            "",
        ),
        image.get(
            "fullHDURL",
            "",
        ),
        image.get(
            "webformatURL",
            "",
        ),
    ]

    for candidate in candidates:
        candidate = str(
            candidate or ""
        ).strip()

        if candidate:
            return candidate

    return None


def relevance(
    query,
    item,
):
    query_words = tokens(query)

    source_text = " ".join(
        [
            str(
                item.get(
                    "tags",
                    "",
                )
            ),
            str(
                item.get(
                    "pageURL",
                    "",
                )
            ),
        ]
    )

    item_words = tokens(
        source_text
    )

    if not query_words:
        return 0.0

    intersection = (
        query_words
        & item_words
    )

    base = (
        len(intersection)
        / max(
            1,
            min(
                len(query_words),
                5,
            ),
        )
    )

    exact_bonus = 0.0

    normalized_tags = str(
        item.get(
            "tags",
            "",
        )
    ).lower()

    for word in query_words:
        if word in normalized_tags:
            exact_bonus += 0.03

    return base + min(
        exact_bonus,
        0.15,
    )


def download_video(
    video,
    query,
    scene_index,
    cut_index,
):
    video_id = int(
        video.get(
            "id",
            0,
        )
        or 0
    )

    page_url = str(
        video.get(
            "pageURL",
            "",
        )
    ).strip()

    url = video_url(video)

    if not video_id or not url:
        return None, None

    if video_id in USED_IDS:
        return None, None

    if url in USED_URLS:
        return None, None

    if (
        page_url
        and page_url in USED_PAGES
    ):
        return None, None

    score = relevance(
        query,
        video,
    )

    if score <= 0:
        return None, None

    path = (
        WORK
        / "pixabay_{:02d}_{:03d}.mp4".format(
            scene_index,
            cut_index,
        )
    )

    try:
        response = requests.get(
            url,
            stream=True,
            timeout=120,
        )

        response.raise_for_status()

        with path.open(
            "wb"
        ) as output:
            for chunk in response.iter_content(
                1024 * 1024
            ):
                if chunk:
                    output.write(chunk)

        if (
            not path.exists()
            or path.stat().st_size < 100000
        ):
            path.unlink(
                missing_ok=True
            )

            return None, None

        source_duration = duration(
            path
        )

        if source_duration < MIN_CUT:
            path.unlink(
                missing_ok=True
            )

            return None, None

        USED_IDS.add(video_id)
        USED_URLS.add(url)

        if page_url:
            USED_PAGES.add(page_url)

        credit = {
            "media_type": "video",
            "video_id": video_id,
            "page_url": page_url,
            "creator_name": str(
                video.get(
                    "user",
                    "",
                )
            ),
            "query": query,
            "relevance": round(
                score,
                3,
            ),
        }

        return path, credit

    except Exception as error:
        log.warning(
            "動画取得失敗 [%s\]: %s",
            query,
            error,
        )

        path.unlink(
            missing_ok=True
        )

        return None, None


def download_image(
    image,
    query,
    scene_index,
    cut_index,
):
    image_id = int(
        image.get(
            "id",
            0,
        )
        or 0
    )

    page_url = str(
        image.get(
            "pageURL",
            "",
        )
    ).strip()

    url = image_url(image)

    unique_id = (
        "image",
        image_id,
    )

    if not image_id or not url:
        return None, None

    if unique_id in USED_IDS:
        return None, None

    if url in USED_URLS:
        return None, None

    if (
        page_url
        and page_url in USED_PAGES
    ):
        return None, None

    score = relevance(
        query,
        image,
    )

    if score <= 0:
        return None, None

    suffix = ".jpg"

    if ".png" in url.lower():
        suffix = ".png"

    path = (
        WORK
        / "pixabay_image_{:02d}_{:03d}{}".format(
            scene_index,
            cut_index,
            suffix,
        )
    )

    try:
        response = requests.get(
            url,
            timeout=120,
        )

        response.raise_for_status()

        path.write_bytes(
            response.content
        )

        if (
            not path.exists()
            or path.stat().st_size < 50000
        ):
            path.unlink(
                missing_ok=True
            )

            return None, None

        with Image.open(path) as picture:
            picture.verify()

        USED_IDS.add(unique_id)
        USED_URLS.add(url)

        if page_url:
            USED_PAGES.add(page_url)

        credit = {
            "media_type": "image",
            "image_id": image_id,
            "page_url": page_url,
            "creator_name": str(
                image.get(
                    "user",
                    "",
                )
            ),
            "query": query,
            "relevance": round(
                score,
                3,
            ),
        }

        return path, credit

    except Exception as error:
        log.warning(
            "画像取得失敗 [%s\]: %s",
            query,
            error,
        )

        path.unlink(
            missing_ok=True
        )

        return None, None


def expand_specifications(
    scene,
    count,
):
    source_cuts = scene.get(
        "visual_cuts",
        [],
    )

    if not source_cuts:
        source_cuts = [
            {
                "narration_excerpt": (
                    scene["narration"][:90]
                ),
                "query": (
                    scene["heading"]
                ),
                "fallback_query": (
                    scene["heading"]
                ),
                "card_title": (
                    scene["heading"]
                ),
                "card_points": [],
            }
        ]

    specifications = []

    suffixes = [
        "",
        " close up",
        " wide shot",
        " natural environment",
        " detailed view",
        " slow motion",
    ]

    for index in range(count):
        source = dict(
            source_cuts[
                index
                % len(source_cuts)
            ]
        )

        cycle = (
            index
            // len(source_cuts)
        )

        if cycle > 0:
            suffix = suffixes[
                cycle
                % len(suffixes)
            ]

            if suffix:
                source["query"] = (
                    clean(
                        source.get(
                            "query",
                            "",
                        )
                    )
                    + suffix
                )[:100]

        specifications.append(
            source
        )

    return specifications


def acquire_assets(
    scenes,
    timeline,
):
    all_assets = []
    credits = []

    for scene_index, scene in enumerate(
        scenes
    ):
        scene_duration = timeline[
            scene_index
        ]["duration"]

        desired_count = max(
            len(
                scene["visual_cuts"]
            ),
            math.ceil(
                scene_duration
                / MAX_CUT
            ),
        )

        specifications = (
            expand_specifications(
                scene,
                desired_count,
            )
        )

        selected_assets = []

        for cut_index, specification in enumerate(
            specifications
        ):
        asset = None

            query_candidates = []

            for query in (
                specification.get(
                    "query",
                    "",
                ),
                specification.get(
                    "fallback_query",
                    "",
                ),
            ):
                query = clean(query)

                if (
                    query
                    and query
                    not in query_candidates
                ):
                    query_candidates.append(
                        query
                    )

            for query in query_candidates:
                video_results = sorted(
                    pixabay(query),
                    key=lambda item: relevance(
                        query,
                        item,
                    ),
                    reverse=True,
                )

                for video in video_results:
                    path, credit = download_video(
                        video,
                        query,
                        scene_index,
                        cut_index,
                    )

                    if not path:
                        continue

                    asset = {
                        "type": "video",
                        "path": path,
                        "spec": specification,
                        "query": query,
                    }

                    credits.append(
                        credit
                    )

                    break

                if asset:
                    break

            if not asset:
                for query in query_candidates:
                    image_results = sorted(
                        pixabay_images(
                            query
                        ),
                        key=lambda item: relevance(
                            query,
                            item,
                        ),
                        reverse=True,
                    )

                    for image in image_results:
                        path, credit = download_image(
                            image,
                            query,
                            scene_index,
                            cut_index,
                        )

                        if not path:
                            continue

                        asset = {
                            "type": "image",
                            "path": path,
                            "spec": specification,
                            "query": query,
                        }

                        credits.append(
                            credit
                        )

                        break

                    if asset:
                        break

            if not asset:
                asset = {
                    "type": "card",
                    "path": None,
                    "spec": specification,
                    "query": "",
                }

            selected_assets.append(
                asset
            )

        all_assets.append(
            selected_assets
        )

        video_count = sum(
            item["type"] == "video"
            for item in selected_assets
        )

        image_count = sum(
            item["type"] == "image"
            for item in selected_assets
        )

        card_count = sum(
            item["type"] == "card"
            for item in selected_assets
        )

        log.info(
            "シーン%d: 動画%d本 画像%d枚 カード%d枚",
            scene_index + 1,
            video_count,
            image_count,
            card_count,
        )

    credits_path = (
        WORK
        / "pixabay_credits.json"
    )

    credits_path.write_text(
        json.dumps(
            credits,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return all_assets, credits


def fit_text_font(
    draw,
    text,
    font_path,
    maximum_size,
    minimum_size,
    maximum_width,
    maximum_height,
    spacing=10,
):
    selected_font = ImageFont.truetype(
        font_path,
        maximum_size,
    )

    for size in range(
        maximum_size,
        minimum_size - 1,
        -2,
    ):
        candidate = ImageFont.truetype(
            font_path,
            size,
        )

        box = draw.multiline_textbbox(
            (0, 0),
            text,
            font=candidate,
            spacing=spacing,
        )

        width = (
            box[2] - box[0]
        )

        height = (
            box[3] - box[1]
        )

        selected_font = candidate

        if (
            width <= maximum_width
            and height <= maximum_height
        ):
            break

    return selected_font


def create_card(
    scene,
    specification,
    index,
):
    path = (
        WORK
        / "card_{:03d}.jpg".format(
            index
        )
    )

    image = Image.new(
        "RGB",
        (W, H),
        (13, 22, 39),
    )

    draw = ImageDraw.Draw(image)

    for y_position in range(H):
        ratio = (
            y_position / max(
                1,
                H - 1,
            )
        )

        color = (
            int(
                13 + 13 * ratio
            ),
            int(
                22 + 20 * ratio
            ),
            int(
                39 + 35 * ratio
            ),
        )

        draw.line(
            (
                0,
                y_position,
                W,
                y_position,
            ),
            fill=color,
        )

    draw.rounded_rectangle(
        (
            42,
            38,
            1238,
            682,
        ),
        radius=28,
        fill=(20, 31, 53),
        outline=(79, 163, 230),
        width=3,
    )

    draw.rounded_rectangle(
        (
            75,
            78,
            1205,
            225,
        ),
        radius=22,
        fill=(29, 46, 76),
    )

    title = clean(
        specification.get(
            "card_title",
            "",
        )
        or scene.get(
            "heading",
            "",
        )
    )

    title_text = wrap(
        title,
        17,
    )

    title_font = fit_text_font(
        draw,
        title_text,
        font(),
        54,
        38,
        1040,
        125,
        spacing=8,
    )

    title_box = draw.multiline_textbbox(
        (0, 0),
        title_text,
        font=title_font,
        spacing=8,
        align="center",
        stroke_width=2,
    )

    title_width = (
        title_box[2]
        - title_box[0]
    )

    title_height = (
        title_box[3]
        - title_box[1]
    )

    title_x = (
        W - title_width
    ) / 2

    title_y = (
        151 - title_height / 2
    )

    draw.multiline_text(
        (
            title_x,
            title_y,
        ),
        title_text,
        font=title_font,
        fill=(255, 228, 92),
        stroke_width=2,
        stroke_fill=(0, 0, 0),
        spacing=8,
        align="center",
    )

    points = specification.get(
        "card_points",
        [],
    )

    if not isinstance(
        points,
        list,
    ):
        points = []

    points = [
        clean(point)
        for point in points
        if clean(point)
    ][:3]

    if not points:
        fallback = clean(
            specification.get(
                "narration_excerpt",
                "",
            )
            or scene.get(
                "narration",
                "",
            )[:90]
        )

        points = [
            fallback
        ]

    content_top = 285
    available_height = 330

    point_area_height = (
        available_height
        // max(
            1,
            len(points),
        )
    )

    for point_index, point in enumerate(
        points
    ):
        point_y = (
            content_top
            + point_index
            * point_area_height
        )

        draw.ellipse(
            (
                86,
                point_y + 13,
                106,
                point_y + 33,
            ),
            fill=(79, 177, 255),
        )

        point_text = wrap(
            point,
            27,
        )

        point_font = fit_text_font(
            draw,
            point_text,
            font(),
            34,
            26,
            1000,
            point_area_height - 15,
            spacing=8,
        )

        draw.multiline_text(
            (
                132,
                point_y,
            ),
            point_text,
            font=point_font,
            fill=(245, 248, 255),
            spacing=8,
        )

    image.save(
        path,
        quality=94,
        optimize=True,
    )

    return path


def prepare_image(
    source,
    output,
):
    with Image.open(source) as image:
        image = image.convert("RGB")

        source_width = image.width
        source_height = image.height

        target_ratio = W / H
        source_ratio = (
            source_width
            / source_height
        )

        if source_ratio > target_ratio:
            new_height = H
            new_width = int(
                H * source_ratio
            )
        else:
            new_width = W
            new_height = int(
                W / source_ratio
            )

        image = image.resize(
            (
                new_width,
                new_height,
            ),
            Image.Resampling.LANCZOS,
        )

        left = max(
            0,
            (
                new_width - W
            )
            // 2,
        )

        top = max(
            0,
            (
                new_height - H
            )
            // 2,
        )

        image = image.crop(
            (
                left,
                top,
                left + W,
                top + H,
            )
        )

        image.save(
            output,
            quality=95,
        )

    return output


def image_clip(
    source,
    output,
    seconds,
    index=0,
):
    prepared = (
        WORK
        / "prepared_{:04d}.jpg".format(
            index
        )
    )

    prepare_image(
        source,
        prepared,
    )

    frames = max(
        1,
        int(
            seconds * FPS
        ),
    )

    motion_mode = (
        index % 4
    )

    if motion_mode == 0:
        zoom_expression = (
            "min(zoom+0.00045,1.10)"
        )

        x_expression = (
            "iw/2-(iw/zoom/2)"
        )

        y_expression = (
            "ih/2-(ih/zoom/2)"
        )

    elif motion_mode == 1:
        zoom_expression = (
            "min(zoom+0.00035,1.08)"
        )

        x_expression = (
            "min(iw-iw/zoom,"
            "(on/{frames})"
            "*(iw-iw/zoom))"
        ).format(
            frames=frames
        )

        y_expression = (
            "ih/2-(ih/zoom/2)"
        )

    elif motion_mode == 2:
        zoom_expression = (
            "1.08"
        )

        x_expression = (
            "max(0,"
            "(1-on/{frames})"
            "*(iw-iw/zoom))"
        ).format(
            frames=frames
        )

        y_expression = (
            "ih/2-(ih/zoom/2)"
        )

    else:
        zoom_expression = (
            "min(zoom+0.0004,1.09)"
        )

        x_expression = (
            "iw/2-(iw/zoom/2)"
        )

        y_expression = (
            "min(ih-ih/zoom,"
            "(on/{frames})"
            "*(ih-ih/zoom))"
        ).format(
            frames=frames
        )

    video_filter = (
        "scale=1600:900:"
        "force_original_aspect_ratio=increase,"
        "crop=1600:900,"
        "zoompan="
        "z='{zoom}':"
        "x='{x}':"
        "y='{y}':"
        "d={frames}:"
        "s=1280x720:"
        "fps=30,"
        "eq=contrast=1.03:saturation=1.04,"
        "format=yuv420p"
    ).format(
        zoom=zoom_expression,
        x=x_expression,
        y=y_expression,
        frames=frames,
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(prepared),
            "-an",
            "-vf",
            video_filter,
            "-t",
            "{:.3f}".format(
                seconds
            ),
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
            str(output),
        ]
    )


def video_clip(
    source,
    output,
    seconds,
    index,
):
    source_duration = duration(
        source
    )

    maximum_start = max(
        0.0,
        source_duration
        - seconds
        - 0.1,
    )

    factor = (
        (
            index * 37
        )
        % 100
    ) / 100

    start = (
        maximum_start
        * (
            0.15
            + 0.7 * factor
        )
    )

    video_filter = (
        "scale=1280:720:"
        "force_original_aspect_ratio=increase,"
        "crop=1280:720,"
        "fps=30,"
        "eq=contrast=1.025:saturation=1.04,"
        "format=yuv420p"
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "{:.3f}".format(
                start
            ),
            "-i",
            str(source),
            "-an",
            "-t",
            "{:.3f}".format(
                seconds
            ),
            "-vf",
            video_filter,
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
            str(output),
        ]
    )
    def calculate_cut_durations(
    scene_duration,
    asset_count,
):
    if asset_count <= 0:
        return [scene_duration]

    if asset_count == 1:
        return [scene_duration]

    base = scene_duration / asset_count

    durations = []

    for index in range(asset_count):
        variation_pattern = (
            -0.65,
            0.35,
            -0.25,
            0.55,
            0.0,
        )

        variation = variation_pattern[
            index % len(variation_pattern)
        ]

        value = max(
            2.8,
            min(
                7.2,
                base + variation,
            ),
        )

        durations.append(value)

    total = sum(durations)

    if total <= 0:
        return [
            scene_duration / asset_count
            for _ in range(asset_count)
        ]

    scale = scene_duration / total

    durations = [
        value * scale
        for value in durations
    ]

    difference = (
        scene_duration
        - sum(durations)
    )

    durations[-1] += difference

    return durations


def clean_old_outputs():
    patterns = (
        "clip_*.mp4",
        "card_*.jpg",
        "prepared_*.jpg",
        "visual.mp4",
     *  "raw.mp4",
        "mixed.mp4",
*       "final.mp4",
        "thumb*ail.jpg",
        "video.txt",
   *)

    for pattern in patterns:
  *     for path in WORK.glob(pattern*:
            try:
               *path.unlink(
                    m*ssing_ok=True
                )

 *          except Exception as erro*:
                log.warning(
   *                "古いファイルを削除できません: %*",
                    error,
    *           )


def create_final(
 *  scenes,
    timeline,
    all_as*ets,
    narr,
    subtitles,
):
 *  clean_old_outputs()

    clips =*[]
    card_number = 0
    global_*ut_number = 0

    for scene_index* scene in enumerate(
        scene*
    ):
        if scene_index >= *en(timeline):
            raise Ru*timeError(
                "タイムライン*不足しています。"
            )

        s*ene_duration = timeline[
            scene_index
        ]["duration"]

        scene_assets = (
       *    all_assets[scene_index]
      *     if scene_index < len(all_asse*s)
            else []
        )

*       if not scene_assets:
      *     scene_assets = [
                {
                    "type": "card",
                    "path": None,
                    "spec": scene[
                        "visual_cuts"
                    ][0],
                    "query": "",
                }
            ]

    *   minimum_count = max(
          * 1,
            math.ceil(
       *        scene_duration
           *    / MAX_CUT
            ),
     *  )

        while len(scene_asset*) < minimum_count:
            spe*ification = scene[
                "visual_cuts"
            ][
                len(scene_assets)
      *         % len(
                  * scene["visual_cuts"]
            *   )
            ]

            sc*ne_assets.append(
                *
                    "type": "card*,
                    "path": None*
                    "spec": speci*ication,
                    "quer*": "",
                }
         *  )

        cut_durations = (
   *        calculate_cut_durations(
 *              scene_duration,
    *           len(scene_assets),
    *       )
        )

        log.in*o(
            "シーン%d映像生成: %.1f秒 /*%dカット",
            scene_index + *,
            scene_duration,
    *       len(scene_assets),
        *

        for cut_index, asset in *numerate(
            scene_assets*        ):
            seconds = c*t_durations[
                cut_index
            ]

            if *econds <= 0:
                conti*ue

            output = (
       *        WORK
                / "cl*p_{:02d}_{:03d}.mp4".format(
     *              scene_index,
       *            cut_index,
           *    )
            )

            a*set_type = asset.get(
            *   "type",
                "card",*            )

            asset_p*th = asset.get(
                "p*th"
            )

            spe*ification = asset.get(
           *    "spec",
                {},
  *         )

            try:
     *          if (
                   *asset_type == "video"
            *       and asset_path
            *       and Path(asset_path).exists*)
                    and duration*asset_path)
                    >=*seconds
                ):
       *            video_clip(
          *             asset_path,
         *              output,
            *           seconds,
              *         global_cut_number,
      *             )

                el*f (
                    asset_type*== "image"
                    and*asset_path
                    and*Path(asset_path).exists()
        *       ):
                    imag*_clip(
                        ass*t_path,
                        ou*put,
                        secon*s,
                        global_*ut_number,
                    )

*               else:
             *      source = create_card(
      *                 scene,
          *             specification,
      *                 card_number,
    *               )

                *   image_clip(
                   *    source,
                      * output,
                        s*conds,
                        glo*al_cut_number,
                   *)

                    card_number*+= 1

            except Exception*as error:
                log.warn*ng(
                    "カット生成失敗。カ*ドへ切替: %s",
                    err*r,
                )

            *   source = create_card(
         *          scene,
                 *  specification,
                 *  card_number,
                )

*               image_clip(
       *            source,
              *     output,
                    s*conds,
                    global_*ut_number,
                )

    *           card_number += 1

     *      if (
                not out*ut.exists()
                or out*ut.stat().st_size
                * 10000
            ):
            *   raise RuntimeError(
           *        "映像カットを生成できません: {}".format*
                        output
  *                 )
               *)

            clips.append(output*
            global_cut_number += *

    if not clips:
        raise *untimeError(
            "映像カットが生成*れませんでした。"
        )

    listing =*WORK / "video.txt"

    listing.wr*te_text(
        "".join(
        *   "file '{}'\n".format(
         *      str(
                    pat*.resolve()
                ).repla*e(
                    "'",
      *             "'\\''",
            *   )
            )
            for*path in clips
        ),
        e*coding="utf-8",
    )

    visual * WORK / "visual.mp4"

    run(
   *    [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
      *     "-an",
            "-c:v",
  *         "copy",
            "-mov*lags",
            "+faststart",
 *          str(visual),
        ]
 *  )

    visual_duration = duratio*(
        visual
    )

    narrat*on_duration = duration(
        na*r
    )

    log.info(
        "映像*間: %.2f秒",
        visual_duration*
    )

    log.info(
        "音声時*: %.2f秒",
        narration_durati*n,
    )

    raw = WORK / "raw.mp*"

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(visual),*            "-i",
            str(*arr),
            "-map",
        *   "0:v:0",
            "-map",
  *         "1:a:0",
            "-c:*",
            "copy",
           *"-c:a",
            "aac",
       *    "-b:a",
            "192k",
  *         "-ar",
            "48000*,
            "-shortest",
       *    "-movflags",
            "+fas*start",
            str(raw),
    *   ]
    )

    mixed = WORK / "mi*ed.mp4"

    if BGM.exists():
    *   narration_length = duration(
  *         narr
        )

        a*dio_filter = (
            "[0:a]"*            "highpass=f=80,"
     *      "acompressor="
            "*hreshold=-18dB:"
            "rati*=2.5:"
            "attack=15:"
  *         "release=180,"
          * "loudnorm="
            "I=-16:"
*           "TP=-1.5:"
            *LRA=11"
            "[voice];"
   *        "[1:a]"
            "atrim*0:{duration:.3f},"
            "as*tpts=N/SR/TB,"
            "volume*0.065"
            "[music];"
    *       "[music][voice]"
          * "sidechaincompress="
            *threshold=0.025:"
            "rat*o=8:"
            "attack=20:"
   *        "release=350"
            *[ducked];"
            "[voice][ducked]"
            "amix="
        *   "inputs=2:"
            "durati*n=first:"
            "dropout_tra*sition=2,"
            "alimiter=l*mit=0.95"
            "[audio]"
  *     ).format(
            duratio*=narration_length
        )

     *  try:
            run(
          *     [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(raw),
                    *-stream_loop",
                   *"-1",
                    "-i",
  *                 str(BGM),
       *            "-filter_complex",
   *                audio_filter,
    *               "-map",
           *        "0:v:0",
                 *  "-map",
                    "[audio]",
                    "-c:v",
*                   "copy",
       *            "-c:a",
              *     "aac",
                    "-*:a",
                    "192k",
 *                  "-ar",
         *          "48000",
               *    "-shortest",
                 *  "-movflags",
                   *"+faststart",
                    *tr(mixed),
                ]
     *      )

        except Exception *s error:
            log.warning(
*               "BGMダッキングに失敗しました。"
*               "通常ミックスへ切り替えます: %s"*
                error,
          * )

            fallback_filter = *
                "[1:a]"
         *      "volume=0.05,"
             *  "atrim=0:{duration:.3f}"
       *        "[music];"
               *"[0:a][music]"
                "am*x="
                "inputs=2:"
  *             "duration=first:"
   *            "dropout_transition=2,*
                "alimiter=limit=0*95"
                "[audio]"
    *       ).format(
                d*ration=narration_length
          * )

            run(
             *  [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(raw),
                    "-s*ream_loop",
                    "-*",
                    "-i",
     *              str(BGM),
          *         "-filter_complex",
      *             fallback_filter,
    *               "-map",
           *        "0:v:0",
                 *  "-map",
                    "[audio]",
                    "-c:v",
*                   "copy",
       *            "-c:a",
              *     "aac",
                    "-*:a",
                    "192k",
 *                  "-ar",
         *          "48000",
               *    "-shortest",
                 *  "-movflags",
                   *"+faststart",
                    *tr(mixed),
                ]
     *      )

    else:
        log.inf*(
            "BGMがないため、ナレーションのみ使用*ます。"
        )

        shutil.cop*2(
            raw,
            mi*ed,
        )

    final = WORK / *final.mp4"

    subtitle_path = (
*       str(
            subtitles.*esolve()
        )
        .replac*(
            "\\",
            "/*,
        )
        .replace(
    *       ":",
            r"\:",
   *    )
        .replace(
          * "'",
            r"\'",
        )*    )

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(mixed),
            "-vf",
            *ass='{}'".format(
                *ubtitle_path
            ),
      *     "-c:v",
            "libx264"*
            "-preset",
          * "veryfast",
            "-crf",
 *          "21",
            "-pix_*mt",
            "yuv420p",
      *     "-c:a",
            "copy",
 *          "-movflags",
           *"+faststart",
            str(fina*),
        ]
    )

    if (
     *  not final.exists()
        or fi*al.stat().st_size
        < 100000*    ):
        raise RuntimeError(*            "最終動画を生成できませんでした。"
   *    )

    return final


def thum*nail_background(
    source,
):
  * canvas = Image.new(
        "RGB"*
        (W, H),
        (15, 23, *8),
    )

    if not source:
    *   return canvas

    source = Pat*(source)

    if not source.exists*):
        return canvas

    try:*        with Image.open(source) as*image:
            image = image.c*nvert("RGB")

            source_r*tio = (
                image.widt*
                / image.height
  *         )

            target_rat*o = W / H

            if source_r*tio > target_ratio:
              * target_height = H
               *target_width = int(
              *     H * source_ratio
            *   )
            else:
           *    target_width = W
             *  target_height = int(
           *        W / source_ratio
         *      )

            image = image*resize(
                (
        *           target_width,
         *          target_height,
         *      ),
                Image.Res*mpling.LANCZOS,
            )

   *        left = max(
              * 0,
                (
            *       target_width - W
          *     )
                // 2,
     *      )

            top = max(
  *             0,
                (
*                   target_height -*H
                )
              * // 2,
            )

            *mage = image.crop(
               *(
                    left,
      *             top,
                *   left + W,
                    t*p + H,
                )
         *  )

            image = ImageEnha*ce.Contrast(
                image*            ).enhance(1.12)

     *      image = ImageEnhance.Color(
*               image
            )*enhance(1.08)

            canvas.*aste(
                image,
     *          (0, 0),
            )

 *  except Exception as error:
     *  log.warning(
            "サムネイル背*読込失敗: %s",
            error,
    *   )

    return canvas


def crea*e_thumbnail(
    title,
    scene,*    all_assets=None,
):
    output*= WORK / "thumbnail.jpg"

    sour*e = None

    if all_assets:
     *  first_scene_assets = (
         *  all_assets[0]
            if len*all_assets) > 0
            else []
        )

        for asset in f*rst_scene_assets:
            if (*                asset.get("type")
*               == "image"
        *       and asset.get("path")
     *      ):
                source = *sset["path"]
                break*
    image = thumbnail_background(*        source
    )

    overlay * Image.new(
        "RGBA",
      * (W, H),
        (0, 0, 0, 0),
   *)

    overlay_draw = ImageDraw.Dr*w(
        overlay
    )

    for *_position in range(W):
        rat*o = (
            x_position
     *      / max(
                1,
  *             W - 1,
            )
*       )

        alpha = int(
   *        205
            * (
      *         1.0 - ratio
            )*        )

        overlay_draw.li*e(
            (
                x*position,
                0,
     *          x_position,
            *   H,
            ),
            f*ll=(
                4,
          *     10,
                22,
     *          max(
                   *25,
                    alpha,
   *            ),
            ),
    *   )

    overlay_draw.rectangle(
*       (
            0,
          * H - 135,
            W,
         *  H,
        ),
        fill=(
   *        0,
            0,
        *   0,
            95,
        ),
 *  )

    image = Image.alpha_compo*ite(
        image.convert("RGBA")*
        overlay,
    ).convert("R*B")

    draw = ImageDraw.Draw(ima*e)

    clean_title = clean(title)*
    if not clean_title:
        c*ean_title = clean(
            sce*e.get(
                "heading",
*               "新しい動画",
          * )
        )

    title_text = wra*(
        clean_title,
        11,*    )

    title_font = fit_text_f*nt(
        draw,
        title_te*t,
        font(),
        76,
   *    52,
        1080,
        430,*        spacing=12,
    )

    box*= draw.multiline_textbbox(
       *(0, 0),
        title_text,
      * font=title_font,
        spacing=*2,
        stroke_width=7,
       *align="left",
    )

    text_heig*t = (
        box[3] - box[1]
    *

    x_position = 65
    y_positi*n = max(
        75,
        (
   *        H
            - text_heigh*
        )
        / 2
        - 2*,
    )

    draw.multiline_text(
*       (
            x_position,
 *          y_position,
        ),
 *      title_text,
        font=tit*e_font,
        fill=(
           *255,
            231,
            *6,
        ),
        stroke_width*8,
        stroke_fill=(
         *  0,
            0,
            0,*        ),
        spacing=12,
   *    align="left",
    )

    headi*g = clean(
        scene.get(
    *       "heading",
            "",
*       )
    )

    if heading:
  *     badge_font = ImageFont.truety*e(
            font(),
           *31,
        )

        badge_text * heading[:22]

        badge_box =*draw.textbbox(
            (0, 0),*            badge_text,
          * font=badge_font,
            stro*e_width=1,
        )

        badg*_width = (
            badge_box[2]
            - badge_box[0]
      *     + 46
        )

        draw.*ounded_rectangle(
            (
  *             65,
                3*,
                65 + badge_width*
                92,
            )*
            radius=15,
          * fill=(
                31,
      *         112,
                190,*            ),
            outline*(
                135,
           *    211,
                255,
    *       ),
            width=2,
   *    )

        draw.text(
        *   (
                88,
         *      46,
            ),
         *  badge_text,
            font=bad*e_font,
            fill=(
       *        255,
                255,
*               255,
            ),*            stroke_width=1,
      *     stroke_fill=(
               *0,
                0,
            *   0,
            ),
        )

  * image.save(
        output,
        quality=95,
        optimize=True,
    )

    return output


def credit_identifier(credit):
    media_type = str(
        credit.get(
            "media_type",
            "",
        )
    )

    if media_type == "image":
        return (
            "image",
            credit.get(
                "image_id"
            ),
        )

    return (
        "video",
        credit.get(
            "video_id"
        ),
    )


def add_credits(
    description,
    credits,
):
    if not credits:
        return description

    lines = [
        "",
        "",
        "素材提供: Pixabay",
        "https://pixabay.com/",
    ]

    seen = set()

    for credit in credits:
        identifier = credit_identifier(
            credit
        )

        if identifier in seen:
            continue

        seen.add(identifier)

        creator = clean(
            credit.get(
                "creator_name",
                "",
            )
        )

        media_type = str(
            credit.get(
                "media_type",
                "video",
            )
        )

        label = (
            "Image"
            if media_type == "image"
            else "Video"
        )

        if creator:
            lines.append(
                "{} by {} on Pixabay".format(
                    label,
                    creator,
                )
            )

        page_url = str(
            credit.get(
                "page_url",
                "",
            )
        ).strip()

        if page_url:
            lines.append(page_url)

    return (
        str(description or "").rstrip()
        + "\n".join(lines)
    )


def credentials():
    value = (
        Credentials.from_authorized_user_info(
            secret("TOKEN_JSON")
        )
    )

    if (
        value.expired
        and value.refresh_token
    ):
        value.refresh(
            Request()
        )

    return value


def upload_video(
    video,
    thumbnail,
    data,
):
    if DRY or not UPLOAD:
        log.info(
            "YouTube投稿をスキップします。"
        )

        return None

    video_duration = duration(
        video
    )

    if not (
        MIN_SEC
        <= video_duration
        <= MAX_SEC
    ):
        raise RuntimeError(
            "8〜10分外のため投稿中止: "
            "{:.1f}秒".format(
                video_duration
            )
        )

    youtube = build(
        "youtube",
        "v3",
        credentials=credentials(),
        cache_discovery=False,
    )

    tags = data.get(
        "tags",
        [],
    )

    if not isinstance(
        tags,
        list,
    ):
        tags = []

    tags = [
        clean(tag)[:30]
        for tag in tags
        if clean(tag)
    ][:20]

    body = {
        "snippet": {
            "title": str(
                data.get(
                    "title",
                    "自動生成動画",
                )
            )[:100],
            "description": str(
                data.get(
                    "description",
                    "",
                )
            )[:5000],
            "tags": tags,
            "categoryId": "27",
            "defaultLanguage": "ja",
        },
        "status": {
            "privacyStatus": PRIVACY,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(
            str(video),
            mimetype="video/mp4",
            chunksize=(
                8 * 1024 * 1024
            ),
            resumable=True,
        ),
        notifySubscribers=False,
    )

    response = None

    while response is None:
        status, response = (
            request.next_chunk()
        )

        if status:
            log.info(
                "アップロード %d%%",
                int(
                    status.progress()
                    * 100
                ),
            )

    video_id = str(
        response["id"]
    )

    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(
                str(thumbnail),
                mimetype="image/jpeg",
            ),
        ).execute()

    except Exception as error:
        log.warning(
            "サムネイル設定失敗: %s",
            error,
        )

    return video_id


def serializable_used_ids():
    converted = []

    for item in USED_IDS:
        if isinstance(item, tuple):
            converted.append(
                ":".join(
                    str(part)
                    for part in item
                )
            )
        else:
            converted.append(
                str(item)
            )

    return sorted(converted)


def validate_environment():
    for command in (
        "ffmpeg",
        "ffprobe",
    ):
        if not shutil.which(command):
            raise RuntimeError(
                command
                + "がありません。"
            )

    font()

    if not PIXABAY_KEY:
        raise RuntimeError(
            "PIXABAY_API_KEYが未設定です。"
        )

    if not os.getenv(
        "GROQ_API_KEY",
        "",
    ).strip():
        raise RuntimeError(
            "GROQ_API_KEYが未設定です。"
        )


def write_metadata(
    data,
    theme,
    final_seconds,
    youtube_url,
    credits,
):
    metadata = dict(data)

    metadata.update(
        {
            "pipeline_version": VERSION,
            "theme": theme,
            "duration_seconds": (
                final_seconds
            ),
            "youtube_url": youtube_url,
            "dry_run": DRY,
            "unique_media_count": len(
                USED_IDS
            ),
            "used_media_ids": (
                serializable_used_ids()
            ),
            "pixabay_credits": credits,
            "bgm_used": BGM.exists(),
            "groq_model": GROQ_MODEL,
            "groq_max_tokens": (
                GROQ_MAX_TOKENS
            ),
            "research_source_limit": (
                RESEARCH_SOURCE_LIMIT
            ),
            "research_chars_per_source": (
                RESEARCH_CHARS_PER_SOURCE
            ),
        }
    )

    metadata_path = (
        WORK / "metadata.json"
    )

    metadata_path.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main():
    validate_environment()

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
        "わかりやすく、最後まで見たくなる雰囲気",
    ).strip()

    if not theme:
        raise RuntimeError(
            "INPUT_VIDEO_TITLEが空です。"
        )

    log.info("=" * 60)
    log.info(
        "PIPELINE_VERSION: %s",
        VERSION,
    )
    log.info(
        "テーマ: %s",
        theme,
    )
    log.info(
        "Groqモデル: %s",
        GROQ_MODEL,
    )
    log.info(
        "Groq最大出力: %d",
        GROQ_MAX_TOKENS,
    )
    log.info(
        "DRY_RUN: %s",
        DRY,
    )
    log.info("=" * 60)

    sources = research(theme)

    data = create_script(
        LLM(),
        theme,
        genre,
        tone,
        sources,
    )

    narr = None
    timeline = None
    seconds = 0.0
    subtitles = None

    for attempt in range(3):
        (
            narr,
            timeline,
            seconds,
            subtitles,
        ) = create_narration(
            data["scenes"]
        )

        log.info(
            "音声実測時間: %.1f秒",
            seconds,
        )

        if (
            MIN_SEC
            <= seconds
            <= MAX_SEC
        ):
            break

        if attempt < 2:
            log.warning(
                "動画時間を再調整します。"
                "試行%d/2",
                attempt + 1,
            )

            data = adjust_duration(
                data,
                seconds,
                theme,
            )

    if not (
        MIN_SEC
        <= seconds
        <= MAX_SEC
    ):
        raise RuntimeError(
            "8〜10分に調整できませんでした: "
            "{:.1f}秒".format(
                seconds
            )
        )

    script_path = (
        WORK / "script.json"
    )

    script_path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    all_assets, credits = (
        acquire_assets(
            data["scenes"],
            timeline,
        )
    )

    data["description"] = (
        add_credits(
            data.get(
                "description",
                "",
            ),
            credits,
        )
    )

    final = create_final(
        data["scenes"],
        timeline,
        all_assets,
        narr,
        subtitles,
    )

    final_seconds = duration(
        final
    )

    log.info(
        "最終動画時間: %.1f秒",
        final_seconds,
    )

    if not (
        MIN_SEC
        <= final_seconds
        <= MAX_SEC
    ):
        raise RuntimeError(
            "最終動画が8〜10分外です: "
            "{:.1f}秒".format(
                final_seconds
            )
        )

    thumbnail = create_thumbnail(
        data.get(
            "title",
            theme,
        ),
        data["scenes"][0],
        all_assets,
    )

    video_id = upload_video(
        final,
        thumbnail,
        data,
    )

    youtube_url = (
        "https://youtu.be/"
        + video_id
        if video_id
        else ""
    )

    write_metadata(
        data,
        theme,
        final_seconds,
        youtube_url,
        credits,
    )

    log.info("=" * 60)
    log.info(
        "処理完了: %s",
        youtube_url
        or str(final),
    )
    log.info(
        "最終動画: %s",
        final,
    )
    log.info(
        "サムネイル: %s",
        thumbnail,
    )
    log.info(
        "使用素材数: %d",
        len(USED_IDS),
    )
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        log.error(
            "処理が中断されました。"
        )

        raise

    except Exception:
        log.exception(
            "動画生成処理に失敗しました。"
        )

        raise
