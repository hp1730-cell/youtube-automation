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

from discord_utils import post_message, get_messages, get_history

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("analyze")

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
CHANNEL_THEME = os.getenv("CHANNEL_THEME") or "幅広いジャンルの雑学・解説（アニメ調イラスト＋ナレーション形式）"
JST = timezone(timedelta(hours=9))
# 週1回の特別枠（企画3をその枠の企画にする）。曜日は 0=月曜 … 5=土曜, 6=日曜。-1 にすると無効
SENIOR_WEEKDAY = int(os.getenv("SENIOR_WEEKDAY", "5"))      # 50歳以上向け
SELFHELP_WEEKDAY = int(os.getenv("SELFHELP_WEEKDAY", "1"))  # 心・考え方の自己啓発（火曜）。若い世代の入口にもする

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
# 今日は週1回の「心・考え方」の自己啓発の日
- 3つ目の企画（plans の3番目）は、考え方や心の持ち方をテーマにした自己啓発の企画にする。トレンドにこだわらなくてよい。
- 企画のイメージ：「うまくいく人の7つの習慣」「人生が好転する考え方」「〇〇と考えるだけでうまくいく」
  「落ち込みやすい人がやめるべき思考のクセ」「成功する人が口にしない言葉」のような、心・思考・習慣の話。
- テーマの例：思考のクセの直し方、物事の受け止め方、自己肯定感、人間関係の悩みの考え方、失敗との向き合い方、
  感謝や言葉の習慣、目標の立て方、比べないための考え方、怒りや不安との付き合い方、決断力、続ける力
- 年齢を限定せず、幅広い世代が見られる内容にする。20〜30代も「自分ごと」として見たくなる切り口を意識する。
- 説得力を出すため、具体的な裏付けを入れる（心理学の研究や有名な実験、偉人・経営者の実際の言葉やエピソード）。
  ただし、裏付けのない話を「研究で証明された」と言い切らない。
- 断定的な「必ず成功する法則」、引き寄せの法則のような根拠のない話、怪しいセミナー・副業・情報商材につながる内容、
  心の病気の診断や治し方のような医療的な助言は避ける。
- 既存の本のタイトル（『7つの習慣』など）をそのまま使わず、オリジナルのタイトルにする。
- 1つ目と2つ目は、いつもどおりトレンドをもとにした企画にする。
"""

WEEKLY_SLOTS = [  # (曜日, Discord に出す印, AI への指示)
    (SENIOR_WEEKDAY, "🧓 50歳以上向けの週1企画", SENIOR_RULE),
    (SELFHELP_WEEKDAY, "🌱 心と考え方の週1企画", SELFHELP_RULE),
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


# ---------------------------------------------------------------- 過去の動画と似た企画を出さない

# タイトルによく出るが、話題の中身を表さない言葉（似ているかの判定では数えない）
GENERIC_WORDS = {"日本", "理由", "真相", "秘密", "意外", "本当", "驚き", "衝撃", "解説", "まとめ", "世界", "歴史", "最新",
                 "方法", "話題", "知識", "雑学", "事実", "正体", "仕組", "仕組み", "結末", "ヤバ", "シニア", "人生"}


def past_titles() -> list[str]:
    """Discord の「アップロードしました」の通知から、これまでに作った動画（本編・ショート）のタイトルを集める。"""
    titles = []
    try:
        for m in get_history(1500):
            text = m.get("content", "")
            if "アップロードしました" not in text:
                continue
            titles += re.findall(r"^「(.+?)」$", text, re.M)              # 本編
            titles += re.findall(r"ショート\d+「(.+?)」", text)          # ショート
    except Exception as e:
        log.warning(f"過去の動画タイトルを取得できませんでした: {e}")
    titles = list(dict.fromkeys(t.strip() for t in titles if t.strip()))
    log.info(f"過去の動画 {len(titles)} 本と重ならないように企画します。")
    return titles


def _norm(text: str) -> str:
    return re.sub(r"[\s　!！?？、。,.・「」『』【】（）()\[\]\-ー〜~…#＃:：/／|｜\"'“”]", "", text or "").lower()


def _key_terms(text: str) -> set[str]:
    """話題の中身を表す言葉（2文字以上のカタカナ・漢字・英数字のまとまり）。"""
    terms = set(re.findall(r"[ァ-ヴー]{2,}|[一-龥々]{2,}|[a-zA-Z0-9][a-zA-Z0-9\-]+", text or ""))
    return {t.lower() for t in terms if t not in GENERIC_WORDS}


def _bigrams(text: str) -> set[str]:
    t = _norm(text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similar_past(title: str, past: list[str]) -> str | None:
    """過去のタイトルと「同じような内容」なら、そのタイトルを返す。
    文字の重なりが3割以上、または話題の言葉が2つ以上共通（例: F-35・重り）なら似ているとみなす。"""
    b, k = _bigrams(title), _key_terms(title)
    for old in past:
        ob = _bigrams(old)
        jac = len(b & ob) / max(1, len(b | ob))
        if jac >= 0.3 or len(k & _key_terms(old)) >= 2:
            return old
    return None


def ai_duplicates(plans: list[dict], past: list[str]) -> dict[str, str]:
    """AI に「過去の動画と同じ内容か」を判定させる。{企画タイトル: 似ている過去のタイトル} を返す。
    同じ題材でも切り口が大きく違えば別物とする。失敗したら空（文字の重なりの判定だけになる）。"""
    if not past or not plans:
        return {}
    try:
        from groq import Groq
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        cand = "\n".join(f"{i}. {p.get('title', '')}：{p.get('summary', '')[:120]}" for i, p in enumerate(plans))
        old = "\n".join(f"- {t}" for t in past[:150])
        prompt = f"""YouTube チャンネルの新しい企画案が、過去に作った動画と「同じような内容」かを判定してください。
同じ題材で、視聴者が「前にも見た」と感じる内容なら重複です（例:「F-35に重りが積まれた理由」と「F-35がレーダーの代わりに重りを積んで納入」）。
同じ題材でも、話の中心がまったく違えば重複ではありません（例:「F-35の重り」と「F-35の価格の決まり方」）。

# 新しい企画案
{cand}

# 過去の動画
{old}

# 出力
JSON のみ: {{"duplicates": [{{"index": 企画案の番号, "past": "似ている過去の動画タイトル"}}]}}（重複がなければ空の配列）"""
        res = client.chat.completions.create(model=GROQ_MODEL, temperature=0,
                                             messages=[{"role": "user", "content": prompt}],
                                             response_format={"type": "json_object"})
        data = json.loads(repair_json(res.choices[0].message.content or "{}"))
        out = {}
        for d in data.get("duplicates", []) if isinstance(data, dict) else []:
            i = d.get("index")
            if isinstance(i, int) and 0 <= i < len(plans):
                out[plans[i].get("title", "")] = str(d.get("past", ""))
        return out
    except Exception as e:
        log.warning(f"AI による重複チェックに失敗（文字の重なりだけで判定します）: {e}")
        return {}


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


def build_prompt(trends: list[str], popular: list[str], past: list[str] | None = None) -> str:
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
- 動画は約8分、無料の実写の動画素材・写真とナレーションで構成する。有名人の顔・他社の映像（テレビ番組など）は使えない前提で成立する企画にする。
- 速報性に頼りすぎず、数週間は見られる「解説・雑学・まとめ」の切り口にする。
- 事実確認が難しいゴシップ、事件の被害者、政治的に偏った主張は避ける。
- スポーツ（特にバスケットボール）に関する企画は出さない。試合・選手・チーム・リーグ・大会の話題も含めて、すべて対象外とする。
- 3つは互いにジャンルや切り口を変える。
{slot_rule()}{past_rule(past or [])}
# 出力
次の形式の JSON のみを出力してください。
{{"plans": [
  {{"title": "動画タイトル案（32文字以内）",
    "hook": "冒頭15秒で何を言って視聴者を掴むか",
    "summary": "動画の内容（100〜150文字）",
    "outline": ["章1の内容", "章2の内容", "章3の内容", "章4の内容"],
    "why_now": "今この企画がウケる理由（参考にしたトレンド）"}}
]}}"""


def past_rule(past: list[str]) -> str:
    if not past:
        return ""
    lst = "\n".join(f"- {t}" for t in past[:80])
    return ("\n# これまでに作った動画（同じ話題・同じ切り口の企画は出さない）\n"
            "下の動画と同じ題材（同じ機体・同じ制度・同じ出来事など）を扱う企画は禁止。"
            "別の題材を選ぶこと。\n" + lst + "\n")


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


def generate_valid_plans(prompt: str, past: list[str] | None = None) -> list[dict]:
    """スポーツなど扱わないジャンルの企画や、過去の動画と似た企画が混ざっていたら、作り直す（最大4回）。
    作り直しで出た良い企画は取っておき、合わせて3つそろえる。週1の特別枠の日は、3つ目を特別枠の企画にする。"""
    past = past or []
    has_slot = bool(today_slot())
    main_pool: list[dict] = []   # 企画1・2（特別枠がない日は1〜3）
    slot_pool: list[dict] = []   # 特別枠（企画3）

    def enough() -> bool:
        return (len(main_pool) >= 2 and slot_pool) if has_slot else len(main_pool) >= 3

    def result() -> list[dict]:
        if has_slot and slot_pool:
            slot_pool[0]["_slot"] = True  # 「特別枠の日です」の表示を付ける目印
        return (main_pool[:2] + slot_pool[:1]) if has_slot else main_pool[:3]

    for attempt in range(4):
        plans = generate_plans(prompt)
        ai_dup = ai_duplicates([p for p in plans if not is_excluded(plan_text_all(p))], past)
        sports, dup = [], []
        for i, p in enumerate(plans):
            title = p.get("title", "")
            taken = [g.get("title", "") for g in main_pool + slot_pool]
            if is_excluded(plan_text_all(p)):
                sports.append(title)
            elif (old := ai_dup.get(title) or similar_past(title, past) or similar_past(title, taken)):
                dup.append(f"{title}（←「{old}」と似ている）")
            elif has_slot and i == 2:
                slot_pool.append(p)
            else:
                main_pool.append(p)
        if enough():
            return result()
        notes = []
        if sports:
            log.warning("扱わないジャンルの企画: " + " / ".join(sports))
            notes.append("スポーツ（特にバスケットボール）の企画は1つも入れない。")
        if dup:
            log.warning("過去の動画と似ている企画: " + " / ".join(dup))
            notes.append("次の企画は過去の動画と題材が同じだったので、まったく別の題材にする: " + " / ".join(dup))
        if main_pool or slot_pool:
            notes.append("次の企画はすでに採用済みなので、これらとも別の題材にする: "
                         + " / ".join(g.get("title", "") for g in main_pool + slot_pool))
        prompt += f"\n\n# 作り直し（{attempt + 1}回目）\n" + "\n".join(f"- {n}" for n in notes) + \
                  "\n3つすべて、条件を満たす新しい企画を出してください。"
    out = result()
    if has_slot and not slot_pool:  # 特別枠が作れなかったときは、普通の企画で埋める
        out = main_pool[:3]
    return out


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


def already_sent_today() -> bool:
    """今日の企画案がすでに Discord に送られているか（予備のタイマーで二重に送らないため）。"""
    marker = f"{datetime.now(JST).strftime('%m/%d')} の企画案です"
    try:
        return any(m.get("author", {}).get("bot") and marker in m.get("content", "") for m in get_messages(limit=50))
    except Exception as e:
        log.warning(f"送信済みかどうかを確認できませんでした（このまま送信します）: {e}")
        return False


# 企画案を送ったあと、この時刻（JST）まで Discord の返信を待ち受ける
REPLY_DEADLINE_HOUR = int(os.getenv("REPLY_DEADLINE_HOUR", "13"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))


# GitHub の定期実行は数時間遅れるのが普通なので、早朝から何度も起動をかけておき、
# 早く起動できた回がこの時刻（JST）まで待ってから企画案を送る
SEND_HOUR = int(os.getenv("PLAN_SEND_HOUR", "8"))
EARLY_WAIT_MAX_MIN = 150                      # 送信時刻のこれより前に起動した回は、何もせず終わる（後の回に任せる）
STARTED = datetime.now(JST)
JOB_BUDGET = timedelta(hours=5, minutes=10)   # 1回の実行で使える時間（GitHub の上限より少し短く）


def wait_until_send_time() -> bool:
    """定期実行が送信時刻より前に起動したら、送信時刻まで待つ。早すぎる回は False を返して何もしない。"""
    import time
    if os.getenv("GITHUB_EVENT_NAME") != "schedule":
        return True
    now = datetime.now(JST)
    send_at = now.replace(hour=SEND_HOUR, minute=0, second=0, microsecond=0)
    if now >= send_at:
        return True
    wait = (send_at - now).total_seconds()
    if wait > EARLY_WAIT_MAX_MIN * 60:
        log.info(f"送信時刻（{SEND_HOUR}:00）まで {wait / 60:.0f}分あるため、この回は何もしません（後の回が送ります）。")
        return False
    log.info(f"送信時刻（{SEND_HOUR}:00）まで {wait / 60:.0f}分待ちます。")
    time.sleep(wait)
    return True


def relay_watch() -> None:
    """実行時間の上限が近いとき、返信の待ち受けだけを行う次の実行を起動して引き継ぐ。"""
    pat, repo = os.getenv("GH_PAT"), os.getenv("GITHUB_REPOSITORY")
    if not pat or not repo:
        return
    try:
        r = requests.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/{os.getenv('WORKFLOW_FILE', 'auto_pipeline.yml')}/dispatches",
            headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"},
            json={"ref": "main", "inputs": {"action_mode": "watch_replies"}}, timeout=30)
        log.info(f"返信の待ち受けを次の実行に引き継ぎました（{r.status_code}）。")
    except Exception as e:
        log.warning(f"待ち受けの引き継ぎに失敗: {e}")


def wait_for_reply():
    """返信（1〜3）が来るまで、締め切りまで数分おきに Discord を確認する。
    来たらその場で動画生成を起動する（毎時の返信チェックに頼らないため）。"""
    import time
    import check_discord
    now = datetime.now(JST)
    final = now.replace(hour=REPLY_DEADLINE_HOUR, minute=0, second=0, microsecond=0)
    deadline = min(final, STARTED + JOB_BUDGET)  # 早朝に起動した回は、途中で次の実行に引き継ぐ
    if now >= deadline:
        log.info("返信の受付時間を過ぎているため、待ち受けはしません。")
        return
    log.info(f"{deadline.strftime('%H:%M')} まで、{POLL_SECONDS // 60}分おきに返信を確認します。")
    while datetime.now(JST) < deadline:
        try:
            if check_discord.check_once():
                log.info("返信を受け付けて、動画生成を開始しました。待ち受けを終了します。")
                return
        except Exception as e:
            log.warning(f"返信の確認でエラー（続けて待ちます）: {e}")
        remaining = (deadline - datetime.now(JST)).total_seconds()
        time.sleep(max(1, min(POLL_SECONDS, remaining)))
    if datetime.now(JST) < final:
        relay_watch()
        return
    log.info("受付時間内に返信がありませんでした。")


def main():
    if os.getenv("ACTION_MODE") == "watch_replies":  # 引き継ぎ：返信の待ち受けだけ
        wait_for_reply()
        return
    if not wait_until_send_time():
        return
    send_plans()
    wait_for_reply()


def send_plans():
    # 定期実行（予備のタイマーを含む）のときは、今日の分が送信済みなら何もしない。手動実行は常に送る
    if os.getenv("GITHUB_EVENT_NAME") == "schedule" and already_sent_today():
        log.info("今日の企画案は送信済みのため、送信はしません（返信の待ち受けだけ行います）。")
        return
    trends = [t for t in fetch_google_trends() if not is_excluded(t)]      # スポーツの話題は最初から渡さない
    popular = [p for p in fetch_youtube_popular() if not is_excluded(p)]
    past = past_titles()
    try:
        plans = generate_valid_plans(build_prompt(trends, popular, past), past)
        if not plans:
            raise RuntimeError("条件に合う企画を作れませんでした。")
    except Exception as e:
        log.error(f"企画生成に失敗しました: {e}")
        post_message(f"⚠️ 今朝の企画案の生成に失敗しました。\n```{str(e)[:1500]}```")
        sys.exit(1)

    today = datetime.now(JST).strftime("%m/%d")
    slot = today_slot() if len(plans) >= 3 and plans[2].get("_slot") else None
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
