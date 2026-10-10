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
    concatenate_videoclips,
    vfx,
    concatenate_audioclips
)

# ログ設定：エラーを詳細に出す
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
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
    log.info("Groq APIにリクエスト送信中...")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7}
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=90)
        res.raise_for_status()
        content = res.json()["choices"][0]["message"]["content"]
        log.info("APIから応答を受信しました。")
        return content
    except Exception as e:
        log.error(f"APIリクエスト失敗: {e}")
        raise e

def generate_storyboard():
    log.info("AI絵コンテを生成しています...")
    prompt = f"""
テーマ: {INPUT_TITLE} / ジャンル: {INPUT_GENRE} / 雰囲気: {INPUT_TONE}
このテーマで、視聴者を惹きつける10〜12シーンの詳細な絵コンテを作成してください。

【出力条件】
1. 各シーンの `text` は詳細に（1シーン250文字以上）。
2. `visual_prompt` は、そのシーンを完璧に表現する具体的で詳細な英語の指示文。
3. `camera_motion` は以下から1つ選択: "zoom_in", "zoom_out", "pan_right", "pan_left"
4. 出力は純粋なJSON形式のみ。マークダウン（```jsonなど）は含めないでください。

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
    try:
        raw = call_groq(prompt)
        log.info("--- [RAW RESPONSE START] ---")
        log.info(raw)
        log.info("--- [RAW RESPONSE END] ---")
        
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        repaired = repair_json(cleaned)
        log.info(f"JSON修復後: {repaired[:100]}...")
        return json.loads(repaired)
    except Exception as e:
        log.error(f"絵コンテ生成中の致命的なエラー: {e}")
        log.error(traceback.format_exc())
        raise e

async def create_narration_async(scenes: list):
    log.info("音声生成を開始します...")
    audio_clips = []
    timeline = []
    current_time = 0.0
    temp_dir = WORK / "temp_audio"
    temp_dir.mkdir(parents=True, exist_ok=True)

    for idx, scene in enumerate(scenes):
        text = scene.get("text", "").strip()
        if not text: text = "解説を続けます。"
        temp_path = temp_dir / f"scene_{idx}.mp3"
        
        log.info(f"シーン {idx} の音声を生成中...")
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
    log.info(f"全音声の結合が完了しました。合計尺: {current_time:.1f}秒")
    return str(narration_path), timeline, current_time

def generate_ai_image(prompt: str, idx: int):
    full_prompt = f"{prompt}, {GLOBAL_STYLE}"
    encoded_prompt = requests.utils.quote(full_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1920&height=1080&nologo=true"
    
    img_path = WORK / f"scene_{idx}.jpg"
    try:
        res = requests.get(url, timeout=30)
        img_path.write_bytes(res.content)
        return str(img_path)
    except Exception as e:
        log.warning(f"画像生成失敗 {idx}: {e}")
        img = Image.new("RGB", (1920, 1080), color=(20, 30, 45))
        img.save(str(img_path))
        return str(img_path)

def apply_motion(clip, motion, duration):
    if motion == "zoom_in":
        return clip.with_effects([vfx.Resize(lambda t: 1 + 0.1 * t)])
    elif motion == "zoom_out":
        return clip.with_effects([vfx.Resize(lambda t: 1.2 - 0.1 * t)])
    elif motion == "pan_right":
        return clip.with_effects([vfx.Resize(1.2), vfx.Scroll(direction="right", speed=50)])
    elif motion == "pan_left":
        return clip.with_effects([vfx.Resize(1.2), vfx.Scroll(direction="left", speed=50)])
    return clip

def create_final_video(timeline: list, scenes: list, narration_path: str):
    log.info("最終レンダリングを開始します...")
    full_clips = []
    audio = AudioFileClip(narration_path)

    for idx, t in enumerate(timeline):
        duration = t["end"] - t["start"]
        scene = scenes[idx]
        log.info(f"シーン {idx} をレンダリング中... ({duration:.1f}秒)")
        
        img_path = generate_ai_image(scene["visual_prompt"], idx)
        clip = ImageClip(img_path).with_duration(duration).resized((1920, 1080))
        motion = scene.get("camera_motion", "zoom_in")
        clip = apply_motion(clip, motion, duration)
        full_clips.append(clip)

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
        log.info("絵コンテの生成に成功しました。")
        
        narration_path, timeline, total_sec = asyncio.run(create_narration_async(storyboard["scenes"]))
        log.info(f"音声生成完了。合計秒数: {total_sec}")

        final_path = create_final_video(timeline, storyboard["scenes"], narration_path)
        log.info(f"動画生成完了: {final_path}")

    except Exception as e:
        log.error("【致命的なエラー】プログラムが異常終了しました")
        log.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
