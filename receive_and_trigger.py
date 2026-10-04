import os
import json
import re
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TOKEN_JSON = os.getenv("TOKEN_JSON")

def check_reply_and_trigger():
    creds = Credentials.from_authorized_user_info(json.loads(TOKEN_JSON))
    service = build("gmail", "v1", credentials=creds)

    # 未読の受信メールを検索
    results = service.users().messages().list(userId="me", q="is:unread label:INBOX").execute()
    messages = results.get("messages", [])

    if not messages:
        print("新しい返信はありません。")
        return

    selected_title = None

    for msg_info in messages:
        msg = service.users().messages().get(userId="me", id=msg_info["id"]).execute()
        headers = msg["payload"]["headers"]
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
        
        # 本文の取得
        snippet = msg.get("snippet", "")
        
        print(f"受信メール確認: {subject} / {snippet}")

        # 返信された数字（1, 2, 3）を正規表現で抽出
        match = re.search(r'\b([1-3])\b', snippet + " " + subject)
        if match:
            chosen_id = int(match.group(1))
            print(f"番号 {chosen_id} が選択されました。")

            # 保存しておいた今日の企画案からタイトルを特定
            if os.path.exists("work/today_ideas.json"):
                with open("work/today_ideas.json", "r", encoding="utf-8") as f:
                    ideas = json.load(f)
                    for idea in ideas:
                        if idea["id"] == chosen_id:
                            selected_title = idea["title"]
                            break

            # 処理したメールを既読にする
            service.users().messages().batchModify(
                userId="me",
                body={"ids": [msg_info["id"]], "removeLabelIds": ["UNREAD"]}
            ).execute()
            break

    if selected_title:
        print(f"選択されたテーマで動画生成を開始します: {selected_title}")
        
        # 環境変数をセットして、先ほどの main.py をsubprocessで実行する
        import subprocess
        env = os.environ.copy()
        env["INPUT_VIDEO_TITLE"] = selected_title
        
        result = subprocess.run(["python", "main.py"], env=env)
        if result.returncode == 0:
            print("動画の生成と投稿が正常に完了しました！")
        else:
            print("動画生成中にエラーが発生しました。")

if __name__ == "__main__":
    check_reply_and_trigger()
