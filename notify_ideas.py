import os
import json
import smtplib
from email.message import EmailMessage
import requests

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
MY_EMAIL = os.getenv("MY_EMAIL")  # あなたのGmailアドレス
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")  # 先ほど作った16桁のアプリパスワード

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
    """SMTPを使ってメールを送信する（ポート587 STARTTLS版）"""
    body = "本日のYouTube自動生成 企画案です。\n制作したい企画の番号（1, 2, 3）をこのメールに返信してください。\n\n"
    for idea in ideas:
        body += f"【番号: {idea['id']}】\nタイトル: {idea['title']}\n概要: {idea['description']}\n\n----\n"

    os.makedirs("work", exist_ok=True)
    with open("work/today_ideas.json", "w", encoding="utf-8") as f:
        json.dump(ideas, f, ensure_ascii=False, indent=2)

    msg = EmailMessage()
    msg.set_content(body)
    msg["Subject"] = "【YouTube自動化】本日の動画企画案をお選びください"
    msg["To"] = MY_EMAIL
    msg["From"] = MY_EMAIL

    try:
        # --- ここから変更 ---
        # SMTP_SSL ではなく SMTP を使用し、ポート 587 を指定
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.set_debuglevel(1) # 原因特定のためデバッグログを出力
        server.starttls()        # TLS暗号化を開始（これが重要）
        server.login(MY_EMAIL, GMAIL_APP_PASSWORD)
        server.send_message(msg)
        server.quit()
        # --- ここまで変更 ---
        print("企画案のメールをSMTPで送信しました。")
    except Exception as e:
        print(f"メール送信エラー: {e}")
        raise e

if __name__ == "__main__":
    ideas = generate_ideas()
    send_notification(ideas)
