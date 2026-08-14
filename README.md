# 朝の市況をLINEに送る（日経VI / Fear&Greed / 世界の株価）

平日の朝、GitHub Actions が次の2通をLINEに送る。既存の `stockdiscipline` とは独立した新規リポジトリ。

1. **画像**：`nikkei225jp.com` のタイル一覧を撮影したもの
2. **Flexメッセージ**：日経VI と Fear&Greed 指数（数値を色分け）＋リンクボタン2つ

見た目の確認は `preview.png` / `preview.html`（LINEの通数を消費せずに配色を詰めるためのプレビュー）。

---

## ファイル構成

```
<新しいリポジトリ>/
├── .github/workflows/morning-notify.yml   ← morning-notify.yml をこの名前・場所に
├── capture_world.py     世界の株価ページの撮影
├── notify_line.py       データ取得・Flex組み立て・送信
├── preview_flex.py      Flexの見え方をローカル確認（任意）
└── requirements.txt
```

---

## 表示ルール

**日経VI**（日経平均プロフィルの公開CSV、前営業日終値）

| 水準 | 表示 |
|---|---|
| 50以上 | 数値が**赤** ＋「⚠ 50超」 |
| 40以上 | 数値が**オレンジ** ＋「⚠ 40超」 |
| 30以上 | 数値は黒、黄色で「30超」バッジのみ（新規テーゼ起票の目安） |
| 30未満 | 数値は黒 |

前日比と、**週平均（直近5営業日）／月平均（直近21営業日）**を併記。

**Fear & Greed 指数**（CNN）

| スコア | 区分 | 色 |
|---|---|---|
| 76–100 | 極端な強欲 | 濃い緑 |
| 55–75 | 強欲 | 緑 |
| 46–54 | 中立 | グレー |
| 26–45 | 恐怖 | オレンジ |
| 0–25 | 極端な恐怖 | 赤 |

前日比と、**週平均（直近7日）／月平均（直近30日）**を併記。

閾値と色は `notify_line.py` 冒頭の `VI_BANDS` / `FG_BANDS` を書き換えれば変わる。

---

## 手順

### 1. リポジトリを作る

**Public** で作成すること。画像を `raw.githubusercontent.com` 経由でLINEに読ませるため、
Privateだと画像が届かない（Flexのテキスト部分は届く）。

### 2. Secrets を登録

Settings → Secrets and variables → Actions → Secrets：

| Name | 値 |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Developers ［Messaging API設定］の長期トークン |
| `LINE_USER_ID` | ［チャネル基本設定］の「あなたのユーザーID」 |

### 3. ワークフローの権限

Settings → Actions → General → Workflow permissions を
**Read and write permissions** にする（スクショを `snapshot` ブランチへ置くため）。

### 4. 初回実行

Actions → `morning-market-line` → **Run workflow**。

- LINEに2通届けば完了
- 画像なしで通知だけ試したいときは、実行時に `skip_image` にチェック

### 5. 撮影範囲を合わせる

初回の画像が意図とずれていたら調整する。

1. 実行結果の **Artifacts → world-screenshot** をダウンロードして現状を確認
2. 手元で `python capture_world.py --grid` を実行すると、100px方眼と座標入りの
   `debug_full.jpg` が出るので、欲しい範囲の `x,y,幅,高さ` を読み取る
3. Settings → Secrets and variables → Actions → **Variables** に登録

| Variable | 例 | 意味 |
|---|---|---|
| `CLIP` | `0,210,1000,1180` | 撮影範囲（ページ座標） |
| `VIEWPORT` | `1000x1320` | ブラウザの表示サイズ |
| `SCROLL_Y` | `200` | CLIP未指定時に撮影前スクロールする量 |
| `WAIT_MS` | `12000` | チャート描画の待ち時間 |
| `BRIEF_URL` | `https://…/morning-brief/` | ボタンのリンク先 |

---

## 配信時刻

平日 **8:00 / 11:40 / 15:00 / 18:00 JST** の4回。cron はUTC表記なので9時間引いた値になっている：

```yaml
- cron: "0 23 * * 0-4"  # 08:00 JST（UTCでは前日23時なので曜日が0-4にずれる）
- cron: "40 2 * * 1-5"  # 11:40 JST
- cron: "0 6 * * 1-5"   # 15:00 JST
- cron: "0 9 * * 1-5"   # 18:00 JST
```

時刻を変えるときはJSTから9時間引いてUTCに直す。0時をまたぐ場合は曜日フィールドもずれる。

### 各回で新しくなる情報

| データ | 更新頻度 |
|---|---|
| 世界の株価の画像 | 毎回その時点のチャートが撮れる（これが日中配信の主目的） |
| F&G指数 | 米市場に連動して日中も動く |
| 日経VI | **日次CSVのため1日1回**。日中の配信では前営業日終値のまま。当日終値は夕方の更新後（18時の回で反映されることが多い）。データ日付はメッセージ内に表示される |

---

## LINEから「取得」で手動実行する（任意）

LINEで `取得`（または `更新` / `test`）と送ると、その場でワークフローが走って最新の通知が届く。
仕組みは：LINEのWebhook → Cloudflare Workers（無料）→ GitHub Actions起動。

なお、**Webhookを組まなくても** GitHubのActionsタブ（スマホならGitHubアプリ）の
「Run workflow」ボタンで同じことはできる。LINEから完結させたい場合のみ以下を設定する。

### a. GitHubのトークンを作る

GitHub → Settings → Developer settings → **Fine-grained personal access tokens** → Generate new token

- Repository access: **このリポジトリだけ**を選択
- Permissions → Repository permissions → **Actions: Read and write**（それ以外は付けない）
- 有効期限が切れたら作り直してWorkerの変数を更新する

### b. Cloudflare Workerを作る

1. [Cloudflare](https://dash.cloudflare.com/) に無料登録 → Workers & Pages → Create Worker
2. エディタに `worker.js` の中身を貼り付けてデプロイ
3. Worker の Settings → Variables and Secrets に以下を **Secret** として登録：

| 変数名 | 値 |
|---|---|
| `LINE_CHANNEL_SECRET` | LINE Developers ［チャネル基本設定］のチャネルシークレット |
| `LINE_CHANNEL_ACCESS_TOKEN` | ［Messaging API設定］の長期トークン |
| `GITHUB_PAT` | 手順aのトークン |
| `GITHUB_REPO` | `ユーザー名/リポジトリ名` |
| `WORKFLOW_FILE` | `morning-notify.yml` |

### c. LINE側にWebhookを設定

LINE Developers ［Messaging API設定］タブで：

1. **Webhook URL** に Worker のURL（`https://～.workers.dev`）を入れて保存 → 「検証」で成功を確認
2. **Webhookの利用** をオンにする
3. 「応答メッセージ」が有効だと定型文が返ってきて邪魔なので、リンク先の
   LINE Official Account Manager で応答メッセージをオフにする

### 動きと所要時間

`取得` と送る → すぐ「取得を開始しました」と返信が来る → **2〜4分後**に通知が届く。
時間がかかるのはGitHub Actionsの起動とブラウザ準備のため（ブラウザはキャッシュして短縮している）。
返信はReply APIなので無料枠を消費しない。本体の通知は通常どおり1通消費する。

---

## 注意点

- **通数**：LINEの課金カウントは「1回のpushリクエスト＝宛先1人で1通」なので、画像+Flexをまとめて送る本構成は
  1回の配信で1通。**1日4回 × 平日 ≒ 月88通**で、無料枠200通の範囲内。
  もしカウントがメッセージ単位（1回2通）に変わっても月176通で枠内だが、配信回数を増やす余地はなくなる。
  実際の消費は LINE Official Account Manager のダッシュボードで確認できる
- **cronのずれ**：GitHub Actions の定時実行は数分〜30分ほど遅れることがあり、混雑時は飛ぶこともある
- **60日ルール**：リポジトリに60日間コミットが無いとスケジュールが自動停止する。
  この構成は毎回 `snapshot` ブランチへpushするので通常は該当しないが、停止メールが来たらActionsから再有効化する
- **F&Gの取得元**：CNNの非公開エンドポイント。仕様変更で落ちる可能性がある。
  落ちてもF&G欄が「取得失敗」になるだけで、日経VIと画像は届く
- **画像の扱い**：`nikkei225jp.com` は Stockbrain Ltd. の著作物。自分1人への配信という私的利用の範囲に留め、
  再配布はしないこと。アクセスも1日1回に限る
- **休場日**：日本の祝日でも実行される。その日は前営業日の値が再送される
- **リポジトリ容量**：`snapshot` ブランチは毎回作り直すので画像が履歴に積み上がらない

---

## ローカルでの確認

```bash
pip install -r requirements.txt
python -m playwright install chromium

python notify_line.py --dry-run   # 送信せずJSONを表示（要ネットワーク）
python preview_flex.py --demo     # ダミー値で preview.html を生成
python capture_world.py --grid    # 撮影範囲の調整用
```
