"""從 Telegram 拉回「喜歡／略過」按鈕的點擊（存進 data/feedback.json），
以及使用者直接傳給 Bot 的文字需求（存進 data/requests.json，由 wishes.py 處理）。

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
    data = tg.call("getUpdates", offset=offset, timeout=0,
                   allowed_updates=["callback_query", "message"])
    count = 0
    for upd in data.get("result", []):
        store.state["telegram_offset"] = upd["update_id"] + 1
        if upd.get("message"):
            _handle_message(tg, store, upd["message"])
            continue
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


def _handle_message(tg: TelegramNotifier, store: Store, msg: dict) -> None:
    """使用者傳給 Bot 的文字 = 「我想找…」需求。只接受自己的聊天室；/ 開頭的指令忽略。"""
    if str(msg.get("chat", {}).get("id", "")) != str(tg.chat_id):
        return
    text = (msg.get("text") or "").strip()
    if not text or text.startswith("/"):
        return
    req = store.add_request(text[:300], datetime.now(timezone.utc).isoformat(timespec="seconds"))
    log.info("收到需求 %s", req["id"])
    try:
        tg.call("sendMessage", chat_id=tg.chat_id,
                text=f"🔎 收到需求：「{req['text']}」\n下一次整理時會幫你找，結果會傳到這裡。")
    except RuntimeError as e:
        log.warning("回覆需求確認失敗：%s", e)
