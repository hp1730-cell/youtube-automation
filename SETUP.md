# YouTube 自動制作パイプライン セットアップ手順

## 全体の流れ

1. **毎朝 8:07ごろ**：Google トレンドと YouTube 急上昇（任意）をもとに、Groq が企画案を3つ作って Discord に送信
2. **8:23〜12:23ごろ（毎時）**：Discord に「1」「2」「3」の返信があれば、その企画本文を渡して動画生成を起動
3. **動画生成**：Gemini で台本 → VOICEVOX で音声 → Pollinations で画像 → 字幕・動き付きで MP4 化
4. **非公開アップロード**：YouTube に非公開で投稿し、Discord に YouTube Studio のリンクを通知
5. **あなたが確認** → YouTube Studio で公開

所要時間の目安は、動画1本あたり 40〜90分（画像生成とレンダリングが大半）です。

---

## 1. ファイルの配置

リポジトリのルートに、次の構成で配置してください。

```
.github/workflows/auto_pipeline.yml
analyze.py
check_discord.py
discord_utils.py
main.py
upload_youtube.py
requirements.txt
get_refresh_token.py   ← 自分のPCで1回だけ使う
assets/bgm.mp3         ← 任意（置くと自動でBGMが入る）
assets/character/      ← 解説キャラ（normal / smile / surprised / thinking の透過PNG）
assets/character/talk/ ← 口パク用（a / i / u / e / o / n の透過PNG）
assets/character_sheet.png  ← キャラの元画像（2×2の表情シート）
assets/mouth_sheet.png      ← 口パクの元画像（3×2の口の形シート）
prepare_character.py   ← シートから透過PNGを作り直すスクリプト
```

- 古い `check_discord.py` / `main.py` は置き換えてください。
- ワークフローのファイル名を変える場合は、YAML 内の `WORKFLOW_FILE` も同じ名前にしてください。
- **ワークフローはデフォルトブランチ（main）にある必要があります。** schedule も workflow_dispatch も、デフォルトブランチのファイルしか動きません。

## 2. Discord Bot の設定

[Discord Developer Portal](https://discord.com/developers/applications) で Bot を開き、次を確認します。

- **Bot → Privileged Gateway Intents → MESSAGE CONTENT INTENT を ON**
  （OFF のままだと、返信の「1」「2」「3」が空文字として読まれ、検出できません）
- チャンネルでの権限：「メッセージを見る」「メッセージを送信」「メッセージ履歴を読む」「リアクションの追加」

## 3. GitHub Secrets / Variables

リポジトリの **Settings → Secrets and variables → Actions** で登録します。

### Secrets（必須）

| 名前 | 内容 |
|---|---|
| `GROQ_API_KEY` | 企画案の生成 |
| `GEMINI_API_KEY` | 台本の生成（Google AI Studio で取得） |
| `DISCORD_BOT_TOKEN` | Discord Bot のトークン |
| `DISCORD_CHANNEL_ID` | 企画案を送るチャンネルのID |
| `GH_PAT` | 動画生成ワークフローの起動用（下記参照） |

`GH_PAT` は Fine-grained personal access token で作成し、対象リポジトリのみ・**Actions: Read and write** の権限だけを付けてください。

### Secrets（YouTube アップロード用）

| 名前 | 内容 |
|---|---|
| `YT_CLIENT_ID` | 手順4で取得 |
| `YT_CLIENT_SECRET` | 手順4で取得 |
| `YT_REFRESH_TOKEN` | 手順4で取得 |

未設定の場合はアップロードをスキップし、動画は Actions の Artifacts からダウンロードできます。

### Secrets（任意）

| 名前 | 内容 |
|---|---|
| `YOUTUBE_API_KEY` | 企画立案時に YouTube 急上昇動画を参考にする（APIキーで可） |
| `PIXABAY_API_KEY` | 画像生成に失敗したときの予備 |
| `POLLINATIONS_TOKEN` | Pollinations のトークン（無料登録で取得でき、レート制限が緩和されます） |

### Variables（任意）

| 名前 | 例 | 内容 |
|---|---|---|
| `CHANNEL_THEME` | `日本の歴史と雑学をアニメ調で解説` | チャンネルの方向性。企画と台本の両方に反映されます。**設定を強く推奨** |
| `DISCORD_ALLOWED_USER_ID` | `123456789012345678` | 自分の返信だけを受け付ける |
| `VOICEVOX_SPEAKER` | `12` | VOICEVOX の話者ID。未設定なら 12=白上虎太郎（11=玄野武宏、13=青山龍星、3=ずんだもん など） |

## 4. YouTube アップロードの準備（1回だけ）

ブラウザだけで完結する方法です（PC に Python を入れる必要はありません）。

### A. Google Cloud の設定
1. [Google Cloud Console](https://console.cloud.google.com/) にログインし、上部のプロジェクト選択から **新しいプロジェクト** を作成
2. 「APIとサービス」→「ライブラリ」で **YouTube Data API v3** を検索して **有効にする**
3. 「APIとサービス」→「OAuth 同意画面」（または「Google Auth Platform」）を開き、アプリ名・メールを入れて作成（対象: **外部**）
4. 「データアクセス」（スコープ）で `https://www.googleapis.com/auth/youtube.upload` を追加
5. 「対象」（公開ステータス）で **本番環境に公開** を押す
   ※「テスト」のままだと**リフレッシュトークンが7日で失効**し、アップロードが止まります。
   審査は不要です（自分で使うだけなら「未確認のアプリ」の警告を進めれば使えます）。
6. 「認証情報」→「認証情報を作成」→「OAuth クライアント ID」
   - アプリケーションの種類: **ウェブ アプリケーション**
   - 承認済みのリダイレクト URI に `https://developers.google.com/oauthplayground` を追加
   - 作成後に表示される **クライアント ID** と **クライアント シークレット** を控える

### B. リフレッシュトークンの取得（OAuth 2.0 Playground）
1. [OAuth 2.0 Playground](https://developers.google.com/oauthplayground) を開く
2. 右上の歯車 ⚙ → **Use your own OAuth credentials** にチェックし、A-6 のクライアント ID とシークレットを入力
3. 左の「Input your own scopes」に `https://www.googleapis.com/auth/youtube.upload` を入れて **Authorize APIs**
4. 動画を投稿したいチャンネルの Google アカウントを選ぶ（「未確認のアプリ」と出たら「詳細」→「（安全ではないページ）に移動」）
5. Step 2 で **Exchange authorization code for tokens** を押し、表示された **Refresh token** を控える
6. 使い終わったら歯車の設定は戻してかまいません（トークンは有効なままです）

### C. GitHub に登録
Settings → Secrets and variables → Actions → **Secrets** タブに、次の3つを登録します。

| 名前 | 値 |
|---|---|
| `YT_CLIENT_ID` | A-6 のクライアント ID |
| `YT_CLIENT_SECRET` | A-6 のクライアント シークレット |
| `YT_REFRESH_TOKEN` | B-5 のリフレッシュトークン |

（Python が使える場合は、デスクトップアプリ用のクライアントを作って `python get_refresh_token.py` で取得しても同じです。）

> 審査を受けていないプロジェクトからアップロードした動画は、API 側で非公開に固定されます。
> このパイプラインは最初から非公開で投稿するため問題ありません。YouTube Studio から手動で公開できます。

**サムネイルの自動設定**には、チャンネルの電話番号認証（中級者向け機能）が必要です。

## 5. 動作確認の順番

Actions タブ → 「YouTube Auto-Production Pipeline」→ **Run workflow** から手動で試せます。

1. `analyze` → Discord に企画案が4通（案内＋企画3つ）届くか
2. Discord で「1」と送信 → `check_replies` → ✅ リアクションが付き、動画生成が始まるか
3. 動画生成の完了後、Discord に YouTube Studio のリンクが届くか

動画生成だけを試す場合は、`generate_video` を選び `video_title` にテーマを入力して実行します。
アップロードせずに確認したい場合は `skip_upload` にチェックを入れてください。

## 6. 調整できる主な項目（環境変数）

`main.py` の上部にまとまっています。YAML の `env:` に追加すると変更できます。

| 名前 | 既定値 | 内容 |
|---|---|---|
| `TARGET_MINUTES` | 8 | 動画の長さの目安（分） |
| `GEMINI_MODELS` | `gemini-2.5-flash,gemini-2.5-flash-lite` | 使うモデル（左から順に試行）。新しいモデルが出たら先頭に追加 |
| `IMAGE_STYLE` | アニメ調 | 全画像に共通で付ける画風の指定 |
| `VOICEVOX_SPEED` | 1.12 | 話す速さ |
| `BGM_VOLUME` | 0.10 | BGM の音量 |
| `BGM_CREDIT` | なし | BGM のクレジット表記（概要欄に入ります） |

## 7. 解説キャラクター

`assets/character/` に表情ごとの透過PNGがあると、画面右下にキャラが表示されます。
台本の各シーンで AI が表情（通常・笑顔・驚き・考え中）を選び、表情が変わるときは小さく跳ねます。
フォルダを削除すればキャラなしの動画になります。

### 口パク

`assets/character/talk/` に口の形6種類（あ・い・う・え・お・閉じ）があると、「通常」の表情のときに口が動きます。
VOICEVOX が返す「どの音を何秒発音するか」のデータを使って、あいうえおの口を音声に合わせて切り替えます。
「ま行・ば行・ぱ行」の子音や「ん」「っ」、文の切れ目では口を閉じます。
VOICEVOX が使えず gTTS になった場合は、声の大きさから口の開き具合を推定します。

「笑顔」「驚き」「考え中」の表情では口は動きません（その表情の口の素材がないため）。
台本では「通常」を6割以上にするよう AI に指示しています。

### 素材を差し替えるとき

- 表情：白背景または透過背景・2×2（左上=通常、右上=笑顔、左下=驚き、右下=考え中）→ `assets/character_sheet.png`
- 口パク：白背景または透過背景・3×2（上段=あ・い・う、下段=え・お・閉じ）→ `assets/mouth_sheet.png`

置き換えたら `python prepare_character.py` を実行してください。
口パクは「閉じ」の顔をベースに、各母音の絵から口元だけを切り取って重ねるので、髪や目は動きません。
画像の左上に書かれた「あ」などの文字は無視されます（「閉じ」の顔の文字は自動で消します）。

今の素材は頭頂部が画像の端で切れているため、上側をぼかして目立たなくしています。
作り直す場合は「頭の上に余白を空けて」と指示すると、よりきれいに表示されます。

## 8. 台本の品質チェックの仕組み

企画とズレた・中身のない台本を防ぐため、台本は次の3段階で作られます。

1. **取材**：Google 検索（Gemini のグラウンディング機能）で、企画に関する具体的な事実を集めた取材メモを作成
2. **執筆**：取材メモの事実を必ず使い、企画の構成どおりに台本を書く
3. **採点**：別の呼び出しで「企画との一致度」「具体性」を10点満点で採点。基準（既定は一致8点・具体性7点）に届かなければ、指摘を渡して最大3回まで書き直し

台本ができた時点で、Discord に章構成と採点結果が届きます。
Artifacts の `research.md`（取材メモ）と `review.json`（採点と指摘）を見ると、どこで内容が弱くなったか確認できます。
基準は環境変数 `MIN_RELEVANCE` / `MIN_SPECIFICITY` / `SCRIPT_ROUNDS` で変更できます。

## 9. クレジット表記について

- **VOICEVOX**：利用規約により「VOICEVOX:キャラクター名」の表記が必要です。概要欄に自動で入ります。キャラクターごとに個別の規約があるため、使う話者の規約も確認してください。
- **BGM**：フリー素材でもクレジット表記が必要なものが多いため、`BGM_CREDIT` に設定してください。

## 10. 公開前のチェックポイント

- 台本の事実関係（特に数字・固有名詞・年号）
- 画像に崩れた文字や不自然な人物が出ていないか
- YouTube Studio の「改変または合成されたコンテンツ」が「はい」になっているか（アップロード時に自動設定しています）
