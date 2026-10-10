import os
import sys
import json
import asyncio
import logging
import requests
import traceback
import time
import random
from pathlib import Path
from datetime import datetime

import edge_tts
from json_repair import repair_json
from PIL import Image
from moviepy import (
    AudioFileClip,
    ImageClip,
    TextClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips,
    vfx
)

# --- 設定 (既存のものを継承) ---
WORK = Path(os.getenv("WORK_DIR", "work"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
EDGE_VOICE = os.getenv("EDGE_VOICE", "ja-JP-NanamiNeural")
# 画像のクオリティを固定するためのスタイルアンカー
STYLE_ANCHOR = "High-end cinematic anime style, highly detailed digital illustration, masterpiece, 8k, vivid colors, expressive character emotions"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("YouTubeAuto")

# --- 【フェーズ2】アセットマネージャー・クラス ---

class AssetManager:
    """音声、画像、超解像、ストックフォトを統合管理するエンジン"""
    
    def __init__(self, storyboard: dict):
        self.storyboard = storyboard
        self.temp_dir = WORK / "temp_assets"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir = WORK / "temp_audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. 音声生成 ---
    async def generate_all_audio(self):
        log.info("【音声エンジン】ナレーション生成中...")
        timeline = []
        current_time = 0.0
        audio_clips = []

        for idx, chapter in enumerate(self.storyboard["chapters"]):
            for s_idx, scene in enumerate(chapter["scenes"]):
                text = scene["narration"]
                temp_path = self.audio_dir / f"scene_{idx}_{s_idx}.mp3"
                
                communicate = edge_tts.Communicate(text, EDGE_VOICE)
                await communicate.save(str(temp_path))
                
                clip = AudioFileClip(str(temp_path))
                duration = clip.duration
                
                timeline.append({
                    "start": current_time,
                    "end": current_time + duration,
                    "duration": duration,
                    "narration": text,
                    "visual_prompts": scene["visual_prompts"],
                    "search_keywords": scene["search_keywords"],
                    "camera_motion": scene["camera_motion"]
                })
                audio_clips.append(clip)
                current_time += duration

        final_audio_path = WORK / "master_narration.mp3"
        final_audio = concatenate_audioclips(audio_clips)
        final_audio.write_audiofile(str(final_audio_path), codec="libmp3lame", logger=None)
        
        return str(final_audio_path), timeline

    # --- 2. 画像生成 & 超解像 ---
    async def generate_all_images(self, timeline: list):
        log.info("【画像エンジン】画像生成 ＋ 超解像プロセス開始...")
        
        for idx, t in enumerate(timeline):
            # 1つのシーンに対して3枚の画像を生成
            prompts = t["visual_prompts"]
            keywords = t["search_keywords"]
            
            for p_idx, prompt in enumerate(prompts):
                img_path = await self._get_single_image(prompt, keywords[p_idx % len(keywords)], idx, p_idx)
                t[f"img_{p_idx}"] = img_path

    async def _get_single_image(self, prompt: str, keyword: str, scene_idx: int, img_idx: int) -> str:
        """画像取得のハイブリッド・フロー (AI -> 超解像 -> Pixabay)"""
        target_path = self.temp_dir / f"scene_{scene_idx}_img_{img_idx}.jpg"
        
        # 1. AI生成の試行
        full_prompt = f"{prompt}, {STYLE_ANCHOR}"
        encoded_prompt = requests.utils.quote(full_prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1920&height=1080&nologo=true"
        
        for attempt in range(2):
            try:
                res = requests.get(url, timeout=20)
                if res.status_code == 200:
                    target_path.write_bytes(res.content)
                    # 【超解像の枠組み】
                    # 本来はここで Real-ESRGAN 等を呼び出す
                    # target_path = self._apply_upscale(target_path)
                    return str(target_path)
                elif res.status_code == 429:
                    await asyncio.sleep(30)
            except:
                await asyncio.sleep(5)

        # 2. Pixabay へのフォールバック
        log.warning(f"AI生成失敗。Pixabayで '{keyword}' を検索中...")
        pixabay_path = self._get_pixabay_image(keyword, scene_idx, img_idx)
        if pixabay_path:
            return pixabay_path

        # 3. 最終手段（単色）
        img = Image.new("RGB", (1920, 1080), color=(30, 30, 30))
        img.save(str(target_path), "JPEG")
        return str(target_path)

    def _get_pixabay_image(self, keyword: str, s_idx: int, i_idx: int) -> str:
        if not PIXABAY_API_KEY: return None
        url = f"https://pixabay.com/api/?key={PIXABAY_API_KEY}&q={requests.utils.quote(keyword)}&image_type=photo&per_page=3"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200 and res.json()["hits"]:
                img_url = res.json()["hits"][0]["webformatURL"]
                img_data = requests.get(img_url, timeout=10).content
                path = self.temp_dir / f"pix_{s_idx}_{i_idx}.jpg"
                path.write_bytes(img_data)
                return str(path)
        except:
            pass
        return None

    # --- 3. アニメーション合成エンジン ---
    def assemble_video(self, timeline: list, narration_path: str) -> str:
        log.info("【合成エンジン】最終的な動画のレンダリングを開始します...")
        scene_clips = []
        audio = AudioFileClip(narration_path)

        for idx, t in enumerate(timeline):
            duration = t["duration"]
            # 1シーン内の3枚の画像を分割してクロスフェードさせる
            sub_duration = duration / 3
            sub_clips = []
            
            for i in range(3):
                img_path = t.get(f"img_{i}")
                if not img_path: continue
                
                clip = (ImageClip(img_path)
                        .with_duration(sub_duration)
                        .resized((1920, 1080)))
                
                # カメラモーション適用
                clip = self._apply_motion(clip, t["camera_motion"], sub_duration)
                
                # クロスフェード（重なり）
                if i > 0:
                    clip = clip.with_effects([vfx.CrossFadeIn(0.5)])
                sub_clips.append(clip)

            if not sub_clips: continue
            
            # シーン内の小クリップを結合
            combined_scene = concatenate_videoclips(sub_clips, method="compose")
            
            # 字幕の追加
            try:
                txt = (TextClip(text=t["narration"], font="Arial-Bold", font_size=40, 
                                color='white', stroke_color='black', stroke_width=1.5,
                                method='caption', size=(1600, None))
                        .with_duration(duration)
                        .with_position(('center', 850)))
                scene_final = CompositeVideoClip([combined_scene, txt], size=(1920, 1080))
            except:
                scene_final = combined_scene

            scene_clips.append(scene_final)

        final_video = concatenate_videoclips(scene_clips, method="compose")
        final_video = final_video.with_audio(audio)
        
        output_path = WORK / "final.mp4"
        final_video.write_videofile(str(output_path), fps=24, codec="libx264", audio_codec="aac", preset="medium")
        return str(output_path)

    def _apply_motion(self, clip, motion, duration):
        if motion == "zoom_in":
            return clip.with_effects([vfx.Resize(lambda t: 1 + 0.05 * (t/duration))])
        elif motion == "zoom_out":
            return clip.with_effects([vfx.Resize(lambda t: 1.1 - 0.05 * (t/duration))])
        elif motion == "pan_right":
            clip = clip.resized((int(1920 * 1.2), 1080))
            return clip.with_position(lambda t: (-int(1920 * 0.2 * (t/duration)), "center"))
        elif motion == "pan_left":
            clip = clip.resized((int(1920 * 1.2), 1080))
            return clip.with_position(lambda t: (int(1920 * 0.2 * (t/duration)), "center"))
        return clip

# --- メイン・パイプライン ---

async def main():
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        log.info("=== Cinematic Motion Engine: 起動 ===")
        
        # 1. 設計図の読み込み
        storyboard_path = WORK / "master_storyboard.json"
        if not storyboard_path.exists():
            raise RuntimeError("master_storyboard.json が見つかりません。先にフェーズ1を実行してください。")
        
        with open(storyboard_path, "r", encoding="utf-8") as f:
            storyboard = json.load(f)

        # 2. アセットマネージャーの初期化
        manager = AssetManager(storyboard)

        # 3. 音声生成
        narration_path, timeline = await manager.generate_all_audio()

        # 4. 画像生成
        await manager.generate_all_images(timeline)

        # 5. 動画合成
        final_path = manager.assemble_video(timeline, narration_path)

        log.info(f"=== 全工程完了! 動画出力: {final_path} ===")

    except Exception as e:
        log.error(f"致命的なエラー: {e}")
        log.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
