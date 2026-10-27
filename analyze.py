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
# 週1回の特別枠（企画3をその枠の企画にする）。曜日は 0=月曜 … 5=土曜, 6=日曜。-1 にすると無効
SENIOR_WEEKDAY = int(os.getenv("SENIOR_WEEKDAY", "5"))      # 50歳以上向け
SELFHELP_WEEKDAY = int(os.getenv("SELFHELP_WEEKDAY", "1"))  # 心と体を整える自己啓発（火曜）。若い世代の入口にもする

SENIOR_RULE = """
# 今日は週1回の「50歳以上向け」の日
- 3つ目の企画（plans の3番目）は、50代以上の男性に向けた企画にする。トレンドにこだわらなくてよい。
- テーマの例：年金・退職金・老後のお金、昭和の出来事や懐かしい話、日本や世界の歴史の裏話、自衛隊・防衛、
  定年後の暮らしや趣味、身近な制度の変更点、健康（医学的に根拠のある内容だけ。断定せず「リスクを下げる」と表現する）
- 投資の勧誘や、特定の商品・サービスをすすめる内容、不安をあおるだけの内容にはしない。
- タイトルは、この世代が自分ごととして思わずクリックしたくなる言葉にする（例：「60代から知らないと損する…」）。
- 1つ目と2つ目は、いつもどおりトレンドをもとにした企画にする。
"""

SELFHELP_RULE = """
# 今日は週1回の「心と体を整える」日
- 3つ目の企画（plans の3番目）は、心と体の調子を整えることをテーマにした自己啓発の企画にする。トレンドにこだわらなくてよい。
- 年齢を限定せず、幅広い世代が見られる内容にする。そのうえで、今の視聴者（50代以上が中心）だけでなく、
  20〜30代も「自分ごと」として見たくなる切り口を意識する。
- テーマの例：睡眠の質、ストレスとの付き合い方、自律神経、呼吸法、姿勢や肩こり、歩くなどの軽い運動の効果、
  食事と集中力、脳の疲れと休み方、スマホ疲れ、朝や夜の過ごし方、感情の整え方、マインドフルネス、孤独との向き合い方
- 必ず具体的な根拠を軸にする（研究結果、公的機関の情報、統計データ）。「前向きに生きよう」のような一般論だけの企画にはしない。
- 医学的に根拠のある内容だけにし、効果を断定しない（「〜が期待できる」「〜という研究がある」と表現する）。
  病気の診断や治療法、薬やサプリメントのすすめ、心の不調の治し方のような医療的な助言はしない。
  動画の最後で「不調が続くときは医師や専門家に相談を」と一言添える前提の企画にする。
- 根性論、怪しい健康法、特定の商品・サービスの宣伝につながる内容は避ける。
- タイトルは、世代を問わず思わずクリックしたくなる言葉にする（例：「寝る前の5分で睡眠の質が変わる理由」）。
- 1つ目と2つ目は、いつもどおりトレンドをもとにした企画にする。
"""

WEEKLY_SLOTS = [  # (曜日, Discord に出す印, AI への指示)
    (SENIOR_WEEKDAY, "🧓 50歳以上向けの週1企画", SENIOR_RULE),
    (SELFHELP_WEEKDAY, "🌱 心と体を整える週1企画", SELFHELP_RULE),
]


# ---------- 扱わないジャンル（スポーツ。特にバスケットボール） ----------
# トレンドと企画案の両方から取り除く。環境変数 EXCLUDE_KEYWORDS（カンマ区切り）で言葉を追加できる
EXCLUDE_KEYWORDS = [
    "バスケ", "バスケットボール", "NBA", "WNBA", "Bリーグ", "B.LEAGUE", "ダンク", "スリーポイント", "レイカーズ",
    "ウォリアーズ", "八村", "河村勇輝", "渡邊雄太", "富永啓生", "basketball",
    "スポーツ", "試合", "選手", "サッカー", "Jリーグ", "W杯", "ワールドカップ", "プレミアリーグ", "メッシ", "ロナウド",
    "野球", "MLB", "NPB", "プロ野球", "大谷翔平", "ドジャース", "甲子園", "ホームラン", "侍ジャパン",
    "テニス", "ゴルフ", "ラグビー", "バレーボール", "卓球", "陸上競技", "マラソン", "駅伝", "五輪", "オリンピック",
    "パラリンピック", "大相撲", "横綱", "ボクシング", "格闘技", "UFC", "RIZIN", "F1", "競馬", "競輪", "フィギュアスケート",
    "football", "soccer", "baseball", "tennis", "golf",
] + [k.strip() for k in os.getenv("EXCLUDE_KEYWORDS", "").split(",") if k.strip()]


def is_excluded(text: str) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in EXCLUDE_KEYWORDS)


def plan_text_all(p: dict) -> str:
    return " ".join([str(p.get(k, "")) for k in ("title", "hook", "summary", "why_now")] + [str(x) for x in p.get("outline", [])])


def today_slot():
    """今日が特別枠の曜日なら (印, 指示) を返す。"""
    wd = datetime.now(JST).weekday()
    for day, label, rule in WEEKLY_SLOTS:
        if day >= 0 and day == wd:
            return label, rule
    return None


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

def slot_rule() -> str:
    slot = today_slot()
    return slot[1] if slot else ""


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
- スポーツ（特にバスケットボール）に関する企画は出さない。試合・選手・チーム・リーグ・大会の話題も含めて、すべて対象外とする。
- 3つは互いにジャンルや切り口を変える。
{slot_rule()}
# 出力
次の形式の JSON のみを出力してください。
{{"plans": [
  {{"title": "動画タイトル案（32文字以内）",
    "hook": "冒頭15秒で何を言って視聴者を掴むか",
    "summary": "動画の内容（100〜150文字）",
    "outline": ["章1の内容", "章2の内容", "章3の内容", "章4の内容"],
    "why_now": "今この企画がウケる理由（参考にしたトレンド）"}}
]}}"""


def extract_plans(data) -> list[dict]:
    """AI の出力がどんな入れ子の形でも、title を持つ企画を取り出す。
    例: {"plans":[...]} / [{"plans":[...]}] / [{...}, {...}] / {"title":...}"""
    if isinstance(data, list):
        return [p for item in data for p in extract_plans(item)]
    if isinstance(data, dict):
        if data.get("title"):
            return [data]
        return [p for v in data.values() if isinstance(v, (list, dict)) for p in extract_plans(v)]
    return []


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
    plans = extract_plans(json.loads(repair_json(raw)))
    if not plans:
        raise RuntimeError(f"企画案を解析できませんでした: {raw[:300]}")
    return plans[:3]


def generate_valid_plans(prompt: str) -> list[dict]:
    """スポーツなど扱わないジャンルの企画が混ざっていたら、作り直す（最大3回）。"""
    best = []
    for attempt in range(3):
        plans = generate_plans(prompt)
        bad = [p for p in plans if is_excluded(plan_text_all(p))]
        if not bad and len(plans) >= 3:
            return plans
        log.warning(f"扱わないジャンルの企画が {len(bad)} 件あったため作り直します（{attempt + 1}回目）: "
                    + " / ".join(p.get("title", "") for p in bad))
        good = [p for p in plans if not is_excluded(plan_text_all(p))]
        if len(good) > len(best):
            best = good
        prompt += ("\n\n# 注意\n前回はスポーツに関する企画が含まれていました。スポーツ（特にバスケットボール）の企画は"
                   "1つも入れずに、3つすべて別のジャンルで作り直してください。")
    return best


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
    trends = [t for t in fetch_google_trends() if not is_excluded(t)]      # スポーツの話題は最初から渡さない
    popular = [p for p in fetch_youtube_popular() if not is_excluded(p)]
    try:
        plans = generate_valid_plans(build_prompt(trends, popular))
        if not plans:
            raise RuntimeError("条件に合う企画を作れませんでした。")
    except Exception as e:
        log.error(f"企画生成に失敗しました: {e}")
        post_message(f"⚠️ 今朝の企画案の生成に失敗しました。\n```{str(e)[:1500]}```")
        sys.exit(1)

    today = datetime.now(JST).strftime("%m/%d")
    slot = today_slot() if len(plans) >= 3 else None
    post_message(f"📅 **{today} の企画案です**\n作りたい企画の番号（1〜3）をこのチャンネルに送信してください。"
                 f"12時台までに返信すると、動画を作成して非公開でアップロードします。"
                 + (f"\n今日は「{slot[0]}」の日です（企画3）。" if slot else ""))
    for i, p in enumerate(plans, 1):
        text = format_plan(i, p)
        if slot and i == 3:
            text = text.replace("\n", f"\n{slot[0]}\n", 1)
        post_message(text)
    log.info("企画案の送信が完了しました。")


if __name__ == "__main__":
    main()
