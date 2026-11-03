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
# 再生リストへの追加には https://www.googleapis.com/auth/youtube の許可が必要。
# トークンに付いている許可をそのまま使うため、更新時には scopes を指定しない（指定すると許可の不足でエラーになる）
PLAYLIST_SHORTS = os.getenv("PLAYLIST_SHORTS", "false").lower() == "true"  # ショートも再生リストに入れるか


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


def upload_video(yt, path: Path, title: str, description: str, tags: list[str]) -> str:
    """動画を非公開でアップロードして、動画IDを返す（一時的なエラーは再試行）。"""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    body = {
        "snippet": {
            "title": title,
            "description": description.replace("<", "＜").replace(">", "＞")[:5000],
            "tags": clean_tags(tags),
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
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response, retries = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log.info(f"アップロード中（{path.name}）… {int(status.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retries < 8:
                retries += 1
                wait = min(60, 2 ** retries) + random.random()
                log.warning(f"一時的なエラー {e.resp.status}。{wait:.0f}秒後に再試行します。")
                time.sleep(wait)
                continue
            raise
    return response["id"]


def find_or_create_playlist(yt, name: str) -> str:
    """名前が一致する自分の再生リストを探し、なければ公開の再生リストとして作る。"""
    token = None
    while True:
        res = yt.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=token).execute()
        for pl in res.get("items", []):
            if pl["snippet"]["title"] == name:
                return pl["id"]
        token = res.get("nextPageToken")
        if not token:
            break
    body = {"snippet": {"title": name, "description": f"{name}の動画をまとめています。", "defaultLanguage": "ja"},
            "status": {"privacyStatus": "public"}}
    pl = yt.playlists().insert(part="snippet,status", body=body).execute()
    log.info(f"再生リスト「{name}」を作成しました。")
    return pl["id"]


def add_to_playlist(yt, name: str, video_ids: list[str]) -> str:
    """動画を再生リストに追加し、Discord に載せる一文を返す。失敗しても投稿自体には影響させない。"""
    if not name or not video_ids:
        return ""
    from googleapiclient.errors import HttpError
    try:
        pid = find_or_create_playlist(yt, name)
        for vid in video_ids:
            yt.playlistItems().insert(part="snippet", body={
                "snippet": {"playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": vid}}}).execute()
        log.info(f"再生リスト「{name}」に {len(video_ids)} 本追加しました。")
        return f"\n📂 再生リスト「{name}」に追加しました（動画を公開すると、再生リストにも表示されます）"
    except HttpError as e:
        if e.resp.status in (401, 403):
            log.warning(f"再生リストの権限がありません: {e}")
            return ("\n⚠️ 再生リストに追加できませんでした（YouTube の許可が足りません）。Playground でスコープ "
                    "`https://www.googleapis.com/auth/youtube` を付けてトークンを取り直してください。")
        log.warning(f"再生リストへの追加に失敗: {e}")
        return f"\n⚠️ 再生リスト「{name}」への追加に失敗しました。手動で追加してください。"
    except Exception as e:
        log.warning(f"再生リストへの追加に失敗: {e}")
        return f"\n⚠️ 再生リスト「{name}」への追加に失敗しました。手動で追加してください。"


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
                        token_uri="https://oauth2.googleapis.com/token")
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)

    title = meta["title"].replace("<", "＜").replace(">", "＞")[:100]
    try:
        video_id = upload_video(yt, video, title, meta["description"], meta.get("tags", []))
    except Exception as e:
        log.error(f"アップロード失敗: {e}")
        notify(f"⚠️ YouTube へのアップロードに失敗しました: {str(e)[:300]}")
        sys.exit(1)
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

    # ショート（本編へのリンクを説明欄に入れる）
    short_lines, short_ids = [], []
    shorts_path = WORK / "shorts.json"
    shorts = json.loads(shorts_path.read_text(encoding="utf-8")) if shorts_path.exists() else []
    for i, sh in enumerate(shorts, 1):
        path = WORK / sh["file"]
        if not path.exists():
            continue
        try:
            desc = sh["description"].replace("{MAIN_URL}", f"https://youtu.be/{video_id}")
            sid = upload_video(yt, path, sh["title"].replace("<", "＜").replace(">", "＞")[:100], desc,
                               meta.get("tags", []))
            log.info(f"ショート{i} アップロード完了: https://youtu.be/{sid}")
            short_ids.append(sid)
            short_lines.append(f"📱 ショート{i}「{sh['title']}」\nhttps://studio.youtube.com/video/{sid}/edit")
        except Exception as e:
            log.warning(f"ショート{i}のアップロードに失敗: {e}")
            short_lines.append(f"⚠️ ショート{i}のアップロードに失敗しました（Artifacts の {sh['file']} を手動で投稿できます）")

    # ジャンル別の再生リストに追加（本編。PLAYLIST_SHORTS=true ならショートも）
    playlist_note = add_to_playlist(yt, meta.get("playlist", ""), [video_id] + (short_ids if PLAYLIST_SHORTS else []))

    minutes = meta.get("duration_sec", 0) / 60
    notify(f"✅ **非公開でアップロードしました**（{minutes:.1f}分）\n"
           f"「{title}」\nhttps://studio.youtube.com/video/{video_id}/edit{thumb_note}{playlist_note}\n"
           + ("\n".join(short_lines) + "\n" if short_lines else "")
           + "内容を確認してから YouTube Studio で公開してください。"
           + ("ショートは本編を公開してから公開すると、説明欄のリンクが有効になります。" if short_lines else ""))


if __name__ == "__main__":
    main()
