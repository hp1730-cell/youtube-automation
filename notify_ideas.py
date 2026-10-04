import os
import json
import base64
import requests
from email.message import EmailMessage
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
TOKEN_JSON = os.getenv("TOKEN_JSON")
MY_EMAIL = os.getenv("MY_EMAIL")  # あなたの通知先メールアドレス

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
    """Gmail APIを使って企画案を自分にメール送信する"""
    creds = Credentials.from_authorized_user_info(json.loads(TOKEN_JSON))
    service = build("gmail", "v1", credentials=creds)

    body = "本日のYouTube自動生成 企画案です。\n制作したい企画の番号（1, 2, 3）をこのメールに返信してください。\n\n"
    for idea in ideas:
        body += f"【番号: {idea['id']}】\nタイトル: {idea['title']}\n概要: {idea['description']}\n\n----\n"

    # 企画案をJSONとして一時保存（返信されたときにタイトルを引くため）
    os.makedirs("work", exist_ok=True)
    with open("work/today_ideas.json", "w", encoding="utf-8") as f:
        json.dump(ideas, f, ensure_ascii=False, indent=2)

    msg = EmailMessage()
    msg.set_content(body)
    # 辞書形式でヘッダーを設定するよう修正
    msg["Subject"] = "【YouTube自動化】本日の動画企画案をお選びください"
    msg["To"] = MY_EMAIL
    msg["From"] = MY_EMAIL

    encoded_message = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": encoded_message}).execute()
    print("企画案のメールを送信しました。")

if __name__ == "__main__":
    ideas = generate_ideas()
    send_notification(ideas)
