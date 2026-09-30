import os
import json
import time
import subprocess
from google import genai
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from gtts import gTTS

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

def generate_script_with_gemini(theme, genre, tone):
    """Gemini APIを使用してAIドラマ風の台本を生成（503エラー対策のリトライ機能付き）"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ GEMINI_API_KEY が設定されていません。")

    client = genai.Client(api_key=api_key)
    
    prompt = f"""
あなたはプロのAIアニメ・ドラマ脚本家です。
以下の条件をもとに、YouTubeのAIドラマ・解説動画用のデータを作成してください。著作権や肖像権を侵害する実在の人物やキャラクターは一切出さず、完全オリジナルの世界観・ストーリーにしてください。

- メインテーマ: {theme}
- ジャンル: {genre}
- 雰囲気・トーン: {tone}

出力フォーマットは必ず以下のJSON形式のみで返答してください。余計な解説文は含めないでください。
{{
  "title": "目を引く30文字以内のYouTube動画タイトル",
  "description": "動画の概要欄テキスト（ハッシュタグ3つ含む）",
  "script": "動画のナレーション原稿（ドラマ風で引き込む文章、150文字程度）"
}}
"""

    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"🤖 Gemini APIに接続中... (試行回数: {attempt + 1}/{max_retries})")
            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt
            )
            clean_text = response.text.strip().replace("```json", "").replace("```", "")
            data = json.loads(clean_text)
            print("✨ 台本生成に成功しました！")
            return data
        except Exception as e:
            print(f"⚠️ 接続エラーが発生しました: {e}")
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5  # 5秒、10秒と待機時間を延ばす
                print(f"⏳ {wait_time}秒後に自動リトライします...")
                time.sleep(wait_time)
            else:
                print("❌ サーバー混雑が解消されないため、フォールバック用の台本を使用します。")
                return {
                    "title": f"【{genre}ドラマ】{theme}",
                    "description": f"{theme}を描くオリジナルAIドラマ。#{genre} #AIドラマ #自動生成",
                    "script": f"静寂の中、物語が動き出す。今回は{theme}をテーマにした特別なストーリーをお届けします。"
                }

def create_voice_audio(script_text):
    """台本からナレーション音声（mp3）を自動生成"""
    print("🎙️ ナレーション音声を生成中...")
    tts = gTTS(text=script_text, lang='ja')
    audio_path = "narration.mp3"
    tts.save(audio_path)
    return audio_path

def create_ai_drama_video(audio_path):
    """FFmpegを使用して、背景画像・音声・字幕を合成した本格動画を作成"""
    print("🎬 動画と音声の合成処理を開始します...")
    video_path = "ai_drama_output.mp4"
    
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=0x111122:s=1280x720:r=30",
        "-i", audio_path,
        "-c:v", "libx264", "-c:a", "aac",
        "-shortest",
        "-pix_fmt", "yuv420p",
        video_path
    ]
    
    subprocess.run(cmd, check=True)
    print("✨ AIドラマ動画の生成が完了しました！")
    return video_path

def upload_to_youtube(video_path, metadata):
    """YouTube APIを使用して動画を投稿"""
    creds = Credentials.from_authorized_user_file("token.json")
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": ["AIドラマ", "Gemini", "YouTube自動化"],
            "categoryId": "24"
        },
        "status": {
            "privacyStatus": "unlisted"
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
    print("🎉 AIドラマ動画の自動アップロード成功！")
    print(f"🎬 動画ID: {response['id']}")
    print(f"🔗 URL: https://youtu.be/{response['id']}")
    print("====================================")

def main():
    restore_credentials()
    
    input_theme = os.environ.get("INPUT_VIDEO_TITLE")
    input_genre = os.environ.get("INPUT_VIDEO_GENRE", "ドラマ")
    input_tone  = os.environ.get("INPUT_VIDEO_TONE", "シネマティック")
    
    if not input_theme:
        raise ValueError("❌ スプレッドシートのA2セル（テーマ）が空です。")
        
    print(f"🎯 AIドラマ制作開始 -> テーマ: {input_theme} (ジャンル: {input_genre}, 雰囲気: {input_tone})")
    
    metadata = generate_script_with_gemini(input_theme, input_genre, input_tone)
    audio_path = create_voice_audio(metadata["script"])
    video_path = create_ai_drama_video(audio_path)
    upload_to_youtube(video_path, metadata)

if __name__ == "__main__":
    main()
