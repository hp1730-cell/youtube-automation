import os
import sys
import json
import asyncio
import logging
import requests
import traceback
import time  # 待ち時間用
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
GLOBAL_STYLE = "High-quality cinematic anime style, Makoto Shinkai inspired, detailed background, vivid colors, 8k, masterpiece"

def call_groq(prompt: str) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7}
    for attempt in range(3):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=60)
            if res.status_code == 200:
                return res.json()["choices"][0]["message"]["content"]
            log.warning(f"Groq API エラー ({res.status_code})")
        except Exception as e:
            log.warning(f"Groq API 例外 ({attempt+1}): {e}")
        time.sleep(3)
    raise RuntimeError("Groq APIの呼び出しに失敗しました。")

def generate_storyboard() -> dict:
    log.info("AI絵コンテを生成中...")
    prompt = f"""
テーマ: {INPUT_TITLE} / ジャンル: {INPUT_GENRE} / 雰囲気: {INPUT_TONE}
このテーマに基づき、10〜12シーンのYouTube動画用絵コンテをJSON形式で作成してください。
【条件】
- 各シーンの `text` は詳細に。
- `visual_prompt` は、そのシーンの内容を完璧に表す、具体的で詳細な英語の指示文。
- `camera_motion` は "zoom_in", "zoom_out", "pan_right", "pan_left" から選択。
- 出力は純粋なJSONのみ。
{{
  "title": "タイトル",
  "description": "概要",
  "scenes": [
    {{ "text": "...", "visual_prompt": "...", "camera_motion": "zoom_in" }}
  ]
}}
"""
    raw_res = call_groq(prompt)
    cleaned = raw_res.replace("```json", "").replace("```", "").strip()
    return json.loads(repair_json(cleaned))

async def create_narration_async(scenes: list):
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
    """【鉄壁の画像生成】API制限を回避し、テーマに沿った画像を確実に確保する"""
    full_prompt = f"{prompt}, {GLOBAL_STYLE}"
    encoded_prompt = requests.utils.quote(full_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1920&height=1080&nologo=true"
    img_path = WORK / f"scene_{idx}.jpg"
    
    for attempt in range(3):  # 最大3回リトライ
        try:
            log.info(f"シーン {idx}: AI画像生成中 (試行 {attempt+1}/3)...")
            res = requests.get(url, timeout=20)
            
            if res.status_code == 200 and "image" in res.headers.get("Content-Type", ""):
                img_path.write_bytes(res.content)
                log.info(f"シーン {idx}: AI画像生成に成功しました。")
                time.sleep(5)  # APIのレートリミットを避けるための待機
                return str(img_path)
            else:
                log.warning(f"シーン {idx}: API拒否 (Status {res.status_code})。リトライします。")
        except Exception as e:
            log.warning(f"シーン {idx}: 通信エラー {e}")
        
        time.sleep(5) # 失敗時は少し待つ

    # --- 【バックアップ策】AIがダメな場合 ---
    log.warning(f"シーン {idx}: AI生成失敗。Picsum(風景写真)を使用します。")
    try:
        fallback_url = f"https://picsum.photos/1920/1080?random={idx}"
        res = requests.get(fallback_url, timeout=15)
        if res.status_code == 200:
            img_path.write_bytes(res.content)
            return str(img_path)
    except:
        pass

    log.warning(f"シーン {idx}: 最終手段として単色画像を使用します。")
    img = Image.new("RGB", (1920, 1080), color=(20, 30, 45))
    img.save(str(img_path), "JPEG")
    return str(img_path)

def apply_motion(clip, motion, duration):
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
    log.info("動画合成を開始します...")
    full_clips = []
    audio = AudioFileClip(narration_path)

    for idx, t in enumerate(timeline):
        duration = t["end"] - t["start"]
        scene = scenes[idx]
        
        # そのシーン専用の画像を生成（テーマが一致する）
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
        
        # 1. 台本生成
        storyboard = generate_storyboard()
        
        # 2. 音声生成
        narration_path, timeline, total_sec = asyncio.run(create_narration_async(storyboard["scenes"]))
        
        # 3. 画像生成 ＋ 動画合成（一気通貫）
        final_path = create_final_video(timeline, storyboard["scenes"], narration_path)
        
        log.info(f"動画生成完了: {final_path}")
    except Exception as e:
        log.error(f"致命的なエラーが発生しました: {e}")
        log.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
