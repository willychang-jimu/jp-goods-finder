# CLAUDE.md — 給接手的 AI 看

## 專案是什麼

jp-goods-finder：追蹤日本購物平台（目前只有樂天），每天挑出符合使用者口味的商品、
偵測真特價，翻成繁中後推 Telegram。與股票專案 tw-stock-daily-highlights- **無關**，不要混用程式或設定。

使用者用**繁體中文**溝通；程式註解、log、推播文字也用繁中。

使用者興趣：潮流小物、居家擺飾、新奇電子產品、穿搭。

## 已定案、不要推翻的決策

- **資料源**：樂天官方 Rakuten Web Service API。**不要爬 Amazon.co.jp**（違反條款、雲端 IP 被擋），之後改用付費 Keepa API。
- **樂天 API 是 2026 新規格**：`applicationId` + `accessKey` 都必填；伺服器端要送 Referer/Origin 且需與「許可Webサイト」一致，否則 403。
  端點在 `config.yaml` 的 `rakuten.*_endpoint`，版本更新時改那裡。
- **特價判斷**：只跟 `data/prices/` 自己記的價格比（30／90 日最低價），**不信網頁上的原價或折扣%**（二重價格）。
  價格一律用「實際價格 = 售價 − 點數回饋」（`rakuten.effective_price`）。
- **AI**：Claude Haiku 4.5，模型 ID `claude-haiku-4-5-20251001`（使用者指定，勿自行換模型）。
  用 structured outputs（`output_config.format` json_schema）拿 JSON。評分結果快取在 `items.json`，同商品不重複呼叫。
- **排程**：外部 cron-job.org 打 GitHub Actions `workflow_dispatch`。**不要加 GitHub 原生 `schedule:`**（對此帳號不穩定）。
- **密鑰**：只從環境變數讀（GitHub Secrets／本機 `.env`）。絕不寫死、不 commit、不印進 log（錯誤訊息別印含 token 的 URL）。
- **推播**：`notify.py` 的通道由 `config.yaml` 的 `notifiers` 開關；新通道實作 `send_picks(picks, title)` 後加到 `build_notifiers()`。

## 架構

```
jpgf/
  __main__.py   CLI：python -m jpgf {daily|feedback} [--dry-run] [--mock] [--date]
  config.py     讀 config.yaml、.env
  rakuten.py    樂天 API client、Item、effective_price、fetch_all（去重）
  store.py      data/ 讀寫（items.json、prices/*.csv、feedback.json、state.json）
  deals.py      detect_deal：30/90 日新低
  ai.py         ClaudeScorer（翻譯＋評分）、NoopScorer（沒 key 時）
  pipeline.py   run_daily：upsert → 存價 → 評分 → 選品 → 推播 → 標記已推
  notify.py     ConsoleNotifier、TelegramNotifier、Pick、format_caption
  feedback.py   Telegram getUpdates → 喜歡／略過
config.yaml     口味、關鍵字、門檻、開關（無密鑰）
data/           由 Actions commit 的資料（勿手動大改格式；若改格式要寫遷移）
tests/          pytest；fixtures/rakuten_sample.json 是 --mock 用的假資料
.github/workflows/daily.yml  正式執行（workflow_dispatch，mode=daily|feedback）
.github/workflows/test.yml   push/PR 跑 pytest ＋ mock 端到端
```

商品 ID：`sha1(itemCode)[:12]`（Telegram callback_data 上限 64 bytes）。日期一律 JST（`pipeline.today_jst`）。

## 開發規則

- 改完先跑：`python -m pytest -q` 和 `python -m jpgf daily --mock --dry-run`（mock 預設寫到 `out/mock-data/`，不會污染 `data/`）。
- 選品／特價邏輯改動要補測試（`tests/test_core.py`）。
- `data/prices/*.csv` 只增不改；`record_prices` 同日重跑會去重。
- Telegram caption 上限 1024 字：欄位先截短再 `html.escape`，不要對跳脫後字串硬切。

## 路線圖

1. ✅ 第 1 階段 MVP（本版）
2. 第 2 階段：GitHub Pages Dashboard（商品卡片、中文標題、價格走勢、喜歡／略過）、降價提醒（追蹤按過喜歡的商品）、Email 週報（新 notifier）
3. 第 3 階段：YouTube Data API 搜「商品名 開封／レビュー」→ Claude 中文摘要；Keepa API 接 Amazon.co.jp

## 待使用者提供／確認

- 常逛的樂天店家或心動商品範例（用來校準 `taste_profile` 與關鍵字）
- 穿搭關鍵字要偏男裝、女裝或中性（目前是中性）
- 樂天 Application ID ＋ Access Key、Anthropic API Key、Telegram Bot Token ＋ Chat ID（放 GitHub Secrets）
- 樂天分類排行榜的 genre ID（config.yaml 註解）尚未用真實 API 驗證
