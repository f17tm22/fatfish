"""对话大脑：触发判定 → 冷却/限流 → 模板功能 → LLM → 清洗。"""

from __future__ import annotations

import logging
import random
import re
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

from . import features, knowledge, persona, websearch
from .channels.base import Inbound
from .llm import BudgetExceeded, LLM, sanitize_reply, tidy_reply
from .store import Store, day_key

logger = logging.getLogger("fatfish.brain")

_MENTION_RE = re.compile(r"<@!?\w+>")


_HALF_END = "，,、；;：:"
_HALF_TAIL = ("因为", "所以", "而且", "但是", "然后", "比如", "就是", "还有", "如果")


def _half_sentence(text: str) -> bool:
    """判断是不是「说话说一半」：停在逗号/顿号/分号，或停在一个还没说完的连接词上。"""
    t = (text or "").rstrip()
    if not t:
        return False
    if t.endswith("…") or t.endswith("..."):
        return False
    if t[-1] in _HALF_END:
        return True
    return t.endswith(_HALF_TAIL)


class Brain:
    def __init__(self, cfg: Dict[str, Any], store: Store, llm: LLM):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self._reply_times: Deque[float] = deque()
        # 「为什么不回」的计数，心跳时汇总打印
        self.skipped_cooldown = 0
        self.skipped_rate = 0
        self.empty_fallbacks = 0

    def reload(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.llm.reload(cfg)

    # ── 判定 ──
    def is_master(self, inbound: Inbound) -> bool:
        ids = list(self.cfg.get("persona", {}).get("master_ids") or [])
        # 群里的 member_openid 与私聊 user_openid 不是同一套 ID，单独列一份
        ids += list(((self.cfg.get("channels") or {}).get("qq") or {}).get("master_members") or [])
        return bool(inbound.user_id) and inbound.user_id in ids

    def _clean_text(self, text: str) -> str:
        return _MENTION_RE.sub("", text or "").strip()

    def _should_reply(self, inbound: Inbound, text: str) -> bool:
        if not text:
            return False
        triggers = self.cfg.get("triggers", {}) or {}
        if inbound.chat_type == "dm":
            return bool(triggers.get("reply_in_dm", True))
        if inbound.mentioned and triggers.get("reply_on_mention", True):
            return True
        if triggers.get("group_keyword_reply", True):
            low = text.lower()
            if any(str(k).lower() in low for k in (triggers.get("keywords") or [])):
                return True
        prob = float(self.cfg.get("persona", {}).get("interrupt_prob", 0.0) or 0.0)
        return prob > 0 and random.random() < prob

    def _cooldown_ok(self, inbound: Inbound) -> bool:
        p = self.cfg.get("persona", {}) or {}
        key = "dm_cooldown_seconds" if inbound.chat_type == "dm" else "cooldown_seconds"
        cooldown = float(p.get(key, 10) or 0)
        if cooldown <= 0:
            return True
        last = float(self.store.chat_state(inbound.chat_id).get("last_reply_ts") or 0)
        return time.time() - last >= cooldown

    def _rate_ok(self) -> bool:
        limit = int(self.cfg.get("persona", {}).get("max_replies_per_minute", 8) or 8)
        now = time.time()
        while self._reply_times and now - self._reply_times[0] > 60:
            self._reply_times.popleft()
        return len(self._reply_times) < limit

    # ── 主流程 ──
    async def handle(self, inbound: Inbound) -> Optional[str]:
        text = self._clean_text(inbound.text)
        if not text:
            return None
        self.store.touch_chat(inbound.chat_id, replied=False)
        # 先落库再判要不要回：以前只有「回了」才记，群里没被叫到的聊天就整段丢失
        self.store.history_add(inbound.chat_id, "user", inbound.user_name or inbound.user_id, text)
        if not self._should_reply(inbound, text):
            return None
        # 模板功能（抽签/猜数字/群报…）零 token，不受冷却限制，
        # 否则玩猜数字连猜两次会被 10 秒冷却吞掉；只有走 LLM 的回复才限流。
        reply = features.try_feature(self, inbound)
        if reply is None:
            if not self._cooldown_ok(inbound):
                self.skipped_cooldown += 1
                logger.info("冷却中，这条不回 chat=%s", inbound.chat_id[-8:])
                return None
            if not self._rate_ok():
                self.skipped_rate += 1
                logger.info("本分钟回复已达上限，这条不回 chat=%s", inbound.chat_id[-8:])
                return None
            reply = await self._chat(inbound, text)
            if not reply:
                self.empty_fallbacks += 1
                logger.warning("模型返回空回复，用兜底句 chat=%s", inbound.chat_id[-8:])
                reply = random.choice(("嗯……（甩尾）", "本鱼刚走神了，再说一遍？", "……你说什么？没听清（尾巴晃了晃）"))
        if not reply:
            return None

        max_chars = int(self.cfg.get("persona", {}).get("max_reply_chars", 120) or 120)
        # 要代码/长文时别按聊天长度截断，否则代码会被砍在半行
        _ask_long = any(w in (inbound.text or "") for w in ("代码", "示例", "写个", "写一段", "详细", "解释下", "方案", "报错"))
        if _ask_long or "```" in reply or reply.count("\n") >= 4:
            max_chars = max(max_chars, 900)
        if getattr(self.llm, "last_finish_reason", "") == "length":
            logger.info("回复被 max_tokens 截断，按句末裁剪 chat=%s", inbound.chat_id[-8:])
        reply = tidy_reply(reply, max_chars)
        reply = sanitize_reply(reply, max_chars, keep_newlines=bool(_ask_long))
        reply = re.sub(r"^(大肥鱼|小鲸鱼|本鱼)\s*[:：]\s*", "", reply).strip()
        if not reply:
            return None
        self.store.history_add(
            inbound.chat_id, "assistant", self.cfg.get("persona", {}).get("self_name", "大肥鱼"), reply
        )
        self.store.touch_chat(inbound.chat_id, replied=True)
        self._reply_times.append(time.time())
        return reply

    async def _chat(self, inbound: Inbound, text: str) -> Optional[str]:
        if not self.llm.budget_ok():
            return self._budget_notice(inbound)
        scene = "私聊" if inbound.chat_type == "dm" else "群聊"
        system = persona.build_system_prompt(
            self.cfg,
            scene=scene,
            speaker=inbound.user_name or inbound.user_id,
            channel=inbound.channel,
            is_master=self.is_master(inbound),
            extra=self.store.kv_get(f"persona_override:{inbound.chat_id}") or "",
        )
        messages = [{"role": "system", "content": system}]
        limit = int(self.cfg.get("persona", {}).get("context_messages", 8) or 8)
        for h in self.store.history_recent(inbound.chat_id, limit):
            if h["role"] == "user":
                messages.append({"role": "user", "content": f"{h['name']}：{h['text']}"})
            else:
                messages.append({"role": "assistant", "content": h["text"]})
        line = f"{inbound.user_name or inbound.user_id}：{text}"
        ambient = (inbound.reply_ctx or {}).get("ambient") or []
        if ambient:
            recent = "；".join(f"{n}：{t}" for n, t in ambient[-6:])
            line = f"（群里刚才在聊：{recent}）\n{line}"
        images = list(getattr(inbound, "images", []) or [])[:2]
        # 本地知识卡：命中关键词才注入，零成本
        notes = knowledge.lookup(self.cfg, text)
        if notes:
            messages.append({
                "role": "system",
                "content": "【背景知识】群里可能会用到的小抄，只在贴题时参考，别整段复述：\n" + notes,
            })
        # 带图且明确要认东西：先让视觉模型说个名字，再拿名字去搜（图本身搜不了）
        if images and websearch.should_search(self.cfg, text, is_master=self.is_master(inbound)):
            probe = [
                {"role": "system", "content": "你是看图助手。只输出一行，四段用｜分隔：中文名称｜英文名或学名｜分子式或关键参数｜检索关键词。认不准就写最接近的两类，别编。"},
                {"role": "user", "content": [
                    {"type": "text", "text": "这是什么？尽量说出准确名称。"},
                    *[{"type": "image_url", "image_url": {"url": u}} for u in images],
                ]},
            ]
            try:
                guess = (await self.llm.chat(probe, channel=inbound.channel, chat_id=inbound.chat_id)).text.strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("看图预判失败：%s", exc)
                guess = ""
            if guess:
                first = guess.splitlines()[0]
                bits = [x.strip() for x in first.replace("|", "｜").split("｜")]
                name = (bits[0][:40] if bits else "").strip('「」"\' ')
                conf = bits[2] if len(bits) > 2 else "中"
                head = name + " " + (bits[1] if len(bits) > 1 else "")
                if not name:
                    found, cost = "", 0.0
                else:
                    if "低" in conf:
                        logger.info("看图预判置信度低（%s），用关键词兜底检索", name[:20])
                    found, cost = await websearch.search(self.cfg, head)
                if found:
                    messages.append({
                        "role": "system",
                        "content": (
                            "【联网核实】按图预判「" + head + "」查到的资料。"
                            "先在图里数清关键特征（环数/官能团/取代基），再到资料里逐条核对："
                            "全部对得上才报名字；只对上一部分就说『像 X，但 Y 对不上』；"
                            "一个都对不上就说查到的资料和图不符，别硬套名字。\n" + found
                        ),
                    })
                if cost:
                    self.store.record_usage(
                        channel=inbound.channel, chat_id=inbound.chat_id, model="deepseek-search",
                        prompt_tokens=0, cache_hit=0, cache_miss=0, completion_tokens=0,
                        cost_cny=cost, ok=True, note="看图后联网核实 1 次",
                    )
        # 联网搜索：命中触发词才查，按次 ¥0.005 记进账本
        if not images and websearch.should_search(self.cfg, text, is_master=self.is_master(inbound)):
            found, cost = await websearch.search(self.cfg, text)
            if found:
                casual = str(((self.cfg.get("search") or {}).get("answer_style") or "")).lower() == "casual"
                if casual:
                    head = ("【联网资料】刚查到的东西，别照念清单：挑一句最有用的，用你自己的口气说，"
                            "最多两句，能不带链接就不带：\n")
                else:
                    head = "【联网资料】以下是刚搜到的结果，用它回答，必要时带上链接，别编造资料里没有的内容：\n"
                messages.append({"role": "system", "content": head + found})
            if cost:
                self.store.record_usage(
                    channel=inbound.channel, chat_id=inbound.chat_id, model="deepseek-search",
                    prompt_tokens=0, cache_hit=0, cache_miss=0, completion_tokens=0,
                    cost_cny=cost, ok=True, note="联网搜索 1 次",
                )
        if images:
            line = (
                "【看图要求】先把图里的关键信息读出来（文字/结构/数字/界面），再作答；"
                "化学式、公式、代码、表格这类专业图，先逐条列特征再下判断，"
                "认不准就说清哪部分看不清，别含糊带过。\n" + line
            )
            parts = [{"type": "text", "text": line}]
            parts += [{"type": "image_url", "image_url": {"url": url}} for url in images]
            messages.append({"role": "user", "content": parts})
        else:
            messages.append({"role": "user", "content": line})
        try:
            # 要代码/正事时把温度压回来：1.35 的聊天温度在这种场合会蹦出乱码
            _t = None
            _tech_words = ("代码", "示例", "命令", "报错", "配置", "写个", "写一段", "方案",
                           "定理", "原理", "公式", "证明", "怎么算", "是什么", "区别", "定义")
            if any(w in (text or "") for w in _tech_words):
                _t = min(0.75, self.llm.temperature)  # 知识/技术问题：要准，不要花样
                messages.append({
                    "role": "system",
                    "content": "（知识/技术问题：先用一句话给准确结论，再用一句人话解释；"
                               "不确定就直说「这个我记不清了」，不要编，也不要提 AI。）",
                })
            reply = await self.llm.chat(messages, channel=inbound.channel, chat_id=inbound.chat_id, temperature=_t)
            text_out = (reply.text or "").strip()
            if _half_sentence(text_out):
                text_out = await self._finish_half(messages, text_out, inbound)
            return text_out
        except BudgetExceeded:
            return self._budget_notice(inbound)
        except Exception as exc:  # noqa: BLE001 - 群聊里失败只回一句，不刷屏
            logger.warning("LLM 调用失败 chat=%s: %s", inbound.chat_id, exc)
            if images:
                # 图片这条通道挂了就退回纯文本，别让整条消息哑掉
                messages[-1] = {"role": "user", "content": line + "（图没看清）"}
                try:
                    reply = await self.llm.chat(messages, channel=inbound.channel, chat_id=inbound.chat_id)
                    return reply.text
                except Exception as exc2:  # noqa: BLE001
                    logger.warning("LLM 纯文本重试也失败: %s", exc2)
            return "……（甩尾）"

    async def _finish_half(self, messages: list, half: str, inbound: Inbound) -> str:
        """「说话说一半」急救：重问一次把句子说完；还不行就地把逗号补成句号。"""
        try:
            again = await self.llm.chat(
                messages + [
                    {"role": "assistant", "content": half},
                    {"role": "user", "content": "（你这条说到一半就断了，把这句话说完，别重开话题，30 字内。）"},
                ],
                channel=inbound.channel,
                chat_id=inbound.chat_id,
            )
            fixed = (again.text or "").strip()
            if fixed and not _half_sentence(fixed):
                logger.info("半句已补全 chat=%s", inbound.chat_id[-8:])
                return fixed
        except Exception as exc:  # noqa: BLE001 - 补句失败不影响正常回复
            logger.warning("补句重问失败: %s", exc)
        return half.rstrip("，,、；;：:") + "。"

    def _budget_notice(self, inbound: Inbound) -> Optional[str]:
        flag = f"budget_notice:{inbound.chat_id}:{day_key()}"
        if self.store.kv_get(flag):
            return None
        self.store.kv_set(flag, "1")
        return "（本鱼今天的零花钱用光了……明天再喂我嘛）"