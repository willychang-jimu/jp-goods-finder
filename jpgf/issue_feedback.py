"""Dashboard 的 ✕／♡ 回饋。

Dashboard 是 GitHub Pages 靜態頁，無法直接寫資料，所以按「送出回饋」會開一張預先填好的
GitHub Issue（標題以「[回饋]」開頭），內容每行一筆：`skip:<商品ID> 商品名` 或 `like:<商品ID> 商品名`。
.github/workflows/feedback-issue.yml 只處理 repo 擁有者開的 Issue，呼叫這裡寫進 data/。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .store import Store

LINE = re.compile(r"^\s*[-*]?\s*(like|skip)\s*:\s*([0-9a-f]{12})\b", re.MULTILINE)


def parse_issue_body(body: str) -> list[tuple[str, str]]:
    """回傳 [(action, item_id)]；同一商品多次出現時以最後一次為準。"""
    last: dict[str, str] = {}
    for action, iid in LINE.findall(body or ""):
        last[iid] = action
    return [(a, i) for i, a in last.items()]


def apply_issue_feedback(store: Store, body: str) -> int:
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for action, iid in parse_issue_body(body):
        if iid in store.items:
            store.add_feedback(iid, action, at, source="dashboard")
            n += 1
    return n
