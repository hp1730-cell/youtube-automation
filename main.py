import os
import sys
import json
import logging
import requests
import time
import traceback
import random
from pathlib import Path
from json_repair import repair_json
import google.generativeai as genai

# --- 設定 ---
WORK = Path(os.getenv("WORK_DIR", "work"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
INPUT_TITLE = os.getenv("INPUT_VIDEO_TITLE", "AIの進化と未来")
INPUT_GENRE = os.getenv("INPUT_VIDEO_GENRE", "自動判定")
INPUT_TONE = os.getenv("INPUT_VIDEO_TONE", "わかりやすく、最後まで見たくなる雰囲気")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("YouTubeAuto")

# 利用可能なモデルのリスト（優先度の高い順）
# 404エラーが出た場合、自動的に次のモデルへ切り替えます
MODELS_TO_TRY = [
    "gemini-1.5-flash",
    "gemini-1.5-flash-latest",
    "gemini-1.5-pro",
    "gemini-1.5-pro-latest",
    "gemini-pro"
]

def call_gemini(prompt: str) -> str:
    """
    Gemini API 呼び出し
    - 404エラー（モデル不在）の場合、別のモデルを試行
    - 429エラー（制限）の場合、指数バックオフで待機
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が設定されていません。")

    genai.configure(api_key=GEMINI_API_KEY)

    for model_name in MODELS_TO_TRY:
        log.info(f"モデル '{model_name}' を使用してリクエストを試行中...")
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            
            if response.text:
                return response.text
            else:
                log.warning(f"モデル '{model_name}' が空のレスポンスを返しました。")
                continue

        except Exception as e:
            error_msg = str(e)
            
            # 【ケース1】モデルが見つからない場合 (404相当)
            if "404" in error_msg or "not found" in error_msg.lower():
                log.error(f"モデル '{model_name}' は見つかりませんでした。次のモデルを試します。")
                continue
            
            # 【ケース2】リクエスト制限に達した場合 (429相当)
            elif "429" in error_msg or "too many requests" in error_msg.lower():
                wait_time = random.uniform(20, 40)
                log.warning(f"API制限検知。{wait_time:.1f}秒待機してリトライします...")
                time.sleep(wait_time)
                continue
            
            # 【ケース3】その他のエラー
            else:
                log.error(f"モデル '{model_name}' で予期せぬエラーが発生: {e}")
                # 致命的なエラーでなければ、少し待って次のモデルを試す
                time.sleep(5)
                continue

    raise RuntimeError("利用可能なすべてのGeminiモデルでリクエストに失敗しました。APIキーの権限または接続を確認してください。")

# --- 階層型生成ロジック ---

def generate_outline(title: str, genre: str, tone: str) -> list:
    log.info("【Step 1】動画全体の構成案（プロット）を作成中...")
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
    log.info(f"【Step 2】章 '{chapter['title']}' の詳細シーンを生成中...")
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
        
        # API負荷軽減のための待機
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
