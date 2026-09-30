"""AI長編ドラマ動画(8〜10分)を自動生成してYouTubeに投稿するスクリプト。

流れ: Geminiで長編台本生成 → Pollinations.aiで画像生成 → 
      Pillowでサムネイル自動生成(New!) → edge-ttsでナレーション生成 → 
      FFmpegでシネマティックBGM生成＆ミックス(New!) → 動画合成 → 
      YouTube投稿＆サムネイル設定(New!)
"""
import asyncio
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

# サムネイル生成用ライブラリ(Pillow)の動的インストールとインポート
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    subprocess.run(["pip", "install", "pillow"], check=True)
    from PIL import Image, ImageDraw, ImageFont

from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from gtts import gTTS
from pydantic import BaseModel

try:
    import edge_tts
except ImportError:
    edge_tts = None

# ---------------------------------------------------------------- 設定
WORK_DIR = Path("work")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
SPEED = float(os.environ.get("NARRATION_SPEED", "1.25"))
TTS_ENGINE = os.environ.get("TTS_ENGINE", "edge")
EDGE_VOICE = os.environ.get("EDGE_VOICE", "ja-JP-NanamiNeural")
PRIVACY = os.environ.get("PRIVACY_STATUS", "unlisted")
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

WIDTH, HEIGHT, FPS = 1280, 720, 30
IMAGE_DIR = Path(os.environ.get("IMAGE_DIR", "images"))
BG_PALETTE = ["0x2b1055", "0x0f2a55", "0x4a1040", "0x0d3b3b"]
SCENE_FADE = 0.4
ZOOM_AMOUNT = 0.15
TITLE_SHOW = (0.5, 4.5)
LEAD_IN = 0.8
TAIL = 2.0
CLIP_GAP = 0.25
MAX_CHARS_PER_CLIP = 50
SUB_CHARS_PER_LINE = 24

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def log(*args):
    print(*args, flush=True)


def run(cmd, timeout=900):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        log(f"❌ コマンド失敗: {cmd[0]} (code {result.returncode})")
        log(result.stderr[-3000:])
        raise RuntimeError(f"{cmd[0]} が失敗しました")
    return result


# ---------------------------------------------------------------- 認証
def restore_credentials():
    token_json = os.environ.get("TOKEN_JSON")
    if not token_json:
        if DRY_RUN:
            log("⚠️ TOKEN_JSONが未設定ですが、DRY_RUNモードのためスキップします。")
            return
        raise ValueError("❌ TOKEN_JSON が設定されていません。")
    Path("token.json").write_text(token_json, encoding="utf-8")


# ---------------------------------------------------------------- 適応型AI台本生成
class Scene(BaseModel):
    text: str
    image_prompt: str


class Drama(BaseModel):
    title: str
    description: str
    visual_style: str
    scenes: list[Scene]


class GenrePlan(BaseModel):
    genre: str
    needs_web_research: bool
    structure: str
    target_sections: int
    audience_angle: str


class ResearchItem(BaseModel):
    topic: str
    facts: list[str]
    cautions: list[str]


class ResearchPack(BaseModel):
    summary: str
    items: list[ResearchItem]


class OutlineSection(BaseModel):
    section_title: str
    purpose: str
    key_points: list[str]
    target_chars: int


class VideoOutline(BaseModel):
    sections: list[OutlineSection]
    hook: str
    ending: str


class ScriptSection(BaseModel):
    section_title: str
    text: str
    image_prompt: str


class ScriptPackage(BaseModel):
    title: str
    description: str
    visual_style: str
    sections: list[ScriptSection]


class ScriptReview(BaseModel):
    needs_revision: bool
    reasons: list[str]
    missing_points: list[str]
    repetition_points: list[str]
    factual_warnings: list[str]


GENRE_GUIDES = {
    "AIドラマ": "登場人物、事件、対立、伏線、逆転、結末を重視。事実解説ではなく物語として構成する。",
    "雑学": "複数の具体的な知識を扱い、なぜそうなのか、具体例、意外性まで説明する。単なる事実の羅列は禁止。",
    "動物・雑学": "動物の行動や身体の特徴を、理由・科学的背景・具体例とともに説明する。",
    "科学・テクノロジー": "結論→仕組み→具体例→身近な影響→意外なポイントの順で、専門用語には簡単な説明を付ける。",
    "歴史": "出来事の背景→経緯→転換点→結果→現在から見た意味を時系列で説明する。",
    "ニュース・時事": "確認できる事実→背景→関係者の発表や反応→影響→今後確認すべき点。推測と事実を混同しない。",
    "ランキング": "順位ごとに理由・具体例・比較軸を説明する。単に名前を並べない。",
    "生活・ライフハック": "悩み→原因→具体的な方法→注意点→使い分け。視聴者が実践できる情報を重視する。",
    "商品・サービス解説": "何ができるか→特徴→メリット・注意点→向いている人→比較・使い方。宣伝文句だけにしない。",
    "都市伝説・ミステリー": "確認済み情報と伝承・推測を明確に分け、断定しない。謎が深まる構成にする。",
    "人物": "人物の経歴→重要な出来事→具体的なエピソード→周囲への影響→現在までの流れ。",
    "その他": "視聴者が疑問を持つ順番に、基礎→具体例→深掘り→意外なポイント→まとめで構成する。",
}


def gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ GEMINI_API_KEY が設定されていません。")
    return genai.Client(api_key=api_key)


def model_candidates():
    # 現行の利用可能モデルを優先し、古いgemini-2.5-flashは候補から除外する。
    # 3.5 Flash-Liteは高頻度処理向け、3.1 Flash-Liteはフォールバック、
    # 3.8 Flashは品質優先の最終候補。
    result = []
    for name in [
        MODEL,
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.8-flash",
    ]:
        if name and name not in result:
            result.append(name)
    return result


def _error_status(error):
    text = str(error)
    match = re.search(r"\b(4\d\d|5\d\d)\b", text)
    return int(match.group(1)) if match else None


def call_structured(client, prompt, schema, *, use_search=False, max_output_tokens=12000, temperature=0.65):
    last_error = None
    for model_name in model_candidates():
        # 404は即座に次のモデルへ。429は12秒待って1回だけ再試行し、
        # それでも駄目なら次のモデルへ。5xx等の一時障害も1回再試行する。
        attempts = 2
        for attempt in range(attempts):
            try:
                tools = None
                if use_search:
                    tools = [types.Tool(google_search=types.GoogleSearch())]
                config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    tools=tools,
                )
                log(f"🤖 Geminiモデル: {model_name}")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )
                if getattr(response, "parsed", None) is not None:
                    parsed = response.parsed
                    if isinstance(parsed, BaseModel):
                        return parsed, response
                return schema.model_validate_json(response.text), response
            except Exception as e:
                last_error = e
                status = _error_status(e)
                log(f"⚠️ Gemini処理エラー ({model_name}): {str(e)[:240]}")
                if status == 404:
                    log(f"↪️ {model_name} は利用できないため、次のモデルへ切り替えます。")
                    break
                if status == 429:
                    if attempt == 0:
                        log("⏳ Geminiのクォータ/レート制限の可能性があるため、12秒待って再試行します。")
                        time.sleep(12)
                        continue
                    log(f"↪️ {model_name} の429が続いたため、次のモデルへ切り替えます。")
                    break
                if status and status >= 500 and attempt == 0:
                    time.sleep(3)
                    continue
                break
    raise RuntimeError(f"Gemini処理に失敗しました: {last_error}")


def classify_theme(client, theme, requested_genre):
    forced = (requested_genre or "").strip()
    if forced and forced not in ("自動判定", "auto", "AUTO"):
        genre = forced
        if genre not in GENRE_GUIDES:
            genre = "その他"
        return GenrePlan(
            genre=genre,
            needs_web_research=genre not in ("AIドラマ",),
            structure=GENRE_GUIDES.get(genre, GENRE_GUIDES["その他"]),
            target_sections=10,
            audience_angle="初見の視聴者にも理解できるようにする",
        )

    prompt = f"""
あなたはYouTube動画の企画編集者です。
テーマを分析し、最適な動画ジャンルと構成を決めてください。

テーマ：{theme}

候補ジャンル：
AIドラマ、雑学、動物・雑学、科学・テクノロジー、歴史、ニュース・時事、ランキング、
生活・ライフハック、商品・サービス解説、都市伝説・ミステリー、人物、その他

ルール：
- テーマそのものから判断する。
- 事実確認が必要なテーマ、現在の情報、数字、ニュース、人物、商品情報などは needs_web_research=true。
- 完全な創作ドラマだけは false。
- structureには、そのジャンルに最適な動画の説明構造を具体的に書く。
- target_sectionsは8〜12。
"""
    plan, _ = call_structured(client, prompt, GenrePlan, max_output_tokens=3000, temperature=0.2)
    if plan.genre not in GENRE_GUIDES:
        plan.genre = "その他"
    if not plan.structure:
        plan.structure = GENRE_GUIDES[plan.genre]
    plan.target_sections = max(8, min(12, plan.target_sections))
    return plan


def extract_grounding_sources(response):
    sources = []
    try:
        metadata = response.candidates[0].grounding_metadata
        chunks = getattr(metadata, "grounding_chunks", None) or []
        for chunk in chunks:
            web = getattr(chunk, "web", None)
            if web:
                title = getattr(web, "title", "") or ""
                uri = getattr(web, "uri", "") or ""
                if uri and uri not in [x["url"] for x in sources]:
                    sources.append({"title": title, "url": uri})
    except Exception:
        pass
    return sources[:10]


def research_theme(client, theme, plan):
    if not plan.needs_web_research:
        return {"summary": "創作テーマのためWeb調査なし", "items": [], "sources": []}

    guide = GENRE_GUIDES.get(plan.genre, GENRE_GUIDES["その他"])
    prompt = f"""
YouTube動画の台本作成前に、テーマについて事実調査をしてください。
Google検索を使い、できるだけ一次情報・公的機関・研究機関・公式情報を優先してください。

テーマ：{theme}
ジャンル：{plan.genre}
構成方針：{guide}

重要ルール：
- 台本に使えそうな具体的事実を複数集める。
- 数字、日付、因果関係は特に慎重に扱う。
- 情報源によって見解が分かれる場合は、その違いが分かるようにする。
- 確認できない噂や断定できない情報は事実として扱わない。
- 同じ内容を言い換えただけの項目を大量に作らない。
- 台本のために必要な情報を十分な量集める。
"""
    pack, response = call_structured(
        client, prompt, ResearchPack, use_search=True,
        max_output_tokens=10000, temperature=0.25
    )
    data = pack.model_dump()
    data["sources"] = extract_grounding_sources(response)
    log(f"🔎 Web調査完了: {len(pack.items)}項目 / 情報源{len(data['sources'])}件")
    return data


def build_outline(client, theme, plan, research, tone):
    research_text = json.dumps(research, ensure_ascii=False, indent=2)
    guide = GENRE_GUIDES.get(plan.genre, GENRE_GUIDES["その他"])
    prompt = f"""
あなたはYouTubeの構成作家です。
以下のテーマについて、8〜10分の動画になる詳細な構成を作ってください。

テーマ：{theme}
ジャンル：{plan.genre}
トーン：{tone}
ジャンル別構成方針：{guide}

目標：
- 8〜10分のナレーションに十分な情報密度を持たせる。
- 目安として8〜12セクション。
- 各セクションには具体的な役割と複数のポイントを入れる。
- 同じことを言い換えて水増ししない。
- 冒頭30〜45秒は特に引き込む。
- 最後は内容を回収し、単なる「いかがでしたか」で終わらない。

調査情報：
{research_text}
"""
    outline, _ = call_structured(client, prompt, VideoOutline, max_output_tokens=7000, temperature=0.45)
    if not outline.sections:
        raise RuntimeError("❌ 動画構成が空です。")
    return outline


def generate_sections(client, theme, plan, outline, research, tone):
    research_text = json.dumps(research, ensure_ascii=False, indent=2)
    guide = GENRE_GUIDES.get(plan.genre, GENRE_GUIDES["その他"])
    sections = []
    total_target = sum(max(250, s.target_chars) for s in outline.sections)
    log(f"✍️ セクション別台本生成開始: {len(outline.sections)}セクション / 目標約{total_target}文字")

    for i, section in enumerate(outline.sections, start=1):
        prompt = f"""
あなたはYouTube長尺動画のプロライターです。
動画全体の一部である第{i}セクションだけを書いてください。

テーマ：{theme}
ジャンル：{plan.genre}
トーン：{tone}
ジャンル構成：{guide}
セクション名：{section.section_title}
このセクションの目的：{section.purpose}
入れるべきポイント：{json.dumps(section.key_points, ensure_ascii=False)}
目標文字数：約{section.target_chars}文字

調査情報：
{research_text}

執筆ルール：
- ナレーションとして自然な日本語にする。
- 視聴者が「なぜ？」「つまりどういうこと？」と思う部分を具体的に説明する。
- 事実と推測を混ぜない。
- 調査情報にない具体的数字や固有の事実を勝手に追加しない。
- 同じ内容を言い換えて水増ししない。
- 専門用語を使う場合は、その意味を簡単に説明する。
- 「ここからは」「いかがでしたか」などの機械的なつなぎを乱用しない。
- ドラマ以外では無意味なセリフを入れない。
- このセクションだけで完結させず、前後と自然につながる文章にする。
- 画像プロンプトは、このセクションの内容を視覚化できる英語で具体的に書く。
"""
        result, _ = call_structured(
            client, prompt, ScriptSection,
            max_output_tokens=5000, temperature=0.65
        )
        result.section_title = section.section_title
        sections.append(result.model_dump())
        log(f"  ✅ セクション{i}/{len(outline.sections)} 完了: {len(result.text)}文字")

    return sections


def assemble_script(client, theme, plan, outline, sections, research, tone):
    combined = "\n\n".join(f"【{s['section_title']}】\n{s['text']}" for s in sections)
    prompt = f"""
以下のセクション台本を元に、YouTube動画のタイトルと概要欄を作成してください。
内容そのものを大幅に書き換えず、タイトル・概要欄の品質を高めることに集中してください。

テーマ：{theme}
ジャンル：{plan.genre}
トーン：{tone}

台本：
{combined}

ルール：
- タイトルはクリックしたくなるが、内容を誇張しすぎない。
- 概要欄は動画内容を自然に要約する。
- ハッシュタグを3〜5個含める。
- visual_styleは全シーンで統一できる英語の画風設定を書く。
"""
    class Meta(BaseModel):
        title: str
        description: str
        visual_style: str
    meta, _ = call_structured(client, prompt, Meta, max_output_tokens=3000, temperature=0.55)
    return {
        "title": meta.title.strip()[:100],
        "description": meta.description.strip(),
        "visual_style": meta.visual_style.strip() or "cinematic documentary style, highly detailed, natural lighting",
        "genre": plan.genre,
        "scenes": sections,
        "sources": research.get("sources", []),
    }


def review_script(client, theme, plan, script, research):
    script_text = "\n\n".join(f"【{s['section_title']}】\n{s['text']}" for s in script["scenes"])
    prompt = f"""
YouTube動画の公開前レビューをしてください。

テーマ：{theme}
ジャンル：{plan.genre}

台本：
{script_text}

調査情報：
{json.dumps(research, ensure_ascii=False, indent=2)}

以下だけを確認してください：
- 明らかな事実誤認
- 調査情報にない断定的な事実の追加
- 同じ内容の繰り返し
- 話の飛躍
- 8〜10分動画として情報が薄い部分
- 冒頭の引きが弱い部分
- 結論が弱い部分

問題がなければ needs_revision=false にしてください。
"""
    review, _ = call_structured(client, prompt, ScriptReview, max_output_tokens=5000, temperature=0.2)
    return review


def revise_script(client, theme, plan, script, review, research, tone):
    script_text = "\n\n".join(f"【{s['section_title']}】\n{s['text']}" for s in script["scenes"])
    prompt = f"""
YouTube動画の台本を修正してください。

テーマ：{theme}
ジャンル：{plan.genre}
トーン：{tone}

現在の台本：
{script_text}

レビュー：
{review.model_dump_json(ensure_ascii=False)}

調査情報：
{json.dumps(research, ensure_ascii=False, indent=2)}

ルール：
- 問題のある部分だけを改善し、良い部分は維持する。
- 事実を新しく追加する場合は調査情報に根拠があるものだけ。
- 内容の薄さを改善する場合、単なる言い換えではなく具体例・背景・理由を追加する。
- セクション数は維持する。
- 各セクションのimage_promptも内容に合わせて維持・修正する。
"""
    result, _ = call_structured(client, prompt, ScriptPackage, max_output_tokens=16000, temperature=0.55)
    return {
        "title": result.title.strip()[:100],
        "description": result.description.strip(),
        "visual_style": result.visual_style.strip(),
        "genre": plan.genre,
        "scenes": [x.model_dump() for x in result.sections],
        "sources": research.get("sources", []),
    }


def generate_script_with_gemini(theme, genre, tone):
    client = gemini_client()
    log("🧠 テーマを分析しています...")
    plan = classify_theme(client, theme, genre)
    log(f"📚 判定ジャンル: {plan.genre} / Web調査: {'あり' if plan.needs_web_research else 'なし'}")

    research = research_theme(client, theme, plan)
    (WORK_DIR / "research.json").write_text(json.dumps(research, ensure_ascii=False, indent=2), encoding="utf-8")

    log("🧩 動画構成を設計しています...")
    outline = build_outline(client, theme, plan, research, tone)
    (WORK_DIR / "outline.json").write_text(json.dumps(outline.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")

    sections = generate_sections(client, theme, plan, outline, research, tone)
    script = assemble_script(client, theme, plan, outline, sections, research, tone)

    log("🔍 台本をレビューしています...")
    review = review_script(client, theme, plan, script, research)
    if review.needs_revision:
        log(f"🛠️ 修正が必要: {len(review.reasons)}件。再生成します...")
        script = revise_script(client, theme, plan, script, review, research, tone)
    else:
        log("✅ 台本レビューOK")

    total_chars = sum(len(s["text"]) for s in script["scenes"])
    log(f"📝 台本完成: {total_chars}文字 / {len(script['scenes'])}セクション")
    return script


def adjust_script_to_duration(client, theme, plan, script, research, tone, actual_seconds):
    min_seconds = 8 * 60
    max_seconds = 10 * 60
    if min_seconds <= actual_seconds <= max_seconds:
        return script

    if actual_seconds < min_seconds:
        action = "増補"
        instruction = "短いセクションを中心に、具体例・背景・理由・比較などを追加して内容を厚くしてください。単なる言い換えは禁止です。"
        target = "8〜10分に届くように"
    else:
        action = "圧縮"
        instruction = "重複、冗長な表現、重要度の低い説明を削り、内容の核を維持してください。"
        target = "10分以内になるように"

    script_text = "\n\n".join(f"【{s['section_title']}】\n{s['text']}" for s in script["scenes"])
    prompt = f"""
YouTube動画の台本を{action}してください。
現在のナレーション実測時間：{actual_seconds:.1f}秒
目標：{target}
テーマ：{theme}
ジャンル：{plan.genre}
トーン：{tone}

台本：
{script_text}

指示：
{instruction}

調査情報：
{json.dumps(research, ensure_ascii=False, indent=2)}

セクション数は維持してください。
"""
    result, _ = call_structured(client, prompt, ScriptPackage, max_output_tokens=18000, temperature=0.45)
    return {
        "title": result.title.strip()[:100],
        "description": result.description.strip(),
        "visual_style": result.visual_style.strip(),
        "genre": plan.genre,
        "scenes": [x.model_dump() for x in result.sections],
        "sources": research.get("sources", []),
    }

# ---------------------------------------------------------------- AI画像＆サムネイル生成
def generate_images_for_scenes(visual_style, scenes):
    IMAGE_DIR.mkdir(exist_ok=True)
    log(f"🎨 全{len(scenes)}シーンのAI画像生成を開始します...")
    
    for i, scene in enumerate(scenes, start=1):
        image_path = IMAGE_DIR / f"scene_{i}.png"
        try:
            prompt_text = f"{visual_style}, {scene['image_prompt']}, masterpiece, anime style, highly detailed, 8k"
            encoded_prompt = urllib.parse.quote(prompt_text)
            url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1280&height=720&seed={i*100}&nologo=true"
            
            log(f" 🎨 シーン{i}/{len(scenes)} の画像を生成中...")
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as resp, open(image_path, 'wb') as f:
                f.write(resp.read())
        except Exception as e:
            log(f"  ⚠️ シーン{i}の画像生成スキップ（背景カラーアニメーションで代用）: {e}")

def create_thumbnail(title, bg_image_path, font_path):
    log("🖼 サムネイル画像を生成中...")
    thumb_path = WORK_DIR / "thumbnail.jpg"
    try:
        img = Image.open(bg_image_path).convert("RGBA")
        # 暗いグラデーションを重ねて文字の視認性を上げる
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 110))
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype(font_path, 80)
        
        # タイトル描画（縁取り付き）
        lines = "\n".join(title[i:i + 15] for i in range(0, len(title), 15)).split("\n")
        y = HEIGHT // 2 - (len(lines) * 90) // 2
        for line in lines:
            if hasattr(draw, "textbbox"):
                bbox = draw.textbbox((0, 0), line, font=font)
                w = bbox[2] - bbox[0]
            else:
                w = draw.textlength(line, font=font) if hasattr(draw, "textlength") else 80 * len(line)
                
            x = (WIDTH - w) // 2
            # 黒い縁取り
            for offset_x, offset_y in [(-4,-4), (4,-4), (-4,4), (4,4), (-4,0), (4,0), (0,-4), (0,4)]:
                draw.text((x + offset_x, y + offset_y), line, font=font, fill="black")
            # メイン文字色（黄色）
            draw.text((x, y), line, font=font, fill="yellow")
            y += 90
            
        img.convert("RGB").save(thumb_path)
        log("✅ サムネイル画像の生成完了")
        return thumb_path
    except Exception as e:
        log(f"⚠️ サムネイル生成エラー: {e}")
        return None


# ---------------------------------------------------------------- ナレーション＆BGM生成
def split_into_clips(scene_texts):
    clips = []
    for scene_index, scene in enumerate(scene_texts):
        for sentence in re.split(r"(?<=[。！？!?])\s*", scene.strip()):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= MAX_CHARS_PER_CLIP:
                clips.append((scene_index, sentence))
                continue
            buf = ""
            for part in re.split(r"(?<=、)", sentence):
                if buf and len(buf) + len(part) > MAX_CHARS_PER_CLIP:
                    clips.append((scene_index, buf))
                    buf = ""
                buf += part
            if buf:
                clips.append((scene_index, buf))
    return clips


def synthesize(text, mp3_path, engine):
    for attempt in range(3):
        try:
            if engine == "edge":
                if edge_tts is None:
                    raise RuntimeError("edge-tts がインストールされていません")
                asyncio.run(edge_tts.Communicate(text, EDGE_VOICE).save(str(mp3_path)))
            else:
                gTTS(text=text, lang="ja", slow=False).save(str(mp3_path))
            return
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def probe_duration(path):
    result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], timeout=60)
    return float(result.stdout.strip())


def create_bgm(duration, out_path):
    """FFmpegのシンセサイザーでシネマティックな低音環境音(BGM)を自動生成"""
    log("🎵 シネマティックBGMを生成中...")
    run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "anoisesrc=a=0.5:c=pink",
        "-af", "lowpass=f=400,tremolo=f=0.1:d=0.5,volume=0.2",
        "-t", str(duration),
        str(out_path)
    ], timeout=120)


def _create_narration(scene_texts, engine):
    log(f"🎙 ナレーション生成中... (エンジン {engine}, 速度 {SPEED}倍)")
    items = split_into_clips(scene_texts)

    lead = WORK_DIR / "lead.wav"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", f"{LEAD_IN}", "-c:a", "pcm_s16le", str(lead)], timeout=60)

    wavs = [lead]
    timeline = []
    cursor = LEAD_IN
    for i, (scene_index, text) in enumerate(items):
        mp3 = WORK_DIR / f"clip_{i:03d}.mp3"
        wav = WORK_DIR / f"clip_{i:03d}.wav"
        synthesize(text, mp3, engine)
        run(["ffmpeg", "-y", "-i", str(mp3),
             "-af", f"atempo={SPEED},apad=pad_dur={CLIP_GAP}",
             "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(wav)], timeout=120)
        duration = probe_duration(wav)
        timeline.append({"text": text, "scene": scene_index, "start": cursor, "end": cursor + duration})
        cursor += duration
        wavs.append(wav)

    list_file = WORK_DIR / "concat.txt"
    list_file.write_text("".join(f"file '{w.resolve()}'\n" for w in wavs), encoding="utf-8")
    narration = WORK_DIR / "narration.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", str(narration)], timeout=300)
    log(f"✅ ナレーション完成: {cursor:.1f}秒 ({len(items)}文)")
    return narration, timeline, cursor


def create_narration(scene_texts):
    engines = [TTS_ENGINE] + (["gtts"] if TTS_ENGINE != "gtts" else [])
    for i, engine in enumerate(engines):
        try:
            return _create_narration(scene_texts, engine)
        except Exception as e:
            log(f"⚠️ {engine} でのナレーション生成に失敗: {e}")
            if i == len(engines) - 1:
                raise


# ---------------------------------------------------------------- 動画合成
def find_japanese_font():
    try:
        result = subprocess.run(["fc-match", "-f", "%{file}", "Noto Sans CJK JP"], capture_output=True, text=True, timeout=10)
        path = result.stdout.strip()
        if path and "CJK" in path and Path(path).exists():
            return path
    except Exception:
        pass
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return path
    raise RuntimeError("❌ 日本語フォントが見つかりません。")


def wrap_text(text, width=SUB_CHARS_PER_LINE):
    return "\n".join(text[i:i + width] for i in range(0, len(text), width))


def write_text_file(name, text):
    path = WORK_DIR / name
    path.write_text(text, encoding="utf-8")
    return path.as_posix()


def find_scene_image(number):
    for ext in ("png", "jpg", "jpeg", "webp"):
        path = IMAGE_DIR / f"scene_{number}.{ext}"
        if path.exists():
            return path
    return None


def scene_frame_bounds(timeline, n_scenes, total):
    starts = {}
    for clip in timeline:
        starts.setdefault(clip["scene"], clip["start"])
    starts[0] = 0.0
    bounds, last = [], 0.0
    for i in range(n_scenes):
        last = starts.get(i, last)
        bounds.append(round(last * FPS))
    bounds.append(round(total * FPS))
    return bounds


def make_scene_segment(index, image, frames):
    out = WORK_DIR / f"seg_{index:02d}.mp4"
    duration = frames / FPS
    fade = f"fade=t=in:st=0:d={SCENE_FADE},fade=t=out:st={max(duration - SCENE_FADE, 0):.3f}:d={SCENE_FADE}"
    center = "x=iw/2-(iw/zoom/2):y=ih/2-(ih/zoom/2)"
    if image is not None:
        mode = index % 3
        if mode == 0:
            move = f"z='1+{ZOOM_AMOUNT}*on/{frames}':{center}"
        elif mode == 1:
            move = f"z='{1 + ZOOM_AMOUNT}-{ZOOM_AMOUNT}*on/{frames}':{center}"
        else:
            move = f"z='{1 + ZOOM_AMOUNT}':x='(iw-iw/zoom)*on/{frames}':y=ih/2-(ih/zoom/2)"
        vf = f"scale={WIDTH * 2}:{HEIGHT * 2}:force_original_aspect_ratio=increase,crop={WIDTH * 2}:{HEIGHT * 2},zoompan={move}:d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},{fade}"
        source = ["-i", str(image)]
    else:
        color = BG_PALETTE[index % len(BG_PALETTE)]
        vf = f"hue=h='12*t',vignette=PI/4,{fade}"
        source = ["-f", "lavfi", "-i", f"color=c={color}:s={WIDTH}x{HEIGHT}:r={FPS}"]
    run(["ffmpeg", "-y", *source, "-vf", vf, "-frames:v", str(frames),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
         "-r", str(FPS), str(out)], timeout=600)
    return out


def create_video(narration, timeline, total_audio, title, scenes):
    log("🎬 動画合成を開始します... (長編のため時間がかかります)")
    font = find_japanese_font()
    output_path = WORK_DIR / "ai_drama_output.mp4"
    total = total_audio + TAIL

    # 1. BGMの生成と音声の事前ミックス（ナレーション＋BGM）
    bgm_path = WORK_DIR / "bgm.wav"
    create_bgm(total, bgm_path)
    
    final_audio = WORK_DIR / "final_audio.wav"
    log("🎵 音声ミックス中...")
    run([
        "ffmpeg", "-y", "-i", str(narration), "-i", str(bgm_path),
        "-filter_complex", "[0:a]volume=1.2[a1];[1:a]volume=0.6[a2];[a1][a2]amix=inputs=2:duration=first:dropout_transition=2",
        str(final_audio)
    ], timeout=120)

    # 2. 映像セグメントの作成
    bounds = scene_frame_bounds(timeline, len(scenes), total)
    segments = []
    for i in range(len(scenes)):
        frames = bounds[i + 1] - bounds[i]
        if frames <= 0:
            continue
        image = find_scene_image(i + 1)
        segments.append(make_scene_segment(i, image, frames))

    list_file = WORK_DIR / "segments.txt"
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in segments), encoding="utf-8")

    # 3. 字幕フィルターの作成
    title_file = write_text_file("title.txt", wrap_text(title, 20))
    speech_start, speech_end = timeline[0]["start"], timeline[-1]["end"]
    filters = [
        f"drawbox=x=0:y=ih-230:w=iw:h=230:color=black@0.4:t=fill:enable='between(t,{speech_start:.2f},{speech_end:.2f})'",
        f"drawtext=fontfile={font}:textfile={title_file}:expansion=none:fontcolor=0x00ffff:fontsize=42:line_spacing=10:borderw=3:bordercolor=black:x=(w-text_w)/2:y=60:enable='between(t,{TITLE_SHOW[0]},{TITLE_SHOW[1]})'",
    ]
    for i, clip in enumerate(timeline):
        sub_file = write_text_file(f"sub_{i:03d}.txt", wrap_text(clip["text"]))
        filters.append(
            f"drawtext=fontfile={font}:textfile={sub_file}:expansion=none:fontcolor=white:fontsize=40:line_spacing=12:borderw=2:bordercolor=black:x=(w-text_w)/2:y=h-205:enable='between(t,{clip['start']:.2f},{clip['end']:.2f})'"
        )

    # 4. 最終合成
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-i", str(final_audio),
        "-vf", ",".join(filters), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.2f}", "-movflags", "+faststart", str(output_path),
    ], timeout=1800)
    log(f"✅ 動画生成完了: {output_path} ({total:.1f}秒)")
    return output_path


# ---------------------------------------------------------------- 投稿
def upload_to_youtube(video_path, thumbnail_path, metadata):
    if DRY_RUN:
        log("🧪 DRY_RUN=true のため、YouTubeアップロードはスキップします。")
        return None

    try:
        creds = Credentials.from_authorized_user_file("token.json")
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        description = metadata["description"] + "\n\n※この動画はAIで自動生成されたオリジナル作品です。"
        body = {
            "snippet": {
                "title": metadata["title"],
                "description": description,
                "tags": [metadata.get("genre", "YouTube"), "Gemini", "YouTube自動化", "AI動画", "長編"],
                "categoryId": "24",
            },
            "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        log("🚀 YouTubeへのアップロードを開始...")
        response, retries = None, 0
        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    log(f"   アップロード中 {int(status.progress() * 100)}%")
            except HttpError as e:
                if "uploadLimitExceeded" in str(e):
                    log("⚠️ YouTubeチャンネルの投稿上限に達しています。テスト投稿モードとして完了します。")
                    return None
                if e.resp.status in (500, 502, 503, 504) and retries < 5:
                    retries += 1
                    time.sleep(2**retries)
                else:
                    raise

        log("=" * 40)
        log(f"🎉 アップロード成功！ 🔗 URL: https://youtu.be/{response['id']}")
        
        # サムネイル設定
        if thumbnail_path and Path(thumbnail_path).exists():
            log("🖼 サムネイルを設定中...")
            try:
                youtube.thumbnails().set(
                    videoId=response['id'],
                    media_body=MediaFileUpload(str(thumbnail_path))
                ).execute()
                log("✅ サムネイル設定完了")
            except Exception as e:
                log(f"⚠️ サムネイル設定エラー: {e}")
                
        log("=" * 40)
        return response["id"]
    except Exception as e:
        log(f"⚠️ アップロード中に例外が発生しました: {e}")
        return None


# ---------------------------------------------------------------- main
def main():
    WORK_DIR.mkdir(exist_ok=True)
    restore_credentials()

    theme = os.environ.get("INPUT_VIDEO_TITLE")
    genre = os.environ.get("INPUT_VIDEO_GENRE") or "自動判定"
    tone = os.environ.get("INPUT_VIDEO_TONE") or "わかりやすく、最後まで見たくなる雰囲気"
    if not theme:
        raise ValueError("❌ スプレッドシートのA2セル（テーマ）が空です。")

    log("=" * 60)
    log(f"🎯 動画制作開始 -> テーマ: {theme}")
    log(f"   指定ジャンル: {genre}")
    log(f"   トーン: {tone}")
    log("=" * 60)

    # 1. テーマに合わせた台本生成
    metadata = generate_script_with_gemini(theme, genre, tone)

    # 2. 実際の音声時間を測り、8〜10分に調整
    client = gemini_client()
    plan = classify_theme(client, theme, genre)
    research = json.loads((WORK_DIR / "research.json").read_text(encoding="utf-8")) if (WORK_DIR / "research.json").exists() else {"items": [], "sources": []}

    for calibration in range(2):
        scene_texts = [sc["text"] for sc in metadata["scenes"]]
        narration, timeline, total_audio = create_narration(scene_texts)
        log(f"⏱️ ナレーション実測: {total_audio / 60:.2f}分")
        if 8 * 60 <= total_audio <= 10 * 60:
            break
        if calibration == 1:
            log("⚠️ 2回調整しても8〜10分に収まらないため、現在の最終版で続行します。")
            break
        metadata = adjust_script_to_duration(client, theme, plan, metadata, research, tone, total_audio)
        (WORK_DIR / "script.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    (WORK_DIR / "script.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. 最終台本が決まってから画像生成
    generate_images_for_scenes(metadata["visual_style"], metadata["scenes"])

    # 4. サムネイル生成
    thumbnail_path = None
    scene1_image = find_scene_image(1)
    if scene1_image:
        thumbnail_path = create_thumbnail(metadata["title"], scene1_image, find_japanese_font())

    # 5. 最終台本でナレーションを再生成（調整後の内容を確実に反映）
    scene_texts = [sc["text"] for sc in metadata["scenes"]]
    narration, timeline, total_audio = create_narration(scene_texts)

    # 6. 動画合成
    video_path = create_video(narration, timeline, total_audio, metadata["title"], metadata["scenes"])

    # 7. YouTube投稿
    upload_to_youtube(video_path, thumbnail_path, metadata)


if __name__ == "__main__":
    main()
