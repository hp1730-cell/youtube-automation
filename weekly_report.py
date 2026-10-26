"""週1回、パイプライン全体が「無料の範囲で」「正常に」動いているかを確認して Discord に報告する

確認すること（どれも無料の API 呼び出しだけで、クォータや料金は消費しない）
  1. GitHub Actions: リポジトリが公開か（公開なら時間無制限で無料）、非公開なら今月の使用分数
                     有料のランナーを使っていないか、過去7日の実行結果
                     60日間コミットがないと定期実行が止まる仕組みへの注意
  2. 各サービスの鍵が有効か: Gemini / Groq / YouTube（ログイン用トークン）/ Pixabay
  3. 料金が発生する設定になっていないかの注意点
"""
import os
import re
import glob
import logging
from datetime import datetime, timezone, timedelta

import requests

from discord_utils import post_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("weekly_report")

JST = timezone(timedelta(hours=9))
GH_API = "https://api.github.com"
REPO = os.getenv("GITHUB_REPOSITORY", "")
GH_TOKEN = os.getenv("GITHUB_TOKEN", "")
FREE_MINUTES = 2000               # 非公開リポジトリの無料枠（分/月）
FREE_RUNNERS = re.compile(r"^(ubuntu|windows|macos)-(latest|\d+(\.\d+)?)$")  # 標準ランナー（公開リポジトリなら無料）
JOB_NAMES = {"trend_analysis": "企画案の送信", "discord_check": "返信チェック", "generate_video": "動画の生成・投稿",
             "_other": "その他（ページの公開・週次チェックなど）"}


def gh(path: str, **params):
    r = requests.get(f"{GH_API}{path}", params=params, timeout=30,
                     headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
                              "X-GitHub-Api-Version": "2022-11-28"})
    r.raise_for_status()
    return r.json()


def list_runs(since: datetime) -> list[dict]:
    runs, page = [], 1
    while True:
        data = gh(f"/repos/{REPO}/actions/runs", per_page=100, page=page,
                  created=f">={since.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        batch = data.get("workflow_runs", [])
        runs += batch
        if len(batch) < 100 or page >= 10:
            return runs
        page += 1


# ---------------------------------------------------------------- 1. GitHub Actions

def check_github() -> tuple[list[str], list[str]]:
    lines, warnings = [], []
    repo = gh(f"/repos/{REPO}")
    public = repo.get("visibility") == "public"
    now = datetime.now(timezone.utc)

    # 有料ランナー（larger runner など）を使っていないか
    paid = []
    for path in glob.glob(".github/workflows/*.y*ml"):
        for m in re.finditer(r"runs-on:\s*['\"]?([\w.\-]+)", open(path, encoding="utf-8").read()):
            if not FREE_RUNNERS.match(m.group(1)):
                paid.append(f"{os.path.basename(path)}: {m.group(1)}")

    if public:
        lines.append("✅ リポジトリは**公開**：GitHub Actions は時間無制限で無料です")
    else:
        # 非公開なら、今月の課金対象の分数を合計して無料枠と比べる
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total_ms = 0
        for run in list_runs(month_start):
            try:
                timing = gh(f"/repos/{REPO}/actions/runs/{run['id']}/timing")
                total_ms += sum(v.get("total_ms", 0) for v in timing.get("billable", {}).values())
            except Exception:
                pass
        used = total_ms / 60000
        days = (now - month_start).days + 1
        projected = used / days * 30
        mark = "✅" if projected < FREE_MINUTES * 0.9 else "⚠️"
        lines.append(f"{mark} リポジトリは**非公開**：今月 {used:.0f}分 / 無料枠 {FREE_MINUTES}分"
                     f"（このペースだと月末に約{projected:.0f}分）")
        if projected >= FREE_MINUTES * 0.9:
            warnings.append("GitHub Actions の無料枠を超えそうです（超えると請求はされず、実行が止まります）。"
                            "リポジトリを公開にすると時間無制限になります。")

    if paid:
        warnings.append("有料のランナー（実行マシン）が指定されています: " + "、".join(paid))
    else:
        lines.append("✅ 実行マシンはすべて無料の標準ランナーです")

    # 過去7日の実行結果（仕事ごとに集計。実行されなかった＝skipped は数えない）
    week = list_runs(now - timedelta(days=7))
    stats: dict[str, dict[str, int]] = {}
    for run in week:
        try:
            for job in gh(f"/repos/{REPO}/actions/runs/{run['id']}/jobs").get("jobs", []):
                c = job.get("conclusion")
                if c in (None, "skipped"):
                    continue
                key = job["name"] if job["name"] in JOB_NAMES else "_other"  # ページ公開などは「その他」にまとめる
                stats.setdefault(key, {}).setdefault(c, 0)
                stats[key][c] += 1
        except Exception:
            pass
    if stats:
        lines.append("📊 過去7日の実行結果")
        for name, s in sorted(stats.items(), key=lambda kv: list(JOB_NAMES).index(kv[0])):
            ok, total = s.get("success", 0), sum(s.values())
            label = JOB_NAMES.get(name, name)
            mark = "✅" if ok == total else "⚠️"
            lines.append(f"　{mark} {label}：{ok}/{total} 回成功")
            if ok < total:
                warnings.append(f"「{label}」が過去7日で {total - ok} 回失敗しています。Actions のログを確認してください。")
    else:
        warnings.append("過去7日に実行がありません。定期実行が止まっている可能性があります。")

    # 公開リポジトリは、60日間コミットがないと定期実行（schedule）が自動で止まる
    pushed = datetime.fromisoformat(repo["pushed_at"].replace("Z", "+00:00"))
    idle = (now - pushed).days
    if idle >= 45:
        warnings.append(f"リポジトリの最後の更新から {idle}日たっています。60日間更新がないと、毎朝の企画案などの"
                        "定期実行が GitHub によって自動停止されます。何か1つコミットしてください。")
    return lines, warnings


# ---------------------------------------------------------------- 2. 各サービスの鍵

def check_keys() -> tuple[list[str], list[str]]:
    lines, warnings = [], []

    def report(name: str, ok: bool | None, detail: str = ""):
        if ok is None:
            lines.append(f"➖ {name}：未設定")
        elif ok:
            lines.append(f"✅ {name}：正常")
        else:
            lines.append(f"❌ {name}：{detail}")
            warnings.append(f"{name} の鍵が使えなくなっています（{detail}）。")

    # Gemini（モデル一覧の取得。生成はしないので無料枠も消費しない）
    key = os.getenv("GEMINI_API_KEY")
    if key:
        try:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                             params={"key": key, "pageSize": 1}, timeout=30)
            report("Gemini", r.ok, f"エラー {r.status_code}")
        except Exception as e:
            report("Gemini", False, str(e)[:80])
    else:
        report("Gemini", None)

    # Groq（モデル一覧の取得）
    key = os.getenv("GROQ_API_KEY")
    if key:
        try:
            r = requests.get("https://api.groq.com/openai/v1/models",
                             headers={"Authorization": f"Bearer {key}"}, timeout=30)
            report("Groq", r.ok, f"エラー {r.status_code}")
        except Exception as e:
            report("Groq", False, str(e)[:80])
    else:
        report("Groq", None)

    # YouTube（ログイン用トークンの更新だけ試す。動画の投稿はしない）
    cid, secret, refresh = os.getenv("YT_CLIENT_ID"), os.getenv("YT_CLIENT_SECRET"), os.getenv("YT_REFRESH_TOKEN")
    if cid and secret and refresh:
        try:
            r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
                "client_id": cid, "client_secret": secret, "refresh_token": refresh, "grant_type": "refresh_token"})
            if r.ok:
                report("YouTube（投稿用のログイン）", True)
            else:
                err = r.json().get("error", r.status_code)
                hint = {"invalid_grant": "トークンの期限切れ・取り消し。Playground で取り直してください",
                        "unauthorized_client": "ID・シークレット・トークンが別々のクライアントのもの",
                        "invalid_client": "クライアントIDかシークレットが間違っています"}.get(err, str(err))
                report("YouTube（投稿用のログイン）", False, hint)
        except Exception as e:
            report("YouTube（投稿用のログイン）", False, str(e)[:80])
    else:
        report("YouTube（投稿用のログイン）", None)

    # Pixabay（画像検索を1回だけ）
    key = os.getenv("PIXABAY_API_KEY")
    if key:
        try:
            r = requests.get("https://pixabay.com/api/", params={"key": key, "q": "sky", "per_page": 3}, timeout=30)
            report("Pixabay", r.ok, f"エラー {r.status_code}")
        except Exception as e:
            report("Pixabay", False, str(e)[:80])
    else:
        report("Pixabay", None)

    # 有料プランの鍵が設定されていないか（Pollinations の有料トークンなど）
    if os.getenv("POLLINATIONS_TOKEN"):
        lines.append("ℹ️ Pollinations のトークンが設定されています。無料プランのものか確認してください")
    return lines, warnings


# ---------------------------------------------------------------- まとめて送信

def main():
    today = datetime.now(JST).strftime("%Y/%m/%d")
    sections, warnings = [], []
    for title, fn in (("🖥️ GitHub Actions", check_github), ("🔑 各サービスの鍵", check_keys)):
        try:
            lines, warns = fn()
        except Exception as e:
            lines, warns = [f"⚠️ 確認できませんでした: {str(e)[:150]}"], [f"{title} の確認でエラーが出ました。"]
        sections.append(f"**{title}**\n" + "\n".join(lines))
        warnings += warns

    sections.append("**💴 料金について**\n"
                    "・Google Cloud（Gemini・YouTube）は、クレジットカード（請求先アカウント）を登録しない限り請求されません\n"
                    "・Groq・Pixabay・VOICEVOX・Discord は、無料プランのままなら請求されません\n"
                    "・念のため月に1回、Google Cloud の「お支払い」に請求先アカウントが付いていないか確認してください")

    if warnings:
        head = f"⚠️ **週次チェック（{today}）：確認が必要な点が {len(warnings)} 件あります**\n" + \
               "\n".join(f"・{w}" for w in warnings)
    else:
        head = f"✅ **週次チェック（{today}）：今週もすべて無料の範囲で正常に動いています**"
    post_message(head + "\n\n" + "\n\n".join(sections))
    log.info("週次チェックを送信しました。")


if __name__ == "__main__":
    main()
