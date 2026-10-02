#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio, html, json, logging, math, os, random, re, shutil, subprocess, time
from pathlib import Path
import edge_tts, requests, trafilatura
from duckduckgo_search import DDGS
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

VERSION = "2026.10.01-groq-support-v1"
WORK = Path(os.getenv("WORK_DIR", "work"))
WORK.mkdir(parents=True, exist_ok=True)

W, H, FPS = 1280, 720, 30
MIN_SEC, TARGET_SEC, MAX_SEC = 480, 540, 600
MIN_CUT, MAX_CUT = 5.0, 8.0

VOICE = os.getenv("EDGE_VOICE", "ja-JP-NanamiNeural")
RATE = os.getenv("EDGE_VOICE_RATE", "+0%")
DRY = os.getenv("DRY_RUN", "true").lower() == "true"
UPLOAD = os.getenv("UPLOAD_TO_YOUTUBE", "true").lower() == "true"
PRIVACY = os.getenv("PRIVACY_STATUS", "private")

# Groqモデルの指定
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
RESULTS = int(os.getenv("PIXABAY_RESULTS_PER_QUERY", "20"))
BGM = Path(os.getenv("BGM_PATH", "assets/background_music.mp3"))

USED_IDS = set()
USED_URLS = set()
USED_PAGES = set()
CACHE = {}

FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("youtube-auto")


def run(cmd, timeout=1800):
    log.info("コマンド実行: %s", cmd[0])
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
    value = html.unescape(str(value))
    value = re.sub(r"https?://\S+", "", value)
    return re.sub(r"\s+", " ", value).strip()


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
        60,
    )
    return float(result.stdout.strip())


def secret(name):
    raw = os.getenv(name, "").strip()
    if not raw:
        raise RuntimeError(name + "が未設定です。")
    return json.loads(raw)


def ass_time(seconds):
    cs = max(0, int(round(seconds * 100)))
    hours, cs = divmod(cs, 360000)
    minutes, cs = divmod(cs, 6000)
    seconds, cs = divmod(cs, 100)
    return "{}:{:02}:{:02}.{:02}".format(
        hours,
        minutes,
        seconds,
        cs,
    )


def esc(value):
    return (
        str(value)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


def wrap(value, length):
    value = clean(value)
    return "\n".join(
        value[index:index + length]
        for index in range(0, len(value), length)
    )


def tokens(value):
    ignored = {
        "the", "and", "with", "from", "into", "inside",
        "outside", "close", "view", "footage", "video",
        "scene", "real", "documentary", "showing",
        "during", "while", "near", "over", "under",
    }
    return {
        word
        for word in re.findall(
            r"[a-z0-9]+",
            str(value).lower(),
        )
        if len(word) >= 3 and word not in ignored
    }


def parse_json(text):
    text = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        str(text).strip(),
        flags=re.I | re.S,
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if not match:
            raise RuntimeError("LLM応答にJSONがありません。")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as error:
            raise RuntimeError("JSONのパースに失敗しました: {}\n該当テキスト: {}".format(error, text[:300]))

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
            raise RuntimeError("GROQ_API_KEYがありません。")

    def post(self, url, headers, payload):
        last_error = None
        for attempt in range(5):
            wait = min(90, 5 * 2 ** attempt) + random.uniform(0, 3)
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

                if response.status_code not in {
                    408, 429, 500, 502, 503, 504
                }:
                    raise last_error

            except requests.RequestException as error:
                last_error = error

            log.warning("API再試行まで%.1f秒", wait)
            time.sleep(wait)

        raise RuntimeError(
            "API呼び出し失敗: {}".format(last_error)
        )

    def ask(self, prompt):
        try:
            result = self.post(
                "https://api.groq.com/openai/v1/chat/completions",
                {
                    "Authorization": "Bearer " + self.groq,
                    "Content-Type": "application/json",
                },
                {
                    "model": GROQ_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "有効なJSONだけを返してください。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": prompt,
                        },
                    ],
                    "temperature": 0.35,
                    "max_tokens": 8000,
                },
            )
            return parse_json(
                result["choices"][0]["message"]["content"]
            )
        except Exception as error:
            raise RuntimeError(
                "Groq API処理に失敗しました: {}".format(error)
            )


def research(theme):
    results = []
    seen = set()

    for query in (
        theme,
        theme + " 仕組み 理由",
        theme + " 意外な事実",
    ):
        log.info("Web検索: %s", query)
        try:
            with DDGS() as ddgs:
                for item in ddgs.text(
                    query,
                    region="jp-jp",
                    max_results=5,
                ):
                    url = str(item.get("href", ""))
                    if url and url not in seen:
                        seen.add(url)
                        results.append(item)
        except Exception as error:
            log.warning("Web検索失敗: %s", error)

    sources = []
    for item in results[:10]:
        url = str(item.get("href", ""))
        try:
            page = trafilatura.fetch_url(url)
            body = (
                trafilatura.extract(
                    page,
                    favor_precision=True,
                )
                if page
                else ""
            )
        except Exception:
            body = ""

        text = clean(body or item.get("body", ""))
        if len(text) >= 200:
            sources.append(
                {
                    "title": str(item.get("title", "")),
                    "url": url,
                    "text": text[:5000],
                }
            )
        if len(sources) >= 5:
            break

    (WORK / "research.json").write_text(
        json.dumps(
            sources,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return sources


def split_scene(scene, theme):
    text = clean(scene.get("narration", ""))
    middle = len(text) // 2
    positions = [
        text.rfind(mark, 0, middle + 80)
        for mark in "。！？"
    ]
    cut = max(positions)
    cut = middle if cut < max(40, middle - 120) else cut + 1

    cuts = scene.get("visual_cuts", [])
    if not isinstance(cuts, list):
        cuts = []

    cut_middle = max(1, len(cuts) // 2)
    first = dict(scene)
    second = dict(scene)

    first["narration"] = text[:cut].strip() or text
    second["narration"] = text[cut:].strip() or text
    second["heading"] = (
        clean(scene.get("heading", theme)) + " 続き"
    )
    first["visual_cuts"] = cuts[:cut_middle] or cuts
    second["visual_cuts"] = cuts[cut_middle:] or cuts
    return first, second


def normalize(data, theme):
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

    scenes = data.get("scenes", [])
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("台本にシーンがありません。")

    while len(scenes) < 9:
        index = max(
            range(len(scenes)),
            key=lambda number: len(
                clean(
                    scenes[number].get("narration", "")
                )
            )
            if isinstance(scenes[number], dict)
            else 0,
        )
        raw_scene = scenes.pop(index)

        if not isinstance(raw_scene, dict):
            raw_scene = {
                "heading": theme,
                "narration": str(raw_scene),
                "visual_cuts": [],
            }

        first, second = split_scene(
            raw_scene,
            theme,
        )
        scenes[index:index] = [first, second]

    while len(scenes) > 11:
        extra = scenes.pop()
        target = scenes[-1]

        target["narration"] = (
            clean(target.get("narration", ""))
            + " "
            + clean(extra.get("narration", ""))
        ).strip()

        target_cuts = target.get("visual_cuts", [])
        extra_cuts = extra.get("visual_cuts", [])

        if not isinstance(target_cuts, list):
            target_cuts = []
        if not isinstance(extra_cuts, list):
            extra_cuts = []

        target["visual_cuts"] = target_cuts + extra_cuts

    for number, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            scene = {
                "heading": "シーン{}".format(number),
                "narration": str(scene),
                "visual_cuts": [],
            }
            scenes[number - 1] = scene

        scene["heading"] = clean(
            scene.get("heading")
            or "シーン{}".format(number)
        )
        scene["narration"] = (
            clean(scene.get("narration", ""))
            or "{}について順番に整理します。".format(theme)
        )

        raw_cuts = scene.get("visual_cuts", [])
        if not isinstance(raw_cuts, list):
            raw_cuts = []

        valid_cuts = []
        for cut in raw_cuts:
            if not isinstance(cut, dict):
                continue

            query = (
                clean(cut.get("query", ""))
                or "{} documentary footage".format(theme)
            )
            points = cut.get("card_points", [])

            if not isinstance(points, list):
                points = []

            valid_cuts.append(
                {
                    "narration_excerpt": clean(
                        cut.get("narration_excerpt", "")
                    ),
                    "query": query[:100],
                    "fallback_query": (
                        clean(cut.get("fallback_query", ""))
                        or "{} close up".format(theme)
                    )[:100],
                    "card_title": (
                        clean(cut.get("card_title", ""))
                        or scene["heading"]
                    )[:28],
                    "card_points": [
                        clean(point)[:44]
                        for point in points
                        if clean(point)
                    ][:3],
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
                    ).format(theme)[:100],
                    "fallback_query": (
                        "{} close up"
                    ).format(theme)[:100],
                    "card_title": scene["heading"][:28],
                    "card_points": [],
                }
            ]

        scene["visual_cuts"] = valid_cuts

    data["scenes"] = scenes
    data["title"] = clean(
        data.get("title") or theme
    )[:100]
    return data


def create_script(llm, theme, genre, tone, sources):
    reference_text = "\n\n".join(
        "[{}] {}\nURL: {}\n{}".format(
            index + 1,
            source["title"],
            source["url"],
            source["text"],
        )
        for index, source in enumerate(sources)
    ) or "資料不足。数値を断定しないこと。"

    prompt = """テーマ「{}」の8〜10分YouTube動画をJSONで設計。
ジャンル「{}」、トーン「{}」。
480〜600秒、約4700〜5500文字、9〜11シーン。
冒頭15秒は強い疑問と意外性。
各シーンに原稿と深く関連するvisual_cutsを8〜12個作る。
各カットにnarration_excerpt、具体的な英語query、fallback_query、短い日本語card_title、card_pointsを入れる。
特に、テーマに関連する象徴的な遺物や具体的アイテム（例：ツタンカーメンなら "Golden Mask", "Sarcophagus", "Ancient Egyptian jewelry" など）を英語queryに具体的に含め、抽象的な表現（"documentary footage" など）だけにならないようにすること。
原稿にない要素、著名人、企業ロゴ、商品名を検索語へ追加しない。
「検索クエリには、"close up", "hyper-realistic", "museum exhibit", "gold texture" などの視覚的特徴を表す単語を組み合わせて、高品質な素材がヒットするように設計してください。」
最終シーンはまとめ。JSON以外は禁止。

参考資料:
{}

形式:
{{"title":"","description":"","genre":"","tags":[""],
"scenes":[{{"heading":"","narration":"","visual_cuts":
[{{"narration_excerpt":"","query":"","fallback_query":"",
"card_title":"","card_points":[""]}}]}}]}}
""".format(
        theme,
        genre,
        tone,
        reference_text,
    )

    data = normalize(
        llm.ask(prompt),
        theme,
    )

    urls = "\n".join(
        "- " + source["url"]
        for source in sources
        if source.get("url")
    )

    data["description"] = (
        str(data.get("description", "")).strip()
        + (
            "\n\n参考資料\n" + urls
            if urls
            else ""
        )
        + "\n\n※AI・自動生成技術を使用しています。"
    )
    return data


def adjust_duration(data, seconds, theme):
    scenes = data["scenes"]

    if seconds < MIN_SEC:
        ratio = min(
            2.2,
            TARGET_SEC / max(seconds, 1),
        )
        filler = (
            " ここまでの事実と推測を分けて整理すると、"
            "残された謎と確認済みの情報が"
            "より明確になります。"
        )

        for scene in scenes:
            text = clean(scene["narration"])
            needed = max(
                0,
                int(len(text) * ratio) - len(text),
            )
            repetitions = max(
                1,
                math.ceil(needed / len(filler)),
            )
            scene["narration"] = (
                text + filler * repetitions
            )[:len(text) + needed]

    else:
        ratio = max(
            0.65,
            TARGET_SEC / seconds,
        )

        for scene in scenes:
            text = clean(scene["narration"])
            shortened = text[
                :max(100, int(len(text) * ratio))
            ]
            stop = shortened.rfind("。")

            if stop > len(shortened) * 0.6:
                shortened = shortened[:stop + 1]

            scene["narration"] = shortened

    return normalize(data, theme)


def subtitle_chunks(text):
    output = []

    for sentence in re.split(
        r"(?<=[。！？!?])",
        clean(text),
    ):
        while len(sentence) > 24:
            position = max(
                sentence.rfind(mark, 9, 25)
                for mark in ("、", "，", " ")
            )
            position = 24 if position < 9 else position + 1
            output.append(
                sentence[:position].strip()
            )
            sentence = sentence[position:].strip()

        if sentence.strip():
            output.append(sentence.strip())

    return output or [clean(text)]


async def synthesize(text, path):
    last_error = None

    for attempt in range(4):
        try:
            await edge_tts.Communicate(
                text,
                VOICE,
                rate=RATE,
            ).save(str(path))

            if path.exists() and path.stat().st_size > 1000:
                return

        except Exception as error:
            last_error = error

        await asyncio.sleep(2 ** attempt)

    raise RuntimeError(
        "音声生成失敗: {}".format(last_error)
    )


def make_ass(events, scenes, timeline, path):
    header = """[Script Info]
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
    lines = [header]

    for index, scene in enumerate(scenes):
        start = timeline[index]["start"]
        end = min(
            timeline[index]["end"],
            start + 2.2,
        )
        lines.append(
            "Dialogue: 1,{},{},Heading,,0,0,0,,{}\n".format(
                ass_time(start),
                ass_time(end),
                esc(scene["heading"]),
            )
        )

    for event in events:
        text = esc(event["text"])

        if len(text) > 17:
            middle = len(text) // 2
            text = text[:middle] + r"\N" + text[middle:]

        lines.append(
            "Dialogue: 0,{},{},Default,,0,0,0,,{}\n".format(
                ass_time(event["start"]),
                ass_time(event["end"]),
                text,
            )
        )

    path.write_text(
        "".join(lines),
        encoding="utf-8-sig",
    )


def create_narration(scenes):
    for path in WORK.glob("voice_*.mp3"):
        path.unlink(missing_ok=True)

    async def create_all():
        for index, scene in enumerate(scenes):
            log.info(
                "音声生成 %d/%d",
                index + 1,
                len(scenes),
            )
            await synthesize(
                scene["narration"],
                WORK / "voice_{:02d}.mp3".format(index),
            )

    asyncio.run(create_all())

    paths = []
    timeline = []
    events = []
    cursor = 0.35

    for index, scene in enumerate(scenes):
        path = WORK / "voice_{:02d}.mp3".format(index)
        scene_duration = duration(path)
        start = cursor
        end = start + scene_duration

        timeline.append(
            {
                "start": start,
                "end": end,
                "duration": scene_duration,
            }
        )

        chunks = subtitle_chunks(scene["narration"])
        total = sum(max(1, len(chunk)) for chunk in chunks)
        subtitle_cursor = start

        for chunk_number, chunk in enumerate(chunks):
            if chunk_number == len(chunks) - 1:
                chunk_end = end
            else:
                estimated = (
                    scene_duration
                    * max(1, len(chunk))
                    / total
                )
                chunk_end = min(
                    end,
                    subtitle_cursor
                    + max(1.5, min(3.8, estimated)),
                )

            events.append(
                {
                    "start": subtitle_cursor,
                    "end": chunk_end,
                    "text": chunk,
                }
            )
            subtitle_cursor = chunk_end

        cursor = end + 0.18
        paths.append(path)

    listing = WORK / "audio.txt"
    listing.write_text(
        "".join(
            "file '{}'\n".format(path.resolve())
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
            str(output),
        ]
    )

    subtitles = WORK / "subtitles.ass"
    make_ass(events, scenes, timeline, subtitles)

    return (
        output,
        timeline,
        duration(output),
        subtitles,
    )


def pixabay(query):
    normalized = clean(query).lower()[:100]

    if not normalized:
        return []

    if normalized in CACHE:
        return CACHE[normalized]

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
        CACHE[normalized] = response.json().get("hits", [])
        return CACHE[normalized]

    except Exception as error:
        log.warning("Pixabay検索失敗: %s", error)
        return []


def video_url(video):
    choices = []

    for quality, penalty in (
        ("medium", 0),
        ("large", 100),
        ("small", 10000),
        ("tiny", 20000),
    ):
        file_data = video.get("videos", {}).get(quality, {})
        url = str(file_data.get("url", ""))
        width = int(file_data.get("width", 0) or 0)
        height = int(file_data.get("height", 0) or 0)

        if (
            url
            and width > height
            and width >= 640
            and height >= 360
        ):
            score = (
                abs(width / height - 16 / 9) * 10000
                + penalty
            )
            choices.append((score, url))

    return sorted(choices)[0][1] if choices else None


def relevance(query, video):
    query_words = tokens(query)
    video_words = tokens(video.get("tags", ""))

    if not query_words:
        return 0

    return (
        len(query_words & video_words)
        / max(1, min(len(query_words), 4))
    )


def download_video(video, query, scene_index, cut_index):
    video_id = int(video.get("id", 0) or 0)
    page_url = str(video.get("pageURL", ""))
    url = video_url(video)

    if (
        not video_id
        or not url
        or video_id in USED_IDS
        or url in USED_URLS
        or (page_url and page_url in USED_PAGES)
        or relevance(query, video) <= 0
    ):
        return None, None

    path = WORK / "pixabay_{:02d}_{:03d}.mp4".format(
        scene_index,
        cut_index,
    )

    try:
        response = requests.get(
            url,
            stream=True,
            timeout=120,
        )
        response.raise_for_status()

        with path.open("wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)

        if (
            path.stat().st_size < 100000
            or duration(path) < MIN_CUT
        ):
            path.unlink(missing_ok=True)
            return None, None

        USED_IDS.add(video_id)
        USED_URLS.add(url)

        if page_url:
            USED_PAGES.add(page_url)

        return (
            path,
            {
                "video_id": video_id,
                "page_url": page_url,
                "creator_name": str(video.get("user", "")),
                "query": query,
            },
        )

    except Exception as error:
        log.warning("動画取得失敗: %s", error)
        path.unlink(missing_ok=True)
        return None, None


def acquire_assets(scenes, timeline):
    all_assets = []
    credits = []

    for scene_index, scene in enumerate(scenes):
        count = max(
            len(scene["visual_cuts"]),
            math.ceil(
                timeline[scene_index]["duration"] / MAX_CUT
            ),
        )

        specifications = [
            scene["visual_cuts"][
                index % len(scene["visual_cuts"])
            ]
            for index in range(count)
        ]

        selected_assets = []

        for cut_index, specification in enumerate(specifications):
            asset = None

            for query in (
                specification["query"],
                specification["fallback_query"],
            ):
                results = sorted(
                    pixabay(query),
                    key=lambda video: relevance(query, video),
                    reverse=True,
                )

                for video in results:
                    path, credit = download_video(
                        video,
                        query,
                        scene_index,
                        cut_index,
                    )

                    if path:
                        asset = {
                            "type": "video",
                            "path": path,
                            "spec": specification,
                        }
                        credits.append(credit)
                        break

                if asset:
                    break

            selected_assets.append(
                asset
                or {
                    "type": "card",
                    "path": None,
                    "spec": specification,
                }
            )

        all_assets.append(selected_assets)

        log.info(
            "シーン%d: 動画%d本",
            scene_index + 1,
            sum(
                item["type"] == "video"
                for item in selected_assets
            ),
        )

    (WORK / "pixabay_credits.json").write_text(
        json.dumps(
            credits,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return all_assets, credits


def create_card(scene, specification, index):
    path = WORK / "card_{:03d}.jpg".format(index)
    image = Image.new("RGB", (W, H), (18, 27, 47))
    draw = ImageDraw.Draw(image)

    title_font = ImageFont.truetype(font(), 54)
    point_font = ImageFont.truetype(font(), 34)

    draw.rounded_rectangle(
        (55, 48, 1225, 672),
        radius=30,
        fill=(25, 38, 66),
        outline=(97, 177, 255),
        width=3,
    )

    draw.multiline_text(
        (85, 120),
        wrap(
            specification.get("card_title")
            or scene["heading"],
            18,
        ),
        font=title_font,
        fill=(255, 228, 92),
        stroke_width=2,
        stroke_fill="black",
        spacing=12,
    )

    points = (
        specification.get("card_points")
        or [
            specification.get("narration_excerpt")
            or scene["narration"][:70]
        ]
    )

    y_position = 310

    for point in points[:3]:
        draw.ellipse(
            (
                92,
                y_position + 8,
                108,
                y_position + 24,
            ),
            fill=(97, 177, 255),
        )
        draw.multiline_text(
            (130, y_position),
            wrap(point, 26),
            font=point_font,
            fill="white",
            spacing=10,
        )
        y_position += 105

    image.save(path, quality=94)
    return path


def image_clip(source, output, seconds):
    frames = max(1, int(seconds * FPS))
    fade = max(0, seconds - 0.22)

    video_filter = (
        "scale=1600:900:"
        "force_original_aspect_ratio=increase,"
        "crop=1600:900,"
        "zoompan=z='min(zoom+0.0005,1.10)':"
        "d={}:s=1280x720:fps=30,"
        "fade=t=in:st=0:d=0.22,"
        "fade=t=out:st={:.3f}:d=0.22,"
        "format=yuv420p"
    ).format(frames, fade)

    run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(source),
            "-an",
            "-vf",
            video_filter,
            "-t",
            "{:.3f}".format(seconds),
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
            str(output),
        ]
    )


def video_clip(source, output, seconds, index):
    source_duration = duration(source)
    maximum_start = max(
        0,
        source_duration - seconds - 0.1,
    )
    factor = ((index * 37) % 100) / 100
    start = maximum_start * (0.25 + 0.5 * factor)
    fade = max(0, seconds - 0.18)

    video_filter = (
        "scale=1280:720:"
        "force_original_aspect_ratio=increase,"
        "crop=1280:720,"
        "fps=30,"
        "fade=t=in:st=0:d=0.18,"
        "fade=t=out:st={:.3f}:d=0.18,"
        "format=yuv420p"
    ).format(fade)

    run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "{:.3f}".format(start),
            "-i",
            str(source),
            "-an",
            "-t",
            "{:.3f}".format(seconds),
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
            str(output),
        ]
    )


def create_final(scenes, timeline, all_assets, narr, subtitles):
    clips = []
    card_number = 0

    for scene_index, scene in enumerate(scenes):
        count = max(
            len(all_assets[scene_index]),
            math.ceil(
                timeline[scene_index]["duration"] / MAX_CUT
            ),
        )
        seconds = timeline[scene_index]["duration"] / count

        for cut_index in range(count):
            asset = (
                all_assets[scene_index][cut_index]
                if cut_index < len(all_assets[scene_index])
                else {
                    "type": "card",
                    "spec": scene["visual_cuts"][
                        cut_index % len(scene["visual_cuts"])
                    ],
                }
            )

            output = WORK / "clip_{:02d}_{:03d}.mp4".format(
                scene_index,
                cut_index,
            )

            if (
                asset["type"] == "video"
                and duration(asset["path"]) >= seconds
            ):
                video_clip(
                    asset["path"],
                    output,
                    seconds,
                    cut_index,
                )
            else:
                source = create_card(
                    scene,
                    asset["spec"],
                    card_number,
                )
                image_clip(source, output, seconds)
                card_number += 1

            clips.append(output)

    listing = WORK / "video.txt"
    listing.write_text(
        "".join(
            "file '{}'\n".format(path.resolve())
            for path in clips
        ),
        encoding="utf-8",
    )

    visual = WORK / "visual.mp4"
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
            "-c:v",
            "copy",
            str(visual),
        ]
    )

    raw = WORK / "raw.mp4"
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(visual),
            "-i",
            str(narr),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            str(raw),
        ]
    )

    mixed = WORK / "mixed.mp4"

    if BGM.exists():
        audio_filter = (
            "[1:a]volume=.055,"
            "atrim=0:{:.3f}[b];"
            "[0:a][b]amix=2:duration=first[a]"
        ).format(duration(narr))

        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(raw),
                "-stream_loop",
                "-1",
                "-i",
                str(BGM),
                "-filter_complex",
                audio_filter,
                "-map",
                "0:v",
                "-map",
                "[a]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
                str(mixed),
            ]
        )
    else:
        shutil.copy2(raw, mixed)

    final = WORK / "final.mp4"
    subtitle_path = (
        str(subtitles.resolve())
        .replace("\\", "/")
        .replace(":", r"\:")
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(mixed),
            "-vf",
            "ass='{}'".format(subtitle_path),
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
            str(final),
        ]
    )
    return final


def create_thumbnail(title, scene):
    source = create_card(
        scene,
        scene["visual_cuts"][0],
        999,
    )
    output = WORK / "thumbnail.jpg"

    image = ImageEnhance.Brightness(
        Image.open(source).convert("RGB")
    ).enhance(0.7)

    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(font(), 68)
    text = wrap(title, 14)

    box = draw.multiline_textbbox(
        (0, 0),
        text,
        font=title_font,
        spacing=14,
        align="center",
        stroke_width=6,
    )

    x_position = (W - (box[2] - box[0])) / 2
    y_position = (H - (box[3] - box[1])) / 2

    draw.multiline_text(
        (x_position, y_position),
        text,
        font=title_font,
        fill="#FFE45C",
        stroke_width=7,
        stroke_fill="black",
        spacing=14,
        align="center",
    )

    image.save(output, quality=95)
    return output


def add_credits(description, credits):
    if not credits:
        return description

    lines = [
        "",
        "",
        "映像素材: Videos provided by Pixabay",
        "https://pixabay.com/",
    ]
    seen = set()

    for credit in credits:
        if credit["video_id"] in seen:
            continue

        seen.add(credit["video_id"])

        if credit.get("creator_name"):
            lines.append(
                "Video by {} on Pixabay".format(
                    credit["creator_name"]
                )
            )

        if credit.get("page_url"):
            lines.append(credit["page_url"])

    return description + "\n".join(lines)


def credentials():
    value = Credentials.from_authorized_user_info(
        secret("TOKEN_JSON")
    )

    if value.expired and value.refresh_token:
        value.refresh(Request())

    return value


def upload_video(video, thumbnail, data):
    if DRY or not UPLOAD:
        log.info("YouTube投稿をスキップします。")
        return None

    if not MIN_SEC <= duration(video) <= MAX_SEC:
        raise RuntimeError("8〜10分外のため投稿中止")

    youtube = build(
        "youtube",
        "v3",
        credentials=credentials(),
        cache_discovery=False,
    )

    tags = data.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    body = {
        "snippet": {
            "title": str(
                data.get("title", "自動生成動画")
            )[:100],
            "description": str(
                data.get("description", "")
            )[:5000],
            "tags": tags[:20],
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
            chunksize=8 * 1024 * 1024,
            resumable=True,
        ),
        notifySubscribers=False,
    )

    response = None

    while response is None:
        status, response = request.next_chunk()

        if status:
            log.info(
                "アップロード %d%%",
                int(status.progress() * 100),
            )

    video_id = str(response["id"])

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


def main():
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(
                command + "がありません。"
            )

    font()

    if not PIXABAY_KEY:
        raise RuntimeError(
            "PIXABAY_API_KEYが未設定です。"
        )

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
    log.info("PIPELINE_VERSION: %s", VERSION)
    log.info("テーマ: %s", theme)
    log.info("DRY_RUN: %s", DRY)
    log.info("=" * 60)

    data = create_script(
        LLM(),
        theme,
        genre,
        tone,
        research(theme),
    )

    narr = None
    timeline = None
    seconds = 0
    subtitles = None

    for attempt in range(3):
        narr, timeline, seconds, subtitles = (
            create_narration(data["scenes"])
        )

        log.info("実測時間: %.1f秒", seconds)

        if MIN_SEC <= seconds <= MAX_SEC:
            break

        if attempt < 2:
            data = adjust_duration(
                data,
                seconds,
                theme,
            )

    if not MIN_SEC <= seconds <= MAX_SEC:
        raise RuntimeError(
            "8〜10分に調整できませんでした: {:.1f}秒".format(
                seconds
            )
        )

    (WORK / "script.json").write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    all_assets, credits = acquire_assets(
        data["scenes"],
        timeline,
    )

    data["description"] = add_credits(
        data.get("description", ""),
        credits,
    )

    final = create_final(
        data["scenes"],
        timeline,
        all_assets,
        narr,
        subtitles,
    )

    final_seconds = duration(final)

    if not MIN_SEC <= final_seconds <= MAX_SEC:
        raise RuntimeError(
            "最終動画が8〜10分外です: {:.1f}秒".format(
                final_seconds
            )
        )

    preview = create_thumbnail(
        data.get("title", theme),
        data["scenes"][0],
    )

    video_id = upload_video(
        final,
        preview,
        data,
    )

    youtube_url = (
        "https://youtu.be/" + video_id
        if video_id
        else ""
    )

    metadata = dict(data)
    metadata.update(
        {
            "pipeline_version": VERSION,
            "theme": theme,
            "duration_seconds": final_seconds,
            "youtube_url": youtube_url,
            "dry_run": DRY,
            "unique_video_count": len(USED_IDS),
            "used_video_ids": sorted(USED_IDS),
            "pixabay_credits": credits,
            "bgm_used": BGM.exists(),
        }
    )

    (WORK / "metadata.json").write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log.info(
        "処理完了: %s",
        youtube_url or final,
    )


if __name__ == "__main__":
    main()
