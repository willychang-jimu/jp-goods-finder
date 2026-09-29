# jp-goods-finder 日本好物追蹤

每天自動掃樂天市場，挑出符合口味的潮流小物、居家擺飾、新奇電子、穿搭，
翻成繁體中文、用 Claude 評分，偵測**真特價**，再用 Telegram 推播每日精選前 5 名。

> 目前狀態：**只開樂天＋Dashboard**。每天記價格、在網頁上看商品與價格走勢；
> Claude 翻譯評分、Telegram 推播都已寫好但先關閉，之後決定要用再補 key、改設定即可。
>
> Dashboard：<https://willychang-jimu.github.io/jp-goods-finder/>

## 運作方式

```
cron-job.org ──(workflow_dispatch)──▶ GitHub Actions: daily.yml
                                         │
  1. 同步 Telegram「喜歡／略過」按鈕 ◀─────┤
  2. 樂天 API：四類關鍵字搜尋 ＋ 分類排行榜  │
  3. 存今日價格 → data/prices/YYYY-MM.csv   │
  4. 新商品丟給 Claude Haiku 翻譯＋評分（結果快取，不重複花錢）
  5. 真特價判斷：實際價格 < 自己記錄的 30／90 日最低價
  6. 挑前 5 名（每類最多 2 個）→ Telegram（圖片＋按鈕）
  7. data/ commit 回 repo
```

### 「真特價」怎麼算

- **不看網頁標示的原價或折扣**（日本常見二重價格）。只跟自己每天記下的價格比。
- **實際價格 = 售價 − 點數回饋**（樂天點數 10 倍的 ¥3,990 → 實付約 ¥3,591）。
- 今天實際價格比過去 30 日（至少 7 天紀錄）或 90 日（至少 30 天紀錄）的最低價**再低 3% 以上**才算。
- 所以剛開始跑的前一週不會有任何特價提醒，這是正常的。

## 安裝設定（一次性）

### 1. 樂天 Rakuten Web Service（免費）

2026 年起樂天 API 改版，舊的應用程式 ID 不能用，必須在新的 Developer Dashboard 重新登錄：

1. 到 <https://webservice.rakuten.co.jp/> 登入樂天會員 → 新增應用程式。
2. 「許可Webサイト」（允許的網站）填 **`https://willychang-jimu.github.io/jp-goods-finder/`**
   （伺服器呼叫時程式會用這個網址當 Referer／Origin，不一致會回 403）。
3. 記下 **Application ID** 和 **Access Key**（兩個都要）。

### 2. Anthropic API Key

<https://console.anthropic.com/> 建立 API Key。使用 Claude Haiku 4.5，
每天約 1–3 次呼叫、每月花費預估 1 美元左右（只有新商品才會送去評分，實際依數量而定）。
沒設定時程式仍會跑，只是不翻譯、所有商品都給 6 分。

### 3. Telegram Bot

1. Telegram 搜尋 **@BotFather** → `/newbot` → 取得 **Bot Token**。
2. 先對你的 bot 傳任何一句話，再打開
   `https://api.telegram.org/bot<TOKEN>/getUpdates`，找 `"chat":{"id": ...}` 那個數字就是 **Chat ID**。

### 4. GitHub Secrets

Repo → Settings → Secrets and variables → Actions → New repository secret：

| 名稱 | 內容 |
|---|---|
| `RAKUTEN_APP_ID` | 樂天 Application ID |
| `RAKUTEN_ACCESS_KEY` | 樂天 Access Key |
| `ANTHROPIC_API_KEY` | Anthropic API Key |
| `TELEGRAM_BOT_TOKEN` | Bot Token |
| `TELEGRAM_CHAT_ID` | Chat ID |

（選用）若許可網站不是預設的 GitHub Pages 網址，在 **Variables** 加 `RAKUTEN_REFERER`。

### 5. 開啟 GitHub Pages（Dashboard）

Repo → Settings → Pages → **Build and deployment → Source 選「GitHub Actions」**。
之後每次 workflow 跑完都會自動更新 Dashboard。

### 6. cron-job.org 排程

建立兩個排程，都是 `POST https://api.github.com/repos/willychang-jimu/jp-goods-finder/actions/workflows/daily.yml/dispatches`，
Header 帶 `Authorization: Bearer <GitHub fine-grained token（此 repo 的 Actions: Read and write）>`、
`Accept: application/vnd.github+json`：

| 排程 | Body | 建議時間 |
|---|---|---|
| 每日精選 | `{"ref":"main","inputs":{"mode":"daily"}}` | 每天 20:00（台灣時間） |
| 回饋同步 | `{"ref":"main","inputs":{"mode":"feedback"}}` | 每 4 小時 |

回饋同步要另外排，是因為 Telegram 只保留 24 小時內的按鈕點擊紀錄。

### 只設樂天也能先跑

只放樂天兩個 Secret 也可以先開始每天記價格（特價判斷要累積天數，越早開始越好）：
- 沒有 `ANTHROPIC_API_KEY`：不翻譯、全部給 6 分；之後補上 key，會自動重新翻譯評分。
- 沒有 Telegram：精選只印在 Actions log，**不會標記成已推播**，之後接上 Telegram 仍會正常推。

## 本機執行

```bash
pip install -r requirements-dev.txt
python -m pytest                          # 單元測試
python -m jpgf daily --mock --dry-run     # 假資料跑完整流程，不需任何金鑰
cp .env.example .env                      # 填入金鑰（.env 不會被 commit）
python -m jpgf daily --dry-run            # 真的打樂天／Claude，但只印在畫面上
```

## Dashboard

每次執行後自動部署到 GitHub Pages，不需要任何 API Key：

- 商品卡片：圖片、實付價、近 90 天價格迷你走勢、真特價標籤
- 分類篩選、只看真特價、搜尋、多種排序
- 點卡片看詳細價格走勢（滑鼠／手指移動可看每天價格，也可切成表格）
- 沒開 Claude 翻譯時，詳細頁有「Google 翻譯」連結
- 本機預覽：`python -m jpgf dashboard --mock --out out/site`，再開 `out/site/index.html`
  （需用 `python -m http.server` 在該目錄起伺服器，直接開檔案會被瀏覽器擋 fetch）

## 調整口味

全部在 [`config.yaml`](config.yaml)：

- `taste_profile`：用中文描述喜好，會直接放進 Claude 的評分提示詞。
- `categories.*.keywords`：每類的日文搜尋關鍵字。
- `selection`：每天推幾個、最低分數、同一商品多久內不重推。
- `deals`：特價門檻。
- `notifiers`：推播通道開關。

在 Telegram 按「👍 喜歡／👎 略過」後，之後的評分會把這些商品當作口味範例；
按過略過的商品不會再推，按過喜歡的只有真特價時才會再推。

## 資料

`data/` 由 Actions 自動 commit，純文字、可直接看 diff：

- `items.json`：商品主檔（中文標題、評分、推播紀錄、最新價格）
- `prices/YYYY-MM.csv`：每日價格快照
- `feedback.json`：喜歡／略過紀錄
- `state.json`：Telegram 讀取進度等

## 路線圖

- [x] **第 1 階段**：樂天 API → 關鍵字／排行榜 → 每日存價 → Claude 翻譯＋評分 → Telegram 每日前 5 名＋喜歡／略過按鈕
- [x] **Dashboard**：GitHub Pages 商品卡片、價格走勢、真特價標籤、篩選排序
- [ ] **第 2 階段**：Dashboard 上的喜歡／略過、降價提醒（追蹤按過喜歡的商品）、Email 週報
- [ ] **第 3 階段**：YouTube 開箱影片搜尋＋中文摘要、Keepa API 接 Amazon.co.jp
