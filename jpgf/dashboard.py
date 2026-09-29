"""產生 GitHub Pages Dashboard：jpgf/web/index.html（靜態頁）＋ data.json（由 data/ 算出）。

不需要任何 API Key；只讀 data/ 裡已記錄的商品與價格。
"""
from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .deals import detect_deal
from .store import Store

WEB_DIR = Path(__file__).resolve().parent / "web"


def build_data(store: Store, cfg: dict, today: str, active_days: int = 14,
               history_days: int = 90) -> dict:
    """只收最近 active_days 天內還有被抓到的商品；價格走勢取最近 history_days 天。"""
    t = date.fromisoformat(today)
    since = (t - timedelta(days=history_days)).isoformat()
    active_since = (t - timedelta(days=active_days)).isoformat()
    history = store.price_history(since, today)
    labels = {k: v.get("label", k) for k, v in cfg["categories"].items()}

    items = []
    for iid, rec in store.items.items():
        if rec.get("last_seen", "") < active_since:
            continue
        series = sorted(history.get(iid, []))
        prices = [p for _, p in series]
        deal = detect_deal(series, rec["last_seen"], cfg["deals"]) if series else None
        scored = rec.get("scored_by") == "claude"
        items.append({
            "id": iid,
            "name": rec.get("name", ""),
            "zh_title": rec.get("zh_title") if scored else None,
            "zh_summary": rec.get("zh_summary") if scored else None,
            "score": rec.get("score") if scored else None,
            "url": rec.get("url", ""),
            "image": rec.get("image_url", ""),
            "shop": rec.get("shop_name", ""),
            "category": rec.get("category", ""),
            "price": rec.get("last_price"),
            "point_rate": rec.get("last_point_rate", 1),
            "effective": rec.get("last_effective_price"),
            "postage_included": rec.get("postage_included", True),
            "reviews": rec.get("review_count", 0),
            "rating": rec.get("review_average", 0),
            "rank": rec.get("last_rank"),
            "first_seen": rec.get("first_seen"),
            "last_seen": rec.get("last_seen"),
            "feedback": rec.get("feedback"),
            "series": series,
            "low": min(prices) if prices else None,
            "high": max(prices) if prices else None,
            "deal": {"kind": deal.kind, "prev_low": deal.prev_low, "drop_pct": deal.drop_pct}
                    if deal else None,
        })

    days_recorded = len({d for s in history.values() for d, _ in s})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "today": today,
        "days_recorded": days_recorded,
        "deal_rules": {k: cfg["deals"][k] for k in
                       ("min_history_days_30", "min_history_days_90", "min_drop_pct")},
        "categories": labels,
        "items": items,
    }


def build_site(store: Store, cfg: dict, out_dir: Path, today: str) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(WEB_DIR / "index.html", out_dir / "index.html")
    (out_dir / ".nojekyll").write_text("")
    data = build_data(store, cfg, today)
    (out_dir / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return out_dir
