import os
import requests
from datetime import datetime, timezone, timedelta

# 環境変数から取得
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
GH_PAT = os.getenv("GH_PAT")
GH_REPO = os.getenv("GITHUB_REPOSITORY")

def check_discord_replies():
    """Discordの最新メッセージをチェックし、最近（20分以内）の数字のみを返す"""
    print(f"Checking Discord channel {DISCORD_CHANNEL_ID}...")
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages?limit=10"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    
    try:
        res = requests.get(url, headers=headers, timeout=30)
        res.raise_for_status()
        messages = res.json()
        
        # 現在時刻 (UTC)
        now = datetime.now(timezone.utc)
        # 判定基準にする時間（例：20分前までを有効とする）
        time_limit = now - timedelta(minutes=20)

        for msg in messages:
            content = msg.get("content", "").strip()
            # メッセージの送信時間を取得 (ISO 8601形式)
            timestamp_str = msg.get("timestamp")
            # 文字列を datetime オブジェクトに変換 (Zを+00:00に置換してパース)
            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))

            # 「1, 2, 3 のいずれか」かつ「20分以内に送られた」メッセージか判定
            if content in ["1", "2", "3"] and timestamp > time_limit:
                print(f"有効な最新返信を発見しました: {content} (送信時間: {timestamp})")
                return content
            elif content in ["1", "2", "3"]:
                print(f"古い返信を発見しましたが、時間切れのため無視します: {content}")

    except Exception as e:
        print(f"Discordチェック中にエラーが発生しました: {e}")
    
    return None

def trigger_video_workflow(choice):
    """GitHub APIを使って動画作成ワークフローを起動する"""
    workflow_id = "pipeline.yml" # あなたのファイル名に合わせてください
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{workflow_id}/dispatches"
    
    headers = {
        "Authorization": f"token {GH_PAT}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    payload = {
        "ref": "main",
        "inputs": {
            "video_title": f"企画案番号{choice}の動画作成",
            "dry_run": "false"
        }
    }
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=30)
        if res.status_code == 204:
            print(f"成功：企画案 {choice} の動画作成をリクエストしました！")
        else:
            print(f"エラー：GitHub API起動失敗 {res.status_code} - {res.text}")
    except Exception as e:
        print(f"GitHub APIリクエスト中にエラーが発生しました: {e}")

if __name__ == "__main__":
    print("--- Discord返信監視プログラム起動 ---")
    choice = check_discord_replies()
    if choice:
        trigger_video_workflow(choice)
    else:
        print("条件に合う新しい返信は見つかりませんでした。")
