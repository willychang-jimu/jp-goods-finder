"""資料儲存：全部是純文字檔，由 GitHub Actions commit 回 repo，diff 易讀。

data/
  items.json          商品主檔（含中文翻譯、評分快取、推播紀錄）
  prices/YYYY-MM.csv  每日價格快照（按月分檔，只增不改）
  feedback.json       Telegram 喜歡／略過紀錄
  state.json          其他狀態（Telegram getUpdates offset 等）
"""
from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path

from .rakuten import Item

PRICE_FIELDS = ["date", "id", "price", "point_rate", "effective_price", "available"]


class Store:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        (self.dir / "prices").mkdir(parents=True, exist_ok=True)
        self.items: dict[str, dict] = self._load_json("items.json", {})
        self.feedback: list[dict] = self._load_json("feedback.json", [])
        self.state: dict = self._load_json("state.json", {})

    # ---------- JSON ----------
    def _load_json(self, name: str, default):
        p = self.dir / name
        if not p.exists():
            return default
        return json.loads(p.read_text(encoding="utf-8"))

    def _save_json(self, name: str, obj) -> None:
        p = self.dir / name
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                       encoding="utf-8")
        tmp.replace(p)

    def save(self) -> None:
        self._save_json("items.json", self.items)
        self._save_json("feedback.json", self.feedback)
        self._save_json("state.json", self.state)

    # ---------- 商品主檔 ----------
    def upsert_item(self, it: Item, today: str) -> dict:
        rec = self.items.setdefault(it.id, {"first_seen": today})
        rec.update({
            "item_code": it.item_code,
            "name": it.name,
            "url": it.url,
            "shop_name": it.shop_name,
            "image_url": it.image_url,
            "category": it.category,
            "review_count": it.review_count,
            "review_average": it.review_average,
            "postage_included": it.postage_included,
            "last_seen": today,
            "last_price": it.price,
            "last_point_rate": it.point_rate,
            "last_effective_price": it.effective_price,
        })
        if it.rank:
            rec["last_rank"] = it.rank
        kws = rec.setdefault("keywords", [])
        kws.extend(k for k in it.keywords if k not in kws)
        return rec

    # ---------- 價格歷史 ----------
    def _price_file(self, d: str) -> Path:
        return self.dir / "prices" / f"{d[:7]}.csv"

    def record_prices(self, items: list[Item], today: str) -> int:
        """寫入今天的價格；同一天重跑不會重複寫（以 (date, id) 去重）。"""
        path = self._price_file(today)
        existing = set()
        if path.exists():
            with open(path, encoding="utf-8", newline="") as f:
                existing = {(r["date"], r["id"]) for r in csv.DictReader(f)}
        new_rows = [
            {"date": today, "id": it.id, "price": it.price, "point_rate": it.point_rate,
             "effective_price": it.effective_price, "available": int(it.available)}
            for it in items if (today, it.id) not in existing
        ]
        write_header = not path.exists()
        with open(path, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=PRICE_FIELDS)
            if write_header:
                w.writeheader()
            w.writerows(new_rows)
        return len(new_rows)

    def price_history(self, since: str, until: str) -> dict[str, list[tuple[str, int]]]:
        """回傳 {id: [(date, effective_price), ...]}，含 since 與 until 兩端。"""
        out: dict[str, list[tuple[str, int]]] = {}
        d0, d1 = date.fromisoformat(since), date.fromisoformat(until)
        months = sorted({(d0 + timedelta(days=i)).isoformat()[:7]
                         for i in range((d1 - d0).days + 1)})
        for m in months:
            path = self.dir / "prices" / f"{m}.csv"
            if not path.exists():
                continue
            with open(path, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    if since <= r["date"] <= until:
                        out.setdefault(r["id"], []).append((r["date"], int(r["effective_price"])))
        return out

    # ---------- 回饋 ----------
    def add_feedback(self, item_id: str, action: str, at: str) -> None:
        self.feedback.append({"id": item_id, "action": action, "at": at})
        rec = self.items.get(item_id)
        if rec is not None:
            rec["feedback"] = action

    def feedback_examples(self, action: str, limit: int) -> list[str]:
        """最近的喜歡／略過商品（中文標題優先），給 Claude 當口味參考。"""
        seen, out = set(), []
        for fb in reversed(self.feedback):
            if fb["action"] != action or fb["id"] in seen:
                continue
            seen.add(fb["id"])
            rec = self.items.get(fb["id"])
            if rec:
                out.append(rec.get("zh_title") or rec.get("name", ""))
            if len(out) >= limit:
                break
        return out
