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
CHARS_PER_MIN = 330        # 日本語ナレーションの目安（文字/分）
TARGET_CHARS = int(TARGET_MINUTES * CHARS_PER_MIN)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# 使う Gemini モデル（左から順に試す）。使えないモデルは自動で飛ばし、
# エラーに書かれた後継モデルや、API の一覧から見つけた新しい flash 系モデルに自動で切り替える。
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", "gemini-3.8-flash,gemini-3.5-flash-lite").split(",") if m.strip()]

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
VOICEVOX_SPEAKER = int(os.getenv("VOICEVOX_SPEAKER") or "3")   # 3 = ずんだもん（ノーマル）
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
TALK_SCALE = float(os.getenv("TALK_SCALE", "1.035"))  # 口パク素材と表情素材の顔の大きさを揃える倍率
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
    thumbnail_text: str = Field(description="サムネイルに大きく載せる短いコピー（8〜14文字）")
    thumbnail_prompt: str = Field(description="サムネイル用の印象的な挿絵の英語プロンプト。文字は含めない。")
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
_DISCOVERED = False                  # API のモデル一覧をもう調べたか


def _discover_models(client) -> list[str]:
    """API のモデル一覧から、文章生成に使える flash 系モデルを新しい順に返す。"""
    found = []
    try:
        for m in client.models.list():
            name = (m.name or "").removeprefix("models/")
            actions = getattr(m, "supported_actions", None) or []
            if "flash" in name and "generateContent" in actions and not any(
                    k in name for k in ("image", "tts", "audio", "live", "embedding", "preview", "exp")):
                found.append(name)
    except Exception as e:
        log.warning(f"モデル一覧の取得に失敗: {e}")

    def version(n: str):
        v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return (float(v.group(1)) if v else 0, "lite" not in n)
    found.sort(key=version, reverse=True)
    if found:
        log.info(f"使える可能性のあるモデル: {', '.join(found[:6])}")
    return found


def call_gemini(prompt: str, config, label: str):
    """モデルを順に試し、混雑時は待って再試行する。"""
    global _DISCOVERED
    client = gemini_client()
    last_err = None
    queue = [m for m in GEMINI_MODELS if m not in _DEAD_MODELS]
    while queue:
        model = queue.pop(0)
        if model in _DEAD_MODELS:
            continue
        for attempt in range(3):
            try:
                log.info(f"Gemini（{model}）: {label}（試行 {attempt + 1}）")
                resp = client.models.generate_content(model=model, contents=prompt, config=config)
                if model in GEMINI_MODELS:  # 次回から最初にこのモデルを使う
                    GEMINI_MODELS.remove(model)
                GEMINI_MODELS.insert(0, model)
                return resp
            except Exception as e:
                last_err = e
                msg = str(e)
                log.warning(f"Gemini エラー: {msg[:300]}")
                quota_zero = "limit: 0" in msg or "limit:0" in msg
                if "404" in msg or "NOT_FOUND" in msg or quota_zero:
                    # このモデルは使えない（終了済み・無料枠なし）。エラー文に後継モデルがあれば候補に足す
                    _DEAD_MODELS.add(model)
                    for nxt in re.findall(r"use models/([\w.\-]+)", msg):
                        if nxt not in _DEAD_MODELS and nxt not in queue:
                            log.info(f"エラーで案内された後継モデル {nxt} を試します。")
                            queue.insert(0, nxt)
                    break
                busy = any(k in msg for k in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500"))
                time.sleep((30 if busy else 5) * (attempt + 1))
        if not queue and not _DISCOVERED:
            _DISCOVERED = True  # 候補が尽きたら、API の一覧から探す（1回だけ）
            queue = [m for m in _discover_models(client) if m not in _DEAD_MODELS]
    raise RuntimeError(f"Gemini の呼び出しに失敗しました（{label}）: {last_err}")


def call_gemini_structured(prompt: str, schema: type[BaseModel], label: str, temperature: float = 0.7):
    from google.genai import types
    config = types.GenerateContentConfig(response_mime_type="application/json",
                                         response_schema=schema, temperature=temperature)
    resp = call_gemini(prompt, config, label)
    return resp.parsed if resp.parsed is not None else schema.model_validate_json(resp.text)


# ---------- 1-1. リサーチ：台本の「中身」になる事実を先に集める ----------

def research_topic() -> str:
    """Google 検索グラウンディングで、企画に関する具体的な事実を集めたメモを作る。
    台本を書く前に材料を揃えることで、抽象的で中身のない台本になるのを防ぐ。"""
    from google.genai import types
    prompt = f"""次の YouTube 動画企画の台本を書くための「取材メモ」を作ってください。
Google 検索で調べ、企画の各章で使える具体的な材料を集めてください。

# 企画
{plan_text()}

# 取材メモの要件
- 企画の構成（章）ごとに見出しを立てる。
- 各見出しの下に、具体的な事実を箇条書きで4〜8個。数字・年・人物・場所・具体的な事例・意外なエピソードを優先する。
- 視聴者が「へえ」と思う意外な事実を、全体で5つ以上含める。
- 諸説ある内容は「諸説あり」と明記する。確認できなかったことは書かない。
- 各事実の末尾に、情報源のサイト名を（）で付ける。
- 日本語で書く。"""
    try:
        config = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())],
                                             temperature=0.3)
        resp = call_gemini(prompt, config, "企画のリサーチ（Google 検索）")
        notes = (resp.text or "").strip()
        if len(notes) > 300:
            log.info(f"取材メモ: {len(notes)}文字")
            return notes
        log.warning("検索付きのリサーチ結果が短いため、検索なしで再試行します。")
    except Exception as e:
        log.warning(f"検索付きのリサーチに失敗しました（検索なしで続行）: {e}")

    config = types.GenerateContentConfig(temperature=0.3)
    resp = call_gemini(prompt.replace("Google 検索で調べ、", "あなたの知識をもとに、"), config, "企画のリサーチ")
    return (resp.text or "").strip()


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
    return call_gemini_structured(prompt, Review, "台本の品質チェック", temperature=0.2)


def count_chars(script: Script) -> int:
    return sum(len(s.narration) for c in script.chapters for s in c.scenes)


def generate_script() -> tuple[Script, Review, str]:
    log.info("=== 企画 ===\n" + plan_text()[:1500])
    research = research_topic()
    (WORK / "research.md").write_text(research, encoding="utf-8")

    best, best_review, best_score, feedback = None, None, -999, ""
    for rnd in range(1, SCRIPT_ROUNDS + 1):
        script = clean_script(call_gemini_structured(build_script_prompt(research, feedback), Script,
                                                     f"台本の執筆（{rnd}回目）"))
        n = count_chars(script)
        review = review_script(script, research)
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
    return best, best_review, research


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
        return "ずんだもん"

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


def build_timeline(script: Script, tts: TTS) -> tuple[list[dict], Path]:
    """シーンごとに音声を作り、開始時刻・長さを確定させる（音声と映像が必ず一致する）。"""
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    timeline, pieces, t = [], [], 0.0
    gap = np.zeros(int(SR * SCENE_GAP), dtype=np.float32)
    total = sum(len(c.scenes) for c in script.chapters)
    for ci, ch in enumerate(script.chapters):
        for si, sc in enumerate(ch.scenes):
            audio, visemes = tts.synth(sc.narration)
            voice_dur = len(audio) / SR
            pieces += [audio, gap]
            dur = voice_dur + SCENE_GAP
            timeline.append({
                "index": len(timeline), "chapter": ci, "chapter_title": ch.title,
                "start": t, "duration": dur, "voice_duration": voice_dur,
                "narration": sc.narration, "image_prompt": sc.image_prompt,
                "search_keyword": sc.search_keyword, "camera": sc.camera,
                "expression": sc.expression, "visemes": visemes,
            })
            t += dur
            log.info(f"音声 {len(timeline)}/{total}: {voice_dur:.1f}秒")
    narration = np.concatenate(pieces)
    path = WORK / "narration.wav"
    write_wav(path, narration)
    log.info(f"ナレーション合計: {t / 60:.1f}分")
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


def render_subtitle(text: str, max_width: int = 1640, center_x: int = W // 2) -> Image.Image:
    """縁取り付きの字幕を RGBA 画像として描画する（ImageMagick 不要）。"""
    lines = wrap_text(text, SUB_FONT, max_width)[:3]
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
        cut_top = (a0[0] > 128).mean() > 0.03
        cut_left = (a0[:, 0] > 128).mean() > 0.03
        cut_right = (a0[:, -1] > 128).mean() > 0.03

        pad = 12
        canvas = Image.new("RGBA", (img.width + pad * 2, img.height + pad), (0, 0, 0, 0))
        canvas.paste(img, (pad, pad), img)
        # 背景から浮かせるための白フチ
        a = canvas.getchannel("A").point(lambda v: 255 if v > 90 else 0)
        a = a.filter(ImageFilter.MaxFilter(13)).filter(ImageFilter.GaussianBlur(1.2))
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

        # 字幕: 文ごとに、文字数に比例して表示時間を割り当てる
        self.subs = []
        for t in timeline:
            sents = split_sentences(t["narration"])
            total_chars = sum(len(s) for s in sents)
            cur = t["start"]
            for k, s in enumerate(sents):
                d = t["voice_duration"] * len(s) / total_chars
                self.subs.append((cur, cur + d + (SCENE_GAP if k == len(sents) - 1 else 0), s))
                cur += d
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


def render_video(timeline: list[dict], narration_path: Path) -> Path:
    log.info("映像のレンダリングを開始します…")
    r = Renderer(timeline, read_wav(narration_path))
    out = WORK / "final.mp4"
    n_frames = r.n_frames

    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "pipe:0",
           "-i", str(narration_path)]
    if BGM_PATH.exists():
        fade_st = max(r.total - 4, 0)
        cmd += ["-stream_loop", "-1", "-i", str(BGM_PATH),
                "-filter_complex",
                f"[2:a]volume={BGM_VOLUME},afade=t=in:d=2,afade=t=out:st={fade_st:.2f}:d=4[bgm];"
                f"[1:a][bgm]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]",
                "-map", "0:v", "-map", "[a]"]
        log.info(f"BGM を使用: {BGM_PATH}")
    else:
        cmd += ["-filter_complex", "[1:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]", "-map", "0:v", "-map", "[a]"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", "-movflags", "+faststart", str(out)]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    started = time.time()
    try:
        for i in range(n_frames):
            proc.stdin.write(r.frame(i / FPS, i).tobytes())
            if i % (FPS * 30) == 0:
                el = time.time() - started
                log.info(f"レンダリング {i / FPS / 60:.1f}/{r.total / 60:.1f}分（経過 {el / 60:.1f}分）")
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg のエンコードに失敗しました。")
    log.info(f"レンダリング完了（{(time.time() - started) / 60:.1f}分）")
    return out


# ============================== 5. サムネイル・メタデータ ==============================

def make_thumbnail(script: Script, timeline: list[dict], seed: int) -> Path:
    TW, TH = 1280, 720
    path = WORK / "thumbnail.jpg"
    src = IMG_DIR / "thumbnail_src.jpg"
    get_scene_image(script.thumbnail_prompt, timeline[0]["search_keyword"], src, seed)
    img = Image.open(src).convert("RGB").resize((TW, TH), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.15)
    img = ImageEnhance.Color(img).enhance(1.2)

    # 文字を読みやすくするため下側を暗くする
    shade = Image.new("L", (TW, TH), 0)
    ImageDraw.Draw(shade).rectangle((0, 360, TW, TH), fill=160)
    shade = shade.filter(ImageFilter.GaussianBlur(60))
    img.paste((0, 0, 0), (0, 0), shade)

    # 驚いた表情のキャラを右下に大きく配置
    text_right = TW - 50
    char = Character(560)
    if char.enabled:
        sprite = char.get("surprised")
        x = TW - sprite.width + 40
        img.paste(sprite, (x, TH - sprite.height + 14), sprite)
        text_right = x + 60

    text = script.thumbnail_text.strip()[:16]
    max_w = text_right - 50
    size = 150
    font = load_font(size)
    lines = [text]
    while size > 70:
        lines = wrap_text(text, font, max_w)
        if len(lines) <= 2:
            break
        size -= 8
        font = load_font(size)
    lines = lines[:2]
    d = ImageDraw.Draw(img)
    line_h = int(size * 1.15)
    y = TH - 40 - len(lines) * line_h
    for line in lines:
        x = 50 if char.enabled else (TW - font.getlength(line)) / 2
        d.text((x, y), line, font=font, fill=(255, 225, 40), stroke_width=12, stroke_fill=(10, 10, 10))
        y += line_h
    img.save(path, "JPEG", quality=90)
    return path


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def build_metadata(script: Script, timeline: list[dict], tts: TTS) -> dict:
    # 概要欄のタイムスタンプ（0:00 から始まり3つ以上あると YouTube のチャプターになる）
    chapters, seen = [], set()
    for t in timeline:
        if t["chapter"] not in seen:
            seen.add(t["chapter"])
            chapters.append(f"{fmt_ts(0 if not chapters else t['start'])} {t['chapter_title']}")
    credits = []
    if tts.credit:
        credits.append(f"音声: {tts.credit}")
    credits.append("イラスト: AI生成")
    if BGM_PATH.exists():
        credits.append(f"BGM: {os.getenv('BGM_CREDIT', '（BGMのクレジットを BGM_CREDIT に設定してください）')}")
    description = (f"{script.description.strip()}\n\n▼目次\n" + "\n".join(chapters)
                   + "\n\n" + "\n".join(credits))
    return {
        "title": script.video_title.strip()[:100],
        "description": description[:4900],
        "tags": script.tags,
        "duration_sec": round(timeline[-1]["start"] + timeline[-1]["duration"], 1),
    }


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
            notify_discord(f"📝 **台本ができました**「{script.video_title}」\n{chapters}\n"
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
        log.info(f"🎉 完了: {WORK / 'final.mp4'}（{meta['duration_sec'] / 60:.1f}分）")
    except Exception as e:
        log.error(f"致命的なエラー: {e}")
        log.error(traceback.format_exc())
        notify_discord(f"⚠️ 動画生成に失敗しました: {str(e)[:500]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
