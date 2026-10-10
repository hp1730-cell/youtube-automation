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
    res = requests.post(url, headers=headers, json=payload, timeout=60)
    return res.json()["choices"][0]["message"]["content"]

def generate_storyboard():
    log.info("AI絵コンテを生成しています...")
    prompt = f"""
テーマ: {INPUT_TITLE} / ジャンル: {INPUT_GENRE} / 雰囲気: {INPUT_TONE}
このテーマで、視聴者を惹きつける10〜12シーンの詳細な絵コンテを作成してください。
【出力条件】
1. 各シーンの `text` は詳細に。
2. `visual_prompt` は具体的で詳細な英語の指示文。
3. `camera_motion` は以下から1つ選択: "zoom_in", "zoom_out", "pan_right", "pan_left"
4. 出力は純粋なJSONのみ。
{{
  "title": "タイトル",
  "description": "概要",
  "scenes": [
    {{ "text": "...", "visual_prompt": "...", "camera_motion": "zoom_in" }}
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

def generate_ai_image(prompt: str, idx: int):
    """
    【完全無料・支払い要求回避版】
    APIが有料プランを要求(402)したりエラーを出したりした場合、
    即座に無料の代替手段に切り替えて、絶対にプログラムを止めない。
    """
    full_prompt = f"{prompt}, {GLOBAL_STYLE}"
    encoded_prompt = requests.utils.quote(full_prompt)
    # メインのAI画像生成URL
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1920&height=1080&nologo=true"
    img_path = WORK / f"scene_{idx}.jpg"
    
    try:
        res = requests.get(url, timeout=20)
        # 200 OK かつ、中身が画像である場合のみ保存
        if res.status_code == 200 and "image" in res.headers.get("Content-Type", ""):
            img_path.write_bytes(res.content)
            return str(img_path)
        else:
            log.warning(f"シーン {idx}: AI画像生成が利用不可 (Status: {res.status_code})。無料バックアップに切り替えます。")
    except Exception as e:
        log.warning(f"シーン {idx}: 通信エラー {e}。無料バックアップに切り替えます。")
    
    # --- 【完全無料バックアップ】 ---
    # Pollinationsがダメな場合、完全に無料の画像サービス(Picsum)からランダムな風景を持ってくる
    try:
        # 1920x1080のランダム画像を取得
        fallback_url = f"https://picsum.photos/1920/1080?random={idx}"
        fallback_res = requests.get(fallback_url, timeout=20)
        if fallback_res.status_code == 200:
            img_path.write_bytes(fallback_res.content)
            return str(img_path)
    except Exception as e:
        log.error(f"バックアップ画像取得失敗: {e}")

    # --- 【最終手段】 ---
    # 全てのAPIがダメな場合は、ライブラリで単色画像を生成（100%成功する）
    log.warning(f"シーン {idx}: 最終フォールバック（単色画像）を使用します。")
    img = Image.new("RGB", (1920, 1080), color=(20, 30, 45))
    img.save(str(img_path), "JPEG")
    return str(img_path)
    except Exception as e:
        log.error(f"画像生成例外 {idx}: {e}")
    
    # 最終フォールバック
    log.warning(f"シーン {idx} はフォールバック画像を使用します。")
    img = Image.new("RGB", (1920, 1080), color=(20, 30, 45))
    img.save(str(img_path), "JPEG")
    return str(img_path)

def create_final_video(timeline: list, scenes: list, narration_path: str):
    log.info("最終レンダリングを開始します...")
    full_clips = []
    audio = AudioFileClip(narration_path)

    for idx, t in enumerate(timeline):
        duration = t["end"] - t["start"]
        scene = scenes[idx]
        log.info(f"シーン {idx} を処理中... ({duration:.1f}秒)")
        
        img_path = generate_ai_image(scene["visual_prompt"], idx)
        # 画像クリップを作成
        clip = ImageClip(img_path).with_duration(duration).resized((1920, 1080))
        
        motion = scene.get("camera_motion", "zoom_in")
        
        # --- エラーの出ない安全なモーション実装 ---
        if motion == "zoom_in":
            clip = clip.with_effects([vfx.Resize(lambda t: 1 + 0.05 * t)])
        elif motion == "zoom_out":
            clip = clip.with_effects([vfx.Resize(lambda t: 1.1 - 0.05 * t)])
        elif motion == "pan_right":
            # 1.2倍にして右へゆっくりずらす（CompositeVideoClip内で処理）
            clip = clip.resized((int(1920 * 1.2), 1080))
            clip = clip.with_position(lambda t: (-int(1920 * 0.2 * (t/duration)), "center"))
        elif motion == "pan_left":
            clip = clip.resized((int(1920 * 1.2), 1080))
            clip = clip.with_position(lambda t: (int(1920 * 0.2 * (t/duration)), "center"))
        else:
            pass # そのまま
            
        # パンニングを有効にするために CompositeVideoClip で包む
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
        log.error("【致命的なエラー】")
        log.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
