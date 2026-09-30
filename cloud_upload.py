# ---------------------------------------------------------------- 長尺台本プロンプト
def build_prompt(theme, genre, tone):
    return (
        "あなたはYouTubeで数十万回再生される長編AIアニメドラマのプロ脚本家です。\n"
        "以下の条件に従い、再生時間が8分〜10分（ナレーション文字数：3,200〜4,000文字）となる、非常に見ごたえのある長編AIドラマ台本を作成してください。\n\n"
        "【厳格な禁止事項】\n"
        "・「静寂に包まれた」「運命の歯車」のような抽象的なポエム・雰囲気だけの文章は一切禁止。\n"
        "・実在の人物、既存作品のキャラ、商標権・著作権に触れる固有名詞は一切使用禁止。\n\n"
        "【長編ドラマの構成ルール】\n"
        f"- メインテーマ: {theme}\n"
        f"- ジャンル: {genre}\n"
        f"- トーン: {tone}\n\n"
        "【構成指定（全16〜20シーン）】\n"
        "1. 【第1〜3シーン（導入・引き）】: 衝撃的な事件、理不尽な状況、または強い問いかけから始まり、視聴者の手を止めさせる。\n"
        "2. 【第4〜10シーン（展開・対話）】: 登場人物同士のリアルな掛け合い・セリフ（「〜」）を多用し、人間関係の対立や伏線を深める。\n"
        "3. 【第11〜16シーン（クライマックス・逆転）】: 隠された真実の発覚、怒涛の展開、または見事な解決（スカッと展開など）。\n"
        "4. 【第17〜20シーン（結末・余韻）】: 納得感のある結末と、深い余韻を残す後日談。\n\n"
        "【各シーンの条件】\n"
        "・1シーンにつき、ナレーション文（text）は200文字程度（話し言葉の3〜5文）。\n"
        "・1シーンにつき、画風と構図を英語で指定したAI画像プロンプト（image_prompt）を具体的に記述する（例: 'cinematic medium shot of a shocked girl, detailed anime style'）。\n\n"
        "出力フォーマットは必ずJSONのみとし、Markdown表記（```json）や余計な解説は含めないでください。\n"
    )

def generate_script_with_gemini(theme, genre, tone):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ GEMINI_API_KEY が設定されていません。")
    client = genai.Client(api_key=api_key)

    models_to_try = [MODEL, "gemini-2.5-flash", "gemini-1.5-flash"]
    
    for model_name in models_to_try:
        log(f"🤖 8〜10分長尺台本を生成中... (モデル: {model_name})")
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=build_prompt(theme, genre, tone),
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=Drama,
                        temperature=0.85,
                        max_output_tokens=8192,  # 長文JSON出力用にトークン数を上限拡張
                    ),
                )
                data = json.loads(response.text)
                scenes = [
                    {"text": sc["text"].strip(), "image_prompt": (sc.get("image_prompt") or "").strip()}
                    for sc in data["scenes"] if sc.get("text", "").strip()
                ]
                # 10シーン以上取れていれば長尺台本として採用
                if len(scenes) >= 10 and data.get("title"):
                    log(f"✨ 8〜10分長尺台本の生成に成功しました！（全{len(scenes)}シーン）")
                    return {
                        "title": data["title"].strip()[:100],
                        "description": data.get("description", "").strip(),
                        "visual_style": (data.get("visual_style") or "").strip(),
                        "scenes": scenes,
                    }
            except Exception as e:
                log(f"⚠️ API応答エラー ({model_name}): {str(e)[:150]}")
                time.sleep(3)

    log("❌ 長尺台本の自動生成に失敗したため、バックアップ用台本に切り替えます。")
    # 長尺用フォールバック
    fallback_scenes = []
    for i in range(1, 16):
        fallback_scenes.append({
            "text": f"第{i}幕の展開です。{theme}をめぐる物語は、予想もしない展開へと進んでいきます。主人公たちの選択が、未来を大きく変えようとしていました。",
            "image_prompt": f"cinematic scene {i} about {theme}, highly detailed anime style"
        })
    return {
        "title": f"【長編AIドラマ】{theme}",
        "description": f"{theme}を描く完全オリジナルAIアニメドラマ。#{genre} #AIドラマ #長編アニメ",
        "visual_style": "Japanese anime style, cinematic lighting, masterpiece, 8k",
        "scenes": fallback_scenes
    }
