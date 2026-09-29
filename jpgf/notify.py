"""推播模組：依 config.yaml 的 notifiers 開關組合多個通道。

新增通道（例如第 2 階段的 Email 週報）只要實作 send_picks()，再加進 build_notifiers()。
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass

import requests

from .config import env
from .deals import Deal

log = logging.getLogger(__name__)


@dataclass
class Pick:
    id: str
    rec: dict              # items.json 的那筆資料
    category_label: str
    rank_score: float
    deal: Deal | None = None


def _yen(n) -> str:
    return f"¥{int(n):,}"


def _cut(text: str, n: int) -> str:
    """在跳脫 HTML 之前截斷，避免切壞標籤或 &amp; 實體。"""
    return text if len(text) <= n else text[: n - 1] + "…"


def format_caption(p: Pick) -> str:
    """Telegram HTML caption（上限 1024 字元）。各欄位先截短再跳脫，確保價格資訊不會被切掉。"""
    r = p.rec
    lines = []
    if p.deal:
        tag = "🔥 90 日新低" if p.deal.kind == "90d" else "💰 30 日新低"
        lines.append(f"<b>{tag}</b>（比先前最低 {_yen(p.deal.prev_low)} 再低 {p.deal.drop_pct}%）")
    lines.append(f"<b>{html.escape(_cut(r.get('zh_title') or r['name'], 60))}</b>")
    if r.get("zh_summary"):
        lines.append(html.escape(_cut(r["zh_summary"], 80)))
    price = f"{_yen(r['last_price'])}"
    if r.get("last_point_rate", 1) > 1:
        price += f"（點數 {r['last_point_rate']} 倍，實付約 {_yen(r['last_effective_price'])}）"
    if not r.get("postage_included", True):
        price += "・運費另計"
    lines.append(price)
    meta = [f"#{p.category_label}", f"評分 {r.get('score', '-')}/10"]
    if r.get("review_count"):
        meta.append(f"★{r.get('review_average', 0)}（{r['review_count']}）")
    lines.append("　".join(meta))
    optional = []  # 太長時依序捨棄的次要行
    if r.get("score_reason"):
        optional.append(f"<i>{html.escape(_cut(r['score_reason'], 40))}</i>")
    if r.get("shop_name"):
        optional.append(f"<code>{html.escape(_cut(r['shop_name'], 40))}</code>")
    while optional and len("\n".join(lines + optional)) > 1024:
        optional.pop()
    return "\n".join(lines + optional)


class ConsoleNotifier:
    name = "console"

    def send_picks(self, picks: list[Pick], title: str) -> None:
        print(f"\n===== {title} =====")
        for i, p in enumerate(picks, 1):
            r = p.rec
            deal = f" [{p.deal.kind} 新低 -{p.deal.drop_pct}%]" if p.deal else ""
            print(f"{i}. ({p.rank_score:.1f}){deal} {r.get('zh_title') or r['name'][:60]}")
            print(f"   {_yen(r['last_price'])} / 實付 {_yen(r['last_effective_price'])}  {r['url']}")
        if not picks:
            print("（今天沒有符合條件的商品）")


class TelegramNotifier:
    name = "telegram"
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None):
        self.token, self.chat_id = token, chat_id
        self.session = session or requests.Session()

    def call(self, method: str, **payload) -> dict:
        resp = self.session.post(self.API.format(token=self.token, method=method),
                                 json=payload, timeout=30)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if not data.get("ok"):
            # 不印 URL（含 token）
            raise RuntimeError(f"Telegram {method} 失敗：{resp.status_code} {data.get('description', '')}")
        return data

    @staticmethod
    def keyboard(p: Pick) -> dict:
        return {"inline_keyboard": [
            [{"text": "👍 喜歡", "callback_data": f"like:{p.id}"},
             {"text": "👎 略過", "callback_data": f"skip:{p.id}"}],
            [{"text": "🔗 看商品", "url": p.rec["url"]}],
        ]}

    def send_picks(self, picks: list[Pick], title: str) -> None:
        self.call("sendMessage", chat_id=self.chat_id, text=f"🛍 <b>{html.escape(title)}</b>",
                  parse_mode="HTML")
        for p in picks:
            caption = format_caption(p)
            try:
                if p.rec.get("image_url"):
                    self.call("sendPhoto", chat_id=self.chat_id, photo=p.rec["image_url"],
                              caption=caption, parse_mode="HTML", reply_markup=self.keyboard(p))
                    continue
            except RuntimeError as e:
                log.warning("sendPhoto 失敗，改傳文字：%s", e)
            self.call("sendMessage", chat_id=self.chat_id, text=caption, parse_mode="HTML",
                      reply_markup=self.keyboard(p), disable_web_page_preview=True)
        if not picks:
            self.call("sendMessage", chat_id=self.chat_id, text="今天沒有符合條件的商品 🙂")


def build_notifiers(cfg: dict, dry_run: bool = False) -> list:
    out: list = []
    ncfg = cfg.get("notifiers", {})
    if ncfg.get("console", True) or dry_run:
        out.append(ConsoleNotifier())
    if dry_run:
        return out
    if ncfg.get("telegram"):
        token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
        if token and chat:
            out.append(TelegramNotifier(token, chat))
        else:
            log.warning("Telegram 已開啟但缺 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，略過")
    return out
