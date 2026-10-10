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
    concatenate_videoclips,
    vfx
)

# --- ログ・環境設定 ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("YouTubeAuto")

WORK = Path(os.getenv("WORK_DIR", "work"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
INPUT_TITLE = os.getenv("INPUT_VIDEO_TITLE", "AIの進化と未来")
INPUT_GENRE = os.getenv("INPUT_VIDEO_GENRE", "自動判定")
INPUT_TONE = os.getenv("INPUT_VIDEO_TONE", "わかりやすく、最後まで見たくなる雰囲気")
EDGE_VOICE = os.getenv("EDGE_VOICE", "ja-JP-NanamiNeural")

# スタイルの統一（ここを書き換えると動画全体の雰囲気が変わります）
GLOBAL_STYLE = "High-quality cinematic anime style, Makoto Shinkai inspired, detailed background, vivid colors, 8k, masterpiece"

def call_groq(prompt: str) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7}
    res = requests.post(url, headers=headers, json=payload, timeout=60)
    return res.json()["choices"][0]["message"]["content"]

def generate_storyboard():
    """絵コンテ（ナレーション＋視覚指示＋カメラワーク）を生成する"""
    log.info("AI絵コンテを生成中...")
    prompt = f"""
テーマ: {INPUT_TITLE} / ジャンル: {INPUT_GENRE} / 雰囲気: {INPUT_TONE}
このテーマで、視聴者を惹きつける10〜12シーンの詳細な絵コンテを作成してください。

【出力条件】
1. 各シーンの `text` は詳細に（1シーン250文字以上）。
2. `visual_prompt` は、そのシーンを完璧に表現する具体的で詳細な英語の指示文。
3. `camera_motion` は以下から1つ選択: "zoom_in", "zoom_out", "pan_right", "pan_left"
4. 出力は純粋なJSON形式のみ。

{{
  "title": "惹きつけるタイトル",
  "description": "概要欄",
  "scenes": [
    {{
      "text": "ナレーション文...",
      "visual_prompt": "Detailed English prompt for AI image generation...",
      "camera_motion": "zoom_in"
    }}
  ]
}}
"""
    raw = call_groq(prompt)
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(repair_json(cleaned))

async def create_narration_async(scenes: list):
    """音声を生成し、各シーンの正確な開始・終了時間を計算する"""
    audio_clips = []
    timeline = []
    current_time = 0.0
    temp_dir = WORK / "temp_audio"
    temp_dir.mkdir(parents=True, exist_ok=True)

    for idx, scene in enumerate(scenes):
        text = scene.get("text", "").strip()
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

def generate_ai_image(prompt: str, idx: int):
    """Pollinations AIを使用して、スタイル統一された画像を生成する"""
    # スタイルを強制的に結合
    full_prompt = f"{prompt}, {GLOBAL_STYLE}"
    encoded_prompt = requests.utils.quote(full_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1920&height=1080&nologo=true"
    
    img_path = WORK / f"scene_{idx}.jpg"
    try:
        res = requests.get(url, timeout=30)
        img_path.write_bytes(res.content)
        return str(img_path)
    except Exception as e:
        log.error(f"画像生成失敗: {e}")
        # フォールバック：単色画像
        img = Image.new("RGB", (1920, 1080), color=(20, 30, 45))
        img.save(str(img_path))
        return str(img_path)

def apply_motion(clip, motion, duration):
    """静止画に仮想カメラワークを適用する"""
    # 1. ズーム系
    if motion == "zoom_in":
        return clip.with_effects([vfx.Resize(lambda t: 1 + 0.1 * t)]) # 徐々に拡大
    elif motion == "zoom_out":
        return clip.with_effects([vfx.Resize(lambda t: 1.2 - 0.1 * t)]) # 徐々に縮小
    
    # 2. パン系 (簡易的にリサイズして位置をずらす)
    elif motion == "pan_right":
        # 1.2倍に拡大して、左から右へずらす
        return clip.with_effects([vfx.Resize(1.2), vfx.Scroll(direction="right", speed=50)]) 
    elif motion == "pan_left":
        return clip.with_effects([vfx.Resize(1.2), vfx.Scroll(direction="left", speed=50)])
    
    return clip

def create_final_video(timeline: list, scenes: list, narration_path: str):
    """絵コンテに基づき、モーションを適用して映像を合成する"""
    full_clips = []
    audio = AudioFileClip(narration_path)

    for idx, t in enumerate(timeline):
        duration = t["end"] - t["start"]
        scene = scenes[idx]
        
        # 1. 画像生成
        img_path = generate_ai_image(scene["visual_prompt"], idx)
        
        # 2. クリップ作成
        clip = ImageClip(img_path).with_duration(duration).resized((1920, 1080))
        
        # 3. カメラワーク適用
        motion = scene.get("camera_motion", "zoom_in")
        clip = apply_motion(clip, motion, duration)
        
        full_clips.append(clip)

    # 全シーンを結合
    final_video = concatenate_videoclips(full_clips, method="compose")
    
    # 【最重要】音声の長さに完全に合わせる（途中で終わる問題を解決）
    final_video = final_video.with_audio(audio)
    final_video = final_video.with_duration(audio.duration) 

    output_path = WORK / "final.mp4"
    final_video.write_videofile(str(output_path), fps=24, codec="libx264", audio_codec="aac", preset="fast", logger=None)
    return str(output_path)

def main():
    WORK.mkdir(parents=True, exist_ok=True)
    log.info("AI絵コンテ・モーションビデオ生成を開始します")

    # 1. 絵コンテ生成
    storyboard = generate_storyboard()
    
    # 2. 音声生成とタイムライン確定
    narration_path, timeline, total_sec = asyncio.run(create_narration_async(storyboard["scenes"]))
    log.info(f"合計尺: {total_sec:.1f}秒")

    # 3. 映像合成 (画像生成含む)
    final_path = create_final_video(timeline, storyboard["scenes"], narration_path)
    log.info(f"完成しました: {final_path}")

if __name__ == "__main__":
    main()
