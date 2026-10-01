"""收藏（★）商品的降價提醒。

流程（每日執行時）：
  1. refresh_prices：收藏的商品如果沒出現在今天的關鍵字結果裡，用 itemCode 直接向樂天查今天的價格。
  2. find_alerts：今天的實付價比「基準價」低 watch.min_drop_pct % 以上才提醒。
     基準價 = 收藏當下的價格；每次提醒後下修成當時的價格，所以同一個價格不會重複通知，
     再降一截才會再通知。
  3. run_watchlist：推播，成功送達後才下修基準價（失敗或只印在 log 時，明天還有機會）。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from .deals import detect_deal
from .notify import Alert
from .rakuten import Item
from .store import Store

log = logging.getLogger(__name__)


def refresh_prices(store: Store, client, today_items: dict[str, Item], cfg: dict, today: str) -> int:
    """替今天沒出現在搜尋結果的收藏商品補查價格；回傳補查成功的件數。"""
    if client is None:
        return 0
    wcfg = cfg.get("watch", {})
    missing = [iid for iid in store.watched_ids() if iid not in today_items][: wcfg.get("max_lookups", 40)]
    found: list[Item] = []
    for iid in missing:
        rec = store.items[iid]
        try:
            it = client.lookup(rec["item_code"], rec.get("category", ""))
        except Exception as e:  # noqa: BLE001 — 單件查不到不該影響其他收藏
            log.warning("收藏商品查價失敗 %s：%s", rec.get("item_code"), e)
            continue
        if it is None or not it.available:
            log.info("收藏商品今天查不到或已賣完：%s", rec.get("item_code"))
            continue
        found.append(it)
    for it in found:
        store.upsert_item(it, today)
    store.record_prices(found, today)
    log.info("收藏商品補查價格：%d / %d 件", len(found), len(missing))
    return len(found)


def find_alerts(store: Store, cfg: dict, today: str) -> list[Alert]:
    min_drop = float(cfg.get("watch", {}).get("min_drop_pct", 3.0))
    since = (date.fromisoformat(today) - timedelta(days=90)).isoformat()
    history = store.price_history(since, today)
    out: list[Alert] = []
    for iid in store.watched_ids():
        rec = store.items[iid]
        price = rec.get("last_effective_price")
        if rec.get("last_seen") != today or price is None:
            continue  # 今天沒有最新價格（查不到／已賣完）
        base = rec.get("alert_baseline")
        if base is None:
            rec["alert_baseline"] = price
            continue
        if price <= base * (1 - min_drop / 100):
            deal = detect_deal(history.get(iid, []), today, cfg["deals"])
            out.append(Alert(iid, rec, price, base, rec.get("watch_price") or base, deal))
    return out


def run_watchlist(store: Store, client, today_items: dict[str, Item], notifiers: list, cfg: dict,
                  today: str, mark: bool = True) -> list[Alert]:
    refresh_prices(store, client, today_items, cfg, today)
    alerts = find_alerts(store, cfg, today)
    if not alerts:
        return []
    title = f"{today} 收藏降價提醒"
    all_ok, delivered = True, False
    for nt in notifiers:
        if not hasattr(nt, "send_alerts"):
            continue
        try:
            nt.send_alerts(alerts, title)
            delivered = delivered or getattr(nt, "name", "") != "console"
        except Exception as e:  # noqa: BLE001
            all_ok = False
            log.error("降價提醒推播 %s 失敗：%s", getattr(nt, "name", nt), e)
    if mark and all_ok and delivered:
        for a in alerts:
            a.rec["alert_baseline"] = a.price
            a.rec["last_alert_at"] = today
    return alerts
