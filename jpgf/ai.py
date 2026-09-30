"""Claude：日文商品名 → 繁體中文標題／摘要，並依使用者口味打分。

用 structured outputs（output_config.format = json_schema）保證回傳合法 JSON。
沒有 ANTHROPIC_API_KEY 時退回 NoopScorer（不翻譯、給中性分數），方便本機乾跑。
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是幫台灣使用者挑選日本網購商品的採購顧問。
你會收到一批樂天市場的商品（日文商品名、分類、價格、評價），請逐一：
1. zh_title：翻成自然的繁體中文（台灣用語）商品標題，30 字以內，去掉「送料無料」「ポイント10倍」「楽天1位」等促銷雜訊，保留品牌與關鍵規格。
2. zh_summary：一句話（40 字以內）說明這是什麼、有什麼特別之處。
3. score：1–10 的整數，代表「這位使用者會想知道這個商品」的程度。依使用者口味描述、喜歡／略過的例子判斷；9–10 只給真的很有特色、很符合口味的商品。促銷詞堆砌、看起來廉價量產、與口味無關的給 1–4。
4. reason：評分理由，20 字以內。
每個商品都要回傳，id 必須原樣照抄。"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "zh_title": {"type": "string"},
                    "zh_summary": {"type": "string"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "zh_title", "zh_summary", "score", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


REQUEST_PLAN_PROMPT = """你是幫台灣使用者在日本樂天市場找東西的採購顧問。
使用者會用中文描述想找的東西。請產生 1–3 組適合在樂天市場搜尋的「日文」關鍵字：
- 每組 1–4 個詞，用空白分隔（例：「バウハウス テーブルランプ」）
- 要能找到符合描述的實體商品；品牌名用日本常見寫法（片假名或英文）
- 盡量避免會混入配件、二手、福袋的寫法
另外用一句繁體中文（20 字以內）整理你理解的需求。"""

REQUEST_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "keywords": {"type": "array", "items": {"type": "string"}},
        "summary_zh": {"type": "string"},
    },
    "required": ["keywords", "summary_zh"],
    "additionalProperties": False,
}


def build_user_message(items: list[dict], taste: str, liked: list[str], skipped: list[str],
                       category_labels: dict[str, str], request: str | None = None) -> str:
    payload = [
        {
            "id": it["id"],
            "name": it["name"][:200],
            "category": category_labels.get(it.get("category", ""), it.get("category", "")),
            "price_jpy": it.get("last_price"),
            "review": f'{it.get("review_average", 0)}★ / {it.get("review_count", 0)}則',
            "shop": it.get("shop_name", ""),
        }
        for it in items
    ]
    parts = [f"## 使用者口味\n{taste.strip()}"]
    if request:
        parts.append(f"## 這次的需求（評分以此為主，口味為輔）\n{request.strip()}\n"
                     "score 代表「這個商品符合這次需求、且使用者會喜歡」的程度；不符合需求的給 1–3。")
    if liked:
        parts.append("## 使用者按過「喜歡」的商品\n" + "\n".join(f"- {t}" for t in liked))
    if skipped:
        parts.append("## 使用者按過「略過」的商品\n" + "\n".join(f"- {t}" for t in skipped))
    parts.append("## 待評分商品\n" + json.dumps(payload, ensure_ascii=False, indent=1))
    return "\n\n".join(parts)


def _clamp(score) -> int:
    try:
        return max(1, min(10, int(score)))
    except (TypeError, ValueError):
        return 1


class ClaudeScorer:
    def __init__(self, api_key: str, model: str):
        import anthropic  # 延遲 import：沒裝 SDK 也能跑 mock 測試

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3)
        self.model = model
        self.disabled = False  # 金鑰無效時停用，後續批次不再白打

    def _call(self, system: str, msg: str, schema: dict, max_tokens: int = 8000) -> dict | None:
        """呼叫 Claude 並用 structured outputs 取回 JSON；失敗回 None（已記 log）。"""
        if self.disabled:
            return None
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": msg}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except self.anthropic.AuthenticationError as e:
            log.error("ANTHROPIC_API_KEY 無效（401），本次跳過所有 Claude 呼叫：%s", e.message)
            self.disabled = True
            return None
        except self.anthropic.APIStatusError as e:
            log.warning("Claude API 錯誤 %s：%s", e.status_code, e.message)
            return None
        except self.anthropic.APIConnectionError as e:
            log.warning("Claude API 連線失敗：%s", e)
            return None
        if resp.stop_reason not in ("end_turn", "stop_sequence"):
            log.warning("Claude 回應未正常結束（%s）", resp.stop_reason)
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            log.warning("Claude 回傳 JSON 無法解析：%s", e)
            return None
        log.info("Claude 用量：input %s / output %s tokens",
                 resp.usage.input_tokens, resp.usage.output_tokens)
        return data

    def score(self, items: list[dict], **ctx) -> dict[str, dict]:
        data = self._call(SYSTEM_PROMPT, build_user_message(items, **ctx), OUTPUT_SCHEMA)
        rows = (data or {}).get("items", [])
        wanted = {it["id"] for it in items}
        return {r["id"]: {**r, "score": _clamp(r["score"])} for r in rows if r["id"] in wanted}

    def plan_request(self, text: str, taste: str) -> dict | None:
        """把中文需求轉成日文搜尋關鍵字。回傳 {"keywords": [...], "summary_zh": "..."} 或 None。"""
        msg = f"## 使用者口味（參考）\n{taste.strip()}\n\n## 需求\n{text.strip()}"
        data = self._call(REQUEST_PLAN_PROMPT, msg, REQUEST_PLAN_SCHEMA, max_tokens=1000)
        if not data:
            return None
        kws = [k.strip() for k in data.get("keywords", []) if k.strip()][:3]
        return {"keywords": kws, "summary_zh": data.get("summary_zh", "")} if kws else None


class NoopScorer:
    """沒有 API Key 時使用：保留日文標題、給中性分數，讓流程能完整乾跑。"""

    def score(self, items: list[dict], **ctx) -> dict[str, dict]:
        return {
            it["id"]: {"id": it["id"], "zh_title": it["name"][:40], "zh_summary": "（未翻譯）",
                       "score": 6, "reason": "未設定 ANTHROPIC_API_KEY"}
            for it in items
        }
