"""実際のトレンド情報を集め、Groq で企画案を3つ作って Discord に送信する"""
import os
import re
import sys
import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import requests
from json_repair import repair_json

from discord_utils import post_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("analyze")

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
CHANNEL_THEME = os.getenv("CHANNEL_THEME") or "幅広いジャンルの雑学・解説（アニメ調イラスト＋ナレーション形式）"
JST = timezone(timedelta(hours=9))


# ---------- トレンド情報の取得（LLM は最新情報を知らないため、実データを渡す） ----------

def fetch_google_trends(n: int = 20) -> list[str]:
    """Google トレンド（日本）の急上昇ワード。APIキー不要。"""
    try:
        res = requests.get("https://trends.google.co.jp/trending/rss?geo=JP", timeout=20,
                           headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        root = ET.fromstring(res.content)
        words = [item.findtext("title") for item in root.iter("item")]
        words = [w for w in words if w][:n]
        log.info(f"Google トレンド: {len(words)} 件取得")
        return words
    except Exception as e:
        log.warning(f"Google トレンドの取得に失敗: {e}")
        return []


def fetch_youtube_popular(n: int = 25) -> list[str]:
    """YouTube の急上昇動画（日本）。YOUTUBE_API_KEY がある場合のみ。"""
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        return []
    try:
        res = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "snippet,statistics", "chart": "mostPopular", "regionCode": "JP",
                    "maxResults": n, "key": key},
            timeout=20,
        )
        res.raise_for_status()
        items = []
        for v in res.json().get("items", []):
            views = int(v.get("statistics", {}).get("viewCount", 0))
            items.append(f"{v['snippet']['title']}（{v['snippet']['channelTitle']} / {views:,}回再生）")
        log.info(f"YouTube 急上昇: {len(items)} 件取得")
        return items
    except Exception as e:
        log.warning(f"YouTube 急上昇の取得に失敗: {e}")
        return []


# ---------- 企画立案 ----------

def build_prompt(trends: list[str], popular: list[str]) -> str:
    today = datetime.now(JST).strftime("%Y年%m月%d日")
    trend_txt = "\n".join(f"- {t}" for t in trends) or "（取得できませんでした）"
    pop_txt = "\n".join(f"- {p}" for p in popular) or "（取得できませんでした）"
    return f"""あなたは日本のYouTube解説チャンネルの敏腕プロデューサーです。
今日は{today}です。以下の実データを参考に、今日制作すべき動画企画を3つ立案してください。

# チャンネルの方向性
{CHANNEL_THEME}

# 今日の Google 急上昇ワード（日本）
{trend_txt}

# 今日の YouTube 急上昇動画（日本）
{pop_txt}

# 条件
- 動画は約8分、AI生成のイラストとナレーションで構成する。実写映像・有名人の顔・他社の映像は使えない前提で成立する企画にする。
- 速報性に頼りすぎず、数週間は見られる「解説・雑学・まとめ」の切り口にする。
- 事実確認が難しいゴシップ、事件の被害者、政治的に偏った主張は避ける。
- 3つは互いにジャンルや切り口を変える。

# 出力
次の形式の JSON のみを出力してください。
{{"plans": [
  {{"title": "動画タイトル案（32文字以内）",
    "hook": "冒頭15秒で何を言って視聴者を掴むか",
    "summary": "動画の内容（100〜150文字）",
    "outline": ["章1の内容", "章2の内容", "章3の内容", "章4の内容"],
    "why_now": "今この企画がウケる理由（参考にしたトレンド）"}}
]}}"""


def generate_plans(prompt: str) -> list[dict]:
    from groq import Groq
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
        response_format={"type": "json_object"},
    )
    raw = completion.choices[0].message.content or ""
    data = json.loads(repair_json(raw))
    plans = data.get("plans", []) if isinstance(data, dict) else data
    plans = [p for p in plans if isinstance(p, dict) and p.get("title")]
    if not plans:
        raise RuntimeError(f"企画案を解析できませんでした: {raw[:300]}")
    return plans[:3]


def format_plan(no: int, p: dict) -> str:
    outline = "\n".join(f"　{i + 1}. {o}" for i, o in enumerate(p.get("outline", [])))
    text = (
        f"**【企画{no}】{p.get('title', '')}**\n"
        f"🎣 **つかみ**: {p.get('hook', '')}\n"
        f"📝 **概要**: {p.get('summary', '')}\n"
        f"📚 **構成**:\n{outline}\n"
        f"📈 **今ウケる理由**: {p.get('why_now', '')}"
    )
    return text[:1900]  # 返信チェック側で1メッセージ＝1企画として読むため、分割させない


def main():
    trends = fetch_google_trends()
    popular = fetch_youtube_popular()
    try:
        plans = generate_plans(build_prompt(trends, popular))
    except Exception as e:
        log.error(f"企画生成に失敗しました: {e}")
        post_message(f"⚠️ 今朝の企画案の生成に失敗しました。\n```{str(e)[:1500]}```")
        sys.exit(1)

    today = datetime.now(JST).strftime("%m/%d")
    post_message(f"📅 **{today} の企画案です**\n作りたい企画の番号（1〜3）をこのチャンネルに送信してください。"
                 f"12時台までに返信すると、動画を作成して非公開でアップロードします。")
    for i, p in enumerate(plans, 1):
        post_message(format_plan(i, p))
    log.info("企画案の送信が完了しました。")


if __name__ == "__main__":
    main()
