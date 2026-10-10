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

# 利用可能な最新モデルのリスト
MODELS_TO_TRY = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-1.5-pro",
    "gemini-1.5-flash"
]

def get_available_models() -> list:
    if not GEMINI_API_KEY: return []
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        available = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                available.append(m.name)
        log.info(f"検出されたモデル: {available}")
        return available
    except Exception as e:
        log.error(f"モデル取得エラー: {e}")
        return []

AVAILABLE_MODELS = []

def call_gemini(prompt: str) -> str:
    global AVAILABLE_MODELS
    if not AVAILABLE_MODELS:
        AVAILABLE_MODELS = get_available_models()
        if not AVAILABLE_MODELS:
            raise RuntimeError("利用可能なモデルが見つかりません。")

    for model_id in AVAILABLE_MODELS:
        log.info(f"モデル '{model_id}' で試行中...")
        try:
            model = genai.GenerativeModel(model_id)
            response = model.generate_content(prompt)
            if response.text:
                return response.text
            continue
        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg or "not found" in error_msg.lower():
                log.warning(f"モデル '{model_id}' は見つかりません。次へ。")
                continue
            elif "429" in error_msg or "too many requests" in error_msg.lower():
                wait_time = random.uniform(20, 40)
                log.warning(f"制限検知。{wait_time:.1f}秒待機...")
                time.sleep(wait_time)
                continue
            else:
                log.error(f"エラー: {e}")
                time.sleep(5)
                continue
    raise RuntimeError("すべてのモデルで失敗しました。")

# --- 防御型解析ロジック ---

def safe_parse_json(raw_res: str, expected_key: str = None) -> dict:
    """
    AIの出力を解析する。
    AIが {"chapters": [...]} と返しても、単なる [...] と返しても、
    期待する構造に強制変換する。
    """
    try:
        cleaned = raw_res.replace("```json", "").replace("```", "").strip()
        data = json.loads(repair_json(cleaned))
        
        # AIがリストを返してきた場合 (例: [ {"title":...}, {"title":...} ])
        if isinstance(data, list):
            log.info("AIがリスト形式で応答しました。辞書形式にラップします。")
            if expected_key:
                return {expected_key: data}
            return {"items": data} # デフォルトのキー
            
        # AIが辞書を返してきたが、期待するキーがない場合
        if isinstance(data, dict) and expected_key and expected_key not in data:
            log.warning(f"期待するキー '{expected_key}' が見つかりません。データ全体をラップします。")
            return {expected_key: data}
            
        return data
    except Exception as e:
        log.error(f"JSON解析失敗: {e} | 元データ: {raw_res[:100]}...")
        raise

# --- 階層型生成ロジック ---

def generate_outline(title: str, genre: str, tone: str) -> list:
    log.info("【Step 1】構成案作成中...")
    prompt = f"Theme: {title}/Genre: {genre}/Tone: {tone}. Create an 8-min YouTube outline. Output ONLY JSON: {{ 'chapters': [ {{ 'title': '...', 'duration': 60, 'goal': '...' }} ] }}"
    raw_res = call_gemini(prompt)
    data = safe_parse_json(raw_res, "chapters")
    return data["chapters"]

def generate_detailed_scenes(chapter: dict) -> list:
    log.info(f"【Step 2】章 '{chapter['title']}' の詳細生成中...")
    prompt = f"Chapter: {chapter['title']}/Goal: {chapter['goal']}. Create 10-15 scenes. Output ONLY JSON: {{ 'scenes': [ {{ 'narration': '...', 'visual_prompts': ['...', '...', '...'], 'search_keywords': ['...'], 'camera_motion': 'zoom_in' }} ] }}"
    raw_res = call_gemini(prompt)
    data = safe_parse_json(raw_res, "scenes")
    return data["scenes"]

def build_master_storyboard() -> dict:
    log.info("=== 階層型ストーリーボード生成プロセス開始 ===")
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
        time.sleep(random.uniform(15, 30))
    storyboard_path = WORK / "master_storyboard.json"
    with open(storyboard_path, "w", encoding="utf-8") as f:
        json.dump(master_storyboard, f, indent=2, ensure_ascii=False)
    log.info(f"=== 生成完了! {storyboard_path} ===")
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
