"""联网搜索：接 DeepSeek Search API（¥5/千次，按次计费）。

触发策略放在 should_search()，真正发请求的是 search()；
结果压成一段「联网资料」注入到本轮提示词，由模型自己决定怎么用。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

import httpx

from . import settings

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.deepseek.com/v1/tools/search"
COST_PER_CALL_CNY = 0.005  # ¥5/千次

DEFAULT_TRIGGERS = (
    "搜一下", "搜搜", "搜索", "查一下", "查查", "上网查", "联网", "最新", "今天的新闻",
    "现在多少钱", "价格", "汇率", "股价", "比分", "天气", "谁是", "什么时候",
)


def _cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return (cfg.get("search") or {}) if isinstance(cfg, dict) else {}


# 问「你会不会联网」这类不该真去查；说「别搜了」也闭嘴
_SKIP_PATTERNS = (
    re.compile(r"(别|不用|不必|不要|甭|没必要).{0,3}(搜|查|联网)"),
    re.compile(r"(搜|查|联网).{0,3}(就别|还是别|不用|没必要|免了)"),
    re.compile(r"(你|鱼).{0,2}(能|会|可以|能不能|会不会|有没有)?\s*(联网|搜索|上网)(吗|么|不|嘛)?\s*[？?]?$"),
    re.compile(r"(你|鱼).{0,2}(能|会|可以|能不能|会不会).{0,3}(联网|搜索|上网)"),
)


def _skip(text: str) -> bool:
    body = (text or "").strip()
    for pat in _SKIP_PATTERNS:
        if pat.search(body):
            if not re.search(r"[A-Za-z0-9]", body):  # 「你能搜 pjsk 吗」有具体目标，仍要查
                return True
    return False


def should_search(cfg: Dict[str, Any], text: str, *, is_master: bool = False) -> bool:
    """要不要联网：总开关 + 触发词；主人问到带问号的时事类问题也放行。"""
    c = _cfg(cfg)
    if not c.get("enabled"):
        return False
    body = (text or "").strip()
    if not body:
        return False
    if _skip(body):
        return False
    words = tuple(c.get("triggers") or DEFAULT_TRIGGERS)
    if any(w in body for w in words):
        return True
    if is_master and c.get("master_questions", True):
        if re.search(r"(吗|呢|么|\?|？)$", body) and len(body) >= 8:
            return any(k in body for k in ("最近", "目前", "今年", "行情", "发布", "更新"))
    return False


def format_results(results: List[Dict[str, Any]], max_items: int = 5) -> str:
    lines = []
    for i, r in enumerate(results[:max_items], 1):
        title = str(r.get("title") or "").strip()
        url = str(r.get("url") or "").strip()
        body = str(r.get("summary") or r.get("snippet") or r.get("content") or "").strip()
        body = re.sub(r"\s+", " ", body)[:220]
        lines.append(f"{i}. {title}（{url}）{('：' + body) if body else ''}")
    return "\n".join(lines)


async def search(cfg: Dict[str, Any], query: str, *, count: int | None = None) -> tuple[str, float]:
    """返回（资料文本, 本次花费）。失败返回空文本，调用方照常回答。"""
    c = _cfg(cfg)
    key_env = str(c.get("api_key_env") or "FATFISH_DEEPSEEK_API_KEY")
    api_key = settings.get(key_env)
    if not api_key:
        logger.warning("联网搜索缺少 key（%s），跳过", key_env)
        return "", 0.0
    n = int(count or c.get("max_results", 5) or 5)
    try:
        async with httpx.AsyncClient(timeout=float(c.get("timeout_seconds", 20)), trust_env=False) as client:
            resp = await client.post(
                str(c.get("endpoint") or ENDPOINT),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"query": query, "count": n},
            )
        if resp.status_code >= 400:
            logger.warning("联网搜索 HTTP %s: %s", resp.status_code, resp.text[:200])
            return "", 0.0
        data = resp.json()
        results = data.get("results") or []
        if not results:
            return "", COST_PER_CALL_CNY
        return format_results(results, max_items=n), COST_PER_CALL_CNY
    except Exception as exc:  # noqa: BLE001
        logger.warning("联网搜索失败：%s", exc)
        return "", 0.0
