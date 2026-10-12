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

def is_within_monitoring_window():
    """現在時刻が日本時間の朝8時〜12時台かどうかを判定"""
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)
    
    current_hour = now_jst.hour
    log.info(f"現在の日本時刻: {now_jst.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 8, 9, 10, 11, 12時であればTrueを返す
    return 8 <= current_hour <= 12

def check_discord_replies():
    """Discordの最新メッセージをチェックし、最近（70分以内）の数字のみを返す"""
    log.info(f"Checking Discord channel {DISCORD_CHANNEL_ID}...")
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages?limit=10"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    
    try:
        res = requests.get(url, headers=headers, timeout=30)
        res.raise_for_status()
        messages = res.json()
        
        now = datetime.now(timezone.utc)
        # 【修正】1時間おきの実行に合わせ、判定時間を70分に延長
        time_limit = now - timedelta(minutes=70)

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
    # 【注意】YAMLファイル名が auto_pipeline.yml の場合はここを書き換えてください
    workflow_id = "auto_pipeline.yml" 
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{workflow_id}/dispatches"
    
    headers = {
        "Authorization": f"token {GH_PAT}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    payload = {
        "ref": "main",
        "inputs": {
            "action_mode": "generate_video", # 統合版YAMLのモード指定に合わせる
            "video_title": f"企画案番号{choice}の動画作成"
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
    
    event_name = os.getenv("GITHUB_EVENT_NAME", "manual")

    if event_name == "schedule":
        # 【修正】8時だけでなく、8時〜12時の間であれば動作するように変更
        if is_within_monitoring_window():
            log.info("監視時間内（8時〜12時）です。Discordをチェックします。")
            choice = check_discord_replies()
            if choice:
                trigger_video_workflow(choice)
            else:
                log.info("条件に合う新しい返信は見つかりませんでした。")
        else:
            log.info("監視時間外（8時〜12時以外）です。終了します。")
            sys.exit(0)
    else:
        log.info("手動実行モードです。Discordをチェックします。")
        choice = check_discord_replies()
        if choice:
            trigger_video_workflow(choice)
        else:
            log.info("条件に合う新しい返信は見つかりませんでした。")
