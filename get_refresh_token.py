"""【自分のPCで1回だけ実行】YouTube アップロード用のリフレッシュトークンを取得する

使い方:
  1. pip install google-auth-oauthlib
  2. Google Cloud Console からダウンロードした OAuth クライアント（デスクトップアプリ）の JSON を
     client_secret.json という名前でこのファイルと同じフォルダに置く
  3. python get_refresh_token.py
  4. ブラウザでアップロード先のチャンネルの Google アカウントを選んで許可する
  5. 表示された3つの値を GitHub の Secrets に登録する
※ client_secret.json とトークンはリポジトリにコミットしないでください。
"""
import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

with open("client_secret.json", encoding="utf-8") as f:
    info = json.load(f).get("installed", {})

print("\n===== GitHub Secrets に登録してください =====")
print(f"YT_CLIENT_ID     = {info.get('client_id')}")
print(f"YT_CLIENT_SECRET = {info.get('client_secret')}")
print(f"YT_REFRESH_TOKEN = {creds.refresh_token}")
