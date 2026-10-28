"""DeepSeek 客户端 + 花费核算 + ¥/日 预算闸门。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
import logging
import re
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

from . import settings
from .store import Store, day_key

PEAK_WINDOWS = ((9, 0, 12, 0), (14, 0, 18, 0))
CN_HOLIDAYS: set[str] = set()  # 法定节假日按空闲计；保守起见留空=按工作日高峰算


logger = logging.getLogger("fatfish.llm")


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    model: str
    prompt_tokens: int
    cache_hit: int
    cache_miss: int
    completion_tokens: int
    cost_cny: float
    peak: bool


def _tz(cfg: Dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.get("budget", {}).get("timezone", "Asia/Shanghai"))
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def is_peak(cfg: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    tz = _tz(cfg)
    now = now or datetime.now(tz)
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in CN_HOLIDAYS:
        return False
    minutes = now.hour * 60 + now.minute
    return any(start <= minutes < end for start, end in ((s * 60 + sm, e * 60 + em) for s, sm, e, em in PEAK_WINDOWS))


def prices_for(cfg: Dict[str, Any], model: str) -> Dict[str, float]:
    table = cfg.get("budget", {}).get("prices", {}) or {}
    base = {"input_cache_hit_peak": 0.04, "input_cache_hit_offpeak": 0.02,
            "input_miss_peak": 2.0, "input_miss_offpeak": 1.0,
            "output_peak": 8.0, "output_offpeak": 4.0}
    for key in (model, "deepseek-flash"):
        if isinstance(table.get(key), dict):
            return {**base, **table[key]}
    return base


def cost_cny(cfg: Dict[str, Any], model: str, *, cache_hit: int, cache_miss: int, completion_tokens: int, peak: bool) -> float:
    p = prices_for(cfg, model)
    suffix = "peak" if peak else "offpeak"
    return (
        cache_hit / 1e6 * p[f"input_cache_hit_{suffix}"]
        + cache_miss / 1e6 * p[f"input_miss_{suffix}"]
        + completion_tokens / 1e6 * p[f"output_{suffix}"]
    )


MAX_TOKENS_CEILING = 2000


class LLM:
    def __init__(self, cfg: Dict[str, Any], store: Store):
        self.cfg = cfg
        self.store = store
        self.reload(cfg)

    def reload(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.llm = cfg.get("llm", {}) or {}
        self.budget = cfg.get("budget", {}) or {}
        self.api_key_env = self.llm.get("api_key_env", "FATFISH_DEEPSEEK_API_KEY")
        self.base_url = str(self.llm.get("base_url", "https://api.deepseek.com")).rstrip("/")
        self.model = str(self.llm.get("model", "deepseek-flash"))
        self.thinking = str(self.llm.get("thinking", "disabled")).lower()
        self.effort = str(self.llm.get("reasoning_effort", "low")).lower()
        self.last_finish_reason = ""
        self.vision_model = str(self.llm.get("vision_model") or "deepseek-chat")
        # 第三方网关（Cloudflare 1010）会按 UA 拦请求，允许配置头部
        self.headers = dict(self.llm.get("headers") or {})
        if self.llm.get("user_agent"):
            self.headers["User-Agent"] = str(self.llm["user_agent"])
        self.send_thinking = bool(self.llm.get("send_thinking", True))
        self.send_penalties = bool(self.llm.get("send_penalties", True))
        self.max_tokens = int(self.llm.get("max_tokens", 240))
        # 输出上限硬闸：聊天机器人单条不需要几千 token，配错就是烧钱
        if self.max_tokens > MAX_TOKENS_CEILING:
            logger.warning(
                "max_tokens=%d 超过上限 %d，按 %d 跑（改 config 里的 llm.max_tokens 才生效）",
                self.max_tokens, MAX_TOKENS_CEILING, MAX_TOKENS_CEILING,
            )
            self.max_tokens = MAX_TOKENS_CEILING
        self.temperature = float(self.llm.get("temperature", 0.9))
        self.timeout = float(self.llm.get("timeout_seconds", 60))

    # ── 预算 ──
    def cap(self) -> float:
        return float(self.budget.get("daily_cap_cny", 5.0))

    def reserve(self) -> float:
        return float(self.budget.get("reserve_cny", 0.10))

    def spent_today(self) -> float:
        return self.store.spend_day(day_key())

    def remaining_today(self) -> float:
        return max(0.0, self.cap() - self.spent_today())

    def budget_ok(self) -> bool:
        return self.remaining_today() > self.reserve()

    # ── 调用 ──
    @staticmethod
    def _has_image(messages: List[Dict[str, Any]]) -> bool:
        for m in messages:
            content = m.get("content")
            if isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "image_url" for part in content
            ):
                return True
        return False

    def _payload(self, messages: List[Dict[str, str]], max_tokens: int | None = None,
                 temperature: float | None = None) -> Dict[str, Any]:
        model = self.vision_model if self._has_image(messages) else self.model
        if model != self.model:
            logger.info("本次带图，换视觉模型 %s", model)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }
        if self.thinking == "enabled" and self.send_thinking:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.effort
        elif self.send_thinking:
            payload["thinking"] = {"type": "disabled"}
            payload["temperature"] = self.temperature if temperature is None else temperature
        else:
            payload["temperature"] = self.temperature if temperature is None else temperature
        # 退化抑制：长上下文 + 0.9 温度时模型爱在句尾编造乱码，给点惩罚
        _fp = float(self.cfg.get("llm", {}).get("frequency_penalty", 0) or 0)
        _pp = float(self.cfg.get("llm", {}).get("presence_penalty", 0) or 0)
        if _fp and self.send_penalties:
            payload["frequency_penalty"] = _fp
        if _pp and self.send_penalties:
            payload["presence_penalty"] = _pp
        return payload

    async def chat(self, messages: List[Dict[str, str]], *, channel: str = "", chat_id: str = "",
                   temperature: float | None = None) -> Reply:
        if not self.budget_ok():
            raise BudgetExceeded(f"今日预算已用 {self.spent_today():.4f}/{self.cap():.2f} 元")
        api_key = settings.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"缺少 API key（{self.api_key_env}）")
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", **self.headers},
                json=self._payload(messages),
            )
            # 空 content（思考吃满额度 / 被截断）时放大 max_tokens 再试一次，成本可忽略
            if resp.status_code < 400:
                _first = resp.json()
                _ch = (_first.get("choices") or [{}])[0].get("message") or {}
                if not str(_ch.get("content") or "").strip():
                    bump = min(4096, int(self.max_tokens * 1.8) + 80)
                    logger.warning("空回复，放大 max_tokens 到 %d 重试一次", bump)
                    self.store.record_usage(
                        channel=channel, chat_id=chat_id, model=self.model,
                        prompt_tokens=0, cache_hit=0, cache_miss=0, completion_tokens=0,
                        cost_cny=0.0, ok=False, note="空 content 重试",
                    )
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", **self.headers},
                        json=self._payload(messages, max_tokens=bump),
                    )
        latency = time.monotonic() - started
        if resp.status_code >= 400:
            self.store.record_usage(
                channel=channel, chat_id=chat_id, model=self.model,
                prompt_tokens=0, cache_hit=0, cache_miss=0, completion_tokens=0,
                cost_cny=0.0, ok=False, note=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
            raise RuntimeError(f"DeepSeek HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        cache_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        cache_miss = int(usage.get("prompt_cache_miss_tokens") or max(0, prompt_tokens - cache_hit))
        completion_tokens = int(usage.get("completion_tokens") or 0)
        peak = is_peak(self.cfg)
        used_model = str(data.get("model") or self.model)
        cost = cost_cny(self.cfg, used_model, cache_hit=cache_hit, cache_miss=cache_miss, completion_tokens=completion_tokens, peak=peak)
        text = ""
        choices = data.get("choices") or []
        self.last_finish_reason = str((choices[0].get("finish_reason") if choices else "") or "")
        if choices:
            msg = choices[0].get("message") or {}
            text = str(msg.get("content") or "")
            if not text.strip():
                # 见过：思考模式吃满 max_tokens 时 content 为空、只有 reasoning_content
                logger.warning(
                    "DeepSeek 返回空 content（finish=%s, reasoning=%d 字），这次回复会哑掉",
                    choices[0].get("finish_reason"), len(str(msg.get("reasoning_content") or "")),
                )
        self.store.record_usage(
            channel=channel, chat_id=chat_id, model=used_model,
            prompt_tokens=prompt_tokens, cache_hit=cache_hit, cache_miss=cache_miss,
            completion_tokens=completion_tokens, cost_cny=cost, ok=True,
            note=f"{latency:.1f}s {'peak' if peak else 'offpeak'}",
        )
        return Reply(
            text=text, model=used_model, prompt_tokens=prompt_tokens,
            cache_hit=cache_hit, cache_miss=cache_miss,
            completion_tokens=completion_tokens, cost_cny=cost, peak=peak,
        )


_SENT_END = "。！？!?…~）)」』\"”"


def tidy_reply(text: str, max_chars: int) -> str:
    """砍掉句尾乱码：有两处标点之间超过 24 字无停顿，就截到前一处标点。"""
    t = (text or "").strip()
    if not t:
        return t
    marks = "。！？!?…~）)」』\"”，,、；;：:"
    last = -1
    # 多行=故意排版（列表/分行说明），别用「24 字无停顿」的乱码规则去砍
    for i, ch in enumerate(t if "\n" not in t else ""):
        if ch in marks:
            if last >= 0 and i - last > 24:
                t = t[: last + 1].rstrip()
                break
            last = i
    if t and t[-1] not in _SENT_END and not t.endswith("…"):
        cut2 = max((t.rfind(ch) for ch in "。！？!?…~）)」』\"”"), default=-1) + 1
        if cut2 >= 6:
            t = t[:cut2].rstrip()
    if re.search(r"(.{6,20}?)\1{2,}", t):  # 复读机式退化
        m = re.search(r"(.{6,20}?)\1{2,}", t)
        t = t[: m.start()].rstrip() or t
    return t


def sanitize_reply(text: str, max_chars: int, keep_newlines: bool = False) -> str:
    """去掉 Markdown 痕迹、截断、压平换行（要代码时保留换行）。"""
    t = (text or "").strip()
    if not t:
        return ""
    for fence in ("```", "~~~"):
        t = t.replace(fence, "")
    t = t.replace("**", "").replace("`", "")
    lines = [ln.rstrip() for ln in t.splitlines() if ln.strip()]
    if keep_newlines and len(lines) > 1:
        t = "\n".join(ln for ln in lines)  # 保留缩进，代码才看得懂
    else:
        t = " ".join(ln.strip() for ln in lines)
    if len(t) > max_chars:
        t = t[: max_chars - 1].rstrip() + "…"
    return t