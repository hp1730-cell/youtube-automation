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

# --- 設定 ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("YouTubeAuto")

WORK = Path(os.getenv("WORK_DIR", "work"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
INPUT_TITLE = os.getenv("INPUT_VIDEO_TITLE", "AIの進化と未来")
INPUT_GENRE = osos.getenv("INPUT_VIDEO_GENRE", "自動判定")
INPUT_TONE = os.getenv("INPUT_VIDEO_TONE", "わかりやすく、最後まで見たくなる雰囲気")
EDGE_VOICE = os.getenv("EDGE_VOICE", "ja-JP-NanamiNeural")
STYLE_ANCHOR = "High-end cinematic anime style, highly detailed digital illustration, masterpiece, 8k, vivid colors, expressive character emotions"
MASTER_STORYBOARD_PATH = WORK / "master_storyboard.json"

MODELS_TO_TRY = [
    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-1.5-pro", "gemini-1.5-flash", "gemini-pro"
]

# --- 共通ユーティリティ ---

def call_gemini(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が設定されていません。")

    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)

    available_model_names = []
    try:
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                available_model_names.append(m.name)
    except:
        available_model_names = [f"models/{m}" for m in MODELS_TO_TRY]

    for model_id in available_model_names:
        log.info(f"モデル '{model_id}' で試行中...")
        try:
            model = genai.GenerativeModel(model_id)
            response = model.generate_content(prompt)
            if response.text:
                return response.text
        except Exception as e:
            err = str(e).lower()
            if "404" in err or "not found" in err:
                continue
            elif "429" in err or "too many requests" in err:
                wait = random.uniform(20, 40)
                log.warning(f"制限検知。{wait:.1f}秒待機...")
                time.sleep(wait)
                continue
            else:
                log.error(f"エラー: {e}")
                time.sleep(5)
                continue
    raise RuntimeError("すべてのモデルで失敗しました。")

def safe_parse_json(raw_res: str, expected_key: str = None) -> dict:
    """
    AIの出力を解析し、構造を強制的に正規化する。
    AIがリストを返した場合でも、expected_key を使って辞書にラップする。
    """
    try:
        cleaned = raw_res.replace("```json", "").replace("```", "").strip()
        data = json.loads(repair_json(cleaned))
        
        # 【重要】AIがリストを直接返してきた場合 (例: [{}, {}])
        if isinstance(data, list):
            log.info(f"AIがリスト形式で応答しました。'{expected_key}' キーでラップします。")
            return {expected_key: data} if expected_key else {"items": data}
            
        # AIが辞書を返したが、期待するキーがない場合
        if isinstance(data, dict) and expected_key and expected_key not in data:
            log.warning(f"期待するキー '{expected_key}' が辞書内に見つかりません。全体をラップします。")
            return {expected_key: data}
            
        return data
    except Exception as e:
        log.error(f"JSON解析失敗: {e} | Raw: {raw_res[:100]}")
        raise

def get_field_flexible(data: dict, potential_keys: list, default_val: str = "") -> str:
    if not isinstance(data, dict): return default_val
    for key in potential_keys:
        if key in data: return data[key]
    return default_val

# --- 【フェーズ1】ストーリーボード生成エンジン ---

def run_phase_1() -> dict:
    log.info("=== [Phase 1] 階層型ストーリーボード生成開始 ===")
    
    # 1. Outline
    prompt_outline = f"Theme: {INPUT_TITLE}/Genre: {INPUT_GENRE}/Tone: {INPUT_TONE}. Create an 8-min YouTube outline. Output ONLY JSON: {{ 'chapters': [ {{ 'title': '...', 'duration': 60, 'goal': '...' }} ] }}"
    raw_outline = call_gemini(prompt_outline)
    
    # ここで確実に chapters リストを取得する
    outline_data = safe_parse_json(raw_outline, "chapters")
    chapters = outline_data.get("chapters", [])
    
    if not chapters:
        raise RuntimeError("構成案（chapters）の取得に失敗しました。AIの応答が不正です。")

    # 2. Details
    master_storyboard = {"video_title": INPUT_TITLE, "chapters": []}
    for chapter in chapters:
        # chapter が dict であることを保証
        if not isinstance(chapter, dict):
            log.warning(f"章のデータが辞書ではありません: {chapter}. スキップします。")
            continue

        chapter_title = get_field_flexible(chapter, ["title", "chapter_title", "name"], "Unknown Chapter")
        log.info(f"章 '{chapter_title}' の詳細化中...")
        
        prompt_detail = f"Chapter: {chapter_title}/Goal: {chapter.get('goal', '')}. Break into 10-15 scenes. Output ONLY JSON: {{ 'scenes': [ {{ 'narration': '...', 'visual_prompts': ['...', '...', '...'], 'search_keywords': ['...'], 'camera_motion': 'zoom_in' }} ] }}"
        raw_detail = call_gemini(prompt_detail)
        
        scene_data = safe_parse_json(raw_detail, "scenes")
        scenes = scene_data.get("scenes", [])

        chapter_data = {
            "chapter_title": chapter_title,
            "chapter_duration": int(get_field_flexible(chapter, ["duration", "seconds"], 60)),
            "chapter_goal": get_field_flexible(chapter, ["goal", "objective"], ""),
            "scenes": scenes
        }
        master_storyboard["chapters"].append(chapter_data)
        time.sleep(random.uniform(10, 20))

    with open(MASTER_STORYBOARD_PATH, "w", encoding="utf-8") as f:
        json.dump(master_storyboard, f, indent=2, ensure_ascii=False)
    
    log.info(f"=== [Phase 1] 完了! 設計図を保存しました: {MASTER_STORYBOARD_PATH} ===")
    return master_storyboard

# --- 【フェーズ2】アセット生成 & アニメーションエンジン ---

class AssetManager:
    def __init__(self, storyboard: dict):
        self.storyboard = storyboard
        self.temp_dir = WORK / "temp_assets"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir = WORK / "temp_audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    async def generate_all_audio(self):
        log.info("【音声】ナレーション生成中...")
        timeline = []
        current_time = 0.0
        audio_clips = []
        for idx, chap in enumerate(self.storyboard["chapters"]):
            for s_idx, scene in enumerate(chap["scenes"]):
                text = scene.get("narration", "...")
                path = self.audio_dir / f"s_{idx}_{s_idx}.mp3"
                await edge_tts.Communicate(text, EDGE_VOICE).save(str(path))
                clip = AudioFileClip(str(path))
                duration = clip.duration
                timeline.append({
                    "start": current_time, "end": current_time + duration,
                    "duration": duration, "narration": text,
                    "visual_prompts": scene.get("visual_prompts", []),
                    "search_keywords": scene.get("search_keywords", []),
                    "camera_motion": scene.get("camera_motion", "zoom_in")
                })
                audio_clips.append(clip)
                current_time += duration
        final_audio_path = WORK / "master_narration.mp3"
        final_audio = concatenate_audioclips(audio_clips)
        final_audio.write_audiofile(str(final_audio_path), codec="libmp3lame", logger=None)
        return str(final_audio_path), timeline

    async def generate_all_images(self, timeline: list):
        log.info("【画像】高解像度生成 ＋ ハイブリッド補完中...")
        for idx, t in enumerate(timeline):
            prompts = t["visual_prompts"]
            keywords = t["search_keywords"]
            for p_idx, prompt in enumerate(prompts):
                img_path = await self._get_image(prompt, keywords[p_idx % len(keywords)], idx, p_idx)
                t[f"img_{p_idx}"] = img_path

    async def _get_image(self, prompt: str, kw: str, s_idx: int, i_idx: int) -> str:
        target = self.temp_dir / f"s_{s_idx}_i_{i_idx}.jpg"
        full_p = f"{prompt}, {STYLE_ANCHOR}"
        url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(full_p)}?width=1920&height=1080&nologo=true"
        for _ in range(2):
            try:
                res = requests.get(url, timeout=20)
                if res.status_code == 200:
                    target.write_bytes(res.content)
                    return str(target)
            except: pass
            await asyncio.sleep(5)
        if PIXABAY_API_KEY:
            try:
                res: res = requests.get(f"https://pixabay.com/api/?key={PIXABAY_API_KEY}&q={requests.utils.quote(kw)}&image_type=photo&per_page=3")
                if res.status_code == 200 and res.json()["hits"]:
                    img_data = requests.get(res.json()["hits"][0]["webformatURL"]).content
                    target.write_bytes(img_data)
                    return str(target)
            except: pass
        img = Image.new("RGB", (1920, 1080), color=(30, 30, 30))
        img.save(str(target), "JPEG")
        return str(target)

    def assemble_video(self, timeline: list, audio_path: str) -> str:
        log.info("【合成】アニメーション・レンダリング開始...")
        scene_clips = []
        audio = AudioFileClip(audio_path)
        for idx, t in enumerate(timeline):
            sub_clips = []
            sub_dur = t["duration"] / 3
            for i in range(3):
                img_p = t.get(f"img_{i}")
                if not img_p: continue
                clip = ImageClip(img_p).with_duration(sub_dur).resized((1920, 1080))
                clip = self._apply_motion(clip, t["camera_motion"], sub_dur)
                if i > 0: clip = clip.with_effects([vfx.CrossFadeIn(0.5)])
                sub_clips.append(clip)
            if not sub_clips: continue
            combined = concatenate_videoclips(sub_clips, method="compose")
            try:
                txt = (TextClip(text=t["narration"], font="Arial-Bold", font_size=40, 
                                color='white', stroke_color='black', stroke_width=1.5,
                                method='caption', size=(1600, None))
                        .with_duration(t["duration"]).with_position(('center', 850)))
                final_s = CompositeVideoClip([combined, txt], size=(1920, 1080))
            except:
                final_s = combined
            scene_clips.append(final_s)
        final_v = concatenate_videoclips(scene_clips, method="compose").with_audio(audio)
        out = WORK / "final.mp4"
        final_v.write_videofile(str(out), fps=24, codec="libx264", audio_codec="aac", preset="medium", logger=None)
        return str(out)

    def _apply_motion(self, clip, motion, duration):
        if motion == "zoom_in": return clip.with_effects([vfx.Resize(lambda t: 1 + 0.05 * (t/duration))])
        if motion == "zoom_out": return clip.with_effects([vfx.Resize(lambda t: 1.1 - 0.05 * (t/duration))])
        if motion == "pan_right":
            clip = clip.resized((int(1920 * 1.2), 1080))
            return clip.with_position(lambda t: (-int(1920 * 0.2 * (t/duration)), "center"))
        if motion == "pan_left":
            clip = clip.resized((int(1920 * 1.2), 1080))
            return clip.with_position(lambda t: (int(1920 * 0.2 * (t/duration)), "center"))
        return clip

# --- メイン・パイプライン ---

async def main():
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        log.info("=== Cinematic Motion Engine: 完全自動パイプライン起動 ===")
        
        # 1. 設計図の確認・生成
        if not MASTER_STORYBOARD_PATH.exists():
            storyboard = run_phase_1()
        else:
            log.info(f"既存の設計図を使用します: {MASTER_STORYBOARD_PATH}")
            with open(MASTER_STORYBOARD_PATH, "r", encoding="utf-8") as f:
                storyboard = json.load(f)

        # 2. アセット生成と動画合成
        manager = AssetManager(storyboard)
        audio_path, timeline = await manager.generate_all_audio()
        await manager.generate_all_images(timeline)
        final_video_path = manager.assemble_video(timeline, audio_path)

        log.info(f"🎉 全工程完了! 動画: {final_video_path}")

    except Exception as e:
        log.error(f"致命的なエラー: {e}")
        log.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
