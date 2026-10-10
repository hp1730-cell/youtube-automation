import os
import sys
import json
import logging
import requests
import time
from pathlib import Path
from json_repair import repair_json

# --- 設定 (既存のものを継承) ---
WORK = Path(os.getenv("WORK_DIR", "work"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
INPUT_TITLE = os.getenv("INPUT_VIDEO_TITLE", "AIの進化と未来")
INPUT_GENRE = os.getenv("INPUT_VIDEO_GENRE", "自動判定")
INPUT_TONE = os.getenv("INPUT_VIDEO_TONE", "わかりやすく、最後まで見たくなる雰囲気")

log = logging.getLogger("YouTubeAuto")

def call_groq(prompt: str) -> str:
    """Groq API 呼び出し (リトライ機能付き)"""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7}
    
    for attempt in range(3):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=60)
            if res.status_code == 200:
                return res.json()["choices"][0]["message"]["content"]
            log.warning(f"Groq API Error ({res.status_code}). Retrying...")
        except Exception as e:
            log.warning(f"Groq API Exception ({attempt+1}): {e}")
        time.sleep(5)
    raise RuntimeError("Groq APIの呼び出しに失敗しました。")

# --- 新しい階層型生成ロジック ---

def generate_outline(title: str, genre: str, tone: str) -> list:
    """【Step 1】動画全体の章立て（構成案）を生成する"""
    log.info("【Step 1】動画全体の構成案（プロット）を作成中...")
    
    prompt = f"""
あなたはプロのYouTube構成作家です。
テーマ: {title} / ジャンル: {genre} / 雰囲気: {tone}

このテーマに基づき、8分間の動画のための「構成案（Outline）」をJSON形式で作成してください。
視聴者が最後まで釘付けになるよう、「起承転結」に基づいたドラマチックな展開にしてください。

【条件】
- 動画を5〜6つの「章 (Chapters)」に分けてください。
- 各章には `title` (章のタイトル), `duration` (想定秒数), `goal` (その章で伝えるべき内容の要約) を含めてください。
- 合計時間が約480秒（8分）になるようにしてください。

出力は純粋なJSONのみ：
{{
  "chapters": [
    {{ "title": "導入: 衝撃の事実", "duration": 60, "goal": "視聴者の興味を惹きつける" }},
    {{ "title": "第1章: 過去の技術", "duration": 90, "goal": "..." }},
    ...
  ]
}}
"""
    raw_res = call_groq(prompt)
    cleaned = raw_res.replace("```json", "").replace("```", "").strip()
    return json.loads(repair_json(cleaned))["chapters"]

def generate_detailed_scenes(chapter: dict) -> list:
    """【Step 2】各章の詳細なシーン（ナレーション＋画像指示）を生成する"""
    log.info(f"【Step 2】章 '{chapter['title']}' の詳細台本を生成中...")
    
    prompt = f"""
あなたはプロの動画脚本家兼、絵コンテ作家です。
章のタイトル: {chapter['title']}
章の目的: {chapter['goal']}
想定時間: {chapter['duration']}秒

この章の内容を、視聴者が飽きないように、10〜15個の「シーン (Scenes)」に分解してください。

【各シーンの条件】
1. `narration`: 視聴者に語りかける、情緒的で詳細な日本語のナレーション（5〜10秒分）。
2. `visual_prompts`: そのナレーションを表現するための「3つの異なる視覚的な英語プロンプト」。
   - 1枚目は「引きの絵 (Wide Shot)」
   - 2枚目は「寄りの絵 (Medium/Close-up Shot)」
   - 3枚目は「動きのある絵 (Action/Dynamic Shot)」
   - ※すべて「High-end cinematic anime style, highly detailed digital illustration, masterpiece, 8k」という画風を前提にすること。
3. `search_keywords`: 画像検索用の短い英語のキーワード（1〜3語）。
4. `camera_motion`: "zoom_in", "zoom_out", "pan_right", "pan_left" から選択。

出力は純粋なJSONのみ：
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
    raw_res = call_groq(prompt)
    cleaned = raw_res.replace("```json", "").replace("```", "").strip()
    return json.loads(repair_json(cleaned))["scenes"]

def build_master_storyboard() -> dict:
    """全てのプロセスを統合し、マスター・ストーリーボードを構築する"""
    log.info("=== 階層型ストーリーボード生成プロセス開始 ===")
    
    # 1. 構成案の作成
    chapters = generate_outline(INPUT_TITLE, INPUT_GENRE, INPUT_TONE)
    
    master_storyboard = {
        "video_title": INPUT_TITLE,
        "chapters": []
    }
    
    # 2. 各章の詳細化
    for chapter in chapters:
        chapter_data = {
            "chapter_title": chapter["title"],
            "chapter_duration": chapter["duration"],
            "chapter_goal": chapter["goal"],
            "scenes": generate_detailed_scenes(chapter)
        }
        master_storyboard["chapters"].append(chapter_data)
        # 章の間に少し休憩（API負荷軽減）
        time.sleep(2)

    # 3. マスター・ストーリーボードの保存
    storyboard_path = WORK / "master_storyboard.json"
    with open(storyboard_path, "w", encoding="utf-8") as f:
            json.dump(master_storyboard, master__storyboard, indent=2, ensure_ascii=False)
            
    log.info(f"=== 生成完了! マスター・ストーリーボード保存: {storyboard_path} ===")
    return master_storyboard

def main():
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        log.info("=== AI階層型ストーリーボード生成システム起動 ===")
        
        # 【フェーズ1】のみを実行
        master_storyboard = build_master_storyboard()
        
        log.info("動画の『脳』が完成しました。次のフェーズ（音声・画像）へ進む準備が整いました。")
        
    except Exception as e:
        log.error(f"致命的なエラーが発生しました: {e}")
        log.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
