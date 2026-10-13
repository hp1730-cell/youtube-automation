"""Discord REST API の共通処理（送信・取得・リアクション）"""
import os
import time
import logging
import urllib.parse

import requests

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
MAX_LEN = 1900  # Discord の上限は 2000 文字。余裕を持たせる


def _headers() -> dict:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN が設定されていません。")
    return {"Authorization": f"Bot {token}", "Content-Type": "application/json"}


def _channel() -> str:
    ch = os.getenv("DISCORD_CHANNEL_ID")
    if not ch:
        raise RuntimeError("DISCORD_CHANNEL_ID が設定されていません。")
    return ch


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """429（レート制限）を考慮したリクエスト。失敗時は例外を投げる。"""
    for _ in range(5):
        res = requests.request(method, url, headers=_headers(), timeout=30, **kwargs)
        if res.status_code == 429:
            wait = float(res.json().get("retry_after", 2))
            log.warning(f"Discord レート制限。{wait:.1f}秒待機します。")
            time.sleep(wait + 0.5)
            continue
        if res.status_code >= 400:
            # 送信失敗を握りつぶさない（以前のコードで企画案が届かなかった原因の一つ）
            raise RuntimeError(f"Discord API エラー {res.status_code}: {res.text[:500]}")
        return res
    raise RuntimeError("Discord API のレート制限が解除されませんでした。")


def split_text(text: str, limit: int = MAX_LEN) -> list[str]:
    """改行の位置でなるべく区切りながら、上限以下に分割する。"""
    chunks, buf = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # 1行が長すぎる場合は強制分割
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = ""
        buf += line
    if buf.strip():
        chunks.append(buf)
    return chunks


def post_message(text: str) -> list[dict]:
    """メッセージを送信（2000文字を超える場合は自動分割）。"""
    sent = []
    for chunk in split_text(text):
        res = _request("POST", f"{API}/channels/{_channel()}/messages", json={"content": chunk})
        sent.append(res.json())
    log.info(f"Discord に {len(sent)} 件のメッセージを送信しました。")
    return sent


def get_messages(limit: int = 50) -> list[dict]:
    """新しい順にメッセージを取得。"""
    res = _request("GET", f"{API}/channels/{_channel()}/messages", params={"limit": limit})
    return res.json()


def add_reaction(message_id: str, emoji: str) -> None:
    e = urllib.parse.quote(emoji)
    _request("PUT", f"{API}/channels/{_channel()}/messages/{message_id}/reactions/{e}/@me")
