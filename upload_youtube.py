"""完成した動画を YouTube に「非公開」でアップロードし、Discord に通知する"""
import os
import sys
import json
import time
import random
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("upload")

WORK = Path(os.getenv("WORK_DIR", "work"))
CATEGORY_ID = os.getenv("YT_CATEGORY_ID", "27")          # 27 = 教育, 24 = エンターテイメント, 22 = ブログ
SYNTHETIC = os.getenv("YT_SYNTHETIC", "true").lower() == "true"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def notify(text: str) -> None:
    if not os.getenv("DISCORD_BOT_TOKEN"):
        return
    try:
        from discord_utils import post_message
        post_message(text)
    except Exception as e:
        log.warning(f"Discord 通知に失敗: {e}")


def clean_tags(tags: list[str]) -> list[str]:
    """タグは合計500文字まで。< > は使えない。"""
    out, total = [], 0
    for t in tags:
        t = t.replace("<", "").replace(">", "").replace(",", "").strip()
        cost = len(t) + (2 if " " in t else 0) + 1
        if t and total + cost <= 480:
            out.append(t)
            total += cost
    return out


def main():
    cid, secret, refresh = (os.getenv("YT_CLIENT_ID"), os.getenv("YT_CLIENT_SECRET"),
                            os.getenv("YT_REFRESH_TOKEN"))
    if not (cid and secret and refresh):
        log.info("YouTube の認証情報が未設定のため、アップロードをスキップします。")
        notify("🎞️ 動画が完成しました（YouTube 認証情報が未設定のため、Actions の Artifacts からダウンロードしてください）。")
        return

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    video = WORK / "final.mp4"
    meta = json.loads((WORK / "metadata.json").read_text(encoding="utf-8"))

    creds = Credentials(None, refresh_token=refresh, client_id=cid, client_secret=secret,
                        token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)

    title = meta["title"].replace("<", "＜").replace(">", "＞")[:100]
    body = {
        "snippet": {
            "title": title,
            "description": meta["description"].replace("<", "＜").replace(">", "＞")[:5000],
            "tags": clean_tags(meta.get("tags", [])),
            "categoryId": CATEGORY_ID,
            "defaultLanguage": "ja",
            "defaultAudioLanguage": "ja",
        },
        "status": {
            "privacyStatus": "private",       # 自分で確認してから YouTube Studio で公開する
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": SYNTHETIC,  # AI 生成コンテンツの開示
        },
    }

    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    response, retries = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log.info(f"アップロード中… {int(status.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retries < 8:
                retries += 1
                wait = min(60, 2 ** retries) + random.random()
                log.warning(f"一時的なエラー {e.resp.status}。{wait:.0f}秒後に再試行します。")
                time.sleep(wait)
                continue
            log.error(f"アップロード失敗: {e}")
            notify(f"⚠️ YouTube へのアップロードに失敗しました: {str(e)[:300]}")
            sys.exit(1)

    video_id = response["id"]
    log.info(f"アップロード完了: https://youtu.be/{video_id}")

    thumb = WORK / "thumbnail.jpg"
    thumb_note = ""
    if thumb.exists():
        try:
            yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg")).execute()
            log.info("サムネイルを設定しました。")
        except HttpError as e:
            log.warning(f"サムネイル設定に失敗（チャンネルの電話番号認証が必要な場合があります）: {e}")
            thumb_note = "\n※ サムネイルは設定できませんでした（Artifacts の thumbnail.jpg を手動で設定してください）。"

    minutes = meta.get("duration_sec", 0) / 60
    notify(f"✅ **非公開でアップロードしました**（{minutes:.1f}分）\n"
           f"「{title}」\nhttps://studio.youtube.com/video/{video_id}/edit\n"
           f"内容を確認してから YouTube Studio で公開してください。{thumb_note}")


if __name__ == "__main__":
    main()
