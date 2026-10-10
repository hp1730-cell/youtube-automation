import os
import json
import asyncio
import logging
import requests
import time
from pathlib import Path

# ログ設定
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("AssetGenerator")

# 環境変数
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
INPUT_TITLE = os.getenv("INPUT_VIDEO_TITLE", "AIの進化と未来")
INPUT_GENRE = os.getenv("INPUT_VIDEO_GENRE", "自動判定")
INPUT_TONE = os.getenv("INPUT_VIDEO_TONE", "わかりやすく、最後まで見たくなる雰囲気")
GLOBAL_STYLE = "High-quality cinematic anime style, Makoto Shinkai inspired, detailed background, vivid colors, 8k, masterpiece"
WORK_DIR = Path(os.getenv("WORK_DIR", "work"))

async def call_groq(prompt: str) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "openai/gpt-oss-120b", "messages": [{"role": "user", "content": prompt}], "temperature": 0.7}
    for attempt in range(3):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=60)
            if res.status_code == 200:
                return res.json()["choices"][0]["message"]["content"]
            log.warning(f"Groq API Error ({res.status_code})")
        except Exception as e:
            log.warning(f"Groq API Exception ({attempt+1}): {e}")
        await asyncio.sleep(3)
    raise RuntimeError("Groq API failed.")

async def generate_asset_prompts() -> list:
    """企画に合わせて、画像生成用の詳細な英語プロンプト集を作る"""
    log.info("画像生成用のプロンプトを作成中...")
    prompt = f"""
テーマ: {INPUT_TITLE} / ジャンル: {INPUT_GENRE} / 雰囲気: {INPUT_TONE}
このテーマに沿った、動画の背景に使える高品質な画像用プロンプトを10個作成してください。
【出力条件】
- 各プロンプトは、非常に具体的で詳細な英語で記述すること。
- 1枚の画像でシーンが成立するような、風景や抽象的なイメージを推奨。
- 出力は必ず以下のJSON形式のみ。

{{
  "prompts": [
    "detailed english prompt 1...",
    "detailed english prompt 2...",
    ...
  ]
}}
"""
    raw = await call_groq(prompt)
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(repair_json(cleaned))["prompts"]

async def download_images(prompts: list):
    """プロンプトを使って画像をダウンロードし、WORK/assets に保存する"""
    asset_dir = WORK / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    
    for idx, p in enumerate(prompts):
        # スタイルを結合
        full_prompt = f"{p}, {GLOBAL_STYLE}"
        encoded_prompt = requests.utils.quote(full_prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1920&height=1080&nologo=true"
        
        img_path = asset_dir / f"asset_{idx}.jpg"
        
        log.info(f"[{idx+1}/{len(prompts)}] 画像生成中: {p[:30]}...")
        
        success = False
        for attempt in range(3): # 3回リトライ
            try:
                res = requests.get(url, timeout=30)
                if res.status_code == 200 and "image" in res.headers.get("Content-Type", ""):
                    img_path.write_bytes(res.content)
                    log.info(f"  -> 成功！")
                    success = True
                    break
                else:
                    log.warning(f"  -> 失敗 (Status {res.status_code})。再試行します...")
            except Exception as e:
                log.warning(f"  -> エラー {e}。再試行します...")
            
            await asyncio.sleep(10) # 【重要】API制限を避けるため、リクエスト間に10秒待機

        if not success:
            log.error(f"シーン {idx}: 画像の取得に失敗しました。スキップします。")

async def main():
    try:
        prompts = await generate_asset_prompts()
        await download_images(prompts)
        log.info("画像の準備が完了しました！")
    except Exception as e:
        log.error(f"致命的なエラー: {e}")
        log.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
