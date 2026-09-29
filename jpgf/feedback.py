"""從 Telegram 拉回「喜歡／略過」按鈕的點擊，存進 data/feedback.json。

用 getUpdates 輪詢（不需要 webhook 伺服器）。注意 Telegram 只保留約 24 小時內的更新，
所以除了每日執行，建議在 cron-job.org 另設一個每幾小時跑 mode=feedback 的排程。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .notify import TelegramNotifier
from .store import Store

log = logging.getLogger(__name__)
ACTIONS = {"like": "喜歡", "skip": "略過"}


def sync_feedback(tg: TelegramNotifier, store: Store) -> int:
    offset = store.state.get("telegram_offset", 0)
    data = tg.call("getUpdates", offset=offset, timeout=0, allowed_updates=["callback_query"])
    count = 0
    for upd in data.get("result", []):
        store.state["telegram_offset"] = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if not cq:
            continue
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id != str(tg.chat_id):
            continue  # 只接受自己的聊天室
        action, _, item_id = (cq.get("data") or "").partition(":")
        if action not in ACTIONS or not item_id:
            continue
        # callback_query 沒有點擊時間，用同步當下時間（誤差在排程間隔內）
        store.add_feedback(item_id, action, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        count += 1
        try:
            tg.call("answerCallbackQuery", callback_query_id=cq["id"],
                    text=f"已記錄：{ACTIONS[action]}")
        except RuntimeError:
            pass  # 超過時效的 callback 無法回應，不影響紀錄
    log.info("Telegram 回饋同步：%d 筆", count)
    return count
