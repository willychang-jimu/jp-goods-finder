"""每日流程：抓樂天 → 存價格 → Claude 翻譯評分 → 判斷特價 → 選出前 N 名 → 推播。"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from .deals import detect_deal
from .notify import Pick
from .rakuten import Item
from .store import Store

log = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))


def today_jst() -> str:
    return datetime.now(JST).date().isoformat()


def dedupe_key(rec: dict) -> str:
    """同一商品常由多家店上架：用中文標題（沒有就用日文名前 40 字）正規化後當作比對鍵。"""
    title = rec.get("zh_title") if rec.get("scored_by") == "claude" else None
    base = title or rec.get("name", "")[:40]
    return re.sub(r"[\s\W_]+", "", base).lower()


def score_new_items(store: Store, seen_ids: list[str], scorer, cfg: dict, real_scorer: bool,
                    on_batch=None) -> int:
    """只替「還沒被 Claude 評過分」的商品呼叫 Claude；結果快取在 items.json。

    on_batch：每批評分完呼叫一次（用來先存檔），執行逾時被中斷也不會白做。"""
    ai = cfg["ai"]
    todo = [store.items[i] | {"id": i} for i in seen_ids
            if "score" not in store.items[i]
            or (real_scorer and store.items[i].get("scored_by") == "noop")]
    # 排行榜商品、評價數多的優先
    todo.sort(key=lambda r: (r.get("last_rank") is None, -r.get("review_count", 0)))
    todo = todo[: ai.get("max_new_items_per_run", 80)]
    if not todo:
        return 0
    labels = {k: v.get("label", k) for k, v in cfg["categories"].items()}
    ctx = {
        "taste": cfg.get("taste_profile", ""),
        "liked": store.feedback_examples("like", ai.get("feedback_examples", 15)),
        "skipped": store.feedback_examples("skip", ai.get("feedback_examples", 15)),
        "category_labels": labels,
    }
    done = 0
    bs = ai.get("batch_size", 20)
    for start in range(0, len(todo), bs):
        batch = todo[start:start + bs]
        results = scorer.score(batch, **ctx)
        for iid, r in results.items():
            rec = store.items[iid]
            rec.update({"zh_title": r["zh_title"], "zh_summary": r["zh_summary"],
                        "score": r["score"], "score_reason": r["reason"],
                        "scored_by": "claude" if real_scorer else "noop"})
            done += 1
        if on_batch:
            on_batch()
    log.info("評分完成 %d / %d 筆", done, len(todo))
    return done


def select_picks(store: Store, seen_ids: list[str], today: str, cfg: dict,
                 require_claude: bool = False) -> list[Pick]:
    """require_claude=True 時只挑 Claude 真的評過分的商品（避免 API 失敗時推出沒評分的東西）。"""
    sel, dcfg = cfg["selection"], cfg["deals"]
    labels = {k: v.get("label", k) for k, v in cfg["categories"].items()}
    since = (date.fromisoformat(today) - timedelta(days=90)).isoformat()
    history = store.price_history(since, today)
    cooldown = (date.fromisoformat(today)
                - timedelta(days=sel.get("resend_cooldown_days", 14))).isoformat()
    min_score = sel.get("min_score", 6)

    cands: list[Pick] = []
    for iid in seen_ids:
        rec = store.items[iid]
        score = rec.get("score")
        if score is None or rec.get("feedback") == "skip":
            continue
        if require_claude and rec.get("scored_by") != "claude":
            continue
        deal = detect_deal(history.get(iid, []), today, dcfg)
        # 特價商品門檻放寬 2 分；沒特價的商品要達 min_score
        if score < (min_score - 2 if deal else min_score):
            continue
        notified = rec.get("notified_at")
        if not deal and (rec.get("feedback") == "like" or (notified and notified >= cooldown)):
            continue  # 已喜歡或近期推過的，只有變特價才再推
        if deal and notified and rec.get("notified_price", 10**9) <= deal.today_price:
            continue  # 推過更便宜的價格了
        bonus = 0.0
        if deal:
            bonus = dcfg.get("bonus_90d", 3.0) if deal.kind == "90d" else dcfg.get("bonus_30d", 2.0)
        cands.append(Pick(iid, rec, labels.get(rec.get("category", ""), ""), score + bonus, deal))

    cands.sort(key=lambda p: (p.rank_score, p.rec.get("review_count", 0)), reverse=True)
    per_cat_max = sel.get("max_per_category", 2)
    picks, per_cat, seen_keys = [], {}, set()
    for p in cands:
        c = p.rec.get("category")
        key = dedupe_key(p.rec)
        if per_cat.get(c, 0) >= per_cat_max or key in seen_keys:
            continue  # 同分類已滿，或同一商品（不同店家）已經選過
        seen_keys.add(key)
        per_cat[c] = per_cat.get(c, 0) + 1
        picks.append(p)
        if len(picks) >= sel.get("daily_top_n", 5):
            break
    return picks


def run_daily(store: Store, items: list[Item], scorer, notifiers: list, cfg: dict,
              today: str, real_scorer: bool, mark_notified: bool = True,
              on_batch=None) -> list[Pick]:
    min_reviews = cfg["selection"].get("min_review_count", 0)
    items = [i for i in items if i.review_count >= min_reviews]
    for it in items:
        store.upsert_item(it, today)
    n = store.record_prices(items, today)
    log.info("今日商品 %d 筆，新寫入價格 %d 筆", len(items), n)

    seen_ids = [it.id for it in items]
    score_new_items(store, seen_ids, scorer, cfg, real_scorer, on_batch=on_batch)
    picks = select_picks(store, seen_ids, today, cfg, require_claude=real_scorer)

    title = f"{today} 日本好物精選"
    all_ok = True
    for nt in notifiers:
        try:
            nt.send_picks(picks, title)
        except Exception as e:  # noqa: BLE001 — 某個通道失敗不影響其他通道
            all_ok = False
            log.error("推播 %s 失敗：%s", getattr(nt, "name", nt), e)
    # 推播失敗、或只有印在 log（沒有真正送到使用者手上）就不標記，之後還有機會再推
    delivered = any(getattr(nt, "name", "") != "console" for nt in notifiers)
    if mark_notified and all_ok and delivered:
        for p in picks:
            p.rec["notified_at"] = today
            p.rec["notified_price"] = p.rec["last_effective_price"]
    store.state["last_run"] = today
    return picks
