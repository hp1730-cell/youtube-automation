import os
import json
import requests

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
# メール設定の代わりにDiscord Webhook URLを取得
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

def call_groq(prompt: str) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.8}
    res = requests.post(url, headers=headers, json=payload, timeout=60)
    return res.json()["choices"][0]["message"]["content"]

def generate_ideas() -> list:
    """LLMに今受けるYouTubeの企画案を3つ考えてもらう"""
    prompt = """
現在YouTubeでバズりやすい、知的興味をそそるトレンドの動画企画案を3つ作成してください。
出力は必ず以下のJSON形式の配列のみで行ってください。

[
  {"id": 1, "title": "企画タイトル1", "description": "この企画が伸びる理由や概要"},
  {"id": 2, "title": "企画タイトル2", "description": "この企画が伸びる理由や概要"},
  {"id": 3, "title": "企画タイトル3", "description": "この企画が伸びる理由や概要"}
]
"""
    raw = call_groq(prompt)
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned)

def send_notification(ideas: list):
    """Discord Webhookを使って通知を送信する"""
    # Discord向けにメッセージを整形（Markdown形式）
    content = "**🚀 本日のYouTube動画企画案が届きました！**\n\n"
    for idea in ideas:
        content += f"**【番号: {idea['id']}】 {idea['title']}**\n{idea['description']}\n\n----\n"
    
    content += "\n制作したい企画の番号を教えてください！"

    # Discordへ送信
    payload = {"content": content}
    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
        res.raise_for_status()
        print("Discordに通知を送信しました。")
    except Exception as e:
        print(f"Discord送信エラー: {e}")
        raise e

if __name__ == "__main__":
    ideas = generate_ideas()
    send_notification(ideas)
