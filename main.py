import os
import sys
import json
import logging
import time
import random
import traceback
from pathlib import Path
import google.generativeai as genai  # 【追加】Gemini用ライブラリ
from json_repair import repair_json

# --- 設定 ---
WORK = Path(os.getenv("WORK_DIR", "work"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # 【変更】Gemini用
INPUT_TITLE = os.getenv("INPUT_VIDEO_TITLE", "AIの進化と未来")
INPUT_GENRE = os.getenv("INPUT_VIDEO_GENRE", "自動判定")
INPUT_TONE = os.getenv("INPUT_VIDEO_TONE", "わかりやすく、最後まで見たくなる雰囲気")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("YouTubeAuto")

# --- Gemini API セットアップ ---
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    # モデルの選択（1.5 Proが長尺に最適）
    model = genai.GenerativeModel('gemini-1.5-pro')
else:
    log.error("GEMINI_API_KEY が設定されていません。")
    sys.exit(1)

def call_gemini(prompt: str) -> str:
    """
    Gemini API 呼び出し
    指数バックオフとジッターを実装
    """
    for attempt in range(5):
        try:
            response = model.generate_content(prompt)
            if response.text:
                return response.text
            else:
                raise ValueError("Gemini returned empty response")
                
        except Exception as e:
            # 429 (Too Many Requests) やその他のエラーへの対処
            wait_time = (2 ** attempt) * 20 + random.uniform(5, 15)
            log.warning(f"Gemini API エラー: {e}. {wait_time:.1f}秒後にリトライします (試行 {attempt+1}/5)...")
            time.sleep(wait_time)
            
    raise RuntimeError("Gemini APIの呼び出しに失敗しました。")

# --- 階層型生成ロジック (Gemini最適化版) ---

def generate_outline(title: str, genre: str, tone: str) -> list:
    log.info("【Step 1】Geminiによる動画全体の構成案（プロット）作成中...")
    
    prompt = f"""
You are a professional YouTube scriptwriter.
Theme: {title} / Genre: {genre} / Tone: {tone}

Create a structural outline for an 8-minute YouTube video. 
The video must follow a dramatic "Introduction, Rising Action, Climax, Falling Action, Resolution" structure.

Output ONLY a valid JSON object with the following format:
{{
  "chapters": [
    {{ "title": "Chapter Title", "duration": 60, "goal": "Description of what to achieve in this chapter" }}
  ]
}}

Requirements:
- Total duration should be approximately 480 seconds.
- Divide the video into 5-6 distinct chapters.
"""
    raw_res = call_gemini(prompt)
    cleaned = raw_res.replace("```json", "").replace("```", "").strip()
    return json.loads(repair_json(cleaned))["chapters"]

def generate_detailed_scenes(chapter: dict) -> list:
    log.info(f"【Step 2】章 '{chapter['title']}' の詳細シーンをGeminiで生成中...")
    
    prompt = f"""
You are a professional storyboard artist and scriptwriter.
Chapter Title: {chapter['title']}
Chapter Goal: {chapter['goal']}

Break this chapter into 10-15 detailed scenes.

For each scene, provide:
1. `narration`: A compelling, emotional Japanese narration (5-10 seconds).
2. `visual_prompts`: An array of 3 detailed English prompts for image generation.
   - Prompt 1: Wide shot (setting the scene)
   - Prompt 2: Medium/Close-up shot (focus on subject/emotion)
   - Prompt 3: Dynamic/Action shot (movement or dramatic angle)
   - ALL prompts must include: "High-end cinematic anime style, highly detailed digital illustration, masterpiece, 8k, vivid colors"
3. `search_keywords`: 1-3 English keywords for stock photo fallback.
4. `camera_motion`: Choose one from ["zoom_in", "zoom_out", "pan_right", "pan_left"].

Output ONLY a valid JSON object:
{{
  "scenes": [
    {{
      "narration": "...",
      "visual_prompts": ["...", "...", "..."],
      "search_keywords": ["...", "..."],
      "camera_motion": "zoom_in"
    }}
  ]
}}
"""
    raw_res = call_gemini(prompt)
    cleaned = raw_res.replace("```json", "").replace("```", "").strip()
    return json.loads(repair_json(cleaned))["scenes"]

def build_master_storyboard() -> dict:
    log.info("=== Gemini 階層型ストーリーボード生成プロセス開始 ===")
    
    chapters = generate_outline(INPUT_TITLE, INPUT_GENRE, INPUT_TONE)
    master_storyboard = {"video_title": INPUT_TITLE, "chapters": []}
    
    for chapter in chapters:
        chapter_data = {
            "chapter_title": chapter["title"],
            "chapter_duration": chapter["duration"],
            "chapter_goal": chapter["goal"],
            "scenes": generate_detailed_scenes(chapter)
        }
        master_storyboard["chapters"].append(chapter_data)
        
        # APIの負荷を抑え、制限を回避するための待機
        wait_time = random.uniform(15, 30)
        log.info(f"章の完了。API保護のため {wait_time:.1f}秒 待機します...")
        time.sleep(wait_time)

    storyboard_path = WORK / "master_storyboard.json"
    with open(storyboard_path, "w", encoding="utf-8") as f:
        json.dump(master_storyboard, f, indent=2, ensure_ascii=False)
            
    log.info(f"=== 生成完了! 保存先: {storyboard_path} ===")
    return master_storyboard

def main():
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        log.info("=== Gemini 高階層ストーリーボード生成システム起動 ===")
        
        master_storyboard = build_master_storyboard()
        log.info("動画の『脳』が完成しました。")
        
    except Exception as e:
        log.error(f"致命的なエラーが発生しました: {e}")
        log.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
