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
    """Gemini APIを使用して、著作権完全クリアのオリジナルAIドラマ台本を生成"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ GEMINI_API_KEY が設定されていません。")

    client = genai.Client(api_key=api_key)
    
    # f-stringではなく通常の文字列結合を使用し、構文エラーを完全に防止
    prompt = (
        "あなたはプロのAIアニメ・ドラマ脚本家です。\n"
        "以下の条件をもとに、YouTubeのAIドラマ・解説動画用のデータを完全にオリジナルの世界観で作成してください。\n"
        "※実在の人物、既存のアニメや映画のキャラクター、商標権・著作権に触れる固有名詞は一切使用しないでください。すべて完全オリジナルの設定にすること。\n\n"
        f"- メインテーマ: {theme}\n"
        f"- ジャンル: {genre}\n"
        f"- 雰囲気・トーン: {tone}\n\n"
        "出力フォーマットは必ず以下のJSON形式のみで返答してください。余計な解説文やマークダウンの前後への文字追加は一切禁止です。\n"
        "{\n"
        '  "title": "目を引く30文字以内のYouTube動画タイトル",\n'
        '  "description": "動画の概要欄テキスト（ハッシュタグ3つ含む。AIドラマ、オリジナルアニメ等）",\n'
        '  "scenes": [\n'
        '    {"text": "第1シーンのナレーション原稿（引き込む導入部分）"},\n'
        '    {"text": "第2シーンのナレーション原稿（物語の核心や展開）"},\n'
        '    {"text": "第3シーンのナレーション原稿（感動的、または意味深な結末）"}\n'
        "  ]\n"
        "}"
    )

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
            print("✨ 本格AIドラマ台本の生成に成功しました！")
            return data
        except Exception as e:
            print(f"⚠️ 接続エラーが発生しました: {e}")
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5
                print(f"⏳ {wait_time}秒後に自動リトライします...")
                time.sleep(wait_time)
            else:
                print("❌ フォールバック用の本格台本を使用します。")
                return {
                    "title": f"【{genre}ドラマ】{theme}",
                    "description": f"{theme}を描く完全オリジナルAIドラマ。#{genre} #AIドラマ #オリジナルアニメ",
                    "scenes": [
                        {"text": f"静寂に包まれた世界。今回は{theme}をめぐる、誰も見たことのない物語をお届けします。"},
                        {"text": "定められた運命の歯車が、音を立てて狂い始めます。隠された真実とは一体何なのでしょうか。"},
                        {"text": "扉の向こうに広がっていたのは、想像を絶する景色でした。物語は、ここから新たな局面を迎えます。"}
                    ]
                }

def create_voice_audio(scenes):
    """各シーンの台本を結合して1つの高品質な音声（mp3）を生成"""
    print("🎙 ナレーション音声をシーンごとに生成・結合中...")
    
    combined_script = ""
    for scene in scenes:
        combined_script += scene["text"] + " ... "
        
    formatted_text = combined_script.replace("。", "。... ").replace("、", "、... ")
    tts = gTTS(text=formatted_text, lang='ja', slow=False)
    audio_path = "narration.mp3"
    tts.save(audio_path)
    return audio_path

def find_japanese_font():
    """利用可能な日本語フォントのパスを自動検出"""
    candidate_paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]
    for path in candidate_paths:
        if os.path.exists(path):
            return path
    return ""

def create_ai_drama_video(audio_path, metadata):
    """FFmpegを使用して、シネマティックな背景・字幕・音声を完全同期させた本格動画を作成"""
    print("🎬 本格的な動画・字幕の合成処理を開始します...")
    output_path = "ai_drama_output.mp4"
    
    font_path = find_japanese_font()
    title = metadata["title"]
    
    font_opt = f"fontfile={font_path}:" if font_path else ""
    
    # FFmpegコマンドで複数シーンの字幕を焼き込む
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=0x11071f:s=1280x720:r=30",
        "-i", audio_path,
        "-filter_complex", 
        f"drawtext={font_opt}text='{title}':fontcolor=0x00ffff:fontsize=42:x=(w-text_w)/2:y=80:enable='between(t,0,999)'," \
        f"drawtext={font_opt}text='{metadata['scenes'][0]['text'][:30]}...':fontcolor=white:fontsize=32:x=(w-text_w)/2:y=350:enable='between(t,0,10)'," \
        f"drawtext={font_opt}text='{metadata['scenes'][1]['text'][:30]}...':fontcolor=white:fontsize=32:x=(w-text_w)/2:y=430:enable='between(t,10,22)'," \
        f"drawtext={font_opt}text='{metadata['scenes'][2]['text'][:30]}...':fontcolor=white:fontsize=32:x=(w-text_w)/2:y=510:enable='between(t,22,45)'",
        "-c:v", "libx264", "-c:a", "aac",
        "-af", "apad=pad_dur=2.0",
        "-shortest",
        "-pix_fmt", "yuv420p",
        output_path
    ]
    
    subprocess.run(cmd, check=True)
    print("✨ フルスペックAIドラマ動画の生成が完了しました！")
    return output_path

def upload_to_youtube(video_path, metadata):
    """YouTube APIを使用して動画を限定公開で自動投稿"""
    creds = Credentials.from_authorized_user_file("token.json")
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": ["AIドラマ", "オリジナルアニメ", "Gemini", "YouTube自動化"],
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
    print("🎉 フルスペックAIドラマ動画の自動アップロード成功！")
    print(f"🎬 動画ID: {response['id']}")
    print(f"🔗 URL: https://youtu.be/{response['id']}")
    print("====================================")

def main():
    restore_credentials()
    
    input_theme = os.environ.get("INPUT_VIDEO_TITLE")
    input_genre = os.environ.get("INPUT_VIDEO_GENRE", "AIドラマ")
    input_tone  = os.environ.get("INPUT_VIDEO_TONE", "シネマティック")
    
    if not input_theme:
        raise ValueError("❌ スプレッドシートのA2セル（テーマ）が空です。")
        
    print(f"🎯 フルスペックAIドラマ制作開始 -> テーマ: {input_theme} (ジャンル: {input_genre}, 雰囲気: {input_tone})")
    
    metadata = generate_script_with_gemini(input_theme, input_genre, input_tone)
    audio_path = create_voice_audio(metadata["scenes"])
    video_path = create_ai_drama_video(audio_path, metadata)
    upload_to_youtube(video_path, metadata)

if __name__ == "__main__":
    main()
