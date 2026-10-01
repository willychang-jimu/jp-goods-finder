"""資料儲存：全部是純文字檔，由 GitHub Actions commit 回 repo，diff 易讀。

data/
  items.json          商品主檔（含中文翻譯、評分快取、推播紀錄）
  prices/YYYY-MM.csv  每日價格快照（按月分檔，只增不改）
  feedback.json       喜歡／略過／收藏／取消收藏紀錄（Telegram 按鈕、Dashboard ✕／♡／★）
  requests.json       使用者傳給 Telegram Bot 的「我想找…」需求與結果
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
        self.requests: list[dict] = self._load_json("requests.json", [])

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
        self._save_json("requests.json", self.requests)
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
    def add_feedback(self, item_id: str, action: str, at: str, source: str = "telegram") -> None:
        """action：like／skip（口味，存在 rec["feedback"]）、watch／unwatch（收藏＝追蹤降價，存在 rec["watched"]）。
        兩組互相獨立：一件商品可以同時 ♡ 又 ★。"""
        self.feedback.append({"id": item_id, "action": action, "at": at, "source": source})
        rec = self.items.get(item_id)
        if rec is None:
            return
        if action == "watch":
            if not rec.get("watched"):  # 已收藏的再按一次不重設基準價
                price = rec.get("last_effective_price")
                rec.update({"watched": True, "watch_at": at, "watch_price": price, "alert_baseline": price})
        elif action == "unwatch":
            rec["watched"] = False
            for k in ("watch_at", "watch_price", "alert_baseline"):
                rec.pop(k, None)
        else:
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

    def liked_examples(self, limit: int) -> list[str]:
        """口味正向範例：按過 ♡ 的商品，加上收藏（★）的商品（收藏代表更想買）。"""
        out = self.feedback_examples("like", limit)
        for iid in reversed(self.watched_ids()):
            rec = self.items[iid]
            title = rec.get("zh_title") or rec.get("name", "")
            if title and title not in out and len(out) < limit * 2:
                out.append(title)
        return out

    def watched_ids(self) -> list[str]:
        return [iid for iid, r in self.items.items() if r.get("watched")]

    # ---------- 需求 ----------
    def add_request(self, text: str, at: str, source: str = "telegram") -> dict:
        req = {"id": f"r{len(self.requests) + 1}", "text": text.strip(), "at": at,
               "source": source, "status": "pending"}
        self.requests.append(req)
        return req

    def pending_requests(self) -> list[dict]:
        return [r for r in self.requests if r.get("status") == "pending"]
