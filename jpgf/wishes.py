"""處理「我想找…」需求：Claude 轉日文關鍵字 → 樂天搜尋 → Claude 依需求評分 → 推回 Telegram。

需求結果裡的商品也會寫進 items.json／價格紀錄，Dashboard 的「我的需求」分頁會顯示。
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from .notify import Pick
from .pipeline import dedupe_key
from .store import Store

log = logging.getLogger(__name__)
REQUEST_CATEGORY = "request"


def process_requests(store: Store, client, scorer, notifiers: list, cfg: dict, today: str,
                     max_per_run: int = 3) -> int:
    """處理最多 max_per_run 個待辦需求，回傳處理完成的數量。"""
    pending = store.pending_requests()[:max_per_run]
    if not pending:
        return 0
    if not hasattr(scorer, "plan_request"):
        log.warning("沒有 ANTHROPIC_API_KEY，無法理解文字需求，先保留待辦")
        return 0
    rcfg = cfg.get("requests", {})
    done = 0
    for req in pending:
        plan = scorer.plan_request(req["text"], cfg.get("taste_profile", ""))
        if not plan:
            log.warning("需求 %s 無法轉成關鍵字，下次再試", req["id"])
            continue
        found = {}
        for kw in plan["keywords"]:
            try:
                for it in client.search(kw, REQUEST_CATEGORY):
                    found.setdefault(it.item_code, it)
            except Exception as e:  # noqa: BLE001
                log.warning("需求搜尋「%s」失敗：%s", kw, e)
        items = list(found.values())
        for it in items:
            prev_cat = store.items.get(it.id, {}).get("category")
            rec = store.upsert_item(it, today)
            if prev_cat and prev_cat != REQUEST_CATEGORY:
                rec["category"] = prev_cat  # 一般清單裡本來就有的商品，保留原分類
            rids = rec.setdefault("request_ids", [])
            if req["id"] not in rids:
                rids.append(req["id"])
        store.record_prices(items, today)

        results = _score_for_request(store, [it.id for it in items], scorer, cfg, req["text"])
        results.sort(key=lambda r: r["score"], reverse=True)
        top, keys = [], set()
        for r in results:
            key = dedupe_key(store.items[r["id"]])
            if r["score"] < rcfg.get("min_score", 5) or key in keys:
                continue
            keys.add(key)
            top.append(r)
        top = top[: rcfg.get("top_n", 5)]

        req.update({"status": "done", "done_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "keywords": plan["keywords"], "summary_zh": plan["summary_zh"],
                    "results": [{"id": r["id"], "score": r["score"], "reason": r["reason"]}
                                for r in results]})
        _send(notifiers, req, top, store)
        done += 1
    return done


def _score_for_request(store: Store, ids: list[str], scorer, cfg: dict, text: str) -> list[dict]:
    ai = cfg["ai"]
    ctx = {
        "taste": cfg.get("taste_profile", ""),
        "liked": store.liked_examples(ai.get("feedback_examples", 15)),
        "skipped": store.feedback_examples("skip", ai.get("feedback_examples", 15)),
        "category_labels": {REQUEST_CATEGORY: "需求搜尋"},
        "request": text,
    }
    out, bs = [], ai.get("batch_size", 20)
    for start in range(0, len(ids), bs):
        batch = [store.items[i] | {"id": i} for i in ids[start:start + bs]]
        for iid, r in scorer.score(batch, **ctx).items():
            rec = store.items[iid]
            # 中文標題／摘要若還沒有就補上；需求分數只存在需求紀錄，不覆蓋一般口味分數
            if rec.get("scored_by") != "claude":
                rec["zh_title"], rec["zh_summary"] = r["zh_title"], r["zh_summary"]
                rec.setdefault("scored_by", "request")
            out.append({"id": iid, "score": r["score"], "reason": r["reason"]})
    return out


def _send(notifiers: list, req: dict, top: list[dict], store: Store) -> None:
    title = f"🔎 你的需求：{req['text'][:40]}"
    picks = []
    for r in top:
        rec = dict(store.items[r["id"]], score=r["score"], score_reason=r["reason"])
        picks.append(Pick(r["id"], rec, "需求搜尋", float(r["score"])))
    for nt in notifiers:
        try:
            if not picks and hasattr(nt, "call"):
                nt.call("sendMessage", chat_id=nt.chat_id, parse_mode="HTML",
                        text=f"{html.escape(title)}\n找不到合適的商品，換個說法再試試看 🙏")
                continue
            nt.send_picks(picks, title)
        except Exception as e:  # noqa: BLE001
            log.error("需求結果推播 %s 失敗：%s", getattr(nt, "name", nt), e)
