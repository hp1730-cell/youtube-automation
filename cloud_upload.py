"""AIドラマ動画を自動生成してYouTubeに投稿するスクリプト。

流れ: Geminiで台本・画像プロンプト生成(503過負荷自動フォールバック対応) →
      Pollinations.aiでAI画像生成 → edge-ttsで文ごとにナレーション →
      FFmpegで1.5倍速＆パン・ズーム動画合成 → YouTubeにアップロード
"""
import asyncio
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from gtts import gTTS
from pydantic import BaseModel

try:
    import edge_tts  # 自然な日本語音声(Microsoft Edgeの読み上げ)。無ければgTTSを使う
except ImportError:
    edge_tts = None

# ---------------------------------------------------------------- 設定
WORK_DIR = Path("work")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
SPEED = float(os.environ.get("NARRATION_SPEED", "1.2"))  # ナレーション速度
TTS_ENGINE = os.environ.get("TTS_ENGINE", "edge")  # edge / gtts
EDGE_VOICE = os.environ.get("EDGE_VOICE", "ja-JP-NanamiNeural")  # 男性声: ja-JP-KeitaNeural
PRIVACY = os.environ.get("PRIVACY_STATUS", "unlisted")  # private / unlisted / public
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

WIDTH, HEIGHT, FPS = 1280, 720, 30
IMAGE_DIR = Path(os.environ.get("IMAGE_DIR", "images"))
BG_PALETTE = ["0x2b1055", "0x0f2a55", "0x4a1040", "0x0d3b3b"]  # 画像が無いシーンの背景色
SCENE_FADE = 0.4  # シーンの切り替え(暗転)の長さ(秒)
ZOOM_AMOUNT = 0.15  # 画像のゆっくりズームの量(15%)
TITLE_SHOW = (0.5, 4.5)  # タイトルを表示する時間(秒)
LEAD_IN = 0.8  # 冒頭の無音(秒)
TAIL = 2.0  # 終わりの余白(秒)
CLIP_GAP = 0.25  # 文と文の間の無音(秒)
MAX_CHARS_PER_CLIP = 50  # 長い文の分割しきい値
SUB_CHARS_PER_LINE = 24  # テロップ1行の文字数

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def log(*args):
    print(*args, flush=True)


def run(cmd, timeout=600):
    """コマンドを実行。失敗時はFFmpegのエラー内容をログに出す。"""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        log(f"❌ コマンド失敗: {cmd[0]} (code {result.returncode})")
        log(result.stderr[-3000:])
        raise RuntimeError(f"{cmd[0]} が失敗しました")
    return result


# ---------------------------------------------------------------- 認証
def restore_credentials():
    """GitHub SecretsからYouTube認証情報を復元"""
    token_json = os.environ.get("TOKEN_JSON")
    if not token_json:
        if DRY_RUN:
            log("⚠️ TOKEN_JSONが未設定ですが、DRY_RUNモードのためスキップします。")
            return
        raise ValueError("❌ TOKEN_JSON が設定されていません。")
    Path("token.json").write_text(token_json, encoding="utf-8")


# ---------------------------------------------------------------- 台本生成
class Scene(BaseModel):
    text: str
    image_prompt: str


class Drama(BaseModel):
    title: str
    description: str
    visual_style: str
    scenes: list[Scene]


def build_prompt(theme, genre, tone):
    return (
        "あなたはプロのAIアニメ・ドラマ脚本家です。\n"
        "以下の条件で、YouTubeのAIドラマ動画用のナレーション台本を完全オリジナルの世界観で作成してください。\n"
        "実在の人物、既存の作品のキャラクター、商標や著作権に触れる固有名詞は使わないでください。\n\n"
        f"- メインテーマ: {theme}\n"
        f"- ジャンル: {genre}\n"
        f"- 雰囲気・トーン: {tone}\n\n"
        "条件:\n"
        "- title: 目を引く30文字以内のタイトル\n"
        "- description: 動画の概要(ハッシュタグ3つを含む)\n"
        "- visual_style: 全シーン共通の画風と登場人物の外見の固定設定(英語)。\n"
        "  例: Japanese anime style, highly detailed, soft lighting, a young man with black hair wearing a trench coat。\n"
        "  どのシーンでも同じ人物・同じ画風で描けるよう、髪型・服・色まで具体的に書く。\n"
        "- scenes: 3〜4シーン。起承転結があり、各シーンは3〜4文。1文は40文字以内。\n"
        "  text: ナレーションとして読み上げるので、記号や括弧、ト書きは使わず、話し言葉の文章だけを書く。\n"
        "  image_prompt: そのシーンを描くイラストの英語プロンプト(構図・背景・光・人物の動き)。\n"
        "  画像内に文字は入れない。実在の人物や既存作品のキャラクターは使わない。\n"
    )


def generate_script_with_gemini(theme, genre, tone):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ GEMINI_API_KEY が設定されていません。")
    client = genai.Client(api_key=api_key)

    # 混雑時に自動でモデルを切り替え
    models_to_try = [MODEL, "gemini-2.5-flash", "gemini-1.5-flash"]
    
    for model_name in models_to_try:
        log(f"🤖 Gemini API接続試行中... (モデル: {model_name})")
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=build_prompt(theme, genre, tone),
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=Drama,
                        temperature=0.9,
                    ),
                )
                data = json.loads(response.text)
                scenes = [
                    {"text": sc["text"].strip(), "image_prompt": (sc.get("image_prompt") or "").strip()}
                    for sc in data["scenes"] if sc.get("text", "").strip()
                ]
                if scenes and data.get("title"):
                    log(f"✨ 台本生成に成功しました ({model_name})")
                    return {
                        "title": data["title"].strip()[:100],
                        "description": data.get("description", "").strip(),
                        "visual_style": (data.get("visual_style") or "").strip(),
                        "scenes": scenes,
                    }
            except Exception as e:
                log(f"⚠️ API応答エラー ({model_name}): {str(e)[:150]}")
                time.sleep(3)

    log("❌ Gemini APIが高負荷のため、救済用バックアップ台本を使用して動画生成を完走させます。")
    return {
        "title": f"【{genre}】{theme}",
        "description": f"{theme}を描く完全オリジナルAIドラマ。#{genre} #AIドラマ #オリジナルアニメ",
        "visual_style": "Japanese anime style, cinematic lighting, masterpiece",
        "scenes": [
            {"text": f"静寂に包まれた世界。今回は{theme}をめぐる、誰も見たことのない物語をお届けします。", "image_prompt": f"mysterious scene about {theme}, anime style"},
            {"text": "定められた運命の歯車が、音を立てて狂い始めます。隠された真実とは一体何なのでしょうか。", "image_prompt": "dramatic clockwork and glowing gears, cinematic"},
            {"text": "扉の向こうに広がっていたのは、想像を絶する景色でした。物語は、ここから新たな局面を迎えます。", "image_prompt": "glowing portal to another world, beautiful background"}
        ]
    }


# ---------------------------------------------------------------- AI画像自動生成
def generate_images_for_scenes(visual_style, scenes):
    """Pollinations.aiを使用してシーンごとのAI背景画像を自動生成。失敗しても絶対に止まらない。"""
    IMAGE_DIR.mkdir(exist_ok=True)
    log("🎨 AI画像の自動生成を開始します...")
    
    for i, scene in enumerate(scenes, start=1):
        image_path = IMAGE_DIR / f"scene_{i}.png"
        try:
            prompt_text = f"{visual_style}, {scene['image_prompt']}, masterpiece, anime style, highly detailed, 8k"
            encoded_prompt = urllib.parse.quote(prompt_text)
            url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1280&height=720&seed={i*100}&nologo=true"
            
            log(f" 🎨 シーン{i}の画像を生成中...")
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as resp, open(image_path, 'wb') as f:
                f.write(resp.read())
            log(f"  ✅ シーン{i}の画像生成完了")
        except Exception as e:
            log(f"  ⚠️ シーン{i}の画像生成スキップ（背景カラーアニメーションで代用）: {e}")


# ---------------------------------------------------------------- ナレーション
def split_into_clips(scene_texts):
    clips = []
    for scene_index, scene in enumerate(scene_texts):
        for sentence in re.split(r"(?<=[。！？!?])\s*", scene.strip()):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= MAX_CHARS_PER_CLIP:
                clips.append((scene_index, sentence))
                continue
            buf = ""
            for part in re.split(r"(?<=、)", sentence):
                if buf and len(buf) + len(part) > MAX_CHARS_PER_CLIP:
                    clips.append((scene_index, buf))
                    buf = ""
                buf += part
            if buf:
                clips.append((scene_index, buf))
    return clips


def synthesize(text, mp3_path, engine):
    for attempt in range(3):
        try:
            if engine == "edge":
                if edge_tts is None:
                    raise RuntimeError("edge-tts がインストールされていません")
                asyncio.run(edge_tts.Communicate(text, EDGE_VOICE).save(str(mp3_path)))
            else:
                gTTS(text=text, lang="ja", slow=False).save(str(mp3_path))
            return
        except Exception as e:
            log(f"⚠️ 音声生成エラー({engine}): {e}")
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def probe_duration(path):
    result = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        timeout=60,
    )
    return float(result.stdout.strip())


def _create_narration(scene_texts, engine):
    log(f"🎙 ナレーション生成中... (エンジン {engine}, 速度 {SPEED}倍)")
    items = split_into_clips(scene_texts)

    lead = WORK_DIR / "lead.wav"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", f"{LEAD_IN}", "-c:a", "pcm_s16le", str(lead)], timeout=60)

    wavs = [lead]
    timeline = []
    cursor = LEAD_IN
    for i, (scene_index, text) in enumerate(items):
        mp3 = WORK_DIR / f"clip_{i:03d}.mp3"
        wav = WORK_DIR / f"clip_{i:03d}.wav"
        synthesize(text, mp3, engine)
        run(["ffmpeg", "-y", "-i", str(mp3),
             "-af", f"atempo={SPEED},apad=pad_dur={CLIP_GAP}",
             "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(wav)], timeout=120)
        duration = probe_duration(wav)
        timeline.append({"text": text, "scene": scene_index, "start": cursor, "end": cursor + duration})
        cursor += duration
        wavs.append(wav)

    list_file = WORK_DIR / "concat.txt"
    list_file.write_text(
        "".join(f"file '{w.resolve()}'\n" for w in wavs), encoding="utf-8"
    )
    narration = WORK_DIR / "narration.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", str(narration)], timeout=300)
    log(f"✅ ナレーション完成: {cursor:.1f}秒 ({len(items)}文)")
    return narration, timeline, cursor


def create_narration(scene_texts):
    engines = [TTS_ENGINE] + (["gtts"] if TTS_ENGINE != "gtts" else [])
    for i, engine in enumerate(engines):
        try:
            return _create_narration(scene_texts, engine)
        except Exception as e:
            log(f"⚠️ {engine} でのナレーション生成に失敗: {e}")
            if i == len(engines) - 1:
                raise
            log("↩️ gTTSで最初から作り直します。")


# ---------------------------------------------------------------- 動画合成
def find_japanese_font():
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", "Noto Sans CJK JP"],
            capture_output=True, text=True, timeout=10,
        )
        path = result.stdout.strip()
        if path and "CJK" in path and Path(path).exists():
            return path
    except Exception:
        pass
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return path
    raise RuntimeError("❌ 日本語フォントが見つかりません。")


def wrap_text(text, width=SUB_CHARS_PER_LINE):
    return "\n".join(text[i:i + width] for i in range(0, len(text), width))


def write_text_file(name, text):
    path = WORK_DIR / name
    path.write_text(text, encoding="utf-8")
    return path.as_posix()


def find_scene_image(number):
    for ext in ("png", "jpg", "jpeg", "webp"):
        path = IMAGE_DIR / f"scene_{number}.{ext}"
        if path.exists():
            return path
    return None


def scene_frame_bounds(timeline, n_scenes, total):
    starts = {}
    for clip in timeline:
        starts.setdefault(clip["scene"], clip["start"])
    starts[0] = 0.0
    bounds, last = [], 0.0
    for i in range(n_scenes):
        last = starts.get(i, last)
        bounds.append(round(last * FPS))
    bounds.append(round(total * FPS))
    return bounds


def make_scene_segment(index, image, frames):
    out = WORK_DIR / f"seg_{index:02d}.mp4"
    duration = frames / FPS
    fade = (f"fade=t=in:st=0:d={SCENE_FADE},"
            f"fade=t=out:st={max(duration - SCENE_FADE, 0):.3f}:d={SCENE_FADE}")
    center = "x=iw/2-(iw/zoom/2):y=ih/2-(ih/zoom/2)"
    if image is not None:
        mode = index % 3
        if mode == 0:
            move = f"z='1+{ZOOM_AMOUNT}*on/{frames}':{center}"
        elif mode == 1:
            move = f"z='{1 + ZOOM_AMOUNT}-{ZOOM_AMOUNT}*on/{frames}':{center}"
        else:
            move = f"z='{1 + ZOOM_AMOUNT}':x='(iw-iw/zoom)*on/{frames}':y=ih/2-(ih/zoom/2)"
        vf = (f"scale={WIDTH * 2}:{HEIGHT * 2}:force_original_aspect_ratio=increase,"
              f"crop={WIDTH * 2}:{HEIGHT * 2},"
              f"zoompan={move}:d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},{fade}")
        source = ["-i", str(image)]
    else:
        color = BG_PALETTE[index % len(BG_PALETTE)]
        vf = f"hue=h='12*t',vignette=PI/4,{fade}"
        source = ["-f", "lavfi", "-i", f"color=c={color}:s={WIDTH}x{HEIGHT}:r={FPS}"]
    run(["ffmpeg", "-y", *source, "-vf", vf, "-frames:v", str(frames),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
         "-r", str(FPS), str(out)], timeout=600)
    return out


def create_video(narration, timeline, total_audio, title, scenes):
    log("🎬 動画合成を開始します...")
    font = find_japanese_font()
    output_path = WORK_DIR / "ai_drama_output.mp4"
    total = total_audio + TAIL

    bounds = scene_frame_bounds(timeline, len(scenes), total)
    segments = []
    for i in range(len(scenes)):
        frames = bounds[i + 1] - bounds[i]
        if frames <= 0:
            continue
        image = find_scene_image(i + 1)
        log(f"🖼 シーン{i + 1}: {image.name if image else '背景アニメーション'} ({frames / FPS:.1f}秒)")
        segments.append(make_scene_segment(i, image, frames))

    list_file = WORK_DIR / "segments.txt"
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in segments), encoding="utf-8")

    title_file = write_text_file("title.txt", wrap_text(title, 20))
    speech_start, speech_end = timeline[0]["start"], timeline[-1]["end"]
    filters = [
        f"drawbox=x=0:y=ih-230:w=iw:h=230:color=black@0.4:t=fill:"
        f"enable='between(t,{speech_start:.2f},{speech_end:.2f})'",
        f"drawtext=fontfile={font}:textfile={title_file}:expansion=none:fontcolor=0x00ffff:"
        f"fontsize=42:line_spacing=10:borderw=3:bordercolor=black:x=(w-text_w)/2:y=60:"
        f"enable='between(t,{TITLE_SHOW[0]},{TITLE_SHOW[1]})'",
    ]
    for i, clip in enumerate(timeline):
        sub_file = write_text_file(f"sub_{i:03d}.txt", wrap_text(clip["text"]))
        filters.append(
            f"drawtext=fontfile={font}:textfile={sub_file}:expansion=none:fontcolor=white:"
            f"fontsize=40:line_spacing=12:borderw=2:bordercolor=black:x=(w-text_w)/2:y=h-205:"
            f"enable='between(t,{clip['start']:.2f},{clip['end']:.2f})'"
        )

    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-i", str(narration),
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-t", f"{total:.2f}",
        "-movflags", "+faststart",
        str(output_path),
    ], timeout=900)
    log(f"✅ 動画生成完了: {output_path} ({total:.1f}秒)")
    return output_path


# ---------------------------------------------------------------- 投稿
def upload_to_youtube(video_path, metadata):
    if DRY_RUN:
        log("🧪 DRY_RUN=true のため、YouTubeアップロードはスキップします。")
        log(f"📁 完成動画は {video_path} に保存されました（Actionsの成果物からダウンロード可能）。")
        return None

    try:
        creds = Credentials.from_authorized_user_file("token.json")
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

        description = metadata["description"] + "\n\n※この動画はAIで自動生成されたオリジナル作品です。"
        body = {
            "snippet": {
                "title": metadata["title"],
                "description": description,
                "tags": ["AIドラマ", "オリジナルアニメ", "Gemini", "YouTube自動化"],
                "categoryId": "24",
            },
            "status": {
                "privacyStatus": PRIVACY,
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(str(video_path), mimetype="video/mp4",
                                chunksize=8 * 1024 * 1024, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        log("🚀 YouTubeへのアップロードを開始...")
        response, retries = None, 0
        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    log(f"   アップロード中 {int(status.progress() * 100)}%")
            except HttpError as e:
                if "uploadLimitExceeded" in str(e):
                    log("⚠️ YouTubeチャンネルの投稿上限に達しています。")
                    log("💡 今回の実行は失敗にせず『テスト投稿モード』として完了します。")
                    log(f"📁 動画ファイル ({video_path}) はGitHub Actions成果物から確認・保存できます。")
                    return None
                if e.resp.status in (500, 502, 503, 504) and retries < 5:
                    retries += 1
                    time.sleep(2**retries)
                else:
                    raise

        log("=" * 40)
        log("🎉 アップロード成功！")
        log(f"🎬 動画ID: {response['id']}")
        log(f"🔗 URL: https://youtu.be/{response['id']}")
        log("=" * 40)
        return response["id"]
    except Exception as e:
        log(f"⚠️ アップロード中に例外が発生しました: {e}")
        log(f"📁 動画自体は正常生成されているため成果物（{video_path}）を保持します。")
        return None


# ---------------------------------------------------------------- main
def main():
    WORK_DIR.mkdir(exist_ok=True)
    restore_credentials()

    theme = os.environ.get("INPUT_VIDEO_TITLE")
    genre = os.environ.get("INPUT_VIDEO_GENRE") or "AIドラマ"
    tone = os.environ.get("INPUT_VIDEO_TONE") or "シネマティック"
    if not theme:
        raise ValueError("❌ スプレッドシートのA2セル（テーマ）が空です。")

    log(f"🎯 制作開始 -> テーマ: {theme} (ジャンル: {genre}, 雰囲気: {tone})")

    # 1. 台本生成（API混雑時の全自動フォールバック対応）
    metadata = generate_script_with_gemini(theme, genre, tone)
    (WORK_DIR / "script.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2. AIアニメ背景画像の全自動生成
    generate_images_for_scenes(metadata["visual_style"], metadata["scenes"])

    # 3. ナレーション音声の作成
    scene_texts = [sc["text"] for sc in metadata["scenes"]]
    narration, timeline, total_audio = create_narration(scene_texts)

    # 4. 動画と字幕の結合
    video_path = create_video(narration, timeline, total_audio, metadata["title"], metadata["scenes"])

    # 5. 投稿
    upload_to_youtube(video_path, metadata)


if __name__ == "__main__":
    main()
