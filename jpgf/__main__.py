"""命令列入口。

  python -m jpgf daily              # 每日流程（抓價格、評分、推播）
  python -m jpgf daily --dry-run    # 只印在畫面上，不推 Telegram、不標記已推播
  python -m jpgf daily --mock       # 用 tests/fixtures 的假資料，不需任何金鑰
  python -m jpgf feedback           # 只同步 Telegram 喜歡／略過按鈕
  python -m jpgf dashboard          # 由 data/ 產生 GitHub Pages 靜態頁到 site/
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .ai import ClaudeScorer, NoopScorer
from .config import ROOT, env, load_config, load_dotenv
from .feedback import sync_feedback
from .notify import TelegramNotifier, build_notifiers
from .pipeline import run_daily, today_jst
from .rakuten import RakutenClient, fetch_all, parse_item
from .store import Store

log = logging.getLogger("jpgf")


def mock_items(cfg: dict):
    data = json.loads((ROOT / "tests" / "fixtures" / "rakuten_sample.json").read_text("utf-8"))
    items = []
    for cat_key, raws in data.items():
        for raw in raws:
            it = parse_item(raw, cat_key)
            if it:
                items.append(it)
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jpgf")
    ap.add_argument("mode", choices=["daily", "feedback", "dashboard"])
    ap.add_argument("--out", default=None, help="dashboard 輸出目錄（預設 site/）")
    ap.add_argument("--config", default=None)
    ap.add_argument("--data-dir", default=None,
                    help="資料目錄（預設 data/；--mock 時預設 out/mock-data，避免污染正式資料）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock", action="store_true", help="使用假資料，不打樂天 API")
    ap.add_argument("--date", default=None, help="覆寫今天日期（YYYY-MM-DD，測試用）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    cfg = load_config(args.config)
    data_dir = args.data_dir or str(ROOT / ("out/mock-data" if args.mock else "data"))
    store = Store(data_dir)
    today = args.date or today_jst()

    if args.mode == "dashboard":
        from .dashboard import build_site

        out = build_site(store, cfg, Path(args.out or ROOT / "site"), today)
        log.info("Dashboard 已產生：%s", out)
        return 0

    tg = None
    if cfg.get("notifiers", {}).get("telegram") and not args.dry_run:
        token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
        if token and chat:
            tg = TelegramNotifier(token, chat)

    # 先同步回饋，讓今天的評分能參考最新的喜歡／略過
    if tg:
        try:
            sync_feedback(tg, store)
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram 回饋同步失敗：%s", e)

    if args.mode == "feedback":
        store.save()
        return 0

    if args.mock:
        items = mock_items(cfg)
    else:
        client = RakutenClient(env("RAKUTEN_APP_ID"), env("RAKUTEN_ACCESS_KEY"),
                               env("RAKUTEN_REFERER"), cfg["rakuten"])
        items = fetch_all(client, cfg["categories"], cfg["rakuten"].get("use_ranking", True))
    if not items:
        log.error("沒有抓到任何商品，請檢查樂天 API 金鑰／許可網站設定")
        store.save()
        return 1

    api_key = env("ANTHROPIC_API_KEY")
    scorer = ClaudeScorer(api_key, cfg["ai"]["model"]) if api_key else NoopScorer()
    if not api_key:
        log.warning("未設定 ANTHROPIC_API_KEY：不翻譯、不評分（全部給 6 分）")

    notifiers = build_notifiers(cfg, dry_run=args.dry_run)
    run_daily(store, items, scorer, notifiers, cfg, today,
              real_scorer=bool(api_key), mark_notified=not args.dry_run)
    store.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
