import os
import sys
import json
import asyncio
import logging
import requests
from pathlib import Path
from datetime import datetime

import edge_tts
from duckduckgo_search import DDGS
from json_repair import repair_json
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

# 尺の目標（8分〜10分 / 許容: 7.5分〜11分）
MIN_SEC, TARGET_SEC, MAX_SEC = 480, 540, 600
ALLOW_MIN_SEC, ALLOW_MAX_SEC = 450, 660


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
    """台本（シーン構成）をJSON形式で生成する"""
    prompt = f"""
以下のテーマに基づき、YouTube動画用の台本をJSON形式で作成してください。
テーマ: {theme}
ジャンル: {INPUT_GENRE}
雰囲気: {INPUT_TONE}

条件:
- 全体で8〜10分程度のボリュームになるよう、約9〜11シーンで構成してください。
- 各シーンには、画面に使うための検索キーワード（search_query）と、ナレーション用のテキスト（text）を含めてください。
- 出力は必ず以下のJSONフォーマットのみ（マークダウンのバッククォート等を含めない純粋なJSON文字列）で行ってください。

{{
  "title": "動画タイトル",
  "description": "YouTubeの概要欄用テキスト",
  "scenes": [
    {{
      "search_query": "英語の検索キーワード（Pixabay用）",
      "text": "このシーンのナレーションテキスト（十分な長さを持たせてください）"
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
    """尺が足りない場合や長すぎる場合に台本を調整する"""
    log.info("音声の尺(%.1f秒)を調整するため、台本を再構成します...", current_seconds)
    prompt = f"""
現在の動画のナレーション音声が {current_seconds:.1f} 秒です。
これを目標の 480秒〜600秒（8〜10分）の範囲に収まるように、シーンのテキストを調整・加筆してください。
現在のJSONデータを維持しつつ、textの内容を調整したJSONを純粋なJSON文字列として出力してください。

{json.dumps(script_data, ensure_ascii=False)}
"""
    raw_res = call_groq(prompt)
    cleaned = raw_res.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        return json.loads(repair_json(cleaned))


async def create_narration_async(scenes: list):
    """edge-ttsを使ってシーンごとの音声を生成し、結合する（非同期）"""
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
        
        # 音声生成
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

# 音声の結合
    final_audio = concatenate_audioclips(audio_clips)
    narration_path = WORK / "narration.mp3"
    final_audio.write_audiofile(str(narration_path), codec="libmp3lame", logger=None)
    
    for clip in audio_clips:
        clip.close()

    return str(narration_path), timeline, current_time


def acquire_assets(scenes: list) -> list:
    """Pixabayから各シーンの背景素材（画像/動画）をダウンロードする"""
    assets = []
    headers = {"Authorization": PIXABAY_API_KEY}
    
    for idx, scene in enumerate(scenes):
        query = scene.get("search_query", "technology")
        asset_path = WORK / f"asset_{idx}.jpg"
        
        try:
            # Pixabay Video API
            v_url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={requests.utils.quote(query)}&per_page=3"
            v_res = requests.get(v_url, timeout=10).json()
            if v_res.get("hits"):
                vid_url = v_res["hits"][0]["videos"]["medium"]["url"]
                v_data = requests.get(vid_url, timeout=30).content
                asset_path = WORK / f"asset_{idx}.mp4"
                asset_path.write_bytes(v_data)
                assets.append(str(asset_path))
                continue
        except Exception as e:
            log.warning("動画素材の取得に失敗しました (%s): %s。画像にフォールバックします。", query, e)

        # フォールバック: Pixabay Image API
        try:
            i_url = f"https://pixabay.com/api/?key={PIXABAY_API_KEY}&q={requests.utils.quote(query)}&image_type=photo&per_page=3"
            i_res = requests.get(i_url, timeout=10).json()
            if i_res.get("hits"):
                img_url = i_res["hits"][0]["largeImageURL"]
                i_data = requests.get(img_url, timeout=30).content
                asset_path.write_bytes(i_data)
                assets.append(str(asset_path))
                continue
        except Exception as e:
            log.warning("画像素材の取得にも失敗しました (%s): %s。ダミー画像を使います。", query, e)

        # 最終フォールバック（簡易的な無地画像生成など、あるいは既存素材の流用）
        # ここではダミーとして白黒のプレースホルダーを生成または適当に処理
        from PIL import Image
        img = Image.new("RGB", (1920, 1080), color=(30, 30, 30))
        img.save(str(asset_path))
        assets.append(str(asset_path))

    return assets


def create_final_video(timeline: list, assets: list, narration_path: str) -> str:
    """背景素材と音声を結合して最終的な動画をレンダリングする（テロップなし・爆速）"""
    video_clips = []
    
    for i, t in enumerate(timeline):
        duration = t["end"] - t["start"]
        asset_path = assets[i]
        
        if asset_path.endswith(".mp4"):
            clip = VideoFileClip(asset_path)
            # 尺の調整（短い場合はループ、長い場合はカット）
            if clip.duration < duration:
                loops = int(duration // clip.duration) + 1
                clip = concatenate_videoclips([clip] * loops)
            # v2.x系に対応した subclipped を使用
            clip = clip.subclipped(0, duration)
        else:
            # v2.x系に対応した with_duration を使用
            clip = ImageClip(asset_path).with_duration(duration)
            
        # 16:9比率にリサイズ・調整（v2.x系では resized を使用）
        clip = clip.resized(newsize=(1920, 1080))
        video_clips.append(clip)

    final_video = concatenate_videoclips(video_clips, method="compose")
    audio = AudioFileClip(narration_path)
    # v2.x系に対応した with_audio を使用
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
    
    # 2. 尺調整ループ付き音声生成
    for attempt in range(3):
        narration_path, timeline, seconds = asyncio.run(create_narration_async(script_data["scenes"]))
        log.info("音声実測時間: %.1f秒", seconds)
        
        if ALLOW_MIN_SEC <= seconds <= ALLOW_MAX_SEC:
            break
        if attempt < 2:
            script_data = adjust_duration(script_data, seconds)

    # 許容範囲外でもエラーにせず、警告を出して続行する
    if not ALLOW_MIN_SEC <= seconds <= ALLOW_MAX_SEC:
        log.warning("尺が目標範囲（450〜660秒）から外れていますが（%.1f秒）、そのまま動画生成を続行します。", seconds)

    # script.jsonの保存
    (WORK / "script.json").write_text(json.dumps(script_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. 素材調達
    log.info("Pixabayから背景素材を調達しています...")
    assets = acquire_assets(script_data["scenes"])

    # 4. 最終動画レンダリング（テロップなし）
    log.info("動画をレンダリングしています...")
    final_video_path = create_final_video(timeline, assets, narration_path)
    log.info("動画生成が完了しました: %s", final_video_path)


if __name__ == "__main__":
    main()
