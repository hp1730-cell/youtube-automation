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
from PIL import Image
from moviepy import (
    AudioFileClip,
    ImageClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips,
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

def call_groq(prompt: str) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7}
    res = requests.post(url, headers=headers, json=payload, timeout=60)
    return res.json()["choices"][0]["message"]["content"]

def generate_script(theme: str) -> dict:
    log.info("台本を生成中...")
    prompt = f"""
テーマ: {theme} / ジャンル: {INPUT_GENRE} / 雰囲気: {INPUT_TONE}
YouTube用の詳細な台本をJSONで作成してください。
【条件】
- 10〜12シーン構成。
- 各シーンの `text` は250〜300文字程度で詳しく。
- 出力は純粋なJSONのみ。
{{
  "title": "タイトル",
  "description": "概要",
  "scenes": [
    {{ "text": "ナレーション...", "camera_motion": "zoom_in" }}
  ]
}}
"""
    raw = call_groq(prompt)
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(repair_json(cleaned))

async def create_narration_async(scenes: list):
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

def apply_motion(clip, motion, duration):
    """高品質なカメラワークの適用"""
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
    log.info("動画を合成中...")
    full_clips = []
    audio = AudioFileClip(narration_path)
    asset_dir = WORK / "assets"
    
    # 準備された素材リストを取得
    available_assets = sorted(list(asset_dir.glob("asset_*.jpg")))

    for idx, t in enumerate(timeline):
        duration = t["end"] - t["start"]
        scene = scenes[idx]
        
        # 【重要】用意された画像リストから順番に使う（足りなければループ）
        asset_index = idx % len(available_assets)
        img_path = available_assets[asset_index]
        
        log.info(f"シーン {idx} を合成中... (画像: {img_path.name})")
        
        clip = ImageClip(str(img_path)).with_duration(duration).resized((1920, 1080))
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
        log.info("=== 高品質動画生成パイプライン起動 ===")
        
        # 1. 台本作成
        script_data = generate_script(INPUT_TITLE)
        
        # 2. 音声生成
        narration_path, timeline, total_sec = asyncio.run(create_narration_async(script_data["scenes"]))
        
        # 3. 映像合成 (用意された画像を使用)
        final_path = create_final_video(timeline, script_dat
