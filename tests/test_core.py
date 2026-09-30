from datetime import date, timedelta

import pytest

from jpgf.config import load_config
from jpgf.deals import detect_deal
from jpgf.feedback import sync_feedback
from jpgf.notify import Pick, format_caption
from jpgf.pipeline import run_daily, select_picks
from jpgf.rakuten import Item, effective_price, parse_item
from jpgf.store import Store

CFG = load_config()
DEAL_CFG = {"min_history_days_30": 7, "min_history_days_90": 30, "min_drop_pct": 3.0}


def days_before(today, n):
    return (date.fromisoformat(today) - timedelta(days=n)).isoformat()


# ---------- 價格與解析 ----------
def test_effective_price_subtracts_points():
    assert effective_price(10000, 1) == 9900
    assert effective_price(3990, 10) == 3591
    assert effective_price(1000, 0) == 1000


def test_parse_item_supports_both_format_versions():
    v2 = {"itemCode": "s:1", "itemName": "n", "itemPrice": 1000, "pointRate": 5,
          "mediumImageUrls": ["https://img/x.jpg?_ex=128x128"], "postageFlag": 1}
    v1 = {"Item": {"itemCode": "s:1", "itemName": "n", "itemPrice": 1000, "pointRate": 5,
                   "mediumImageUrls": [{"imageUrl": "https://img/x.jpg?_ex=128x128"}]}}
    a, b = parse_item(v2, "gadgets"), parse_item(v1, "gadgets")
    assert a.image_url == b.image_url == "https://img/x.jpg?_ex=500x500"
    assert a.effective_price == 950 and a.id == b.id
    assert a.postage_included is False and b.postage_included is True
    assert parse_item({"itemName": "no code"}, "x") is None


# ---------- 特價判斷 ----------
def test_no_deal_without_enough_history():
    t = "2026-09-29"
    hist = [(days_before(t, i), 1000) for i in range(1, 4)] + [(t, 500)]
    assert detect_deal(hist, t, DEAL_CFG) is None


def test_30d_low_detected():
    t = "2026-09-29"
    hist = [(days_before(t, i), 1000) for i in range(1, 10)] + [(t, 900)]
    d = detect_deal(hist, t, DEAL_CFG)
    assert d and d.kind == "30d" and d.prev_low == 1000 and d.drop_pct == 10.0


def test_90d_low_preferred_and_small_drop_ignored():
    t = "2026-09-29"
    hist = [(days_before(t, i), 1000) for i in range(1, 60)]
    assert detect_deal(hist + [(t, 990)], t, DEAL_CFG) is None      # 只降 1%
    d = detect_deal(hist + [(t, 800)], t, DEAL_CFG)
    assert d.kind == "90d"


def test_not_a_deal_if_it_was_cheaper_before():
    t = "2026-09-29"
    hist = [(days_before(t, i), 1000) for i in range(1, 20)] + [(days_before(t, 5), 700), (t, 800)]
    assert detect_deal(hist, t, DEAL_CFG) is None


# ---------- 儲存 ----------
def mk(code, price, cat="gadgets", pr=1):
    return Item(item_code=code, name=f"商品{code}", price=price, point_rate=pr, url="https://x/",
                shop_name="shop", image_url="", category=cat, review_count=10)


def test_record_prices_is_idempotent_per_day(tmp_path):
    s = Store(tmp_path)
    items = [mk("a:1", 1000), mk("a:2", 2000)]
    assert s.record_prices(items, "2026-09-29") == 2
    assert s.record_prices(items, "2026-09-29") == 0
    hist = s.price_history("2026-09-01", "2026-09-30")
    assert hist[items[0].id] == [("2026-09-29", 990)]


# ---------- 選品 ----------
class FakeNotifier:
    name = "fake"

    def send_picks(self, picks, title):
        self.last = picks


NT = [FakeNotifier()]


class FixedScorer:
    def __init__(self, score):
        self.s = score
        self.calls = 0

    def score(self, items, **ctx):
        self.calls += 1
        return {i["id"]: {"id": i["id"], "zh_title": f"中文{i['id']}", "zh_summary": "摘要",
                          "score": self.s, "reason": "r"} for i in items}


def test_daily_flow_scores_once_and_respects_cooldown(tmp_path):
    s, scorer, t = Store(tmp_path), FixedScorer(8), "2026-09-29"
    items = [mk("a:1", 1000)]
    picks = run_daily(s, items, scorer, NT, CFG, t, real_scorer=True)
    assert len(picks) == 1 and scorer.calls == 1
    # 隔天：不重新評分、冷卻期內不再推
    picks = run_daily(s, items, scorer, NT, CFG, "2026-09-30", real_scorer=True)
    assert picks == [] and scorer.calls == 1


def test_deal_overrides_cooldown(tmp_path):
    s, scorer, t = Store(tmp_path), FixedScorer(7), "2026-09-29"
    for i in range(10, 0, -1):
        run_daily(s, [mk("a:1", 1000)], scorer, [], CFG, days_before(t, i), real_scorer=True)
    picks = run_daily(s, [mk("a:1", 850)], scorer, [], CFG, t, real_scorer=True)
    assert len(picks) == 1 and picks[0].deal.kind == "30d"


def test_skip_feedback_and_low_score_filtered(tmp_path):
    s, t = Store(tmp_path), "2026-09-29"
    run_daily(s, [mk("a:1", 1000)], FixedScorer(3), [], CFG, t, real_scorer=True, mark_notified=False)
    assert select_picks(s, [mk("a:1", 1).id], t, CFG) == []
    run_daily(s, [mk("b:1", 1000)], FixedScorer(9), [], CFG, t, real_scorer=True, mark_notified=False)
    s.add_feedback(mk("b:1", 1).id, "skip", t)
    assert select_picks(s, [mk("b:1", 1).id], t, CFG) == []


def test_max_per_category(tmp_path):
    s, t = Store(tmp_path), "2026-09-29"
    items = [mk(f"g:{i}", 1000 + i) for i in range(5)] + [mk("f:1", 1000, cat="fashion")]
    picks = run_daily(s, items, FixedScorer(8), [], CFG, t, real_scorer=True)
    cats = [p.rec["category"] for p in picks]
    assert cats.count("gadgets") == CFG["selection"]["max_per_category"] and "fashion" in cats


def test_noop_scores_are_replaced_once_key_exists(tmp_path):
    s, t = Store(tmp_path), "2026-09-29"
    run_daily(s, [mk("a:1", 1000)], FixedScorer(6), [], CFG, t, real_scorer=False, mark_notified=False)
    real = FixedScorer(9)
    run_daily(s, [mk("a:1", 1000)], real, [], CFG, t, real_scorer=True, mark_notified=False)
    assert real.calls == 1 and s.items[mk("a:1", 1).id]["score"] == 9


# ---------- 推播／回饋 ----------
def test_caption_escapes_html_and_fits_limit():
    rec = {"name": "x", "zh_title": "<b>壞</b>" * 300, "last_price": 3990, "last_point_rate": 10,
           "last_effective_price": 3591, "url": "https://x/", "shop_name": "s&s"}
    cap = format_caption(Pick("id", rec, "新奇電子", 8.0))
    assert len(cap) <= 1024 and cap.count("<b>") == cap.count("</b>") == 1
    assert "實付約 ¥3,591" in cap


class FakeTG:
    chat_id = "42"

    def __init__(self, updates):
        self.updates, self.calls = updates, []

    def call(self, method, **kw):
        self.calls.append((method, kw))
        return {"ok": True, "result": self.updates if method == "getUpdates" else True}


def test_sync_feedback_only_accepts_own_chat(tmp_path):
    s = Store(tmp_path)
    ups = [
        {"update_id": 5, "callback_query": {"id": "c1", "data": "like:abc", "message": {"chat": {"id": 42}}}},
        {"update_id": 6, "callback_query": {"id": "c2", "data": "like:evil", "message": {"chat": {"id": 99}}}},
        {"update_id": 7, "callback_query": {"id": "c3", "data": "bogus", "message": {"chat": {"id": 42}}}},
    ]
    assert sync_feedback(FakeTG(ups), s) == 1
    assert [f["id"] for f in s.feedback] == ["abc"]
    assert s.state["telegram_offset"] == 8


# ---------- Claude ----------
def test_claude_scorer_parses_structured_output():
    import json
    from types import SimpleNamespace

    from jpgf.ai import ClaudeScorer

    rows = [{"id": "a", "zh_title": "鑰匙包", "zh_summary": "s", "score": 42, "reason": "r"},
            {"id": "zzz", "zh_title": "?", "zh_summary": "?", "score": 5, "reason": "?"}]
    captured = {}

    def create(**kw):
        captured.update(kw)
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps({"items": rows}))],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    sc = ClaudeScorer("sk-test", "claude-haiku-4-5-20251001")
    sc.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    out = sc.score([{"id": "a", "name": "キーケース", "category": "trend_goods"}],
                   taste="t", liked=["喜歡的"], skipped=[], category_labels={"trend_goods": "潮流小物"})
    assert out == {"a": {**rows[0], "score": 10}}
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert "潮流小物" in captured["messages"][0]["content"] and "喜歡的" in captured["messages"][0]["content"]


def test_console_only_run_does_not_mark_notified(tmp_path):
    from jpgf.notify import ConsoleNotifier

    s, t = Store(tmp_path), "2026-09-29"
    run_daily(s, [mk("a:1", 1000)], FixedScorer(8), [ConsoleNotifier()], CFG, t, real_scorer=True)
    assert "notified_at" not in s.items[mk("a:1", 1).id]


# ---------- Dashboard ----------
def test_dashboard_data_hides_noop_scores_and_flags_deals(tmp_path):
    from jpgf.dashboard import build_data, build_site

    s, t = Store(tmp_path / "data"), "2026-09-29"
    for i in range(10, 0, -1):
        run_daily(s, [mk("a:1", 1000), mk("b:1", 500)], FixedScorer(6), [], CFG,
                  days_before(t, i), real_scorer=False)
    run_daily(s, [mk("a:1", 800), mk("b:1", 500)], FixedScorer(6), [], CFG, t, real_scorer=False)
    d = build_data(s, CFG, t)
    by = {i["name"]: i for i in d["items"]}
    a = by["商品a:1"]
    assert a["deal"]["kind"] == "30d" and a["score"] is None and a["zh_title"] is None
    assert len(a["series"]) == 11 and a["low"] == 792 and by["商品b:1"]["deal"] is None
    assert d["days_recorded"] == 11
    out = build_site(s, CFG, tmp_path / "site", t)
    assert (out / "index.html").exists() and (out / "data.json").exists()


def test_clean_url_strips_rafcid_with_app_id():
    from jpgf.rakuten import clean_url

    assert clean_url("https://item.rakuten.co.jp/s/1/?rafcid=wsc_i_is_abc-123") == "https://item.rakuten.co.jp/s/1/"
    assert clean_url("https://item.rakuten.co.jp/s/1/?a=1&rafcid=x") == "https://item.rakuten.co.jp/s/1/?a=1"
    it = parse_item({"itemCode": "s:1", "itemPrice": 100, "itemUrl": "https://x/?rafcid=secret"}, "c")
    assert "secret" not in it.url


def test_no_picks_from_unscored_items_when_claude_expected(tmp_path):
    s, t = Store(tmp_path), "2026-09-29"
    # 先前沒有 key 時留下的 noop 分數
    run_daily(s, [mk("a:1", 1000)], FixedScorer(9), [], CFG, t, real_scorer=False, mark_notified=False)

    class Broken:
        def score(self, items, **ctx):
            return {}

    picks = run_daily(s, [mk("a:1", 1000)], Broken(), NT, CFG, t, real_scorer=True)
    assert picks == [] and "notified_at" not in s.items[mk("a:1", 1).id]


def test_same_product_from_two_shops_is_picked_once(tmp_path):
    s, t = Store(tmp_path), "2026-09-29"

    class SameTitle(FixedScorer):
        def score(self, items, **ctx):
            return {i["id"]: {"id": i["id"], "zh_title": "LACOSTE L1212 Polo 衫", "zh_summary": "",
                              "score": 9, "reason": ""} for i in items}

    picks = run_daily(s, [mk("shopa:1", 1000, cat="fashion"), mk("shopb:1", 1100, cat="fashion")],
                      SameTitle(9), [], CFG, t, real_scorer=True)
    assert len(picks) == 1


def test_scores_saved_after_each_batch(tmp_path):
    s, t, calls = Store(tmp_path), "2026-09-29", []
    items = [mk(f"a:{i}", 1000 + i) for i in range(45)]  # batch_size 20 → 3 批
    run_daily(s, items, FixedScorer(7), [], CFG, t, real_scorer=True, on_batch=lambda: calls.append(1))
    assert len(calls) == 3


# ---------- Dashboard ✕／♡（GitHub Issue）----------
def test_parse_issue_body_last_action_wins_and_ignores_junk():
    from jpgf.issue_feedback import parse_issue_body

    body = """Dashboard 回饋

skip:abcdef012345 某商品
like:0123456789ab 另一個
like:abcdef012345 改成喜歡
rm -rf / ; skip:notanid
"""
    got = dict((i, a) for a, i in parse_issue_body(body))
    assert got == {"abcdef012345": "like", "0123456789ab": "like"}


def test_issue_feedback_hides_skipped_from_dashboard(tmp_path):
    from jpgf.dashboard import build_data
    from jpgf.issue_feedback import apply_issue_feedback

    s, t = Store(tmp_path), "2026-09-29"
    run_daily(s, [mk("a:1", 1000), mk("b:1", 900)], FixedScorer(8), [], CFG, t, real_scorer=True,
              mark_notified=False)
    a, b = mk("a:1", 1).id, mk("b:1", 1).id
    assert apply_issue_feedback(s, f"skip:{a} x\nlike:{b} y\nskip:ffffffffffff 不存在") == 2
    ids = {i["id"]: i for i in build_data(s, CFG, t)["items"]}
    assert a not in ids and ids[b]["feedback"] == "like"
    assert s.feedback_examples("skip", 5) == [f"中文{a}"]


# ---------- Telegram 文字需求 ----------
def test_telegram_text_becomes_request_and_commands_ignored(tmp_path):
    s = Store(tmp_path)
    ups = [
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "想找包浩斯風桌燈"}},
        {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/start"}},
        {"update_id": 3, "message": {"chat": {"id": 99}, "text": "陌生人的需求"}},
    ]
    tg = FakeTG(ups)
    sync_feedback(tg, s)
    assert [r["text"] for r in s.requests] == ["想找包浩斯風桌燈"]
    assert s.pending_requests()[0]["status"] == "pending"
    assert any(m == "sendMessage" for m, _ in tg.calls)


class FakeRakuten:
    def search(self, kw, category):
        return [Item(item_code=f"s:{kw}:{i}", name=f"{kw}{i}", price=1000 + i, point_rate=1,
                     url="https://x/", shop_name="shop", image_url="", category=category)
                for i in range(3)]


class PlanScorer(FixedScorer):
    def __init__(self):
        super().__init__(8)
        self.ctx = None

    def plan_request(self, text, taste):
        return {"keywords": ["バウハウス ランプ"], "summary_zh": "包浩斯桌燈"}

    def score(self, items, **ctx):
        self.ctx = ctx
        return {i["id"]: {"id": i["id"], "zh_title": f"燈{i['id']}", "zh_summary": "",
                          "score": 9 - n, "reason": "符合"} for n, i in enumerate(items)}


def test_process_request_searches_scores_and_sends(tmp_path):
    from jpgf.dashboard import build_data
    from jpgf.wishes import process_requests

    s, t = Store(tmp_path), "2026-09-29"
    s.add_request("想找包浩斯風桌燈", "2026-09-29T00:00:00+00:00")
    nt, sc = FakeNotifier(), PlanScorer()
    assert process_requests(s, FakeRakuten(), sc, [nt], CFG, t) == 1
    req = s.requests[0]
    assert req["status"] == "done" and req["keywords"] == ["バウハウス ランプ"]
    assert len(req["results"]) == 3 and len(nt.last) == 3
    assert sc.ctx["request"] == "想找包浩斯風桌燈"
    # 需求分數不寫進一般口味分數
    assert all("score" not in s.items[r["id"]] for r in req["results"])
    d = build_data(s, CFG, t)
    assert d["requests"][0]["text"] == "想找包浩斯風桌燈" and len(d["request_items"]) == 3
    assert d["items"] == []  # 需求結果不混進一般清單


def test_request_waits_without_claude(tmp_path):
    from jpgf.ai import NoopScorer
    from jpgf.wishes import process_requests

    s = Store(tmp_path)
    s.add_request("想找東西", "x")
    assert process_requests(s, FakeRakuten(), NoopScorer(), [], CFG, "2026-09-29") == 0
    assert s.requests[0]["status"] == "pending"
