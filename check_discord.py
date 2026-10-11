import os
import sys
import logging
import requests
from datetime import datetime, timezone, timedelta

# --- ロガーの初期化 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

# 環境変数から取得
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
GH_PAT = os.getenv("GH_PAT")
GH_REPO = os.getenv("GITHUB_REPOSITORY")

def is_it_eight_am():
    """現在時刻が日本時間の朝8時台（8:00〜8:59）かどうかを判定"""
    # 日本標準時 (JST) を取得
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)
    
    current_hour = now_jst.hour
    log.info(f"現在の日本時刻: {now_jst.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 8時台であれば True
    return current_hour == 8

def check_discord_replies():
    """Discordの最新メッセージをチェックし、最近（20分以内）の数字のみを返す"""
    log.info(f"Checking Discord channel {DISCORD_CHANNEL_ID}...")
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages?limit=10"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    
    try:
        res = requests.get(url, headers=headers, timeout=30)
        res.raise_for_status()
        messages = res.json()
        
        now = datetime.now(timezone.utc)
        time_limit = now - timedelta(minutes=20)

        for msg in messages:
            content = msg.get("content", "").strip()
            timestamp_str = msg.get("timestamp")
            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))

            if content in ["1", "2", "3"] and timestamp > time_limit:
                log.info(f"有効な最新返信を発見しました: {content} (送信時間: {timestamp})")
                return content
            elif content in ["1", "2", "3"]:
                log.info(f"古い返信を発見しましたが、時間切れのため無視します: {content}")

    except Exception as e:
        log.error(f"Discordチェック中にエラーが発生しました: {e}")
    
    return None

def trigger_video_workflow(choice):
    """GitHub APIを使って動画作成ワークフローを起動する"""
    workflow_id = "youtube_auto.yml" 
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
            log.info(f"成功：企画案 {choice} の動画作成ワークフロー（{workflow_id}）を起動しました！")
        else:
            log.error(f"エラー：GitHub API起動失敗 {res.status_code} - {res.text}")
    except Exception as e:
        log.error(f"GitHub APIリクエスト中にエラーが発生しました: {e}")

if __name__ == "__main__":
    log.info("--- Discord返信監視プログラム起動 ---")
    
    # 【重要】手動実行(workflow_dispatch)か、自動実行(schedule)かを判別
    event_name = os.getenv("GITHUB_EVENT_NAME", "manual")

    if event_name == "schedule":
        # 自動実行（朝8時）の場合
        if is_it_eight_am():
            log.info("自動実行時間（朝8時）です。Discordをチェックします。")
            choice = check_discord_replies()
            if choice:
                trigger_video_workflow(choice)
            else:
                log.info("条件に合う新しい返信は見つかりませんでした。")
        else:
            log.info("自動実行時間ではありません（朝8時以外）。終了します。")
            sys.exit(0)
    else:
        # 手動実行（workflow_dispatch）の場合
        log.info("手動実行モードです。Discordをチェックします。")
        choice = check_discord_replies()
        if choice:
            trigger_video_workflow(choice)
        else:
            log.info("条件に合う新しい返信は見つかりませんでした。")
