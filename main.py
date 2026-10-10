import os
import sys
import json
import asyncio
import logging
import requests
import traceback
from pathlib import Path
from datetime import datetime

import edge_tts
from json_repair import repair_json
from PIL import Image, ImageDraw, ImageFont
from moviepy import (
    AudioFileClip,
    ImageClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips,  # ← これが抜けていました！追加しました
    vfx
)

# --- ログ設定 ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("YouTubeAuto")

# --- 環境変数・定数 ---
WORK = Path(os.getenv("WORK_DIR", "work"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
INPUT_TITLE = os.getenv("INPUT_VIDEO_TITLE", "AIの進化と未来")
INPUT_GENRE = os.getenv("INPUT_VIDEO_GENRE", "自動判定")
INPUT_TONE = os.getenv("INPUT_VIDEO_TONE", "わかりやすく、最後まで見たくなる雰囲気")
EDGE_VOICE = os.getenv("EDGE_VOICE", "ja-JP-NanamiNeural")

# スタイル統一用
GLOBAL_STYLE = "High-quality cinematic anime style, Makoto Shinkai inspired, detailed background, vivid colors, 8k, masterpiece"

def call_groq(prompt: str) -> str:
    """Groq APIを叩いてテキストを生成する"""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7}
    
    for attempt in range(3):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=60)
            if res.status_code == 200:
                return res.json()["choices"][0]["message"]["content"]
            log.warning(f"Groq API エラー (Status {res.status_code}): {res.text}")
        except Exception as e:
            log.warning(f"Groq API 例外 (Attempt {attempt + 1}): {e}")
        import time
        time.sleep(3)
    
    raise RuntimeError("Groq APIの呼び出しに失敗しました。")

def generate_storyboard() -> dict:
    """絵コンテを生成する"""
    log.info("AI絵コンテを生成しています...")
    prompt = f"""
テーマ: {INPUT_TITLE} / ジャンル: {INPUT_GENRE} / 雰囲気: {INPUT_TONE}
このテーマで、視聴者を惹きつける10〜12シーンの絵コンテを作成してください。
【出力条件】
1. 各シーンの `text` は詳細に。
2. `visual_prompt` は具体的で詳細な英語の指示文。
3. `camera_motion` は以下から1つ選択: "zoom_in", "zoom_out", "pan_right", "pan_left"
4. 出力は純粋なJSON形式のみ。
{{
  "title": "タイトル",
  "description": "概要",
  "scenes": [
    {{
      "text": "ナレーション文...",
      "visual_prompt": "Detailed English prompt...",
      "camera_motion": "zoom_in"
    }}
  ]
}}
"""
    raw_res = call_groq(prompt)
    cleaned = raw_res.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        return json.loads(repair_json(cleaned))

async def create_narration_async(scenes: list):
    """音声を生成し、タイムラインを計算する"""
    audio_clips = []
    timeline = []
    current_time = 0.0
    temp_dir = WORK / "temp_audio"
    temp_dir.mkdir(parents=True, exist_ok=True)

    for idx, scene in enumerate(scenes):
        text = scene.get("text", "").strip()
        if not text: text = "解説を続けます。"
        temp_path = temp_dir / f"scene_{idx}.mp3"
        
        communicate = edge_tts.Communicate(text, EDGE_VOICE)
        await communicate.save(str(temp_path))
        
        clip = AudioFileClip(str(temp_path))
        duration = clip.duration
        timeline.append({"start": current_time, "end": current_time + duration})
        audio_clips.append(clip)
        current_time += duration

    final_audio = concatenate_audioclips(audio_clips)
    narration_path = WORK / "narration.mp3"
    final_audio.write_audiofile(str(narration_path), codec="libmp3lame", logger=None)
    return str(narration_path), timeline, current_time

def generate_ai_image(prompt: str, idx: int) -> str:
    """【重要】画像生成。エラー時はPicsumか単色で確実に返す"""
    full_prompt = f"{prompt}, {GLOBAL_STYLE}"
    encoded_prompt = requests.utils.quote(full_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1920&height=1080&nologo=true"
    img_path = WORK / f"scene_{idx}.jpg"
    
    try:
        res = requests.get(url, timeout=20)
        if res.status_code == 200 and "image" in res.headers.get("Content-Type", ""):
            img_path.write_bytes(res.content)
            return str(img_path)
        log.warning(f"シーン {idx}: AI画像生成失敗 ({res.status_code})。バックアップへ。")
    except Exception as e:
        log.warning(f"シーン {idx}: AI画像生成例外 ({e})。バックアップへ。")

    try:
        fallback_url = f"https://picsum.photos/1920/1080?random={idx}"
        res = requests.get(fallback_url, timeout=15)
        if res.status_code == 200:
            img_path.write_bytes(res.content)
            return str(img_path)
    except Exception:
        pass

    img = Image.new("RGB", (1920, 1080), color=(20, 30, 45))
    img.save(str(img_path), "JPEG")
    return str(img_path)

def apply_motion(clip, motion, duration):
    """カメラワークの適用"""
    if motion == "zoom_in":
        return clip.with_effects([vfx.Resize(lambda t: 1 + 0.05 * t)])
    elif motion == "zoom_out":
        return clip.with_effects([vfx.Resize(lambda t: 1.1 - 0.05 * t)])
    elif motion == "pan_right":
        clip = clip.resized((int(1920 * 1.2), 1080))
        return clip.with_position(lambda t: (-int(1920 * 0.2 * (t/duration)), "center"))
    elif motion == "pan_left":
        clip = clip.resized((int(1920 * 1.2), 1080))
        return clip.with_position(lambda t: (int(1920 * 0.2 * (t/duration)), "center"))
    return clip

def create_final_video(timeline: list, scenes: list, narration_path: str) -> str:
    log.info("最終レンダリングを開始します...")
    full_clips = []
    audio = AudioFileClip(narration_path)

    for idx, t in enumerate(timeline):
        duration = t["end"] - t["start"]
        scene = scenes[idx]
        log.info(f"シーン {idx} を処理中... ({duration:.1f}秒)")
        
        img_path = generate_ai_image(scene["visual_prompt"], idx)
        clip = ImageClip(img_path).with_duration(duration).resized((1920, 1080))
        
        motion = scene.get("camera_motion", "zoom_in")
        clip = apply_motion(clip, motion, duration)
        
        scene_final = CompositeVideoClip([clip], size=(1920, 1080)).with_duration(duration)
        full_clips.append(scene_final)

    final_video = concatenate_videoclips(full_clips, method="compose")
    final_video = final_video.with_audio(audio)
    final_video = final_video.with_duration(audio.duration) 

    output_path = WORK / "final.mp4"
    final_video.write_videofile(str(output_path), fps=24, codec="libx264", audio_codec="aac", preset="fast", logger=None)
    return str(output_path)

def main():
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        log.info("=== AI絵コンテ・モーションビデオ生成システム起動 ===")
        storyboard = generate_storyboard()
        narration_path, timeline, total_sec = asyncio.run(create_narration_async(storyboard["scenes"]))
        final_path = create_final_video(timeline, storyboard["scenes"], narration_path)
        log.info(f"動画生成完了: {final_path}")
    except Exception as e:
        log.error(f"致命的なエラーが発生しました: {e}")
        log.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
