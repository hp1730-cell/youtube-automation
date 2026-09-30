import os
import json
import subprocess
from google import genai
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

def restore_credentials():
    """GitHub SecretsからYouTube認証情報を復元"""
    token_json = os.environ.get("TOKEN_JSON")
    client_secret_json = os.environ.get("CLIENT_SECRET_JSON")

    if not token_json or not client_secret_json:
        raise ValueError("❌ TOKEN_JSON / CLIENT_SECRET_JSON が設定されていません。")

    with open("token.json", "w", encoding="utf-8") as f:
        f.write(token_json)
        
    with open("client_secret.json", "w", encoding="utf-8") as f:
        f.write(client_secret_json)

def generate_script_with_gemini(theme):
    """Gemini APIを使用してYouTube用動画の台本・タイトル・概要欄を自動生成"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("⚠️️ GEMINI_API_KEY が未設定のため、デフォルトタイトルを使用します。")
        return {
            "title": f"【自動投稿】{theme}",
            "description": "自動生成テスト動画です。",
            "script": "これはテストナレーションです。"
        }

    client = genai.Client(api_key=api_key)
    
    prompt = f"""
あなたはプロのYouTube動画クリエイターです。
以下の「テーマ」をもとに、YouTube動画のタイトル、概要欄、短いナレーション原稿を作成してください。

テーマ: {theme}

出力フォーマットは必ず以下のJSON形式のみで返答してください。余計な解説文は含めないでください。
{{
  "title": "目を引く30文字以内のYouTube動画タイトル",
  "description": "動画の概要欄テキスト（ハッシュタグ3つ含む）",
  "script": "動画のナレーション原稿（100文字以内の短い解説文章）"
}}
"""

    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt
    )
    
    try:
        clean_text = response.text.strip().replace("```json", "").replace("```", "")
        data = json.loads(clean_text)
        print("🤖 Geminiによる台本生成が完了しました：")
        print(f"  タイトル: {data.get('title')}")
        print(f"  概要欄: {data.get('description')}")
        print(f"  原稿: {data.get('script')}")
        return data
    except Exception as e:
        print(f"⚠️ JSONパースエラー: {e}")
        return {
            "title": f"【自動作成】{theme}",
            "description": f"{theme}についてのショート解説動画です。",
            "script": f"今回は{theme}について解説します。"
        }

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
    return video_path

def upload_to_youtube(video_path, metadata):
    """YouTube APIを使用して動画を投稿"""
    creds = Credentials.from_authorized_user_file("token.json")
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": ["AI自動生成", "Gemini", "YouTubeAPI"],
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

    print("====================================")
    print("🎉 クラウド自動アップロード成功！")
    print(f"🎬 動画ID: {response['id']}")
    print(f"🔗 URL: https://youtu.be/{response['id']}")
    print("====================================")

def main():
    restore_credentials()
    
    # GAS/スプレッドシートから送られてきたテーマを取得
    input_theme = os.environ.get("INPUT_VIDEO_TITLE", "宇宙の謎")
    
    # 1. Geminiで台本・タイトル・概要欄を生成
    metadata = generate_script_with_gemini(input_theme)
    
    # 2. 動画ファイルの生成
    video_path = generate_test_video()
    
    # 3. YouTube投稿
    upload_to_youtube(video_path, metadata)

if __name__ == "__main__":
    main()
