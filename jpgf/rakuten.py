"""樂天市場 API（Rakuten Web Service，2026 新規格）。

2026 年起新規格要求：
- applicationId 與 accessKey 兩個都要帶（舊帳號發的 ID 不能用，要在 Rakuten Developers 重新登錄）。
- 伺服器端呼叫時要送 Referer / Origin，且必須與應用程式設定的「許可Webサイト」一致，
  否則即使金鑰正確也會回 403。本專案用 GitHub Pages 網址當作許可網站。
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import requests

log = logging.getLogger(__name__)


@dataclass
class Item:
    item_code: str
    name: str
    price: int                 # 税込售價（日圓）
    point_rate: int            # 點數倍率，1 = 1 倍 = 1%
    url: str
    shop_name: str
    image_url: str
    category: str              # config.yaml 裡的分類 key
    review_count: int = 0
    review_average: float = 0.0
    postage_included: bool = True
    available: bool = True
    source: str = "search"     # search / ranking
    rank: int | None = None
    keywords: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return item_id(self.item_code)

    @property
    def effective_price(self) -> int:
        return effective_price(self.price, self.point_rate)


def item_id(item_code: str) -> str:
    """短 ID：Telegram callback_data 上限 64 bytes，所以不直接用 itemCode。"""
    return hashlib.sha1(item_code.encode("utf-8")).hexdigest()[:12]


def effective_price(price: int, point_rate: int) -> int:
    """實際價格 = 售價 − 點數回饋。

    近似值：樂天點數實際以税抜金額計算，這裡用税込售價 × 倍率%，
    誤差在幾圓內，對「是否創新低」的判斷影響可忽略。
    """
    rate = 1 if point_rate is None else max(int(point_rate), 0)
    return int(price) - math.floor(int(price) * rate / 100)


def clean_url(url: str) -> str:
    """移除樂天附加的 rafcid 追蹤參數：它的值含有 applicationId，repo 公開時不能 commit。"""
    if not url:
        return ""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "rafcid"]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _first_image(raw: dict) -> str:
    imgs = raw.get("mediumImageUrls") or raw.get("smallImageUrls") or []
    if not imgs:
        return ""
    first = imgs[0]
    url = first.get("imageUrl", "") if isinstance(first, dict) else str(first)
    # 樂天縮圖網址帶 ?_ex=128x128，改大一點給 Telegram 顯示
    return url.split("?")[0] + "?_ex=500x500" if url else ""


def parse_item(raw: dict, category: str, source: str = "search") -> Item | None:
    """同時支援 formatVersion=1（{"Item": {...}}）與 2（扁平）的回應。"""
    if "Item" in raw and isinstance(raw["Item"], dict):
        raw = raw["Item"]
    code = raw.get("itemCode")
    if not code or raw.get("itemPrice") is None:
        return None
    return Item(
        item_code=code,
        name=raw.get("itemName", ""),
        price=int(raw["itemPrice"]),
        point_rate=int(raw["pointRate"]) if raw.get("pointRate") is not None else 1,
        url=clean_url(raw.get("itemUrl", "")),
        shop_name=raw.get("shopName", ""),
        image_url=_first_image(raw),
        category=category,
        review_count=int(raw.get("reviewCount") or 0),
        review_average=float(raw.get("reviewAverage") or 0),
        postage_included=int(raw.get("postageFlag") or 0) == 0,
        available=int(raw.get("availability", 1) or 0) == 1,
        source=source,
        rank=raw.get("rank"),
    )


def _items_from_response(data: dict) -> list[dict]:
    return data.get("Items") or data.get("items") or []


class RakutenClient:
    def __init__(self, app_id: str, access_key: str, referer: str, cfg: dict,
                 session: requests.Session | None = None):
        if not app_id or not access_key:
            raise ValueError("需要 RAKUTEN_APP_ID 與 RAKUTEN_ACCESS_KEY")
        self.app_id = app_id
        self.access_key = access_key
        self.cfg = cfg
        self.session = session or requests.Session()
        parsed = urlparse(referer) if referer else None
        self.headers = {"User-Agent": "jp-goods-finder/0.1"}
        if parsed and parsed.scheme:
            self.headers["Referer"] = referer
            self.headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        self._last_call = 0.0

    def _get(self, url: str, params: dict) -> dict:
        wait = self.cfg.get("request_interval_sec", 1.2) - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        params = {"applicationId": self.app_id, "accessKey": self.access_key,
                  "format": "json", "formatVersion": 2, **params}
        for attempt in range(3):
            self._last_call = time.time()
            resp = self.session.get(url, params=params, headers=self.headers, timeout=20)
            if resp.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                # 錯誤訊息不含金鑰（金鑰在 query string，不印 URL）
                raise RuntimeError(f"Rakuten API {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise RuntimeError("Rakuten API 429 重試多次仍失敗")

    def search(self, keyword: str, category: str) -> list[Item]:
        params = {
            "keyword": keyword,
            "hits": self.cfg.get("hits_per_keyword", 30),
            "sort": self.cfg.get("sort", "standard"),
            "availability": 1,
            "imageFlag": 1,
        }
        if self.cfg.get("min_price"):
            params["minPrice"] = self.cfg["min_price"]
        if self.cfg.get("ng_keywords"):
            params["NGKeyword"] = self.cfg["ng_keywords"]
        data = self._get(self.cfg["search_endpoint"], params)
        items = [parse_item(r, category, "search") for r in _items_from_response(data)]
        for it in items:
            if it:
                it.keywords.append(keyword)
        return [i for i in items if i]

    def lookup(self, item_code: str, category: str) -> Item | None:
        """用 itemCode 直接查單一商品（收藏商品沒出現在今天的關鍵字結果時用）。
        不帶 availability／minPrice 限制；回傳的 Item.available 為 False 代表已賣完。"""
        data = self._get(self.cfg["search_endpoint"], {"itemCode": item_code, "hits": 1})
        for raw in _items_from_response(data):
            it = parse_item(raw, category, "lookup")
            if it and it.item_code == item_code:
                return it
        return None

    def ranking(self, genre_id: int, category: str) -> list[Item]:
        data = self._get(self.cfg["ranking_endpoint"], {"genreId": genre_id})
        items = [parse_item(r, category, "ranking") for r in _items_from_response(data)]
        return [i for i in items if i]


def fetch_all(client, categories: dict, use_ranking: bool = True) -> list[Item]:
    """跑完所有分類的關鍵字搜尋（＋排行榜），依 itemCode 去重。單一請求失敗只記 log。"""
    found: dict[str, Item] = {}
    for cat_key, cat in categories.items():
        jobs = [("search", kw) for kw in cat.get("keywords", [])]
        if use_ranking:
            jobs += [("ranking", g) for g in cat.get("ranking_genre_ids", [])]
        for kind, arg in jobs:
            try:
                got = client.search(arg, cat_key) if kind == "search" else client.ranking(arg, cat_key)
            except Exception as e:  # noqa: BLE001 — 單一關鍵字失敗不該讓整次執行掛掉
                log.warning("樂天 %s %s 失敗：%s", kind, arg, e)
                continue
            log.info("樂天 %s %s → %d 筆", kind, arg, len(got))
            for it in got:
                if it.item_code in found:
                    prev = found[it.item_code]
                    prev.keywords.extend(k for k in it.keywords if k not in prev.keywords)
                    if it.rank and not prev.rank:
                        prev.rank, prev.source = it.rank, "ranking"
                else:
                    found[it.item_code] = it
    return list(found.values())
