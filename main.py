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

# --- Gemini モデル自動検出ロジック ---

def get_available_models() -> list:
    """
    APIキーに紐づく、実際に使用可能なモデルのリストを取得する
    """
    if not GEMINI_API_KEY:
        return []
    
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        # 利用可能なモデルをリストアップ
        available_models = []
        for m in genai.list_models():
            # generateContent メソッドが使えるモデルのみを抽出
            if 'generateContent' in m.supported_generation_methods:
                available_models.append(m.name)
        
        # m.name は 'models/gemini-1.5-flash' のような形式
        log.info(f"利用可能なモデルを検出しました: {available_models}")
        return available_models
    except Exception as e:
        log.error(f"モデルのリスト取得中にエラーが発生しました: {e}")
        return []

# グローバル変数として保持
AVAILABLE_MODELS = []

def call_gemini(prompt: str) -> str:
    """
    Gemini API 呼び出し
    - 検出された利用可能なモデルを順番に試行
    - 429エラーへの指数バックオフ
    """
    global AVAILABLE_MODELS
    
    if not AVAILABLE_MODELS:
        AVAILABLE_MODELS = get_available_models()
        if not AVAILABLE_MODELS:
            raise RuntimeError("利用可能なGeminiモデルが一つも見つかりませんでした。APIキーの設定を確認してください。")

    for model_id in AVAILABLE_MODELS:
        log.info(f"モデル '{model_id}' を使用してリクエストを試行中...")
        try:
            model = genai.GenerativeModel(model_id)
            response = model.generate_content(prompt)
            
            if response.text:
                return response.text
            else:
                log.warning(f"モデル '{model_id}' が空のレスポンスを返しました。")
                continue

        except Exception as e:
            error_msg = str(e)
            
            # 【ケース1】モデルが見つからない場合 (404相当)
            if "404" in error_msg or "not found" in error_msg.lower():
                log.error(f"モデル '{model_id}' は見つかりませんでした。次のモデルを試します。")
                continue
            
            # 【ケース2】リクエスト制限に達した場合 (429相当)
            elif "429" in error_msg or "too many requests" in error_msg.lower():
                wait_time = random.uniform(20, 40)
                log.warning(f"API制限検知。{wait_time:.1f}秒待機してリトライします...")
                time.sleep(wait_time)
                continue
            
            # 【ケース3】その他のエラー
            else:
                log.error(f"モデル '{model_id}' で予期せぬエラーが発生: {e}")
                time.sleep(5)
                continue

    raise RuntimeError(f"利用可能なすべてのモデル ({AVAILABLE_MODELS}) でリクエストに失敗しました。")

# --- 階層型生成ロジック (変更なし) ---

def generate_outline(title: str, genre: str, tone: str) -> list:
    log.info("【Step 1】動画全体の構成案（プロット）を作成中...")
    prompt = f"You are a professional YouTube scriptwriter. Theme: {title} / Genre: {genre} / Tone: {tone}. Create an 8-minute video outline in JSON format: {{ 'chapters': [ {{ 'title': '...', 'duration': 60, 'goal': '...' }} ] }}. Output ONLY JSON."
    raw_res = call_gemini(prompt)
    cleaned = raw_res.replace("```json", "").replace("```", "").strip()
    return json.loads(repair_json(cleaned))["chapters"]

def generate_detailed_scenes(chapter: dict) -> list:
    log.info(f"【Step 2】章 '{chapter['title']}' の詳細シーンを生成中...")
    prompt = f"You are a professional storyboard artist. Chapter: {chapter['title']} / Goal: {chapter['goal']}. Break into 10-15 scenes. For each scene provide JSON: {{ 'scenes': [ {{ 'narration': '...', 'visual_prompts': ['...', '...', '...'], 'search_keywords': ['...'], 'camera_motion': 'zoom_in' }} ] }}. All prompts must be in English and include 'High-end cinematic anime style, 8k'. Output ONLY JSON."
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
        time.sleep(random.uniform(15, 30))
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
