"""YouTube 動画の自動生成エンジン

  1. 台本     : ① Google 検索で企画の事実を集めた「取材メモ」を作成
               ② 取材メモをもとに Gemini（構造化出力）で台本を執筆
               ③ 別の視点で「企画との一致度・具体性」を採点し、基準未満なら指摘を渡して書き直し
  2. 音声     : VOICEVOX（使えない場合は gTTS）でシーンごとにナレーションを合成
  3. 画像     : Pollinations で 1シーン1枚生成（失敗時は Pixabay → 代替画像）
  4. 映像     : 中心基準のゆっくりしたズーム／パン、シーン間クロスフェード、日本語字幕、章ラベル、
               解説キャラ（表情切り替え・声に合わせた揺れ）
  5. 書き出し : フレームを ffmpeg に直接流し込み、BGM（任意）と音量調整をかけて MP4 化
  出力: work/final.mp4, thumbnail.jpg, metadata.json, script.json, research.md, review.json
"""
import io
import os
import re
import sys
import json
import time
import wave
import random
import logging
import subprocess
import traceback
import urllib.parse
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("YouTubeAuto")

# ============================== 設定 ==============================
WORK = Path(os.getenv("WORK_DIR", "work"))
IMG_DIR = WORK / "images"
AUDIO_DIR = WORK / "audio"

W, H = 1920, 1080
FPS = int(os.getenv("VIDEO_FPS", "30"))
SR = 24000                 # 音声のサンプリングレート
SCENE_GAP = 0.35           # シーン間の無音（秒）
FADE = 0.6                 # クロスフェード（秒）
SRC_SCALE = 1.18           # ズーム・パン用に元画像を少し大きめに用意する倍率

TARGET_MINUTES = float(os.getenv("TARGET_MINUTES", "8"))
CHARS_PER_MIN = int(os.getenv("CHARS_PER_MIN", "400"))  # 1分あたりのナレーション文字数の目安（VOICEVOX 1.12倍速）
TARGET_CHARS = int(TARGET_MINUTES * CHARS_PER_MIN)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# 使う Gemini モデル（左から順に試す）。使えないモデルは自動で飛ばし、
# エラーに書かれた後継モデルや、API の一覧から見つけた新しい flash 系モデルに自動で切り替える。
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", "gemini-3.8-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
GEMINI_BUDGET_SEC = float(os.getenv("GEMINI_BUDGET_SEC", "300"))  # 1回の呼び出しで Gemini を待つ時間の上限（秒）

# Gemini が使えないとき（混雑・上限超過）の予備。企画案と同じ Groq の無料枠を使う
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODELS = [m.strip() for m in os.getenv("GROQ_MODELS", "openai/gpt-oss-120b,llama-3.3-70b-versatile").split(",") if m.strip()]

PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
POLLINATIONS_TOKEN = os.getenv("POLLINATIONS_TOKEN")
POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "flux")
IMAGE_INTERVAL = float(os.getenv("IMAGE_INTERVAL", "3"))   # 画像生成 API の呼び出し間隔（秒）
IMAGE_STYLE = os.getenv(
    "IMAGE_STYLE",
    "anime style illustration, detailed background art, soft cinematic lighting, "
    "harmonious color palette, clean lines, no text, no letters, no watermark",
)

VOICEVOX_URL = os.getenv("VOICEVOX_URL", "http://localhost:50021")
VOICEVOX_SPEAKER = int(os.getenv("VOICEVOX_SPEAKER") or "12")  # 12 = 白上虎太郎（ふつう）。Variables で変更可
VOICEVOX_SPEED = float(os.getenv("VOICEVOX_SPEED", "1.12"))
TTS_ENGINE = os.getenv("TTS_ENGINE", "voicevox")               # voicevox / gtts

FONT_PATH = os.getenv("FONT_PATH", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")
BGM_PATH = Path(os.getenv("BGM_PATH", "assets/bgm.mp3"))
BGM_VOLUME = float(os.getenv("BGM_VOLUME", "0.10"))

INPUT_TITLE = (os.getenv("INPUT_VIDEO_TITLE") or "").strip()
INPUT_PLAN = (os.getenv("INPUT_PLAN_TEXT") or "").strip()
CHANNEL_THEME = os.getenv("CHANNEL_THEME") or "幅広いジャンルの雑学・解説"

CAMERA_MOTIONS = ("zoom_in", "zoom_out", "pan_left", "pan_right")
EXPRESSIONS = ("normal", "smile", "surprised", "thinking")

# 解説キャラクター（assets/character/*.png があれば画面右下に表示）
CHARACTER_DIR = Path(os.getenv("CHARACTER_DIR", "assets/character"))
CHARACTER_HEIGHT = int(os.getenv("CHARACTER_HEIGHT", "430"))
TALK_SCALE = float(os.getenv("TALK_SCALE", "1.12"))  # 口パク素材と表情素材の顔の大きさを揃える倍率
MOUTHS = ("a", "i", "u", "e", "o", "n")

# 台本の品質チェック（企画との一致度・具体性）の合格ライン（10点満点）
MIN_RELEVANCE = int(os.getenv("MIN_RELEVANCE", "8"))
MIN_SPECIFICITY = int(os.getenv("MIN_SPECIFICITY", "7"))
SCRIPT_ROUNDS = int(os.getenv("SCRIPT_ROUNDS", "3"))


# ============================== 1. 台本 ==============================

class Scene(BaseModel):
    narration: str = Field(description="日本語のナレーション。話し言葉で1〜3文、40〜90文字。")
    image_prompt: str = Field(description="このシーンの挿絵を描くための英語プロンプト。被写体・場所・構図・光を具体的に。文字は含めない。")
    search_keyword: str = Field(description="予備の画像検索用の英単語1〜2語。")
    camera: str = Field(description="zoom_in, zoom_out, pan_left, pan_right のいずれか。")
    expression: str = Field(description="このシーンでの解説キャラの表情。normal, smile, surprised, thinking のいずれか。")


class Chapter(BaseModel):
    title: str = Field(description="章のタイトル（日本語、15文字以内）")
    scenes: list[Scene]


class Script(BaseModel):
    video_title: str = Field(description="YouTube の動画タイトル（日本語、40文字以内）")
    description: str = Field(description="動画の概要欄の本文（日本語、3〜5行）")
    tags: list[str] = Field(description="YouTube のタグ 8〜15個")
    thumbnail_text: str = Field(description="サムネイルに大きく載せる2行のコピー。1行目と2行目を改行(\\n)で区切り、各行2〜7文字。"
                                            "単語の途中で改行しない。思わずクリックしたくなる強い言葉（例: スマホが\\n危ない！）")
    thumbnail_prompt: str = Field(description="サムネイル用の挿絵の英語プロンプト。動画の主題そのもの（例: スマホの話ならスマートフォン本体、"
                                              "お金の話なら紙幣や財布）を画面の左〜中央に大きくはっきり描く。明るく高コントラストで、背景はシンプル。"
                                              "画面の右側3分の1は人物を置くので主題を置かない。人物・文字・ロゴは描かない。")
    chapters: list[Chapter]


class Review(BaseModel):
    relevance: int = Field(description="企画（タイトル・つかみ・構成）にどれだけ沿っているか。1〜10")
    specificity: int = Field(description="具体的な事実・数字・事例がどれだけ含まれ、中身があるか。1〜10")
    issues: list[str] = Field(description="改善すべき具体的な問題点（日本語）。企画から逸れている箇所、抽象的で中身のない箇所、繰り返し、事実の誤りの疑いなど")
    off_topic_scenes: list[int] = Field(description="企画と関係が薄い、または中身のないシーンの番号")


def plan_text() -> str:
    return INPUT_PLAN or f"テーマ: {INPUT_TITLE or 'おまかせ（視聴者が驚く身近な雑学）'}"


def gemini_client():
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が設定されていません。")
    from google import genai
    return genai.Client(api_key=GEMINI_API_KEY)


_DEAD_MODELS: set[str] = set()      # 404 などで使えなかったモデル
_GEMINI_SKIP_UNTIL = 0.0             # Gemini が混雑・上限超過で全滅したら、しばらく Groq を直接使う
_DISCOVERED = False                  # API のモデル一覧をもう調べたか


def _discover_models(client) -> list[str]:
    """API のモデル一覧から、文章生成に使える flash 系モデルを新しい順に返す（上位4つまで）。"""
    found = []
    try:
        for m in client.models.list():
            name = (m.name or "").removeprefix("models/")
            actions = getattr(m, "supported_actions", None) or []
            if name.startswith("gemini-") and "flash" in name and "generateContent" in actions and not any(
                    k in name for k in ("image", "tts", "audio", "live", "embedding", "preview", "exp", "omni")):
                found.append(name)
    except Exception as e:
        log.warning(f"モデル一覧の取得に失敗: {e}")

    def version(n: str):
        v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return (float(v.group(1)) if v else 0, "lite" not in n)
    found.sort(key=version, reverse=True)
    if found:
        log.info(f"使える可能性のあるモデル: {', '.join(found[:4])}")
    return found[:4]


def call_gemini(prompt: str, config, label: str, budget_sec: float = GEMINI_BUDGET_SEC, models: list | None = None):
    """モデルを順に試す。混雑（503）は間隔をあけて再試行、上限超過（429）は早めに次のモデルへ。
    1回の呼び出しで待つ時間の合計は budget_sec まで（GitHub Actions の無料枠を無駄にしないため）。"""
    global _DISCOVERED
    client = gemini_client()
    deadline = time.time() + budget_sec
    last_err = None
    queue = [m for m in (models or GEMINI_MODELS) if m not in _DEAD_MODELS]
    while queue and time.time() < deadline:
        model = queue.pop(0)
        if model in _DEAD_MODELS:
            continue
        for attempt in range(3):
            if time.time() >= deadline:
                break
            try:
                log.info(f"Gemini（{model}）: {label}（試行 {attempt + 1}）")
                resp = client.models.generate_content(model=model, contents=prompt, config=config)
                if models is None:  # 次回から最初にこのモデルを使う
                    if model in GEMINI_MODELS:
                        GEMINI_MODELS.remove(model)
                    GEMINI_MODELS.insert(0, model)
                return resp
            except Exception as e:
                last_err = e
                msg = str(e)
                log.warning(f"Gemini エラー: {msg[:200]}")
                if "404" in msg or "NOT_FOUND" in msg or "limit: 0" in msg or "limit:0" in msg:
                    # このモデルは使えない（終了済み・無料枠なし）。エラー文に後継モデルがあれば候補に足す
                    _DEAD_MODELS.add(model)
                    for nxt in re.findall(r"use models/([\w.\-]+)", msg):
                        if nxt not in _DEAD_MODELS and nxt not in queue:
                            log.info(f"エラーで案内された後継モデル {nxt} を試します。")
                            queue.insert(0, nxt)
                    break
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    if attempt >= 1:
                        break          # 上限超過は1回だけ待って再試行し、だめなら次のモデルへ
                    time.sleep(min(20, max(0, deadline - time.time())))
                    continue
                # 混雑（503）などは、少しずつ間隔をあけて再試行
                time.sleep(min(30 * (2 ** attempt), max(0, deadline - time.time())))
        if not queue and not _DISCOVERED and models is None and time.time() < deadline:
            _DISCOVERED = True  # 候補が尽きたら、API の一覧から探す（1回だけ）
            queue = [m for m in _discover_models(client) if m not in _DEAD_MODELS]
    raise RuntimeError(f"Gemini の呼び出しに失敗しました（{label}）: {last_err}")


# ---------- Groq（Gemini が使えないときの予備。無料枠あり） ----------

def call_groq(prompt: str, label: str, temperature: float, json_mode: bool) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY が設定されていません。")
    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY)
    last_err = None
    for model in GROQ_MODELS:
        for attempt in range(2):
            try:
                log.info(f"Groq（{model}）: {label}（試行 {attempt + 1}）")
                kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
                r = client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": prompt}],
                    temperature=temperature, max_completion_tokens=16000, **kwargs)
                text = (r.choices[0].message.content or "").strip()
                if text:
                    return text
            except Exception as e:
                last_err = e
                log.warning(f"Groq エラー: {str(e)[:200]}")
                time.sleep(20)
    raise RuntimeError(f"Groq の呼び出しに失敗しました（{label}）: {last_err}")


# ---------- 共通の呼び出し口：Gemini → だめなら Groq ----------

def _gemini_available() -> bool:
    if time.time() < _GEMINI_SKIP_UNTIL:
        log.info("Gemini は直前に使えなかったため、Groq を使います。")
        return False
    return bool(GEMINI_API_KEY)


def _gemini_failed(e: Exception) -> None:
    """Gemini が全モデルで失敗したら、10分間は試さずに Groq を使う（待ち時間の無駄を防ぐ）。"""
    global _GEMINI_SKIP_UNTIL
    _GEMINI_SKIP_UNTIL = time.time() + 600
    log.warning(f"Gemini が使えないため Groq に切り替えます: {str(e)[:150]}")


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return m.group(1).strip() if m else text


def llm_text(prompt: str, label: str, temperature: float = 0.3) -> str:
    if _gemini_available():
        from google.genai import types
        try:
            resp = call_gemini(prompt, types.GenerateContentConfig(temperature=temperature), label)
            if (resp.text or "").strip():
                return resp.text.strip()
        except Exception as e:
            _gemini_failed(e)
    return call_groq(prompt, label, temperature, json_mode=False)


def llm_json(prompt: str, schema: type[BaseModel], label: str, temperature: float = 0.7):
    if _gemini_available():
        from google.genai import types
        try:
            config = types.GenerateContentConfig(response_mime_type="application/json",
                                                 response_schema=schema, temperature=temperature)
            resp = call_gemini(prompt, config, label)
            return resp.parsed if resp.parsed is not None else schema.model_validate_json(resp.text)
        except Exception as e:
            _gemini_failed(e)
    from json_repair import repair_json
    schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    full = (f"{prompt}\n\n# 出力形式\n次の JSON スキーマに従う JSON オブジェクトだけを出力してください。"
            f"説明文やコードブロックは付けないこと。\n{schema_text}")
    last_err = None
    for _ in range(2):
        raw = call_groq(full, label, temperature, json_mode=True)
        try:
            return schema.model_validate_json(repair_json(_strip_code_fence(raw)))
        except Exception as e:
            last_err = e
            log.warning(f"Groq の出力を読み取れませんでした: {str(e)[:150]}")
    raise RuntimeError(f"{label} に失敗しました: {last_err}")


# ---------- 1-1. リサーチ：台本の「中身」になる事実を先に集める ----------

def web_search_snippets(max_chars: int = 6000) -> str:
    """無料の検索（DuckDuckGo）で、企画に関する検索結果の要約文を集める。APIキー不要。"""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
    except ImportError:
        log.warning("検索ライブラリ（ddgs）がないため、Web 検索をスキップします。")
        return ""
    title = INPUT_TITLE or re.sub(r"[*【】企画\d]", "", plan_text().splitlines()[0]).strip()
    outline = [re.sub(r"^[\s　\d.、．]+", "", l).strip() for l in plan_text().splitlines()
               if re.match(r"^[\s　]*\d+[.．、]", l)]
    queries = [title] + [f"{title} {o}" for o in outline[:4]]
    seen, lines = set(), []
    try:
        with DDGS() as ddgs:
            for q in queries:
                try:
                    for r in ddgs.text(q, region="jp-jp", max_results=6) or []:
                        url = r.get("href") or r.get("url") or ""
                        if url in seen:
                            continue
                        seen.add(url)
                        lines.append(f"- {r.get('title', '')}：{r.get('body', '')}（{url}）")
                except Exception as e:
                    log.warning(f"検索に失敗（{q}）: {str(e)[:120]}")
                time.sleep(2)
    except Exception as e:
        log.warning(f"Web 検索に失敗しました: {str(e)[:150]}")
    text = "\n".join(lines)[:max_chars]
    log.info(f"Web 検索結果: {len(lines)}件")
    return text


def research_topic() -> str:
    """企画に関する具体的な事実を集めた「取材メモ」を作る。
    台本を書く前に材料を揃えることで、抽象的で中身のない台本になるのを防ぐ。
    ① Gemini の Google 検索（無料枠で使えれば）→ ② 無料の Web 検索結果＋AI のまとめ、の順に試す。"""
    from google.genai import types
    rules = """# 取材メモの要件
- 企画の構成（章）ごとに見出しを立てる。
- 各見出しの下に、具体的な事実を箇条書きで4〜8個。数字・年・人物・場所・具体的な事例・意外なエピソードを優先する。
- 視聴者が「へえ」と思う意外な事実を、全体で5つ以上含める。
- 諸説ある内容は「諸説あり」と明記する。確認できなかったことは書かない。
- 各事実の末尾に、情報源のサイト名を（）で付ける。
- 日本語で書く。"""
    base = f"次の YouTube 動画企画の台本を書くための「取材メモ」を作ってください。\n\n# 企画\n{plan_text()}\n\n"

    # ① Google 検索付き：無料枠では使えないことが多いので、1モデル・短時間だけ試す
    try:
        config = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())],
                                             temperature=0.3)
        resp = call_gemini(base + "Google 検索で調べ、企画の各章で使える具体的な材料を集めてください。\n\n" + rules,
                           config, "企画のリサーチ（Google 検索）", budget_sec=60, models=GEMINI_MODELS[:1])
        notes = (resp.text or "").strip()
        if len(notes) > 300:
            log.info(f"取材メモ（Google 検索）: {len(notes)}文字")
            return notes
    except Exception as e:
        log.warning(f"Google 検索付きのリサーチは使えませんでした（別の方法で続行）: {str(e)[:120]}")

    # ② 無料の Web 検索結果をもとに、AI にまとめさせる
    snippets = web_search_snippets()
    if snippets:
        prompt = (base + "以下の Web 検索結果を主な材料にし、足りない部分はあなたの確かな知識で補ってください。"
                  "検索結果と矛盾することは書かないでください。\n\n# Web 検索結果\n" + snippets + "\n\n" + rules)
    else:
        prompt = base + "あなたの確かな知識をもとに、企画の各章で使える具体的な材料を集めてください。\n\n" + rules
    notes = llm_text(prompt, "企画のリサーチ")
    log.info(f"取材メモ: {len(notes)}文字")
    return notes


# ---------- 1-2. 台本の執筆 ----------

def build_script_prompt(research: str, feedback: str = "") -> str:
    return f"""あなたは日本の人気YouTube解説チャンネルの構成作家です。
次の企画と取材メモをもとに、約{TARGET_MINUTES:.0f}分の動画台本を作成してください。

# 企画（この内容から絶対に逸れないこと）
{plan_text()}

# 取材メモ（台本の事実はここから使う）
{research}

# チャンネルの方向性
{CHANNEL_THEME}

# 内容の要件（最重要）
- 企画のタイトル・つかみ・構成に忠実に従う。章は企画の構成の順番どおりに対応させる。
- すべてのシーンに、取材メモにある具体的な情報（数字・固有名詞・事例・エピソード）を最低1つ入れる。
- 「いかがでしたか」「様々な」「色々な」「重要です」のような、中身のない言い回しやつなぎだけのシーンは作らない。
- 前のシーンと同じことを言い換えて繰り返さない。1シーンごとに新しい情報を1つ進める。
- 取材メモにない事実を作らない。諸説あるものは断定しない。

# 構成・話し方の要件
- ナレーションはすべて自然な日本語の話し言葉（です・ます調）。音声合成で読み上げるため、
  記号・絵文字・括弧書き・読み間違えやすい英字略語は使わない。
- 数字は算用数字で書く（例: 1.5倍、65本、2024年、3000万円）。漢数字は「一番」「一緒」のような言葉の中だけで使う。
- ナレーションの合計は約{TARGET_CHARS}文字（±10%）。これが動画の長さを決めるので必ず守る。
- 冒頭の2〜3シーンで、企画の「つかみ」の内容を使って視聴者の興味を強く引く。
- 章は4〜6個。各章は6〜10シーン。1シーンのナレーションは1〜3文、40〜90文字。
- 最後の章で要点をまとめ、最後のシーンで高評価とチャンネル登録を自然に促す。
- expression は解説キャラの表情。通常の説明→normal、意外な事実→surprised、楽しい話題や締め→smile、
  問いかけや考察→thinking。normal のときだけ口が動くので、全体の6割以上は normal にし、
  ほかの表情は印象的な場面に絞って使う。

# 画像プロンプトの要件
- image_prompt は英語で、そのシーンのナレーション内容が一目で分かる具体的な場面を描写する。
  （例: 年号の話ならその時代の街並み、数字の比較なら比較対象そのもの）
- 文字・ロゴ・実在の有名人・既存アニメのキャラクターは含めない。
- 同じ人物が繰り返し登場する場合は、毎回同じ外見の説明（年齢・髪型・服装）を入れる。
- camera は単調にならないよう、zoom_in / zoom_out / pan_left / pan_right を織り交ぜる。
{feedback}"""


# ---------- 1-3. 品質チェック：企画とズレていないか・中身があるかを別の目で採点 ----------

def review_script(script: Script, research: str) -> Review:
    numbered, no = [], 0
    for ch in script.chapters:
        numbered.append(f"\n## {ch.title}")
        for s in ch.scenes:
            no += 1
            numbered.append(f"[{no}] {s.narration}")
    prompt = f"""あなたは厳しい YouTube 動画の編集長です。次の台本を、企画と取材メモに照らして採点してください。
甘い採点はしないでください。企画から逸れた内容、抽象的で中身のないシーン、繰り返しは厳しく減点します。

# 企画
{plan_text()}

# 取材メモ
{research[:6000]}

# 台本（動画タイトル: {script.video_title}）
{chr(10).join(numbered)}"""
    return llm_json(prompt, Review, "台本の品質チェック", temperature=0.2)


def count_chars(script: Script) -> int:
    return sum(len(s.narration) for c in script.chapters for s in c.scenes)


def generate_script() -> tuple[Script, Review, str]:
    log.info("=== 企画 ===\n" + plan_text()[:1500])
    research = research_topic()
    (WORK / "research.md").write_text(research, encoding="utf-8")

    best, best_review, best_score, feedback = None, None, -999, ""
    for rnd in range(1, SCRIPT_ROUNDS + 1):
        try:
            script = clean_script(llm_json(build_script_prompt(research, feedback), Script, f"台本の執筆（{rnd}回目）"))
        except Exception as e:
            if best is not None:  # 書き直しに失敗しても、それまでの最良の台本で進める
                log.warning(f"台本の書き直しに失敗したため、前回までの台本を使います: {str(e)[:150]}")
                break
            raise
        n = count_chars(script)
        try:
            review = review_script(script, research)
        except Exception as e:  # 採点だけ失敗した場合は、台本はそのまま使う
            log.warning(f"品質チェックに失敗したため、採点なしで進めます: {str(e)[:150]}")
            review = Review(relevance=0, specificity=0, issues=["品質チェックを実行できませんでした"], off_topic_scenes=[])
            if best is None:
                best, best_review = script, review
            break
        length_ok = n >= TARGET_CHARS * 0.8
        score = review.relevance + review.specificity - (0 if length_ok else 3)
        log.info(f"台本 {rnd}回目: {len(script.chapters)}章 / {sum(len(c.scenes) for c in script.chapters)}シーン / "
                 f"{n}文字（目標 {TARGET_CHARS}） / 企画との一致 {review.relevance} / 具体性 {review.specificity}")
        for issue in review.issues[:8]:
            log.info(f"  指摘: {issue}")
        if score > best_score:
            best, best_review, best_score = script, review, score
        if review.relevance >= MIN_RELEVANCE and review.specificity >= MIN_SPECIFICITY and length_ok:
            break
        # 指摘を次の執筆にフィードバックして書き直させる
        lines = ["\n# 前回の台本への編集長からの指摘（すべて直すこと）"]
        lines += [f"- {i}" for i in review.issues[:10]]
        if review.off_topic_scenes:
            lines.append(f"- 前回の {', '.join(map(str, review.off_topic_scenes[:15]))} 番のシーンは"
                         "企画と関係が薄いか中身がありませんでした。同じ失敗をしないこと。")
        if not length_ok:
            lines.append(f"- ナレーション合計が{n}文字で短すぎます。取材メモの事実を使って約{TARGET_CHARS}文字にしてください。")
        feedback = "\n".join(lines)
    best = expand_script(best, research)
    return best, best_review, research


def expand_script(script: Script, research: str) -> Script:
    """台本が目標の文字数に届かないとき、短い章から順に1章ずつ書き足す。
    AI は長い文章を一度に書くと短くまとめがちなので、章単位に分けて確実に長さを出す。"""
    for rnd in range(2):
        total = count_chars(script)
        if total >= TARGET_CHARS * 0.9:
            break
        per_ch = TARGET_CHARS / max(1, len(script.chapters))
        log.info(f"台本が短いため章ごとに書き足します（{total}文字 / 目標 {TARGET_CHARS}文字、{rnd + 1}回目）")
        titles = "\n".join(f"{i + 1}. {c.title}" for i, c in enumerate(script.chapters))
        order = sorted(range(len(script.chapters)),
                       key=lambda i: sum(len(x.narration) for x in script.chapters[i].scenes))
        for i in order:
            if count_chars(script) >= TARGET_CHARS * 0.95:
                break
            ch = script.chapters[i]
            cur = sum(len(x.narration) for x in ch.scenes)
            if cur >= per_ch * 0.9:
                continue
            want = int(per_ch)
            prev_tail = script.chapters[i - 1].scenes[-1].narration if i > 0 else "（動画の冒頭）"
            next_head = script.chapters[i + 1].scenes[0].narration if i + 1 < len(script.chapters) else "（動画の締め）"
            current = "\n".join(f"- {x.narration}" for x in ch.scenes)
            prompt = f"""あなたは日本の人気YouTube解説チャンネルの構成作家です。
次の動画台本のうち「第{i + 1}章 {ch.title}」だけを、内容を深掘りして書き直してください。

# 企画
{plan_text()}

# 動画全体の章立て
{titles}

# 取材メモ（事実はここから使う）
{research[:8000]}

# 今の第{i + 1}章のナレーション（{cur}文字。短すぎるので書き足す）
{current}

# 直前の章の最後のセリフ
{prev_tail}

# 直後の章の最初のセリフ
{next_head}

# 要件
- 第{i + 1}章のナレーション合計を約{want}文字にする（{int(want * 0.9)}〜{int(want * 1.15)}文字）。これを必ず守る。
- 今の内容は残したうえで、取材メモの具体的な事実（数字・事例・エピソード・理由・身近な例え）を足して深掘りする。
- 他の章の内容と重複させない。前後の章とつながるようにする。
- 1シーンは1〜3文、40〜90文字。シーン数は{max(6, round(want / 65))}前後。
- ナレーションは自然な日本語の話し言葉（です・ます調）。記号・絵文字・括弧書きは使わない。
- 数字は算用数字で書く（例: 1.5倍、65本、2024年）。
- expression は normal / smile / surprised / thinking のいずれか（6割以上 normal）。
- camera は zoom_in / zoom_out / pan_left / pan_right を織り交ぜる。
- image_prompt は英語で、そのシーンの内容が一目で分かる具体的な場面。文字・ロゴ・実在の有名人は含めない。
- title は「{ch.title}」のままにする。"""
            try:
                new = llm_json(prompt, Chapter, f"第{i + 1}章の書き足し", temperature=0.7)
                tmp = clean_script(Script(video_title=script.video_title, description="", tags=[],
                                          thumbnail_text="", thumbnail_prompt="", chapters=[new]))
                new_ch = tmp.chapters[0]
                new_len = sum(len(x.narration) for x in new_ch.scenes)
                if new_len > cur:
                    new_ch.title = ch.title
                    script.chapters[i] = new_ch
                    log.info(f"  第{i + 1}章: {cur} → {new_len}文字")
            except Exception as e:
                log.warning(f"  第{i + 1}章の書き足しに失敗（元のまま）: {str(e)[:150]}")
    log.info(f"台本の最終的な長さ: {count_chars(script)}文字（目標 {TARGET_CHARS}文字、"
             f"約{count_chars(script) / CHARS_PER_MIN:.1f}分の見込み）")
    return script


def clean_script(script: Script) -> Script:
    """読み上げに不要な記号の除去、空シーンの削除、camera・expression の正規化。"""
    for ch in script.chapters:
        cleaned = []
        for s in ch.scenes:
            s.narration = re.sub(r"[\"“”「」『』（）()【】\[\]*#＊＃~〜]", "", s.narration).strip()
            if len(s.narration) < 2:
                continue
            if s.camera not in CAMERA_MOTIONS:
                s.camera = random.choice(CAMERA_MOTIONS)
            if s.expression not in EXPRESSIONS:
                s.expression = "normal"
            cleaned.append(s)
        ch.scenes = cleaned
    script.chapters = [c for c in script.chapters if c.scenes]
    if not script.chapters:
        raise RuntimeError("台本にシーンがありません。")
    return script


def notify_discord(text: str) -> None:
    if not (os.getenv("DISCORD_BOT_TOKEN") and os.getenv("DISCORD_CHANNEL_ID")):
        return
    try:
        from discord_utils import post_message
        post_message(text)
    except Exception as e:
        log.warning(f"Discord 通知に失敗: {e}")


# ============================== 2. 音声 ==============================

def decode_audio(path_or_bytes, tempo: float = 1.0) -> np.ndarray:
    """任意の音声を ffmpeg で 24kHz モノラル float32 に変換する。"""
    cmd = ["ffmpeg", "-v", "error", "-i", "pipe:0" if isinstance(path_or_bytes, bytes) else str(path_or_bytes)]
    if tempo != 1.0:
        cmd += ["-filter:a", f"atempo={tempo}"]
    cmd += ["-f", "f32le", "-ac", "1", "-ar", str(SR), "pipe:1"]
    out = subprocess.run(cmd, input=path_or_bytes if isinstance(path_or_bytes, bytes) else None,
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


class TTS:
    def __init__(self):
        self.engine = "gtts"
        self.credit = ""
        if TTS_ENGINE == "voicevox" and self._wait_voicevox():
            self.engine = "voicevox"
            self.credit = f"VOICEVOX:{self._speaker_name()}"
        log.info(f"音声エンジン: {self.engine} {self.credit}")

    def _wait_voicevox(self, timeout: int = 240) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if requests.get(f"{VOICEVOX_URL}/version", timeout=5).ok:
                    return True
            except requests.RequestException:
                pass
            time.sleep(3)
        log.warning("VOICEVOX に接続できなかったため gTTS を使用します。")
        return False

    def _speaker_name(self) -> str:
        try:
            for sp in requests.get(f"{VOICEVOX_URL}/speakers", timeout=10).json():
                if any(st["id"] == VOICEVOX_SPEAKER for st in sp["styles"]):
                    return sp["name"]
        except Exception:
            pass
        return "白上虎太郎"

    def synth(self, text: str) -> tuple[np.ndarray, list | None]:
        """音声と口パク用のタイミング [(開始秒, 終了秒, 口の形), ...] を返す。
        口の形は a/i/u/e/o/n（n=閉じ）。gTTS ではタイミングが取れないため None。"""
        for attempt in range(3):
            try:
                if self.engine == "voicevox":
                    return self._synth_voicevox(text)
                return self._synth_gtts(text), None
            except Exception as e:
                log.warning(f"音声合成に失敗（{attempt + 1}回目）: {e}")
                time.sleep(3)
        log.error(f"音声合成をあきらめ、無音にします: {text[:30]}")
        return np.zeros(int(SR * max(2.0, len(text) / 6)), dtype=np.float32), []

    def _synth_voicevox(self, text: str) -> tuple[np.ndarray, list]:
        q = requests.post(f"{VOICEVOX_URL}/audio_query",
                          params={"text": text, "speaker": VOICEVOX_SPEAKER}, timeout=120)
        q.raise_for_status()
        query = q.json()
        query.update({"speedScale": VOICEVOX_SPEED, "outputSamplingRate": SR, "outputStereo": False,
                      "prePhonemeLength": 0.05, "postPhonemeLength": 0.05})
        r = requests.post(f"{VOICEVOX_URL}/synthesis", params={"speaker": VOICEVOX_SPEAKER},
                          json=query, timeout=300)
        r.raise_for_status()
        with wave.open(io.BytesIO(r.content)) as wf:
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            rate = wf.getframerate()
        audio = decode_audio(r.content) if rate != SR else pcm.astype(np.float32) / 32768.0
        return audio, visemes_from_query(query, len(audio) / SR)

    def _synth_gtts(self, text: str) -> np.ndarray:
        from gtts import gTTS
        buf = io.BytesIO()
        gTTS(text=text, lang="ja").write_to_fp(buf)
        return decode_audio(buf.getvalue(), tempo=1.2)  # gTTS はゆっくりなので少し速める


CLOSED_CONSONANTS = {"m", "my", "b", "by", "p", "py"}  # 唇を閉じて発音する子音


def visemes_from_query(query: dict, actual_dur: float) -> list:
    """VOICEVOX の audio_query（モーラごとの子音・母音の長さ）から口の形のタイミングを作る。"""
    speed = query.get("speedScale", 1.0) or 1.0
    t = query.get("prePhonemeLength", 0.1) / speed
    vis = []
    for ap in query.get("accent_phrases", []):
        for m in ap.get("moras", []):
            vowel = (m.get("vowel") or "").lower()
            shape = vowel if vowel in ("a", "i", "u", "e", "o") else "n"   # N（ん）・cl（っ）は閉じ
            cons = (m.get("consonant_length") or 0) / speed
            if cons > 0:
                vis.append((t, t + cons, "n" if m.get("consonant") in CLOSED_CONSONANTS else shape))
                t += cons
            vl = (m.get("vowel_length") or 0) / speed
            vis.append((t, t + vl, shape))
            t += vl
        pm = ap.get("pause_mora")
        if pm:
            d = (pm.get("vowel_length") or 0) / speed
            vis.append((t, t + d, "n"))
            t += d
    total = t + query.get("postPhonemeLength", 0.1) / speed
    if total > 0 and actual_dur > 0:  # 実際の音声の長さとのズレを補正
        k = actual_dur / total
        vis = [(a * k, b * k, sh) for a, b, sh in vis]
    return vis


def write_wav(path: Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())


def main_subtitle_layout() -> tuple:
    """本編の字幕のフォント・横幅・行数（キャラがいるときは、キャラと重ならない幅）。"""
    font = load_font(58)
    char = Character(CHARACTER_HEIGHT)
    width = (W - char.width - 30) - 80 if char.enabled else 1640
    return font, width, 2


def synth_with_subtitles(tts: "TTS", text: str, font, width: int, max_lines: int):
    """字幕1枚ぶんずつ音声を作ってつなげる。字幕の切り替わりと音声の区切りが必ず一致する。
    戻り値: (音声, 口パクのタイミング, [(開始秒, 終了秒, 字幕), ...])"""
    parts = subtitle_chunks(text, font, width, max_lines)
    audios, visemes, subs, t = [], [], [], 0.0
    for part in parts:
        audio, vis = tts.synth(part)
        dur = len(audio) / SR
        audios.append(audio)
        subs.append((t, t + dur, part))
        if visemes is not None:
            visemes = None if vis is None else visemes + [(a + t, b + t, sh) for a, b, sh in vis]
        t += dur
    return np.concatenate(audios), visemes, subs


def build_timeline(script: Script, tts: TTS) -> tuple[list[dict], Path]:
    """シーンごとに音声を作り、開始時刻・長さを確定させる（音声と映像が必ず一致する）。"""
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    timeline, pieces, t = [], [], 0.0
    gap = np.zeros(int(SR * SCENE_GAP), dtype=np.float32)
    total = sum(len(c.scenes) for c in script.chapters)
    font, width, lines = main_subtitle_layout()
    for ci, ch in enumerate(script.chapters):
        for si, sc in enumerate(ch.scenes):
            audio, visemes, sub_parts = synth_with_subtitles(tts, sc.narration, font, width, lines)
            voice_dur = len(audio) / SR
            pieces += [audio, gap]
            dur = voice_dur + SCENE_GAP
            timeline.append({
                "index": len(timeline), "chapter": ci, "chapter_title": ch.title,
                "start": t, "duration": dur, "voice_duration": voice_dur,
                "narration": sc.narration, "image_prompt": sc.image_prompt,
                "search_keyword": sc.search_keyword, "camera": sc.camera,
                "expression": sc.expression, "visemes": visemes, "sub_parts": sub_parts,
            })
            t += dur
            log.info(f"音声 {len(timeline)}/{total}: {voice_dur:.1f}秒")
    narration = np.concatenate(pieces)
    path = WORK / "narration.wav"
    write_wav(path, narration)
    chars = sum(len(x["narration"]) for x in timeline)
    log.info(f"ナレーション合計: {t / 60:.1f}分（{chars}文字、実測 {chars / max(t / 60, 0.01):.0f}文字/分。"
             f"長さがずれる場合は CHARS_PER_MIN をこの値にしてください）")
    return timeline, path


# ============================== 3. 画像 ==============================

def prepare_image(raw: bytes) -> Image.Image:
    """中央を基準に 16:9 にクロップし、ズーム余白込みのサイズにリサイズする（引き伸ばしで歪ませない）。"""
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = img.size
    target = W / H
    if w / h > target:
        nw = int(h * target)
        img = img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w / target)
        img = img.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    return img.resize((int(W * SRC_SCALE), int(H * SRC_SCALE)), Image.LANCZOS)


def placeholder_image(seed: int) -> Image.Image:
    """すべての取得に失敗したときの代替画像（単色の灰色ではなく、ぼかしたグラデーション）。"""
    rng = random.Random(seed)
    c1 = tuple(rng.randint(20, 90) for _ in range(3))
    c2 = tuple(rng.randint(90, 180) for _ in range(3))
    sw, sh = int(W * SRC_SCALE), int(H * SRC_SCALE)
    grad = np.linspace(0, 1, sw)[None, :, None]
    arr = (np.array(c1) * (1 - grad) + np.array(c2) * grad).repeat(sh, axis=0)
    return Image.fromarray(arr.astype(np.uint8)).filter(ImageFilter.GaussianBlur(40))


def fetch_pollinations(prompt: str, seed: int) -> bytes | None:
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(f"{prompt}, {IMAGE_STYLE}", safe="")
    params = {"width": W, "height": H, "nologo": "true", "seed": seed, "model": POLLINATIONS_MODEL}
    headers = {"Authorization": f"Bearer {POLLINATIONS_TOKEN}"} if POLLINATIONS_TOKEN else {}
    for attempt in range(3):
        try:
            res = requests.get(url, params=params, headers=headers, timeout=150)
            if res.status_code == 200 and res.headers.get("content-type", "").startswith("image") \
                    and len(res.content) > 20_000:
                return res.content
            if res.status_code == 429:
                log.warning("画像生成のレート制限。30秒待機します。")
                time.sleep(30)
                continue
            log.warning(f"画像生成エラー {res.status_code}")
        except requests.RequestException as e:
            log.warning(f"画像生成タイムアウト等: {e}")
        time.sleep(5 * (attempt + 1))
    return None


def fetch_pixabay(keyword: str) -> bytes | None:
    if not PIXABAY_API_KEY:
        return None
    for image_type in ("illustration", "photo"):  # 画風を揃えるためイラストを優先
        try:
            res = requests.get("https://pixabay.com/api/", timeout=30, params={
                "key": PIXABAY_API_KEY, "q": keyword, "image_type": image_type,
                "orientation": "horizontal", "safesearch": "true", "per_page": 5})
            hits = res.json().get("hits", []) if res.ok else []
            if hits:
                return requests.get(hits[0]["largeImageURL"], timeout=60).content  # 高解像度版
        except Exception as e:
            log.warning(f"Pixabay エラー: {e}")
    return None


def get_scene_image(prompt: str, keyword: str, path: Path, seed: int) -> Path:
    if path.exists():
        return path
    for source, raw in (("Pollinations", lambda: fetch_pollinations(prompt, seed)),
                        ("Pixabay", lambda: fetch_pixabay(keyword))):
        data = raw()
        if data:
            try:
                prepare_image(data).save(path, "JPEG", quality=92)
                return path
            except Exception as e:
                log.warning(f"{source} の画像を読み込めませんでした: {e}")
    log.warning(f"画像を取得できなかったため代替画像を使用: {prompt[:50]}")
    placeholder_image(seed).save(path, "JPEG", quality=92)
    return path


def generate_images(timeline: list[dict], base_seed: int) -> None:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    for t in timeline:
        log.info(f"画像 {t['index'] + 1}/{len(timeline)}")
        t["image"] = str(get_scene_image(t["image_prompt"], t["search_keyword"],
                                         IMG_DIR / f"scene_{t['index']:03d}.jpg", base_seed + t["index"]))
        time.sleep(IMAGE_INTERVAL)


# ============================== 4. 映像 ==============================

def load_font(size: int) -> ImageFont.FreeTypeFont:
    for p in (FONT_PATH, "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        if Path(p).exists():
            return ImageFont.truetype(p, size, index=0)  # index 0 = 日本語(JP)
    raise RuntimeError("日本語フォントが見つかりません。fonts-noto-cjk をインストールしてください。")


SUB_FONT = None
LABEL_FONT = None


def wrap_text(text: str, font, max_width: int) -> list[str]:
    """日本語を1文字単位で折り返す。句読点が行頭に来ないよう調整。"""
    lines, cur = [], ""
    for ch in text:
        if font.getlength(cur + ch) > max_width and cur:
            if ch in "、。！？」』）":
                cur += ch
                ch = ""
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


# ---------- 字幕の漢数字 → 算用数字（表示だけ。読み上げは元の文のまま） ----------

_KDIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KSMALL = {"十": 10, "百": 100, "千": 1000}
_KBIG = "万億兆"
_KNUM_CHARS = "〇零一二三四五六七八九十百千万億兆"
# 数字のあとに続くと「数量」と判断できる単位（1文字の漢数字はこれが続くときだけ変換する）
_COUNTERS = ("パーセント", "メートル", "キロ", "センチ", "ミリ", "トン", "ドル", "ユーロ", "ポイント", "カ月", "ヶ月", "か月",
             "%", "％", "年", "月", "日", "時間", "分", "秒", "歳", "才", "円", "人", "本", "個", "件", "回", "倍", "隻", "機",
             "台", "両", "名", "社", "国", "階", "位", "点", "割", "枚", "冊", "匹", "頭", "羽", "杯", "週", "度", "号", "条",
             "番目", "つ", "万", "億", "兆", "千", "百", "km", "kg", "m", "g")
# 変換すると意味が変わる・不自然になる言葉（そのまま漢字で残す）
_KEEP = ("一番", "一度", "一緒", "一方", "一部", "一般", "一時", "一体", "一気", "一斉", "一応", "一切", "一層", "一旦",
         "一生", "一定", "一致", "一流", "一種", "一面", "一見", "一瞬", "一言", "一同", "一家", "一行", "一員", "一環",
         "一目", "一人", "一つ", "一日中", "一年中", "唯一", "統一", "同一", "万一", "十分", "十字", "一石二鳥", "二人三脚",
         "三日坊主", "七五三", "八百屋", "八百長", "千差万別", "百聞", "一石", "四季", "四方", "八方", "二度と", "一向",
         "五月雨", "一長一短", "十人十色", "九死", "千載", "万全", "万能", "万歳", "億劫", "百貨", "千羽", "四六時中",
         "第一次世界大戦", "第二次世界大戦", "第一人者", "第一線", "第一印象")


def _parse_small(s: str) -> int:
    """「三千五百二十」や「二〇二四」のような、万未満の漢数字を数にする。"""
    if s and all(c in _KDIGIT for c in s):           # 位取りのない並び（二〇二四）
        return int("".join(str(_KDIGIT[c]) for c in s))
    total, cur = 0, 0
    for c in s:
        if c in _KDIGIT:
            cur = _KDIGIT[c]
        elif c in _KSMALL:
            total += (cur or 1) * _KSMALL[c]
            cur = 0
    return total + cur


def _kanji_to_arabic(s: str) -> str:
    """「一億二千万」→「1億2000万」、「六十五」→「65」。万・億・兆は単位として残す。"""
    out, buf = "", ""
    for c in s:
        if c in _KBIG:
            out += (str(_parse_small(buf)) if buf else "1") + c
            buf = ""
        else:
            buf += c
    if buf:
        out += str(_parse_small(buf))
    return out


def _overlaps_keep_word(text: str, s: int, e: int) -> bool:
    """text[s:e] が「一番」「十分」などの変換しない言葉と重なっているか。"""
    for k in _KEEP:
        start = text.find(k, max(0, s - len(k)))
        while start != -1 and start < e:
            if start + len(k) > s:
                return True
            start = text.find(k, start + 1)
    return False


_NUM_RE = re.compile(rf"[{_KNUM_CHARS}]+(?:点[〇零一二三四五六七八九]+)?")


def display_numbers(text: str) -> str:
    """字幕の表示用に、数量を表す漢数字を算用数字にする（例: 一点五→1.5、六十五本→65本、二〇二四年→2024年）。
    「一番」「一緒」「十分」などの言葉は変換しない。"""
    out, pos = [], 0
    for m in _NUM_RE.finditer(text):
        s, e = m.start(), m.end()
        token = m.group()
        before, after = text[max(0, s - 4):s], text[e:e + 5]
        if _overlaps_keep_word(text, s, e) or before.endswith(("何", "数", "幾")):
            continue
        int_part, _, dec_part = token.partition("点")
        numeric_like = len(int_part) >= 2 or dec_part or after.startswith(_COUNTERS) or before.endswith("第")
        if not numeric_like or (len(int_part) == 1 and int_part in _KBIG + "千百十"):
            continue
        if int_part and int_part[0] in _KBIG:  # 「万が一」などの先頭が単位の語は対象外
            continue
        try:
            conv = _kanji_to_arabic(int_part)
            if dec_part:
                conv += "." + "".join(str(_KDIGIT[c]) for c in dec_part)
        except Exception:
            continue
        out.append(text[pos:s] + conv)
        pos = e
    out.append(text[pos:])
    return "".join(out)


def render_subtitle(text: str, max_width: int = 1640, center_x: int = W // 2) -> Image.Image:
    """縁取り付きの字幕を RGBA 画像として描画する（ImageMagick 不要）。"""
    lines = wrap_text(display_numbers(text), SUB_FONT, max_width)[:3]
    line_h = int(SUB_FONT.size * 1.35)
    img = Image.new("RGBA", (W, line_h * len(lines) + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        x = center_x - SUB_FONT.getlength(line) / 2
        y = 20 + i * line_h
        d.text((x + 4, y + 5), line, font=SUB_FONT, fill=(0, 0, 0, 140))  # 影
        d.text((x, y), line, font=SUB_FONT, fill=(255, 255, 255, 255),
               stroke_width=7, stroke_fill=(20, 20, 20, 255))
    return img


def render_label(text: str) -> Image.Image:
    """左上の章ラベル。"""
    pad = 22
    tw = int(LABEL_FONT.getlength(text))
    img = Image.new("RGBA", (tw + pad * 2, LABEL_FONT.size + pad * 2 - 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=16, fill=(15, 15, 25, 170))
    d.text((pad, pad - 6), text, font=LABEL_FONT, fill=(255, 255, 255, 255))
    return img


def split_sentences(text: str) -> list[str]:
    parts = [p for p in re.split(r"(?<=[。！？!?])", text) if p.strip()]
    merged = []
    for p in parts:  # 短すぎる文は前の文とまとめる
        if merged and len(p) < 8:
            merged[-1] += p
        else:
            merged.append(p)
    return merged or [text]


def subtitle_chunks(text: str, font, max_width: int, max_lines: int = 2) -> list[str]:
    """字幕1枚が max_lines 行に収まるよう、文を読点などの切れ目で分割する（文字が切れて消えないように）。"""
    def fits(t: str) -> bool:
        return len(wrap_text(t, font, max_width)) <= max_lines
    out = []
    for sent in split_sentences(text):
        if fits(sent):
            out.append(sent)
            continue
        clauses = [c for c in re.split(r"(?<=[、，,：:；;])", sent) if c]
        buf = ""
        for c in clauses:
            if buf and not fits(buf + c):
                out.append(buf)
                buf = ""
            buf += c
            while not fits(buf):  # 読点がなく1つの節が長すぎるときは、行の区切りで強制的に分ける
                lines = wrap_text(buf, font, max_width)
                head = "".join(lines[:max_lines])
                out.append(head)
                buf = buf[len(head):]
        if buf:
            out.append(buf)
    # 極端に短い断片（「。」だけなど）は前の字幕にまとめる
    merged = []
    for c in out:
        if merged and len(c.strip()) <= 2 and fits(merged[-1] + c):
            merged[-1] += c
        else:
            merged.append(c)
    return merged or [text]


def subtitle_timing(timeline: list[dict], font, max_width: int, max_lines: int, gap: float) -> list:
    """各シーンの字幕を分割し、文字数に比例して表示時間を割り当てる。"""
    subs = []
    for sc in timeline:
        if sc.get("sub_parts"):
            # 字幕ごとに音声を作っているので、音声の区切りどおりに切り替える（ずれない）。
            # 次の字幕までは前の字幕を出したままにし、最後の字幕はシーンの終わりまで表示する
            sp = sc["sub_parts"]
            for k, (a, b, x) in enumerate(sp):
                end = sp[k + 1][0] if k + 1 < len(sp) else sc["duration"]
                subs.append((sc["start"] + a, sc["start"] + end, x))
            continue
        parts = subtitle_chunks(sc["narration"], font, max_width, max_lines)
        total_chars = sum(len(x) for x in parts) or 1
        cur = sc["start"]
        for k, x in enumerate(parts):
            d = sc["voice_duration"] * len(x) / total_chars
            subs.append((cur, cur + d + (gap if k == len(parts) - 1 else 0), x))
            cur += d
    return subs


def smoothstep(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


class Character:
    """解説キャラクター。表情ごとの透過PNGを読み込み、白フチを付けて保持する。"""

    def __init__(self, height: int):
        self.sprites: dict[str, Image.Image] = {}
        if CHARACTER_DIR.exists():
            for e in EXPRESSIONS:
                p = CHARACTER_DIR / f"{e}.png"
                if p.exists():
                    self.sprites[e] = self._prepare(Image.open(p).convert("RGBA"), height)
        # 口パク用（「通常」の顔で、口の形だけが違う6枚）
        self.talk: dict[str, Image.Image] = {}
        talk_dir = CHARACTER_DIR / "talk"
        if talk_dir.exists() and all((talk_dir / f"{m}.png").exists() for m in MOUTHS):
            for m in MOUTHS:
                self.talk[m] = self._prepare(Image.open(talk_dir / f"{m}.png").convert("RGBA"),
                                             round(height * TALK_SCALE))
        if self.sprites and "normal" not in self.sprites:
            self.sprites["normal"] = next(iter(self.sprites.values()))
        if self.talk and "normal" not in self.sprites:
            self.sprites["normal"] = self.talk["n"]
        log.info(f"解説キャラクター: {'あり（' + '・'.join(self.sprites) + '）' if self.sprites else 'なし'}"
                 f"{' / 口パクあり' if self.talk else ''}")

    @property
    def enabled(self) -> bool:
        return bool(self.sprites)

    @property
    def lipsync(self) -> bool:
        return bool(self.talk)

    @property
    def width(self) -> int:
        return max([s.width for s in self.sprites.values()] + [s.width for s in self.talk.values()] + [0])

    def _prepare(self, img: Image.Image, height: int) -> Image.Image:
        img = img.crop(img.getbbox())
        img = img.resize((round(img.width * height / img.height), height), Image.LANCZOS)
        a0 = np.asarray(img.getchannel("A"))
        # 素材の上・左右が画像の端でまっすぐ切れているか（髪の毛が見切れている素材など）
        # 髪の先が端に少し触れているだけなら「切れている」とはみなさない（不透明な部分が辺の15%以上で判定）
        cut_top = (a0[0] > 128).mean() > 0.15
        cut_left = (a0[:, 0] > 128).mean() > 0.15
        cut_right = (a0[:, -1] > 128).mean() > 0.15

        pad = 12
        canvas = Image.new("RGBA", (img.width + pad * 2, img.height + pad), (0, 0, 0, 0))
        canvas.paste(img, (pad, pad), img)
        # 背景から浮かせるための白フチ
        a = canvas.getchannel("A").point(lambda v: 255 if v > 90 else 0)
        # 太らせてからぼかし→しきい値で角を丸め、髪の毛先でフチがギザギザになるのを防ぐ
        a = a.filter(ImageFilter.MaxFilter(15)).filter(ImageFilter.GaussianBlur(8))
        a = a.point(lambda v: 255 if v > 100 else 0).filter(ImageFilter.GaussianBlur(1.5))
        a = Image.fromarray((np.asarray(a).astype(np.float32) * 0.95).astype(np.uint8))
        out = Image.new("RGBA", canvas.size, (255, 255, 255, 0))
        out.putalpha(a)
        out.alpha_composite(canvas)

        # 切れている辺は、白フチごとなめらかに消して「まっすぐな切れ目」を目立たなくする
        alpha = np.asarray(out.getchannel("A")).astype(np.float32)
        n = max(12, height // 9)
        ramp = np.linspace(0, 1, n, dtype=np.float32)
        ramp = ramp * ramp * (3 - 2 * ramp)
        if cut_top:
            alpha[:pad] = 0
            alpha[pad:pad + n] *= ramp[:, None]
        if cut_left:
            alpha[:, :pad] = 0
            alpha[:, pad:pad + n] *= ramp[None, :]
        if cut_right:
            alpha[:, -pad:] = 0
            alpha[:, -pad - n:-pad] *= ramp[::-1][None, :]
        out.putalpha(Image.fromarray(alpha.astype(np.uint8)))
        return out

    def get(self, expression: str, mouth: str = "n") -> Image.Image:
        """「通常」の表情（または表情素材がない場合）は口パク用の絵を使う。"""
        if self.talk and (expression == "normal" or expression not in self.sprites):
            return self.talk.get(mouth, self.talk["n"])
        return self.sprites.get(expression) or self.sprites["normal"]


def mouth_track(timeline: list[dict], env: np.ndarray, n_frames: int) -> list[str]:
    """フレームごとの口の形。VOICEVOX のタイミングがあればそれを、なければ声の大きさを使う。"""
    track = ["n"] * n_frames
    lead = 1  # 口の動きを1フレームだけ先行させると、音とぴったり合って見える
    for sc in timeline:
        vis = sc.get("visemes")
        if vis is None:  # gTTS など：音量から口の開きを推定
            f0 = int(sc["start"] * FPS)
            f1 = min(n_frames, int((sc["start"] + sc["voice_duration"]) * FPS))
            for f in range(f0, f1):
                v = env[f]
                track[f] = "a" if v > 0.6 else "e" if v > 0.35 else "u" if v > 0.15 else "n"
            continue
        for a, b, shape in vis:
            f0 = max(0, int(round((sc["start"] + a) * FPS)) - lead)
            f1 = min(n_frames, int(round((sc["start"] + b) * FPS)) - lead)
            for f in range(f0, max(f1, f0 + 1)):
                if f < n_frames:
                    track[f] = shape
    # ちらつき防止：1フレームしか続かない口の形は、直前の形に置き換える
    for f in range(1, n_frames - 1):
        if track[f] != track[f - 1] and track[f] != track[f + 1]:
            track[f] = track[f - 1]
    return track


def voice_envelope(narration: np.ndarray, n_frames: int) -> np.ndarray:
    """フレームごとの声の大きさ（0〜1）。キャラを話しているように揺らすのに使う。"""
    hop = SR / FPS
    env = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        seg = narration[int(i * hop):int((i + 1) * hop)]
        if len(seg):
            env[i] = float(np.sqrt(np.mean(seg ** 2)))
    ref = np.percentile(env[env > 0], 95) if (env > 0).any() else 1.0
    env = np.clip(env / max(ref, 1e-6), 0, 1)
    for i in range(1, n_frames):  # なめらかにする
        env[i] = 0.55 * env[i - 1] + 0.45 * env[i]
    return env


class Renderer:
    def __init__(self, timeline: list[dict], narration: np.ndarray | None = None):
        global SUB_FONT, LABEL_FONT
        SUB_FONT, LABEL_FONT = load_font(58), load_font(34)
        self.tl = timeline
        self.total = timeline[-1]["start"] + timeline[-1]["duration"]
        self.n_frames = int(self.total * FPS) + 1
        self._img_cache: dict[int, Image.Image] = {}
        self._labels: dict[int, Image.Image] = {}

        self.char = Character(CHARACTER_HEIGHT)
        self.env = voice_envelope(narration, self.n_frames) if (self.char.enabled and narration is not None) \
            else np.zeros(self.n_frames, dtype=np.float32)
        self.mouth = mouth_track(timeline, self.env, self.n_frames) if self.char.lipsync else None
        # キャラがいる場合、字幕はキャラと重ならない左側の領域に置く
        if self.char.enabled:
            right = W - self.char.width - 30
            self.sub_width, self.sub_center = right - 80, right // 2 + 10
        else:
            self.sub_width, self.sub_center = 1640, W // 2

        # 字幕: 2行に収まる長さに分けて、文字数に比例して表示時間を割り当てる
        self.subs = subtitle_timing(timeline, SUB_FONT, self.sub_width, 2, SCENE_GAP)
        self._sub_cache: dict[int, Image.Image] = {}

    def _image(self, idx: int) -> Image.Image:
        if idx not in self._img_cache:
            if len(self._img_cache) > 4:
                self._img_cache.pop(min(self._img_cache))
            self._img_cache[idx] = Image.open(self.tl[idx]["image"]).convert("RGB")
        return self._img_cache[idx]

    def _scene_frame(self, idx: int, local_t: float) -> Image.Image:
        """Ken Burns: 中心基準のズーム・パンを浮動小数の切り抜き範囲で計算し、滑らかに動かす。"""
        sc = self.tl[idx]
        img = self._image(idx)
        sw, sh = img.size
        p = smoothstep(local_t / (sc["duration"] + FADE))
        motion = sc["camera"]
        if motion == "zoom_in":
            f = 0.98 - 0.13 * p
        elif motion == "zoom_out":
            f = 0.85 + 0.13 * p
        else:
            f = 0.86
        cw, ch = sw * f, sh * f
        cx, cy = sw / 2, sh / 2
        span = (sw - cw) / 2 * 0.9
        if motion == "pan_left":
            cx = sw / 2 + span * (1 - 2 * p)
        elif motion == "pan_right":
            cx = sw / 2 - span * (1 - 2 * p)
        box = (cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
        return img.resize((W, H), Image.BILINEAR, box=box)

    def _scene_at(self, t: float) -> int:
        lo, hi = 0, len(self.tl) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.tl[mid]["start"] <= t:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _subtitle_at(self, t: float):
        for i, (s, e, text) in enumerate(self.subs):
            if s <= t < e:
                if i not in self._sub_cache:
                    self._sub_cache = {i: render_subtitle(text, self.sub_width, self.sub_center)}
                return self._sub_cache[i]
        return None

    def _draw_character(self, frame: Image.Image, idx: int, t: float, fi: int) -> None:
        sc = self.tl[idx]
        fi = min(fi, self.n_frames - 1)
        mouth = self.mouth[fi] if self.mouth else "n"
        sprite = self.char.get(sc.get("expression", "normal"), mouth)
        local = t - sc["start"]
        bob = 4 if self.char.lipsync else 10                         # 口パクがあるときは揺れを控えめに
        y_off = -bob * float(self.env[fi])                           # 声に合わせて上下に揺れる
        y_off += 3 * np.sin(2 * np.pi * t / 3.2)                     # 呼吸
        prev = self.tl[idx - 1].get("expression") if idx > 0 else None
        if prev != sc.get("expression") and local < 0.3:            # 表情が変わったら小さく跳ねる
            y_off -= 16 * np.sin(np.pi * local / 0.3)
        center_x = W - 30 - self.char.width / 2                      # 素材の幅が違っても顔の位置を揃える
        x = int(round(center_x - sprite.width / 2))
        y = H - sprite.height + 14 + int(round(y_off))
        frame.paste(sprite, (x, y), sprite)

    def frame(self, t: float, fi: int = 0) -> Image.Image:
        idx = self._scene_at(t)
        sc = self.tl[idx]
        local = t - sc["start"]
        frame = self._scene_frame(idx, local)
        end = sc["start"] + sc["duration"]
        if idx + 1 < len(self.tl) and t > end - FADE:  # 次のシーンへクロスフェード
            alpha = smoothstep((t - (end - FADE)) / FADE)
            frame = Image.blend(frame, self._scene_frame(idx + 1, 0.0), alpha)

        # 左上に章ラベル
        ci = sc["chapter"]
        if ci not in self._labels:
            self._labels[ci] = render_label(f"第{ci + 1}章　{sc['chapter_title']}")
        label = self._labels[ci]
        frame.paste(label, (48, 44), label)

        if self.char.enabled:
            self._draw_character(frame, idx, t, fi)

        sub = self._subtitle_at(t)
        if sub is not None:
            frame.paste(sub, (0, H - sub.height - 50), sub)
        return frame


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as wf:
        return np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def encode_video(frame_fn, n_frames: int, size: tuple[int, int], total: float,
                 narration_path: Path, out: Path, label: str = "映像") -> Path:
    """フレームを ffmpeg に流し込み、ナレーション（＋BGM）と合わせて MP4 にする。"""
    w, h = size
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "pipe:0",
           "-i", str(narration_path)]
    if BGM_PATH.exists():
        fade_st = max(total - 4, 0)
        cmd += ["-stream_loop", "-1", "-i", str(BGM_PATH),
                "-filter_complex",
                f"[2:a]volume={BGM_VOLUME},afade=t=in:d=2,afade=t=out:st={fade_st:.2f}:d=4[bgm];"
                f"[1:a][bgm]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]",
                "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-filter_complex", "[1:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]", "-map", "0:v", "-map", "[a]"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", "-movflags", "+faststart", str(out)]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    started = time.time()
    try:
        for i in range(n_frames):
            proc.stdin.write(frame_fn(i / FPS, i).tobytes())
            if i % (FPS * 30) == 0:
                el = time.time() - started
                log.info(f"{label}のレンダリング {i / FPS / 60:.1f}/{total / 60:.1f}分（経過 {el / 60:.1f}分）")
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg のエンコードに失敗しました（{label}）。")
    log.info(f"{label}のレンダリング完了（{(time.time() - started) / 60:.1f}分）")
    return out


def render_video(timeline: list[dict], narration_path: Path) -> Path:
    log.info("映像のレンダリングを開始します…")
    if BGM_PATH.exists():
        log.info(f"BGM を使用: {BGM_PATH}")
    r = Renderer(timeline, read_wav(narration_path))
    return encode_video(r.frame, r.n_frames, (W, H), r.total, narration_path, WORK / "final.mp4", "本編")


# ============================== 5. サムネイル・メタデータ ==============================

def split_thumb_text(text: str) -> list[str]:
    """サムネの文言を2行に分ける。改行があればそれに従い、なければ助詞などの切れ目で中央付近を選ぶ。"""
    text = text.strip().replace("\\n", "\n")
    if "\n" in text:
        return [l.strip() for l in text.split("\n") if l.strip()][:2]
    if len(text) <= 7:
        return [text]
    mid = len(text) / 2
    cands = [i + 1 for i, ch in enumerate(text[:-1])
             if ch in "のもはがをにでとへや、！？!?" or (ch in "ーっ" and i + 1 < len(text))]
    # カタカナ語の途中で切らない
    kata = lambda c: "゠" <= c <= "ヿ"
    cands = [i for i in cands if not (kata(text[i - 1]) and kata(text[i]))]
    cut = min(cands, key=lambda i: abs(i - mid)) if cands else int(mid)
    if abs(cut - mid) > len(text) * 0.35:
        cut = int(mid)
    return [text[:cut], text[cut:]]


def _fit_font(line: str, max_w: int, max_size: int, min_size: int = 70):
    size = max_size
    font = load_font(size)
    while font.getlength(line) > max_w and size > min_size:
        size -= 6
        font = load_font(size)
    return font


def make_thumbnail(script: Script, timeline: list[dict], seed: int) -> Path:
    """主題が伝わる絵＋大きな2行の文字＋驚き顔のキャラ、のYouTubeらしいサムネを作る。"""
    TW, TH = 1280, 720
    path = WORK / "thumbnail.jpg"
    src = IMG_DIR / "thumbnail_src.jpg"
    get_scene_image(script.thumbnail_prompt, timeline[0]["search_keyword"], src, seed)
    img = Image.open(src).convert("RGB")
    # 中央から 16:9 に切り抜いて、明るく・鮮やかに
    img = img.resize((TW, TH), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.2)
    img = ImageEnhance.Color(img).enhance(1.3)
    img = ImageEnhance.Brightness(img).enhance(1.05)

    # 文字の後ろ（左下）だけを暗くして、絵の主題は明るいまま残す
    shade = Image.new("L", (TW, TH), 0)
    ImageDraw.Draw(shade).rectangle((0, 400, 900, TH), fill=175)
    shade = shade.filter(ImageFilter.GaussianBlur(70))
    img.paste((0, 0, 0), (0, 0), shade)
    img = img.convert("RGBA")

    # 驚き顔のキャラを右下に大きく
    text_right = TW - 40
    char = Character(600)
    if char.enabled:
        sprite = char.get("surprised")
        x = TW - sprite.width + 70
        img.alpha_composite(sprite, (x, TH - sprite.height + 14))
        text_right = max(760, x + 90)

    # 2行の大きな文字（1行目=白、2行目=黄色）。行ごとに入る最大サイズにする
    lines = split_thumb_text(script.thumbnail_text)
    max_w = text_right - 45
    styles = [((255, 255, 255), (0, 0, 0)), ((255, 221, 0), (0, 0, 0))]
    fonts = [_fit_font(l, max_w, 175 if len(lines) > 1 else 190) for l in lines]
    if len(fonts) == 2:  # 2行の大きさが極端に違わないようにそろえる
        small = min(f.size for f in fonts)
        fonts = [load_font(min(f.size, int(small * 1.25))) for f in fonts]
    total_h = sum(int(f.size * 1.12) for f in fonts)
    y = TH - 38 - total_h
    layer = Image.new("RGBA", (TW, TH), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i, (line, font) in enumerate(zip(lines, fonts)):
        fill, stroke = styles[min(i, 1)] if len(lines) > 1 else styles[1]
        sw = max(8, font.size // 11)
        d.text((40 + 7, y + 9), line, font=font, fill=(0, 0, 0, 160), stroke_width=sw, stroke_fill=(0, 0, 0, 160))  # 影
        d.text((40, y), line, font=font, fill=fill, stroke_width=sw, stroke_fill=stroke)
        y += int(font.size * 1.12)
    img.alpha_composite(layer)
    img.convert("RGB").save(path, "JPEG", quality=92)
    return path


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def make_credits(tts: TTS) -> list[str]:
    credits = []
    if tts.credit:
        credits.append(f"音声: {tts.credit}")
    credits.append("イラスト: AI生成")
    if BGM_PATH.exists():
        credits.append(f"BGM: {os.getenv('BGM_CREDIT', '（BGMのクレジットを BGM_CREDIT に設定してください）')}")
    return credits


def build_metadata(script: Script, timeline: list[dict], tts: TTS) -> dict:
    # 概要欄のタイムスタンプ（0:00 から始まり3つ以上あると YouTube のチャプターになる）
    chapters, seen = [], set()
    for t in timeline:
        if t["chapter"] not in seen:
            seen.add(t["chapter"])
            chapters.append(f"{fmt_ts(0 if not chapters else t['start'])} {t['chapter_title']}")
    credits = make_credits(tts)
    description = (f"{script.description.strip()}\n\n▼目次\n" + "\n".join(chapters)
                   + "\n\n" + "\n".join(credits))
    return {
        "title": script.video_title.strip()[:100],
        "description": description[:4900],
        "tags": script.tags,
        "duration_sec": round(timeline[-1]["start"] + timeline[-1]["duration"], 1),
    }


# ============================== 6. ショート動画 ==============================
# 本編の台本から一番「へえ」となる話題を選び、縦長・1分以内のショートを作る。
# 画像は本編のものを使い回すので、画像生成の追加はない。

SHORTS_COUNT = int(os.getenv("SHORTS_COUNT", "2"))       # 0 にするとショートを作らない
SHORT_MAX_SEC = float(os.getenv("SHORT_MAX_SEC", "58"))  # ショートの最大の長さ（秒）
SW, SH = 1080, 1920                                       # 縦長
SHORT_GAP = 0.15                                          # ショートは間を詰めてテンポよく
PANEL_Y, PANEL_W, PANEL_H = 300, 1080, 810                # 中央の絵の位置と大きさ（4:3）
SHORT_SUB_Y = 1130                                        # 字幕の上端
SHORT_CHAR_H = int(os.getenv("SHORT_CHAR_HEIGHT", "560"))


class ShortScene(BaseModel):
    narration: str = Field(description="日本語のナレーション。話し言葉で1〜2文、20〜60文字。")
    source_scene: int = Field(description="このシーンの絵として使う本編シーンの番号（台本一覧の [番号]）")
    expression: str = Field(description="解説キャラの表情。normal, smile, surprised, thinking のいずれか。")


class Short(BaseModel):
    title: str = Field(description="ショートのタイトル（日本語、30文字以内、ハッシュタグは付けない）")
    hook_text: str = Field(description="画面上部にずっと表示する見出し。2行（改行\\nで区切る）、各行2〜9文字。単語の途中で改行しない")
    scenes: list[ShortScene]


class ShortsPlan(BaseModel):
    shorts: list[Short]


def plan_shorts(script: Script) -> list[Short]:
    numbered, no = [], 0
    for ch in script.chapters:
        numbered.append(f"\n## {ch.title}")
        for sc in ch.scenes:
            no += 1
            numbered.append(f"[{no}] {sc.narration}")
    chars_min, chars_max = int(SHORT_MAX_SEC * 4.0), int(SHORT_MAX_SEC * 4.8)
    prompt = f"""あなたは YouTube ショート動画の構成作家です。次の本編動画の台本から、ショート動画（縦長・1分以内）を{SHORTS_COUNT}本作ってください。

# 本編のタイトル
{script.video_title}

# 本編の台本（[番号] はシーン番号）
{chr(10).join(numbered)}

# ショートの要件
- 1本のショートは、本編の中で最も意外で「へえ」となる話題1つに絞る。ショート同士で話題を重ねない。
- 1シーン目は、2秒で指が止まる問いかけか意外な事実から始める（あいさつはしない）。
- ナレーション合計は{chars_min}〜{chars_max}文字。1シーンは1〜2文、20〜60文字。6〜10シーン。
- 事実は本編の台本に書かれていることだけを使う。
- 最後のシーンは「続きは本編で詳しく解説しています」のように、本編へ自然に誘導して終える。
- 記号・絵文字・括弧書き・読み間違えやすい英字略語は使わない（音声合成で読み上げるため）。
- 数字は算用数字で書く（例: 1.5倍、65本、2024年）。
- source_scene には、そのシーンの内容に最も合う本編シーンの番号を入れる。同じ番号を3回以上続けない。
- expression は、意外な事実→surprised、問いかけ→thinking、締め→smile、それ以外→normal。半分以上は normal にする。
- hook_text は、画面上部に大きく出す見出し。タイトルより短く、強い言葉で。"""
    plan = llm_json(prompt, ShortsPlan, "ショートの構成", temperature=0.7)
    shorts = []
    for sh in plan.shorts[:SHORTS_COUNT]:
        scenes = []
        for sc in sh.scenes:
            sc.narration = re.sub(r"[\"“”「」『』（）()【】\[\]*#＊＃~〜]", "", sc.narration).strip()
            if len(sc.narration) < 2:
                continue
            sc.source_scene = min(max(sc.source_scene, 1), no)
            if sc.expression not in EXPRESSIONS:
                sc.expression = "normal"
            scenes.append(sc)
        if scenes:
            sh.scenes = scenes
            shorts.append(sh)
    return shorts


def build_short_timeline(short: Short, tts: TTS, long_tl: list[dict], idx: int) -> tuple[list[dict], Path]:
    tl, pieces, t = [], [], 0.0
    gap = np.zeros(int(SR * SHORT_GAP), dtype=np.float32)
    font = load_font(66)
    for k, sc in enumerate(short.scenes):
        audio, visemes, sub_parts = synth_with_subtitles(tts, sc.narration, font, SW - 90, 3)
        voice = len(audio) / SR
        dur = voice + SHORT_GAP
        if t + dur > SHORT_MAX_SEC and tl:
            log.warning(f"ショート{idx}: {SHORT_MAX_SEC:.0f}秒を超えるため、残りのシーンを省きます。")
            break
        pieces += [audio, gap]
        tl.append({"index": k, "start": t, "duration": dur, "voice_duration": voice,
                   "narration": sc.narration, "expression": sc.expression, "visemes": visemes,
                   "sub_parts": sub_parts, "camera": ("zoom_in", "zoom_out")[k % 2],
                   "image": long_tl[sc.source_scene - 1]["image"]})
        t += dur
    path = WORK / f"short_{idx}_narration.wav"
    write_wav(path, np.concatenate(pieces))
    return tl, path


class ShortRenderer:
    """縦長ショートの描画：上に見出し、中央に絵、その下に字幕、一番下に解説キャラ。"""

    def __init__(self, timeline: list[dict], narration: np.ndarray, hook_text: str):
        self.tl = timeline
        self.total = timeline[-1]["start"] + timeline[-1]["duration"]
        self.n_frames = int(self.total * FPS) + 1
        self.sub_font = load_font(66)
        self.char = Character(SHORT_CHAR_H)
        self.env = voice_envelope(narration, self.n_frames)
        self.mouth = mouth_track(timeline, self.env, self.n_frames) if self.char.lipsync else None
        self.hook = self._render_hook(hook_text)
        self._src: dict[str, Image.Image] = {}
        self._bg: dict[str, Image.Image] = {}
        self.subs = subtitle_timing(timeline, self.sub_font, SW - 90, 3, SHORT_GAP)
        self._sub_cache: dict[int, Image.Image] = {}

    def _render_hook(self, text: str) -> Image.Image:
        lines = split_thumb_text(text)
        layer = Image.new("RGBA", (SW, PANEL_Y), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        max_size = 110
        while True:  # 見出しが上の帯（PANEL_Y）に収まるまで小さくする
            fonts = [_fit_font(l, SW - 80, max_size, 50) for l in lines]
            if len(fonts) == 2:
                small = min(f.size for f in fonts)
                fonts = [load_font(min(f.size, int(small * 1.2))) for f in fonts]
            total_h = sum(int(f.size * 1.15) for f in fonts)
            if total_h <= PANEL_Y - 50 or max_size <= 50:
                break
            max_size -= 6
        y = (PANEL_Y - total_h) // 2
        colors = [(255, 255, 255), (255, 221, 0)] if len(lines) > 1 else [(255, 221, 0)]
        for line, font, color in zip(lines, fonts, colors):
            sw = max(7, font.size // 11)
            x = (SW - font.getlength(line)) / 2
            d.text((x + 5, y + 7), line, font=font, fill=(0, 0, 0, 150), stroke_width=sw, stroke_fill=(0, 0, 0, 150))
            d.text((x, y), line, font=font, fill=color, stroke_width=sw, stroke_fill=(0, 0, 0))
            y += int(font.size * 1.15)
        return layer

    def _source(self, path: str) -> Image.Image:
        if path not in self._src:
            if len(self._src) > 4:
                self._src.pop(next(iter(self._src)))
                self._bg.pop(next(iter(self._bg)), None)
            img = Image.open(path).convert("RGB")
            self._src[path] = img
            # 背景：同じ絵を縦長に切り抜いて、ぼかして暗くしたもの
            w, h = img.size
            cw = int(h * SW / SH)
            bg = img.crop(((w - cw) // 2, 0, (w - cw) // 2 + cw, h)).resize((SW, SH), Image.BILINEAR)
            bg = bg.filter(ImageFilter.GaussianBlur(28))
            self._bg[path] = ImageEnhance.Brightness(bg).enhance(0.45)
        return self._src[path]

    def _scene_frame(self, idx: int, local_t: float) -> Image.Image:
        sc = self.tl[idx]
        img = self._source(sc["image"])
        frame = self._bg[sc["image"]].copy()
        sw, sh = img.size
        p = smoothstep(local_t / (sc["duration"] + FADE))
        f = 0.96 - 0.12 * p if sc["camera"] == "zoom_in" else 0.84 + 0.12 * p
        ch = sh * f
        cw = ch * PANEL_W / PANEL_H
        cx, cy = sw / 2, sh / 2
        panel = img.resize((PANEL_W, PANEL_H), Image.BILINEAR, box=(cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2))
        frame.paste(panel, (0, PANEL_Y))
        return frame

    def _scene_at(self, t: float) -> int:
        idx = 0
        for i, sc in enumerate(self.tl):
            if sc["start"] <= t:
                idx = i
        return idx

    def _subtitle_at(self, t: float):
        for i, (s, e, text) in enumerate(self.subs):
            if s <= t < e:
                if i not in self._sub_cache:
                    lines = wrap_text(display_numbers(text), self.sub_font, SW - 90)[:3]
                    line_h = int(self.sub_font.size * 1.32)
                    img = Image.new("RGBA", (SW, line_h * len(lines) + 30), (0, 0, 0, 0))
                    d = ImageDraw.Draw(img)
                    for k, line in enumerate(lines):
                        x = (SW - self.sub_font.getlength(line)) / 2
                        d.text((x, 10 + k * line_h), line, font=self.sub_font, fill=(255, 255, 255),
                               stroke_width=7, stroke_fill=(20, 20, 20))
                    self._sub_cache = {i: img}
                return self._sub_cache[i]
        return None

    def frame(self, t: float, fi: int = 0) -> Image.Image:
        idx = self._scene_at(t)
        sc = self.tl[idx]
        frame = self._scene_frame(idx, t - sc["start"])
        end = sc["start"] + sc["duration"]
        if idx + 1 < len(self.tl) and t > end - FADE:
            frame = Image.blend(frame, self._scene_frame(idx + 1, 0.0), smoothstep((t - (end - FADE)) / FADE))
        frame.paste(self.hook, (0, 0), self.hook)

        if self.char.enabled:
            fi = min(fi, self.n_frames - 1)
            sprite = self.char.get(sc.get("expression", "normal"), self.mouth[fi] if self.mouth else "n")
            y_off = -4 * float(self.env[fi]) + 3 * np.sin(2 * np.pi * t / 3.2)
            local = t - sc["start"]
            prev = self.tl[idx - 1].get("expression") if idx > 0 else None
            if prev != sc.get("expression") and local < 0.3:
                y_off -= 16 * np.sin(np.pi * local / 0.3)
            x = int(SW / 2 - sprite.width / 2)
            frame.paste(sprite, (x, SH - sprite.height + 14 + int(round(y_off))), sprite)

        sub = self._subtitle_at(t)
        if sub is not None:
            frame.paste(sub, (0, SHORT_SUB_Y), sub)
        return frame


def make_shorts(script: Script, long_tl: list[dict], tts: TTS, credits: list[str]) -> list[dict]:
    """ショートを作って work/short_N.mp4 と work/shorts.json に保存する。失敗しても本編には影響させない。"""
    if SHORTS_COUNT <= 0:
        return []
    try:
        shorts = plan_shorts(script)
    except Exception as e:
        log.warning(f"ショートの構成を作れなかったため、ショートは作りません: {str(e)[:200]}")
        return []
    results = []
    for i, sh in enumerate(shorts, 1):
        try:
            log.info(f"ショート{i}: {sh.title}（{len(sh.scenes)}シーン）")
            tl, wav = build_short_timeline(sh, tts, long_tl, i)
            r = ShortRenderer(tl, read_wav(wav), sh.hook_text)
            out = encode_video(r.frame, r.n_frames, (SW, SH), r.total, wav, WORK / f"short_{i}.mp4", f"ショート{i}")
            title = sh.title.strip()[:90] + " #Shorts"
            description = (f"{sh.title.strip()}\n\n▼本編はこちら\n{{MAIN_URL}}\n\n" + "\n".join(credits)
                           + "\n\n#Shorts")
            results.append({"file": out.name, "title": title, "description": description,
                            "duration_sec": round(r.total, 1)})
        except Exception as e:
            log.warning(f"ショート{i}の作成に失敗しました（本編はそのまま続行）: {str(e)[:200]}")
            log.warning(traceback.format_exc())
    (WORK / "shorts.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


# ============================== メイン ==============================

def main():
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        log.info("=== 動画生成パイプライン起動 ===")
        if not INPUT_PLAN and not INPUT_TITLE:
            log.warning("企画本文もタイトルも指定されていません。おまかせテーマで作成します。")

        script_path = WORK / "script.json"
        if script_path.exists():  # 再実行時に台本を作り直さない
            script = Script.model_validate_json(script_path.read_text(encoding="utf-8"))
            log.info("既存の台本を使用します。")
        else:
            script, review, _ = generate_script()
            script_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
            (WORK / "review.json").write_text(review.model_dump_json(indent=2), encoding="utf-8")
            chapters = "\n".join(f"　{i + 1}. {c.title}（{len(c.scenes)}シーン）" for i, c in enumerate(script.chapters))
            issues = "\n".join(f"　・{x}" for x in review.issues[:3])
            notify_discord(f"📝 **台本ができました**「{script.video_title}」（{count_chars(script)}文字・"
                           f"約{count_chars(script) / CHARS_PER_MIN:.0f}分の見込み）\n{chapters}\n"
                           f"企画との一致 {review.relevance}/10・具体性 {review.specificity}/10"
                           + (f"\n残った指摘:\n{issues}" if issues else "")
                           + "\nこのまま音声・画像・映像の制作に進みます。")
        log.info(f"タイトル: {script.video_title}")

        tts = TTS()
        timeline, narration_path = build_timeline(script, tts)
        seed = random.randint(1, 10_000_000)
        generate_images(timeline, seed)
        render_video(timeline, narration_path)
        make_thumbnail(script, timeline, seed + 99_999)

        meta = build_metadata(script, timeline, tts)
        (WORK / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(f"🎉 本編完成: {WORK / 'final.mp4'}（{meta['duration_sec'] / 60:.1f}分）")

        shorts = make_shorts(script, timeline, tts, make_credits(tts))
        log.info(f"ショート: {len(shorts)}本作成")
    except Exception as e:
        log.error(f"致命的なエラー: {e}")
        log.error(traceback.format_exc())
        notify_discord(f"⚠️ 動画生成に失敗しました: {str(e)[:500]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
