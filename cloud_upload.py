import os
import subprocess
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

def restore_credentials():
    """GitHub Secretsから認証情報を一時ファイルとして復元"""
    token_json = os.environ.get("TOKEN_JSON")
    client_secret_json = os.environ.get("CLIENT_SECRET_JSON")

    if not token_json or not client_secret_json:
        raise ValueError("❌ GitHub Secrets (TOKEN_JSON / CLIENT_SECRET_JSON) が設定されていません。")

    with open("token.json", "w", encoding="utf-8") as f:
        f.write(token_json)
        
    with open("client_secret.json", "w", encoding="utf-8") as f:
        f.write(client_secret_json)

    print("🔑 認証情報の復元に成功しました。")

def generate_test_video():
    """テスト用動画をFFmpegで自動作成（3秒間のブルー画面）"""
    video_path = "cloud_test.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=1280x720:d=3",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-t", "3",
        "-c:v", "libx264", "-c:a", "aac",
        video_path
    ]
    subprocess.run(cmd, check=True)
    print("🎬 クラウド側でテスト動画の作成が完了しました。")
    return video_path

def upload_to_youtube(video_path, video_title="【クラウド自動投稿テスト】GitHub Actions"):
    """YouTube APIを使用して動画を投稿"""
    creds = Credentials.from_authorized_user_file("token.json")
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": video_title,
            "description": "GitHub Actionsから完全自動生成・投稿されたテスト動画です。",
            "tags": ["GitHubActions", "自動化", "API"],
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": "unlisted"  # 限定公開
        }
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)

    print("🚀 YouTubeへのアップロードを開始します...")
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"進捗: {int(status.progress() * 100)}%")

    print("====================================")
    print("🎉 クラウド自動アップロード成功！")
    print(f"🎬 動画ID: {response['id']}")
    print(f"🔗 URL: https://youtu.be/{response['id']}")
    print("====================================")

def main():
    restore_credentials()
    
    # 外部（GAS等）からタイトルパラメータが渡されている場合は使用
    video_title = os.environ.get("INPUT_VIDEO_TITLE", "【クラウド自動投稿テスト】GitHub Actions")
    
    video_path = generate_test_video()
    upload_to_youtube(video_path, video_title)

if __name__ == "__main__":
    main()
