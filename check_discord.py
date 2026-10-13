"""Discord の返信（1〜3）を確認し、選ばれた企画の本文を添えて動画生成ワークフローを起動する"""
import os
import re
import sys
import logging
from datetime import datetime, timezone, timedelta

import requests

from discord_utils import get_messages, add_reaction, post_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("check_discord")

GH_PAT = os.getenv("GH_PAT")
GH_REPO = os.getenv("GITHUB_REPOSITORY")
WORKFLOW_FILE = os.getenv("WORKFLOW_FILE", "auto_pipeline.yml")
DISPATCH_REF = os.getenv("GITHUB_REF_NAME") or "main"
ALLOWED_USER = (os.getenv("DISCORD_ALLOWED_USER_ID") or "").strip()
MAX_AGE = timedelta(hours=float(os.getenv("REPLY_MAX_AGE_HOURS", "12")))

DONE_EMOJI = "✅"
FAIL_EMOJI = "❌"
ZEN2HAN = str.maketrans("１２３", "123")


def parse_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def already_handled(msg: dict) -> bool:
    """Bot 自身が ✅ / ❌ を付けたメッセージは処理済み（二重起動防止）。"""
    return any(r.get("me") and r.get("emoji", {}).get("name") in (DONE_EMOJI, FAIL_EMOJI)
               for r in msg.get("reactions", []))


def find_reply_and_plan(messages: list[dict]):
    """新しい順のメッセージから、未処理の返信と、それに対応する企画本文を探す。"""
    now = datetime.now(timezone.utc)
    for i, msg in enumerate(messages):
        author = msg.get("author", {})
        if author.get("bot"):
            continue
        if ALLOWED_USER and author.get("id") != ALLOWED_USER:
            continue
        content = msg.get("content", "").strip().translate(ZEN2HAN)
        if content not in ("1", "2", "3"):
            if content == "" and not msg.get("attachments"):
                log.warning("内容が空のメッセージがあります。Bot の MESSAGE CONTENT INTENT が"
                            "有効になっているか確認してください。")
            continue
        if now - parse_time(msg["timestamp"]) > MAX_AGE:
            log.info(f"古い返信のため無視します: {content}")
            continue
        if already_handled(msg):
            log.info(f"処理済みの返信です: {content}")
            return None  # これより古い返信は見る必要がない

        # 返信より前に Bot が投稿した「【企画N】」を探す（messages は新しい順）
        pattern = re.compile(rf"^\**【企画{content}】")
        for older in messages[i + 1:]:
            if older.get("author", {}).get("bot") and pattern.match(older.get("content", "")):
                return msg, content, older["content"]
        log.warning(f"返信 {content} に対応する企画案が見つかりませんでした。")
        return msg, content, None
    return None


def extract_title(plan_text: str) -> str:
    m = re.search(r"【企画\d】([^*\n]+)", plan_text)
    return m.group(1).strip() if m else ""


def trigger_video_workflow(title: str, plan_text: str) -> bool:
    if not GH_PAT or not GH_REPO:
        log.error("GH_PAT または GITHUB_REPOSITORY が設定されていません。")
        return False
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    payload = {
        "ref": DISPATCH_REF,
        "inputs": {
            "action_mode": "generate_video",
            "video_title": title[:200],
            "plan_text": plan_text[:20000],
        },
    }
    res = requests.post(url, json=payload, timeout=30, headers={
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if res.status_code == 204:
        log.info(f"動画生成ワークフロー（{WORKFLOW_FILE}）を起動しました。")
        return True
    log.error(f"GitHub API 起動失敗 {res.status_code}: {res.text[:500]}")
    if res.status_code == 404:
        log.error(f"WORKFLOW_FILE（{WORKFLOW_FILE}）が実際のファイル名と一致しているか、"
                  "GH_PAT に Actions の書き込み権限があるか確認してください。")
    return False


def main():
    log.info("--- Discord 返信チェック開始 ---")
    found = find_reply_and_plan(get_messages(limit=50))
    if not found:
        log.info("新しい返信はありませんでした。")
        return

    msg, choice, plan_text = found
    if not plan_text:
        add_reaction(msg["id"], FAIL_EMOJI)
        post_message(f"⚠️ 企画{choice}の本文が見つからなかったため、動画生成を開始できませんでした。")
        return

    title = extract_title(plan_text)
    # 先にリアクションを付けてから起動（次回のチェックで二重起動しないように）
    add_reaction(msg["id"], DONE_EMOJI)
    if trigger_video_workflow(title, plan_text):
        post_message(f"🎬 企画{choice}「{title}」の動画生成を開始しました。完了まで1〜2時間ほどかかります。")
    else:
        add_reaction(msg["id"], FAIL_EMOJI)
        post_message(f"⚠️ 企画{choice}の動画生成ワークフローを起動できませんでした。Actions のログを確認してください。")
        sys.exit(1)


if __name__ == "__main__":
    main()
