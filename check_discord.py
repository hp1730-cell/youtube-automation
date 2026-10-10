import os
import requests

# GitHub Secretsから環境変数を取得
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
GH_PAT = os.getenv("GH_PAT")
GH_REPO = os.getenv("GITHUB_REPOSITORY") # 自動的に "ユーザー名/リポジトリ名" が入ります

def check_discord_replies():
    """Discordの最新メッセージをチェックして、1, 2, 3のいずれかがあればそれを返す"""
    print(f"Checking Discord channel {DISCORD_CHANNEL_ID}...")
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages?limit=5"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    
    try:
        res = requests.get(url, headers=headers, timeout=30)
        res.raise_for_status()
        messages = res.json()
        
        # 最新のメッセージから順に見て、中身が "1", "2", "3" のいずれかであるか確認
        for msg in messages:
            content = msg.get("content", "").strip()
            if content in ["1", "2", "3"]:
                print(f"返信を発見しました: {content}")
                return content
    except Exception as e:
        print(f"Discordチェック中にエラーが発生しました: {e}")
    
    return None

def trigger_video_workflow(choice):
    """GitHub APIを使って、動画作成ワークフロー(pipeline.yml)を起動する"""
    # 起動したいワークフローのファイル名を指定
    workflow_id = "youtube_auto.yml" 
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{workflow_id}/dispatches"
    
    headers = {
        "Authorization": f"token {GH_PAT}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    # 動画作成ワークフローに渡す入力値 (pipeline.yml の inputs に合わせる)
    payload = {
        "ref": "main",
        "inputs": {
            "video_title": f"企画案番号{choice}の動画作成",
            "video_genre": "自動判定",
            "video_tone": "わかりやすく、最後まで見たくなる雰囲気",
            "dry_run": "false" # 本番作成にする場合はfalse
        }
    }
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=30)
        if res.status_code == 204:
            print(f"成功：企画案 {choice} の動画作成ワークフローを起動しました！")
        else:
            print(f"エラー：GitHub APIの起動に失敗しました。ステータスコード: {res.status_code}, 内容: {res.text}")
    except Exception as e:
        print(f"GitHub APIリクエスト中にエラーが発生しました: {e}")

if __name__ == "__main__":
    print("--- Discord返信監視プログラム起動 ---")
    choice = check_discord_replies()
    if choice:
        trigger_video_workflow(choice)
    else:
        print("条件に合う返信は見つかりませんでした。次回チェックまで待機します。")
