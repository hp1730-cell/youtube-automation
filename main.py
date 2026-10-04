import os
import sys
import json
import asyncio
import logging
import requests
from pathlib import Path
from datetime import datetime

import edge_tts
from json_repair import repair_json
from PIL import Image, ImageDraw, ImageFont
from moviepy import (
    AudioFileClip,
    ImageClip,
    VideoFileClip,
    concatenate_audioclips,
    concatenate_videoclips
)

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("YouTubeAuto")

# 環境変数・定数
WORK = Path(os.getenv("WORK_DIR", "work"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
INPUT_TITLE = os.getenv("INPUT_VIDEO_TITLE", "AIの進化と未来")
INPUT_GENRE = os.getenv("INPUT_VIDEO_GENRE", "自動判定")
INPUT_TONE = os.getenv("INPUT_VIDEO_TONE", "わかりやすく、最後まで見たくなる雰囲気")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
EDGE_VOICE = os.getenv("EDGE_VOICE", "ja-JP-NanamiNeural")

# 尺の目標（8分〜10分）
MIN_SEC, TARGET_SEC, MAX_SEC = 480, 540, 600
ALLOW_MIN_SEC, ALLOW_MAX_SEC = 350, 660


def call_groq(prompt: str) -> str:
    """Groq APIを叩いてテキストを生成する"""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }
    
    for attempt in range(3):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=60)
            if res.status_code == 200:
                return res.json()["choices"][0]["message"]["content"]
            log.warning("Groq API エラー (Status %d): %s", res.status_code, res.text)
        except Exception as e:
            log.warning("Groq API 例外 (Attempt %d): %s", attempt + 1, e)
        import time
        time.sleep(3)
    
    raise RuntimeError("Groq APIの呼び出しに失敗しました。")


def generate_script(theme: str) -> dict:
    """台本（シーン構成・高精度な検索クエリ付き）をJSON形式で生成する"""
    prompt = f"""
以下のテーマに基づき、YouTube動画用の詳細な台本をJSON形式で作成してください。
テーマ: {theme}
ジャンル: {INPUT_GENRE}
雰囲気: {INPUT_TONE}

条件:
- 全体で8〜10分程度（合計2,400字〜3,000字以上）のボリュームになるよう、10〜12シーンで構成してください。
- 各シーンの `text` には、長めで詳しい解説文（1シーンあたり250〜300文字程度）を記述してください。
- 各シーンの `search_query` には、テーマやシーンの文脈に**正確に一致する英語の検索キーワード**（例: テーマがプリンなら "caramel pudding dessert", "making sweet food" など具体的でPixabayでヒットしやすいもの）を必ず指定してください。汎用的な単語（"technology"等）は禁止です。
- 出力は必ず以下のJSONフォーマットのみ（マークダウンのバッククォート等を含めない純粋なJSON文字列）で行ってください。

{{
  "title": "動画タイトル（YouTube用）",
  "description": "YouTubeの概要欄用テキスト",
  "scenes": [
    {{
      "search_query": "具体的でテーマに沿った英語の検索キーワード",
      "text": "このシーンの十分な長さを持たせたナレーションテキスト..."
    }}
  ]
}}
"""
    raw_res = call_groq(prompt)
    cleaned = raw_res.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        fixed = repair_json(cleaned)
        return json.loads(fixed)


def adjust_duration(script_data: dict, current_seconds: float) -> dict:
    """尺が足りない場合に台本を再構成する"""
    log.info("音声の尺(%.1f秒)を調整するため、台本を再構成します...", current_seconds)
    prompt = f"""
現在の動画のナレーション音声が {current_seconds:.1f} 秒です。
これを目標の 480秒〜600秒（8〜10分）の範囲に収まるように、シーンのテキストを調整・加筆してください。
search_queryは維持しつつ、textの内容を充実させたJSONを純粋なJSON文字列として出力してください。

{json.dumps(script_data, ensure_ascii=False)}
"""
    raw_res = call_groq(prompt)
    cleaned = raw_res.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        return json.loads(repair_json(cleaned))


async def create_narration_async(scenes: list):
    """edge-ttsを使ってシーンごとの音声を生成し、結合する"""
    audio_clips = []
    timeline = []
    current_time = 0.0
    
    temp_dir = WORK / "temp_audio"
    temp_dir.mkdir(parents=True, exist_ok=True)

    for idx, scene in enumerate(scenes):
        scene_start = current_time
        text = scene.get("text", "").strip()
        if not text:
            text = "解説を続けます。"

        temp_path = temp_dir / f"scene_{idx}.mp3"
        
        communicate = edge_tts.Communicate(text, EDGE_VOICE)
        await communicate.save(str(temp_path))
        
        clip = AudioFileClip(str(temp_path))
        audio_clips.append(clip)
        
        current_time += clip.duration
        timeline.append({
            "scene_index": idx,
            "start": scene_start,
            "end": current_time
        })

    final_audio = concatenate_audioclips(audio_clips)
    narration_path = WORK / "narration.mp3"
    final_audio.write_audiofile(str(narration_path), codec="libmp3lame", logger=None)
    
    for clip in audio_clips:
        clip.close()

    return str(narration_path), timeline, current_time


def acquire_assets(scenes: list) -> list:
    """Pixabayから各シーンの背景素材（関連性の高い画像/動画）を調達する"""
    assets = []
    
    for idx, scene in enumerate(scenes):
        query = scene.get("search_query", INPUT_TITLE)
        asset_path = WORK / f"asset_{idx}.jpg"
        
        # 1. Pixabay Video API
        try:
            v_url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={requests.utils.quote(query)}&per_page=3&safesearch=true"
            v_res = requests.get(v_url, timeout=10).json()
            if v_res.get("hits"):
                vid_url = v_res["hits"][0]["videos"]["medium"]["url"]
                v_data = requests.get(vid_url, timeout=30).content
                asset_path = WORK / f"asset_{idx}.mp4"
                asset_path.write_bytes(v_data)
                assets.append(str(asset_path))
                continue
        except Exception as e:
            log.warning("動画素材の取得に失敗しました (%s): %s", query, e)

        # 2. Pixabay Image API（フォールバック）
        try:
            i_url = f"https://pixabay.com/api/?key={PIXABAY_API_KEY}&q={requests.utils.quote(query)}&image_type=photo&per_page=3&safesearch=true"
            i_res = requests.get(i_url, timeout=10).json()
            if i_res.get("hits"):
                img_url = i_res["hits"][0]["largeImageURL"]
                i_data = requests.get(img_url, timeout=30).content
                asset_path.write_bytes(i_data)
                assets.append(str(asset_path))
                continue
        except Exception as e:
            log.warning("画像素材の取得に失敗しました (%s): %s", query, e)

        # 3. 最終フォールバック（無地ではなくトーンカラーの背景）
        img = Image.new("RGB", (1920, 1080), color=(20, 30, 45))
        img.save(str(asset_path))
        assets.append(str(asset_path))

    return assets


def generate_thumbnail(title: str):
    """YouTube用の目を引くサムネイル画像（thumbnail.jpg）を自動生成する"""
    log.info("YouTube用サムネイルを生成しています...")
    thumb_path = WORK / "thumbnail.jpg"
    
    # ベース画像を準備（適当な高解像度背景、またはPixabayから1枚調達）
    bg_img = None
    try:
        i_url = f"https://pixabay.com/api/?key={PIXABAY_API_KEY}&q={requests.utils.quote(INPUT_TITLE)}&image_type=photo&per_page=3"
        i_res = requests.get(i_url, timeout=10).json()
        if i_res.get("hits"):
            img_url = i_res["hits"][0]["largeImageURL"]
            img_data = requests.get(img_url, timeout=15).content
            from io import BytesIO
            bg_img = Image.open(BytesIO(img_data)).convert("RGB").resize((1280, 720))
    except Exception:
        pass

    if bg_img is None:
        bg_img = Image.new("RGB", (1280, 720), color=(15, 23, 42))

    # 暗めのオーバーレイをかけて文字を引き立たせる
    overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 140))
    bg_img = Image.alpha_composite(bg_img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(bg_img)
    
    # フォントの読み込み（Ubuntu環境のNoto Sans CJK）
    font_path = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
    if not os.path.exists(font_path):
        font_path = "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"
    
    try:
        font_large = ImageFont.truetype(font_path, 70)
        font_sub = ImageFont.truetype(font_path, 35)
    except Exception:
        font_large = ImageFont.load_default()
        font_sub = ImageFont.load_default()

    # タイトル文字列の描画（影付きで立体的に見せる）
    # 簡易的に折り返し
    text = title if len(title) <= 18 else title[:18] + "\n" + title[18:]
    
    # 影
    draw.text((82, 222), text, font=font_large, fill=(0, 0, 0))
    # メイン文字（黄色/白の目立つ配色）
    draw.text((80, 220), text, font=font_large, fill=(255, 230, 0))

    # サブテキスト
    draw.text((82, 522), "完全解説", font=font_sub, fill=(0, 0, 0))
    draw.text((80, 520), "完全解説", font=font_sub, fill=(255, 255, 255))

    bg_img.save(str(thumb_path), quality=95)
    log.info("サムネイルを生成しました: %s", thumb_path)


def create_final_video(timeline: list, assets: list, narration_path: str) -> str:
    """背景素材と音声を結合して最終的な動画をレンダリングする"""
    video_clips = []
    
    for i, t in enumerate(timeline):
        duration = t["end"] - t["start"]
        asset_path = assets[i]
        
        if asset_path.endswith(".mp4"):
            clip = VideoFileClip(asset_path)
            if clip.duration < duration:
                loops = int(duration // clip.duration) + 1
                clip = concatenate_videoclips([clip] * loops)
            clip = clip.subclipped(0, duration)
        else:
            clip = ImageClip(asset_path).with_duration(duration)
            
        clip = clip.resized((1920, 1080))
        video_clips.append(clip)

    final_video = concatenate_videoclips(video_clips, method="compose")
    audio = AudioFileClip(narration_path)
    final_video = final_video.with_audio(audio)
    
    output_path = WORK / "final.mp4"
    final_video.write_videofile(
        str(output_path),
        fps=24,
        codec="libx264",
        audio_codec="aac",
        preset="fast",
        logger=None
    )
    return str(output_path)


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    log.info("動画生成パイプラインを開始します: %s", INPUT_TITLE)

    # 1. 台本生成
    script_data = generate_script(INPUT_TITLE)
    
    # 2. 尺調整ループ付き音声生成（最大2回）
    for attempt in range(2):
        narration_path, timeline, seconds = asyncio.run(create_narration_async(script_data["scenes"]))
        log.info("音声実測時間: %.1f秒", seconds)
        
        if ALLOW_MIN_SEC <= seconds <= ALLOW_MAX_SEC:
            break
        if attempt < 1:
            script_data = adjust_duration(script_data, seconds)

    if not ALLOW_MIN_SEC <= seconds <= ALLOW_MAX_SEC:
        log.warning("尺が目標範囲から外れていますが（%.1f秒）、そのまま動画生成を続行します。", seconds)

    # script.jsonの保存
    (WORK / "script.json").write_text(json.dumps(script_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. 素材調達（高精度クエリ）
    log.info("Pixabayから関連性の高い背景素材を調達しています...")
    assets = acquire_assets(script_data["scenes"])

    # 4. サムネイル生成
    generate_thumbnail(script_data.get("title", INPUT_TITLE))

    # 5. 最終動画レンダリング
    log.info("動画をレンダリングしています...")
    final_video_path = create_final_video(timeline, assets, narration_path)
    log.info("動画生成が完了しました: %s", final_video_path)


if __name__ == "__main__":
    main()
