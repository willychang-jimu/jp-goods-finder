"""真特價判斷：只跟「自己記錄的歷史實際價格」比，不信網頁上的原價／折扣標示。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class Deal:
    kind: str              # "90d" 或 "30d"
    today_price: int
    prev_low: int          # 過去 N 天（不含今天）的最低實際價格
    drop_pct: float
    history_days: int


def detect_deal(history: list[tuple[str, int]], today: str, cfg: dict) -> Deal | None:
    """history: [(date, effective_price)]，需包含今天那筆。

    規則：今天的實際價格比「過去 30／90 天、不含今天」的最低價再低 min_drop_pct% 以上，
    且紀錄天數夠多，才算真特價。90 日新低優先於 30 日新低。
    """
    todays = [p for d, p in history if d == today]
    if not todays:
        return None
    price = min(todays)
    t = date.fromisoformat(today)
    past = [(d, p) for d, p in history if d < today]
    min_drop = float(cfg.get("min_drop_pct", 3.0))

    for kind, days, need in (("90d", 90, cfg.get("min_history_days_90", 30)),
                             ("30d", 30, cfg.get("min_history_days_30", 7))):
        since = (t - timedelta(days=days)).isoformat()
        window = [p for d, p in past if d >= since]
        n_days = len({d for d, _ in past if d >= since})
        if n_days < need or not window:
            continue
        low = min(window)
        drop = (low - price) / low * 100 if low else 0.0
        if drop >= min_drop:
            return Deal(kind, price, low, round(drop, 1), n_days)
    return None
