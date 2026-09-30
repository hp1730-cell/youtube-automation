"""AI長編ドラマ動画(8〜10分)を自動生成してYouTubeに投稿するスクリプト。

流れ: Geminiで長編台本生成 → Pollinations.aiで画像生成 → 
      Pillowでサムネイル自動生成(New!) → edge-ttsでナレーション生成 → 
      FFmpegでシネマティックBGM生成＆ミックス(New!) → 動画合成 → 
      YouTube投稿＆サムネイル設定(New!)
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

# サムネイル生成用ライブラリ(Pillow)の動的インストールとインポート
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    subprocess.run(["pip", "install", "pillow"], check=True)
    from PIL import Image, ImageDraw, ImageFont

from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from gtts import gTTS
from pydantic import BaseModel

try:
    import edge_tts
except ImportError:
    edge_tts = None

# ---------------------------------------------------------------- 設定
WORK_DIR = Path("work")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
SPEED = float(os.environ.get("NARRATION_SPEED", "1.5"))
TTS_ENGINE = os.environ.get("TTS_ENGINE", "edge")
EDGE_VOICE = os.environ.get("EDGE_VOICE", "ja-JP-NanamiNeural")
PRIVACY = os.environ.get("PRIVACY_STATUS", "unlisted")
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

WIDTH, HEIGHT, FPS = 1280, 720, 30
IMAGE_DIR = Path(os.environ.get("IMAGE_DIR", "images"))
BG_PALETTE = ["0x2b1055", "0x0f2a55", "0x4a1040", "0x0d3b3b"]
SCENE_FADE = 0.4
ZOOM_AMOUNT = 0.15
TITLE_SHOW = (0.5, 4.5)
LEAD_IN = 0.8
TAIL = 2.0
CLIP_GAP = 0.25
MAX_CHARS_PER_CLIP = 50
SUB_CHARS_PER_LINE = 24

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def log(*args):
    print(*args, flush=True)


def run(cmd, timeout=900):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        log(f"❌ コマンド失敗: {cmd[0]} (code {result.returncode})")
        log(result.stderr[-3000:])
        raise RuntimeError(f"{cmd[0]} が失敗しました")
    return result


# ---------------------------------------------------------------- 認証
def restore_credentials():
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
        "あなたはYouTubeで数十万回再生される長編AIアニメドラマのプロ脚本家です。\n"
        "以下の条件に従い、再生時間が8分〜10分（ナレーション文字数：3,200〜4,000文字）となる、非常に見ごたえのある長編AIドラマ台本を作成してください。\n\n"
        "【厳格な禁止事項】\n"
        "・「静寂に包まれた」「運命の歯車」のような抽象的なポエム・雰囲気だけの文章は一切禁止。\n"
        "・実在の人物、既存作品のキャラ、商標権・著作権に触れる固有名詞は一切使用禁止。\n\n"
        "【長編ドラマの構成ルール】\n"
        f"- メインテーマ: {theme}\n"
        f"- ジャンル: {genre}\n"
        f"- トーン: {tone}\n\n"
        "【構成指定（全16〜20シーン）】\n"
        "1. 【第1〜3シーン（導入・引き）】: 衝撃的な事件、理不尽な状況、または強い問いかけから始まり、視聴者の手を止めさせる。\n"
        "2. 【第4〜10シーン（展開・対話）】: 登場人物同士のリアルな掛け合い・セリフ（「〜」）を多用し、人間関係の対立や伏線を深める。\n"
        "3. 【第11〜16シーン（クライマックス・逆転）】: 隠された真実の発覚、怒涛の展開、または見事な解決（スカッと展開など）。\n"
        "4. 【第17〜20シーン（結末・余韻）】: 納得感のある結末と、深い余韻を残す後日談。\n\n"
        "【各シーンの条件】\n"
        "- title: 目を引く30文字以内のタイトル\n"
        "- description: 動画の概要(ハッシュタグ3つを含む)\n"
        "- visual_style: 全シーン共通の画風と登場人物の外見の固定設定(英語)。\n"
        "・1シーンにつき、ナレーション文（text）は200文字程度（話し言葉の3〜5文）。\n"
        "・1シーンにつき、画風と構図を英語で指定したAI画像プロンプト（image_prompt）を具体的に記述する。\n\n"
        "出力フォーマットは必ずJSONのみとし、Markdown表記や余計な解説は含めないでください。\n"
    )


def generate_script_with_gemini(theme, genre, tone):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ GEMINI_API_KEY が設定されていません。")
    client = genai.Client(api_key=api_key)

    models_to_try = [MODEL, "gemini-2.5-flash", "gemini-1.5-flash"]
    
    for model_name in models_to_try:
        log(f"🤖 8〜10分長尺台本を生成中... (モデル: {model_name})")
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=build_prompt(theme, genre, tone),
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=Drama,
                        temperature=0.85,
                        max_output_tokens=8192,
                    ),
                )
                data = json.loads(response.text)
                scenes = [
                    {"text": sc["text"].strip(), "image_prompt": (sc.get("image_prompt") or "").strip()}
                    for sc in data["scenes"] if sc.get("text", "").strip()
                ]
                if len(scenes) >= 8 and data.get("title"):
                    log(f"✨ 8〜10分長尺台本の生成に成功しました！（全{len(scenes)}シーン）")
                    return {
                        "title": data["title"].strip()[:100],
                        "description": data.get("description", "").strip(),
                        "visual_style": (data.get("visual_style") or "").strip(),
                        "scenes": scenes,
                    }
            except Exception as e:
                log(f"⚠️ API応答エラー ({model_name}): {str(e)[:150]}")
                time.sleep(3)

    log("❌ 長尺台本の自動生成に失敗したため、バックアップ用台本に切り替えます。")
    fallback_scenes = []
    for i in range(1, 16):
        fallback_scenes.append({
            "text": f"第{i}幕の展開です。{theme}をめぐる物語は、予想もしない展開へと進んでいきます。果たして、この先に待ち受ける真実とは何なのでしょうか。",
            "image_prompt": f"cinematic scene {i} about {theme}, highly detailed anime style"
        })
    return {
        "title": f"【長編AIドラマ】{theme}",
        "description": f"{theme}を描く完全オリジナル長編AIアニメドラマ。#{genre} #AIドラマ #長編アニメ",
        "visual_style": "Japanese anime style, cinematic lighting, masterpiece, 8k",
        "scenes": fallback_scenes
    }


# ---------------------------------------------------------------- AI画像＆サムネイル生成
def generate_images_for_scenes(visual_style, scenes):
    IMAGE_DIR.mkdir(exist_ok=True)
    log(f"🎨 全{len(scenes)}シーンのAI画像生成を開始します...")
    
    for i, scene in enumerate(scenes, start=1):
        image_path = IMAGE_DIR / f"scene_{i}.png"
        try:
            prompt_text = f"{visual_style}, {scene['image_prompt']}, masterpiece, anime style, highly detailed, 8k"
            encoded_prompt = urllib.parse.quote(prompt_text)
            url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1280&height=720&seed={i*100}&nologo=true"
            
            log(f" 🎨 シーン{i}/{len(scenes)} の画像を生成中...")
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as resp, open(image_path, 'wb') as f:
                f.write(resp.read())
        except Exception as e:
            log(f"  ⚠️ シーン{i}の画像生成スキップ（背景カラーアニメーションで代用）: {e}")

def create_thumbnail(title, bg_image_path, font_path):
    log("🖼 サムネイル画像を生成中...")
    thumb_path = WORK_DIR / "thumbnail.jpg"
    try:
        img = Image.open(bg_image_path).convert("RGBA")
        # 暗いグラデーションを重ねて文字の視認性を上げる
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 110))
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype(font_path, 80)
        
        # タイトル描画（縁取り付き）
        lines = "\n".join(title[i:i + 15] for i in range(0, len(title), 15)).split("\n")
        y = HEIGHT // 2 - (len(lines) * 90) // 2
        for line in lines:
            if hasattr(draw, "textbbox"):
                bbox = draw.textbbox((0, 0), line, font=font)
                w = bbox[2] - bbox[0]
            else:
                w = draw.textlength(line, font=font) if hasattr(draw, "textlength") else 80 * len(line)
                
            x = (WIDTH - w) // 2
            # 黒い縁取り
            for offset_x, offset_y in [(-4,-4), (4,-4), (-4,4), (4,4), (-4,0), (4,0), (0,-4), (0,4)]:
                draw.text((x + offset_x, y + offset_y), line, font=font, fill="black")
            # メイン文字色（黄色）
            draw.text((x, y), line, font=font, fill="yellow")
            y += 90
            
        img.convert("RGB").save(thumb_path)
        log("✅ サムネイル画像の生成完了")
        return thumb_path
    except Exception as e:
        log(f"⚠️ サムネイル生成エラー: {e}")
        return None


# ---------------------------------------------------------------- ナレーション＆BGM生成
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
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def probe_duration(path):
    result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], timeout=60)
    return float(result.stdout.strip())


def create_bgm(duration, out_path):
    """FFmpegのシンセサイザーでシネマティックな低音環境音(BGM)を自動生成"""
    log("🎵 シネマティックBGMを生成中...")
    run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "anoisesrc=a=0.5:c=pink",
        "-af", "lowpass=f=400,tremolo=f=0.1:d=0.5,volume=0.2",
        "-t", str(duration),
        str(out_path)
    ], timeout=120)


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
    list_file.write_text("".join(f"file '{w.resolve()}'\n" for w in wavs), encoding="utf-8")
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


# ---------------------------------------------------------------- 動画合成
def find_japanese_font():
    try:
        result = subprocess.run(["fc-match", "-f", "%{file}", "Noto Sans CJK JP"], capture_output=True, text=True, timeout=10)
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
    fade = f"fade=t=in:st=0:d={SCENE_FADE},fade=t=out:st={max(duration - SCENE_FADE, 0):.3f}:d={SCENE_FADE}"
    center = "x=iw/2-(iw/zoom/2):y=ih/2-(ih/zoom/2)"
    if image is not None:
        mode = index % 3
        if mode == 0:
            move = f"z='1+{ZOOM_AMOUNT}*on/{frames}':{center}"
        elif mode == 1:
            move = f"z='{1 + ZOOM_AMOUNT}-{ZOOM_AMOUNT}*on/{frames}':{center}"
        else:
            move = f"z='{1 + ZOOM_AMOUNT}':x='(iw-iw/zoom)*on/{frames}':y=ih/2-(ih/zoom/2)"
        vf = f"scale={WIDTH * 2}:{HEIGHT * 2}:force_original_aspect_ratio=increase,crop={WIDTH * 2}:{HEIGHT * 2},zoompan={move}:d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},{fade}"
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
    log("🎬 動画合成を開始します... (長編のため時間がかかります)")
    font = find_japanese_font()
    output_path = WORK_DIR / "ai_drama_output.mp4"
    total = total_audio + TAIL

    # 1. BGMの生成と音声の事前ミックス（ナレーション＋BGM）
    bgm_path = WORK_DIR / "bgm.wav"
    create_bgm(total, bgm_path)
    
    final_audio = WORK_DIR / "final_audio.wav"
    log("🎵 音声ミックス中...")
    run([
        "ffmpeg", "-y", "-i", str(narration), "-i", str(bgm_path),
        "-filter_complex", "[0:a]volume=1.2[a1];[1:a]volume=0.6[a2];[a1][a2]amix=inputs=2:duration=first:dropout_transition=2",
        str(final_audio)
    ], timeout=120)

    # 2. 映像セグメントの作成
    bounds = scene_frame_bounds(timeline, len(scenes), total)
    segments = []
    for i in range(len(scenes)):
        frames = bounds[i + 1] - bounds[i]
        if frames <= 0:
            continue
        image = find_scene_image(i + 1)
        segments.append(make_scene_segment(i, image, frames))

    list_file = WORK_DIR / "segments.txt"
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in segments), encoding="utf-8")

    # 3. 字幕フィルターの作成
    title_file = write_text_file("title.txt", wrap_text(title, 20))
    speech_start, speech_end = timeline[0]["start"], timeline[-1]["end"]
    filters = [
        f"drawbox=x=0:y=ih-230:w=iw:h=230:color=black@0.4:t=fill:enable='between(t,{speech_start:.2f},{speech_end:.2f})'",
        f"drawtext=fontfile={font}:textfile={title_file}:expansion=none:fontcolor=0x00ffff:fontsize=42:line_spacing=10:borderw=3:bordercolor=black:x=(w-text_w)/2:y=60:enable='between(t,{TITLE_SHOW[0]},{TITLE_SHOW[1]})'",
    ]
    for i, clip in enumerate(timeline):
        sub_file = write_text_file(f"sub_{i:03d}.txt", wrap_text(clip["text"]))
        filters.append(
            f"drawtext=fontfile={font}:textfile={sub_file}:expansion=none:fontcolor=white:fontsize=40:line_spacing=12:borderw=2:bordercolor=black:x=(w-text_w)/2:y=h-205:enable='between(t,{clip['start']:.2f},{clip['end']:.2f})'"
        )

    # 4. 最終合成
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-i", str(final_audio),
        "-vf", ",".join(filters), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.2f}", "-movflags", "+faststart", str(output_path),
    ], timeout=1800)
    log(f"✅ 動画生成完了: {output_path} ({total:.1f}秒)")
    return output_path


# ---------------------------------------------------------------- 投稿
def upload_to_youtube(video_path, thumbnail_path, metadata):
    if DRY_RUN:
        log("🧪 DRY_RUN=true のため、YouTubeアップロードはスキップします。")
        return None

    try:
        creds = Credentials.from_authorized_user_file("token.json")
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        description = metadata["description"] + "\n\n※この動画はAIで自動生成されたオリジナル作品です。"
        body = {
            "snippet": {
                "title": metadata["title"],
                "description": description,
                "tags": ["AIドラマ", "オリジナルアニメ", "Gemini", "YouTube自動化", "長編"],
                "categoryId": "24",
            },
            "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
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
                    log("⚠️ YouTubeチャンネルの投稿上限に達しています。テスト投稿モードとして完了します。")
                    return None
                if e.resp.status in (500, 502, 503, 504) and retries < 5:
                    retries += 1
                    time.sleep(2**retries)
                else:
                    raise

        log("=" * 40)
        log(f"🎉 アップロード成功！ 🔗 URL: https://youtu.be/{response['id']}")
        
        # サムネイル設定
        if thumbnail_path and Path(thumbnail_path).exists():
            log("🖼 サムネイルを設定中...")
            try:
                youtube.thumbnails().set(
                    videoId=response['id'],
                    media_body=MediaFileUpload(str(thumbnail_path))
                ).execute()
                log("✅ サムネイル設定完了")
            except Exception as e:
                log(f"⚠️ サムネイル設定エラー: {e}")
                
        log("=" * 40)
        return response["id"]
    except Exception as e:
        log(f"⚠️ アップロード中に例外が発生しました: {e}")
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

    log(f"🎯 長編制作開始 -> テーマ: {theme} (ジャンル: {genre}, 雰囲気: {tone})")

    metadata = generate_script_with_gemini(theme, genre, tone)
    (WORK_DIR / "script.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    generate_images_for_scenes(metadata["visual_style"], metadata["scenes"])
    
    # サムネイル生成
    thumbnail_path = None
    scene1_image = find_scene_image(1)
    if scene1_image:
        thumbnail_path = create_thumbnail(metadata["title"], scene1_image, find_japanese_font())
    
    scene_texts = [sc["text"] for sc in metadata["scenes"]]
    narration, timeline, total_audio = create_narration(scene_texts)
    
    video_path = create_video(narration, timeline, total_audio, metadata["title"], metadata["scenes"])
    upload_to_youtube(video_path, thumbnail_path, metadata)


if __name__ == "__main__":
    main()
