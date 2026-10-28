"""QQ 官方机器人渠道：WebSocket 收 @ 消息，REST 被动回复（msg_id + msg_seq）。

协议参考 ~/.hermes/hermes-agent/gateway/platforms/qqbot/（QQ 开放平台 v2 API）。
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
import logging
import random
import time
from typing import Any, Dict, Optional, Set

import aiohttp
import httpx

from .. import settings
from .base import Channel, Handler, Inbound

logger = logging.getLogger("fatfish.qq")

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

GROUPS_PATH = settings.DATA_DIR / "qq" / "groups.json"


def remember_group(group_openid: str, *, event: str = "GROUP_ADD_ROBOT") -> None:
    """记下机器人被加进的群，diagnose 和排障用。"""
    goid = (group_openid or "").strip()
    if not goid:
        return
    try:
        GROUPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if GROUPS_PATH.exists():
            data = json.loads(GROUPS_PATH.read_text(encoding="utf-8")) or {}
        data = dict(data) if isinstance(data, dict) else {}
        data[goid] = {"event": event, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        GROUPS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.warning("记录群信息失败: %s", goid[:12])


INTENTS = (1 << 25) | (1 << 30) | (1 << 12) | (1 << 26)

MSG_EVENTS = {
    "GROUP_AT_MESSAGE_CREATE",
    "GROUP_MESSAGE_CREATE",  # 「接收所有消息」全量模式，intent 同为 1<<25
    "C2C_MESSAGE_CREATE",
    "AT_MESSAGE_CREATE",
    "DIRECT_MESSAGE_CREATE",
}


PROACTIVE_LINES = [
    "（甩甩尾鳍）群里好安静……本鱼要饿扁了。",
    "喂，都没人说话了吗？本鱼在这儿呢。",
    "安静得能听见本鱼甩尾巴的声音了。",
    "（悄悄游过）没人就没人嘛，本鱼自己玩。",
]

STABLE_SESSION_SECONDS = 15.0


def _is_debug_text(text: str, words: tuple) -> bool:
    """技术/调试类话题：这类问题按主人要求只在私聊说，不刷群。"""
    low = (text or "").lower()
    return any(w in low for w in words)


def _mentions_bot(d: Dict[str, Any]) -> bool:
    """全量群消息里判断这条是否 @ 了本机器人。

    平台把 @ 替换成 <@openid> / <@all>，被 @ 的成员在 mentions 里；
    mentions 项若带 bot/is_you 标记优先用它，否则退化为「mentions 里出现 bot 标记」。
    """
    for m in d.get("mentions") or []:
        if not isinstance(m, dict):
            continue
        if m.get("bot") is True or m.get("is_you") is True:
            return True
    content = str(d.get("content") or "")
    return "<@all>" in content


def next_backoff(lived_seconds: float, current: float) -> tuple[float, float]:
    """一次连接结束后决定等多久再重连：短命连接指数退避，稳定连接立即重连。

    返回 (等待秒数, 新的退避基数)。
    """
    if lived_seconds >= STABLE_SESSION_SECONDS:
        return 0.0, 2.0
    delay = max(1.0, current)
    return delay, min(delay * 2, 60.0)


def _effective_text(text: str, event: str) -> str:
    """去掉 @ 标记后的有效文本。群 @ 只发一个 @ 没打字时，当作“在吗”而不是静默丢弃。"""
    stripped = _strip_mention(text)
    if stripped:
        return stripped
    if event == "GROUP_AT_MESSAGE_CREATE":
        return "在吗"
    return ""


def _strip_mention(text: str) -> str:
    import re

    return re.sub(r"<@!?\w+>", "", text or "").strip()


def _strip_face(text: str) -> str:
    """QQ 的表情在 content 里是 <faceType=..,faceId="0",ext=".."> 这种标签，别当正文。"""
    import re

    cleaned = re.sub(r"<faceType=[^>]*>", "", text or "")
    cleaned = re.sub(r"<emoji[^>]*>", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


class QQChannel(Channel):
    name = "qq"

    def __init__(self, cfg: Dict[str, Any], on_message: Handler, store: Any = None):
        super().__init__(cfg, on_message, store)
        q = cfg.get("channels", {}).get("qq", {}) or {}
        self.app_id = str(q.get("app_id") or settings.get("QQ_APP_ID") or "").strip()
        self.secret = (settings.get("FATFISH_QQ_CLIENT_SECRET") or settings.get("QQ_CLIENT_SECRET") or "").strip()
        self.group_policy = str(q.get("group_policy", "allow_all") or "allow_all")
        # group_reply: all（谁 @ 都回）| master_only（只有主人 @ 才回，其余全员沉默）
        self.group_reply = str(q.get("group_reply", "all") or "all")
        self.master_members = {str(x) for x in (q.get("master_members") or [])}
        self.allowed_groups: Set[str] = {str(g) for g in (q.get("allowed_groups") or [])}
        from .. import names as _names0

        self.member_names: Dict[str, str] = _names0.config_map(cfg)
        self.reply_in_guild = bool(q.get("reply_in_guild", False))
        chime = q.get("chime") or {}
        self.chime_enabled = bool(chime.get("enabled", False))
        self.chime_names = [str(x).lower() for x in (chime.get("names") or [])]
        self.chime_commands = [str(x).lower() for x in (chime.get("commands") or [])]
        self.chime_prob = float(chime.get("random_prob", 0.0) or 0.0)
        self.chime_cooldown = float(chime.get("cooldown_seconds", 60) or 0)
        self.chime_max_per_hour = int(chime.get("max_per_hour", 30) or 30)
        self._chime_state: Dict[str, Dict[str, Any]] = {}
        self._group_hist: Dict[str, deque] = {}
        proactive = q.get("proactive") or {}
        self.proactive_enabled = bool(proactive.get("enabled", False))
        self.proactive_idle = float(proactive.get("idle_minutes", 90) or 0) * 60
        self.proactive_max_day = int(proactive.get("max_per_day", 3) or 0)
        self._group_last_seen: Dict[str, float] = {}
        self._dm_chats: list = []
        self._recent_asks: Dict[str, tuple] = {}  # 同一个人同一句话短时间只回一次
        # 一分钟内的收/发统计，心跳时打出来，用来看清"为什么不回"
        self.stats: Dict[str, int] = {
            "group": 0, "c2c": 0, "reply": 0,
            "skip_notcalled": 0, "skip_dup": 0, "skip_image": 0,
        }
        self._last_skip_log = 0.0
        features = cfg.get("features", {}) or {}
        self.debug_to_dm = bool(features.get("debug_to_dm", True))
        self.debug_words = tuple(
            str(w).lower()
            for w in (
                features.get("debug_dm_keywords")
                or ["api-key", "apikey", "key", "token", "日志", "报错", "代码", "配置",
                    "部署", "dsh", "sql", "数据库", "接口", "调试", "报错", "traceback"]
            )
        )
        self._proactive_sent: Dict[str, int] = {}
        self._proactive_task: Optional[asyncio.Task] = None
        self.group_history_limit = int(q.get("group_history_limit", 8) or 0)
        tr = cfg.get("triggers", {}) or {}
        # 调参台「群里命中唤醒词也回」：原来只在 brain 里判，群消息在渠道层就被丢了 → 勾了没用
        self.group_keyword_reply = bool(tr.get("group_keyword_reply", True))
        self.keywords = [str(k).lower() for k in (tr.get("keywords") or []) if str(k).strip()]
        # 群里叫别的机器人时不插话（例如「@claude大小姐」）
        self.chime_ignore = [str(x).lower() for x in ((q.get("chime") or {}).get("ignore_mentions") or [])]
        self._http: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._running = False
        self._token = ""
        self._token_exp = 0.0
        self._session_id: Optional[str] = None
        self._seq: Optional[int] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._named_seen: Set[str] = set()
        self._dedup: Dict[str, float] = {}
        self._last_frame_ts = 0.0
        rt = cfg.get("runtime", {}) or {}
        self.same_text_window = float(rt.get("same_text_window_seconds", 60) or 0)
        self.msg_dedup_window = float(rt.get("msg_dedup_seconds", 300) or 0)
        self.zombie_silent_min = float(rt.get("zombie_silent_seconds", 90) or 0)

    def reload(self, cfg: Dict[str, Any]) -> None:
        """热更新渠道层参数（原先只有 brain 会 reload，接话概率/冒泡改了必须重启，等于调参台白调）。"""
        self.cfg = cfg
        q = cfg.get("channels", {}).get("qq", {}) or {}
        self.group_policy = str(q.get("group_policy", "allow_all") or "allow_all")
        self.group_reply = str(q.get("group_reply", "all") or "all")
        self.master_members = {str(x) for x in (q.get("master_members") or [])}
        self.allowed_groups = {str(g) for g in (q.get("allowed_groups") or [])}
        from .. import names as _names

        self.member_names = _names.config_map(cfg)
        chime = q.get("chime") or {}
        self.chime_enabled = bool(chime.get("enabled", False))
        self.chime_prob = float(chime.get("random_prob", 0.0) or 0.0)
        self.chime_cooldown = float(chime.get("cooldown_seconds", 60) or 0)
        self.chime_max_per_hour = int(chime.get("max_per_hour", 30) or 30)
        self.chime_names = [str(x).lower() for x in (chime.get("names") or [])]
        self.chime_commands = [str(x).lower() for x in (chime.get("commands") or [])]
        self.chime_ignore = [str(x).lower() for x in (chime.get("ignore_mentions") or [])]
        proactive = q.get("proactive") or {}
        self.proactive_enabled = bool(proactive.get("enabled", False))
        self.proactive_idle = float(proactive.get("idle_minutes", 90) or 0) * 60
        self.proactive_max_day = int(proactive.get("max_per_day", 3) or 0)
        features = cfg.get("features", {}) or {}
        self.debug_to_dm = bool(features.get("debug_to_dm", True))
        self.group_history_limit = int(q.get("group_history_limit", 8) or 0)
        tr = cfg.get("triggers", {}) or {}
        # 调参台「群里命中唤醒词也回」：原来只在 brain 里判，群消息在渠道层就被丢了 → 勾了没用
        self.group_keyword_reply = bool(tr.get("group_keyword_reply", True))
        self.keywords = [str(k).lower() for k in (tr.get("keywords") or []) if str(k).strip()]
        rt = cfg.get("runtime", {}) or {}
        self.same_text_window = float(rt.get("same_text_window_seconds", 60) or 0)
        self.msg_dedup_window = float(rt.get("msg_dedup_seconds", 300) or 0)
        self.zombie_silent_min = float(rt.get("zombie_silent_seconds", 90) or 0)
        logger.info(
            "QQ 渠道参数已热更新 群策略=%s 群回复=%s 接话=%s 概率=%.2f 冒泡=%s 同句窗口=%.0fs",
            self.group_policy, self.group_reply, self.chime_enabled, self.chime_prob,
            self.proactive_enabled, self.same_text_window,
        )

    # ── 生命周期 ──
    async def start(self) -> None:
        if not self.app_id or not self.secret:
            logger.error("QQ 未配置 app_id/client_secret（QQ_APP_ID / QQ_CLIENT_SECRET），跳过")
            return
        self._http = httpx.AsyncClient(trust_env=False, timeout=30.0)
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("QQ 渠道启动 appid=%s", self.app_id)

    async def stop(self) -> None:
        self._running = False
        for task in (self._heartbeat_task, self._proactive_task, self._task):
            if task:
                task.cancel()
        self._heartbeat_task = None
        self._proactive_task = None
        self._task = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._http:
            await self._http.aclose()
            self._http = None

    async def _run(self) -> None:
        backoff = 2.0
        while self._running:
            started = time.time()
            try:
                await self._session_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("QQ 连接中断: %s", exc)
            lived = time.time() - started
            delay, backoff = next_backoff(lived, backoff)
            if delay:
                logger.warning("QQ 会话仅存活 %.0fs，%.0fs 后重连", lived, delay)
                await asyncio.sleep(delay)
            else:
                logger.info("QQ 会话结束（存活 %.0fs），立即重连", lived)

    async def _session_once(self) -> None:
        token = await self._ensure_token()
        gw = await self._gateway_url(token)
        logger.info("QQ 网关 URL: %s", gw.split("?")[0])
        async with aiohttp.ClientSession(trust_env=False) as session:
            ws = await session.ws_connect(gw, timeout=aiohttp.ClientTimeout(total=20), heartbeat=None)
            self._ws = ws
            try:
                async for frame in ws:
                    if frame.type == aiohttp.WSMsgType.TEXT:
                        self._last_frame_ts = time.time()
                        try:
                            payload = json.loads(frame.data)
                        except Exception:  # noqa: BLE001
                            continue
                        await self._dispatch_ws(payload)
                    elif frame.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        raise RuntimeError("QQ WebSocket 关闭")
            finally:
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    self._heartbeat_task = None
                self._ws = None
                if not ws.closed:
                    await ws.close()

    def should_speak(self, group: str, now: float, day: str) -> bool:
        """长时间没人说话时要不要主动冒个泡（主动消息有平台配额，默认关闭）。"""
        if not self.proactive_enabled or not group:
            return False
        last = self._group_last_seen.get(group)
        if not last or now - last < self.proactive_idle:
            return False
        key = f"{group}:{day}"
        if self._proactive_sent.get(key, 0) >= self.proactive_max_day:
            return False
        return True

    def mark_spoken(self, group: str, day: str) -> None:
        key = f"{group}:{day}"
        self._proactive_sent[key] = self._proactive_sent.get(key, 0) + 1

    async def _proactive_loop(self) -> None:
        """每 60s 扫一遍：冷场够久就主动说一句（群里会收到主动消息，注意配额）。"""
        while self._running:
            await asyncio.sleep(60)
            if not self.proactive_enabled:
                continue
            day = time.strftime("%Y-%m-%d")
            for group in list(self._group_last_seen):
                if not self.should_speak(group, time.time(), day):
                    continue
                line = random.choice(PROACTIVE_LINES)
                inbound = Inbound(
                    channel=self.name,
                    chat_id=group,
                    chat_type="group",
                    user_id="self",
                    user_name="大肥鱼",
                    text="",
                    message_id="",
                    mentioned=True,
                    reply_ctx={"group_openid": group},
                )
                if await self.send(inbound, line):
                    self.mark_spoken(group, day)
                    self._group_last_seen[group] = time.time()
                    logger.info("QQ 主动冒泡 group=%s", group[-8:])

    def _remember_group_line(self, d: Dict[str, Any]) -> None:
        """把群消息记进环形缓存，回复时当上下文用（@ 和闲聊都算）。"""
        if not self.group_history_limit:
            return
        group = str(d.get("group_openid") or "")
        text = _effective_text(str(d.get("content") or ""), "GROUP_MESSAGE_CREATE").strip()
        if not group or not text:
            return
        member = str((d.get("author") or {}).get("member_openid") or "")
        self._group_last_seen[group] = time.time()
        buf = self._group_hist.setdefault(group, deque(maxlen=self.group_history_limit))
        buf.append((self.member_name(member), text[:120]))

    def _recent_group_lines(self, group: str) -> list:
        buf = self._group_hist.get(group)
        return list(buf)[:-1] if buf else []

    def member_name(self, member_openid: str) -> str:
        """群成员固定称呼：config 写死 > 自己报的名字 > 群友后四位。"""
        from .. import names

        uid = str(member_openid or "")
        store = getattr(self, "store", None)
        if store is not None:
            # 主人说过「6289 是小明」但当时还没见过完整 openid：等人一开口就认领
            waiting = names.pending_for(store, uid)
            if waiting and not names.self_named(store, uid):
                names.bind(store, uid, waiting)
                names.clear_pending(store, uid)
                logger.info("QQ 群友 %s 认领了主人预先给的名字「%s」", uid[-6:], waiting)
        if uid and store is not None:
            try:  # 记一份见过的完整 openid，方便事后按尾号起名
                store.kv_set(f"seen:{uid}", str(int(time.time())))
            except Exception:  # noqa: BLE001
                pass
        name = names.resolve(store, self.cfg, uid)
        if uid and uid not in self._named_seen and name.startswith("群友"):
            self._named_seen.add(uid)
            logger.info("QQ 群新面孔 member=%s（还没起名，显示为 %s）", uid, name)
        return name

    def _record_group_line(self, d: Dict[str, Any], member: str, text: str) -> None:
        """渠道层就丢掉的消息也进历史：否则群里「没叫本鱼」的那半段记录是空的。"""
        if self.store is None or not (text or "").strip():
            return
        group = str(d.get("group_openid") or "")
        if not group:
            return
        try:
            self.store.history_add(group, "user", self.member_name(member), text.strip())
            self.store.touch_chat(group, replied=False)
        except Exception:  # noqa: BLE001 - 记不上不该影响回话
            logger.debug("群消息落库失败", exc_info=True)

    def _keyword_hit(self, text: str) -> bool:
        """triggers.group_keyword_reply + triggers.keywords：没 @ 但说了唤醒词也接。"""
        if not self.group_keyword_reply or not self.keywords:
            return False
        low = (text or "").lower()
        return any(k in low for k in self.keywords)

    def _should_chime(self, group: str, raw_text: str) -> bool:
        """全量模式下这条群消息要不要接：叫名字 / 命中指令词 / 小概率随机，且有冷却与频次上限。"""
        if not self.chime_enabled or not group:
            return False
        import re as _re

        text = _re.sub(r"<@!?[^>]+>", " ", raw_text)
        text = text.replace("<@all>", " ").strip()
        if not text:
            return False
        low = text.lower()

        if self.chime_ignore and any(x in low for x in self.chime_ignore) and not any(
            n and n in low for n in self.chime_names
        ):
            return False  # 叫的是别的机器人，不插话
        hit = any(n and n in low for n in self.chime_names)
        if not hit and any(c and c in low for c in self.chime_commands):
            hit = True
        if not hit:
            hit = self.chime_prob > 0 and random.random() < self.chime_prob
        if not hit:
            return False

        now = time.time()
        st = self._chime_state.setdefault(group, {"last": 0.0, "times": []})
        if now - float(st["last"]) < self.chime_cooldown:
            return False
        st["times"] = [x for x in st["times"] if now - x < 3600]
        if len(st["times"]) >= self.chime_max_per_hour:
            return False
        st["last"] = now
        st["times"].append(now)
        logger.info("QQ 群接话 group=%s text=%r", group[-8:], text[:20])
        return True

    # ── 鉴权 ──
    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        assert self._http is not None
        resp = await self._http.post(
            TOKEN_URL, json={"appId": self.app_id, "clientSecret": self.secret}
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"QQ token HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"QQ token 响应异常: {data}")
        self._token = str(token)
        self._token_exp = time.time() + int(data.get("expires_in", 7200))
        return self._token

    async def _gateway_url(self, token: str) -> str:
        assert self._http is not None
        resp = await self._http.get(
            f"{API_BASE}/gateway", headers={"Authorization": f"QQBot {token}"}
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"QQ gateway HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        url = data.get("url")
        if not url:
            raise RuntimeError(f"QQ gateway 响应异常: {data}")
        return str(url)

    # ── WS 事件 ──
    async def _dispatch_ws(self, payload: Dict[str, Any]) -> None:
        op = payload.get("op")
        seq = payload.get("s")
        if isinstance(seq, int):
            self._seq = seq
        if op == 10:  # Hello
            interval = int((payload.get("d") or {}).get("heartbeat_interval", 30000))
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            self._heartbeat_task = asyncio.create_task(self._heartbeat(interval))
            if self.proactive_enabled and not self._proactive_task:
                self._proactive_task = asyncio.create_task(self._proactive_loop())
            await self._identify()
        elif op == 0:
            t = payload.get("t")
            d = payload.get("d") or {}
            logger.info("QQ 事件 t=%s", t)
            if t in ("GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT"):
                gid = str(d.get("group_openid") or "")
                logger.info("QQ 群变更 %s group=%s", t, gid)
                remember_group(gid, event=t)
            if t == "READY":
                self._session_id = (d.get("session_id") or self._session_id) if isinstance(d, dict) else self._session_id
                logger.info("QQ READY session=%s", str(self._session_id)[:8] if self._session_id else "?")
            elif t in MSG_EVENTS:
                asyncio.create_task(self._handle_event(t, d))
            elif t not in ("READY",):
                logger.info("QQ 未处理事件 t=%s 字段=%s", t, ",".join(sorted(d.keys()))[:120])
        elif op == 7:  # Reconnect
            raise RuntimeError("QQ 要求重连")
        elif op == 9:  # Invalid session
            self._session_id = None
            raise RuntimeError("QQ 会话失效")

    async def _identify(self) -> None:
        if not self._ws or self._ws.closed:
            return
        token = await self._ensure_token()
        if self._session_id:
            await self._ws.send_json(
                {"op": 6, "d": {"token": f"QQBot {token}", "session_id": self._session_id, "seq": self._seq}}
            )
            logger.info("QQ Resume 已发送")
            return
        logger.info("QQ Identify intents=%s", INTENTS)
        await self._ws.send_json(
            {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": INTENTS,
                    "shard": [0, 1],
                    "properties": {"$os": "macOS", "$browser": "fatfish", "$device": "fatfish"},
                },
            }
        )
        logger.info("QQ Identify 已发送")

    async def _heartbeat(self, interval_ms: int) -> None:
        delay = max(5.0, interval_ms / 1000.0 * 0.8)
        silent_limit = max(self.zombie_silent_min, delay * 2.5)
        while True:
            await asyncio.sleep(delay)
            ws = self._ws
            if not ws or ws.closed:
                return
            silent = time.time() - (self._last_frame_ts or time.time())
            if silent > silent_limit:
                logger.warning("QQ 连接 %.0fs 无任何帧（僵尸连接），主动断开重连", silent)
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                await ws.send_json({"op": 1, "d": self._seq})
            except Exception:  # noqa: BLE001
                return

    # ── 入站 ──
    @staticmethod
    def _quoted_parts(d: Dict[str, Any]) -> tuple:
        """message_type=103 是引用消息：被引用的原文在 msg_elements[].content，
        被引用的图在 msg_elements[].attachments。返回 (文本, 附件, 图片URL)。"""
        try:
            is_quote = int(d.get("message_type") or 0) in (102, 103)
        except (TypeError, ValueError):
            is_quote = False
        elements = d.get("msg_elements")
        if not is_quote or not isinstance(elements, list):
            return "", [], []
        texts: list = []
        atts: list = []
        for e in elements:
            if not isinstance(e, dict):
                continue
            t = str(e.get("content") or "").strip()
            if t:
                texts.append(t)
            atts.extend(a for a in (e.get("attachments") or []) if isinstance(a, dict))
        return " ".join(texts), atts, QQChannel._attachment_image_urls(atts)

    @staticmethod
    def _attachment_image_urls(atts: list) -> list:
        urls = []
        for att in atts:
            url = str(att.get("url") or "")
            if not url:
                continue
            ct = str(att.get("content_type") or "").lower()
            low = url.lower()
            looks_image = (
                ct.startswith("image")
                or any(k in low for k in ("gchatpic", "multimedia.nt.qq.com", "gchat.qpic.cn", "/download?appid="))
                or low.split("?")[0].endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
                or (not ct and not url.lower().split("?")[0].endswith((".mp4", ".amr", ".silk", ".wav")))
            )
            if looks_image:
                urls.append(url)
        return urls

    @classmethod
    def _image_urls(cls, d: Dict[str, Any]) -> list:
        """QQ 群图 URL 常常没有扩展名（gchatpic / multimedia.nt.qq.com），只靠 content_type 会漏。"""
        atts = [a for a in (d.get("attachments") or []) if isinstance(a, dict)]
        urls = cls._attachment_image_urls(atts)
        if atts:
            logger.info("QQ 附件 %d 个，识别为图片 %d 个", len(atts), len(urls))
        return urls

    async def _image_payloads(self, urls: list) -> list:
        """把图下载成 data URL：deepseek-flash 原生支持图片输入（实测），不必 OCR。"""
        urls = urls[:2]
        out = []
        if not urls or not self._http:
            return out
        for url in urls:
            try:
                resp = await self._http.get(url)
                if resp.status_code != 200 or not resp.content:
                    continue
                mime = "image/png"
                ct = str(resp.headers.get("content-type") or "")
                if ct.startswith("image/"):
                    mime = ct.split(";")[0].strip()
                elif url.lower().split("?")[0].endswith((".jpg", ".jpeg")):
                    mime = "image/jpeg"
                out.append(f"data:{mime};base64," + __import__("base64").b64encode(resp.content).decode())
            except Exception as exc:  # noqa: BLE001
                logger.warning("QQ 图片下载失败: %s", exc)
        if out:
            logger.info("QQ 图片已转 base64 %d 张（原生多模态直读）", len(out))
        return out

    async def _image_text(self, d: Dict[str, Any]) -> str:
        """兜底：模型侧不支持图片时，退回本地 OCR。"""
        urls = self._image_urls(d)
        if not urls or not self._http:
            return ""
        import asyncio as _asyncio
        import tempfile as _tempfile
        from pathlib import Path as _Path

        from ..vision import ocr_image

        out = []
        for url in urls[:2]:
            try:
                resp = await self._http.get(url)
                if resp.status_code != 200:
                    continue
                path = _Path(_tempfile.gettempdir()) / f"fatfish_in_{int(time.time() * 1000)}.png"
                path.write_bytes(resp.content)
                text = await _asyncio.get_running_loop().run_in_executor(None, ocr_image, str(path))
                try:
                    path.unlink()
                except OSError:
                    pass
                if text:
                    out.append(text)
            except Exception as exc:  # noqa: BLE001 - 图片挂了不影响正常聊天
                logger.warning("QQ 图片识别失败: %s", exc)
        if out:
            logger.info("QQ 图片 OCR 命中 %d 张，%d 字", len(out), sum(len(t) for t in out))
        return " / ".join(out)

    async def _handle_event(self, event: str, d: Dict[str, Any]) -> None:
        try:
            msg_id = str(d.get("id") or "")
            now = time.time()
            self._dedup = {k: v for k, v in self._dedup.items() if now - v < self.msg_dedup_window}
            if msg_id and msg_id in self._dedup:
                return
            if msg_id:
                self._dedup[msg_id] = now
            if event == "C2C_MESSAGE_CREATE":
                self.stats["c2c"] += 1
            elif event in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
                self.stats["group"] += 1
            quote_text, quote_atts, quote_img_urls = self._quoted_parts(d)
            quoted_any = bool(quote_text or quote_atts)
            has_image = bool(self._image_urls(d))
            content = _effective_text(str(d.get("content") or ""), event)
            if not content and not has_image and not quoted_any:
                if d.get("ark_data"):
                    # 卡片/合并转发聊天记录：内容在卡片里，机器人拿不到，给个明确回话总比沉默好
                    content = "（发来一张卡片/聊天记录，本鱼只能看到卡片标题，里面的内容看不到）"
                    logger.info("QQ 收到卡片消息 type=%s，按可读内容兜底", d.get("message_type"))
                else:
                    return
            plain = _strip_face(content)
            if not plain and not has_image and not quoted_any:
                # 纯表情消息，没 @ 就不接话，免得对着一个表情回话
                if not _mentions_bot(d):
                    logger.debug("QQ 群纯表情消息，忽略")
                    return
                content = "（发了个表情）"
            if (
                self.debug_to_dm
                and plain
                and _is_debug_text(plain, self.debug_words)
                and (_mentions_bot(d) or event == "GROUP_AT_MESSAGE_CREATE")
            ):
                if self._dm_chats:
                    logger.info("QQ 群内调试类问题转私聊 text=%r", plain[:30])
                    dm = Inbound(
                        channel=self.name,
                        chat_id=self._dm_chats[-1],
                        chat_type="dm",
                        user_id=self._dm_chats[-1],
                        user_name="主人",
                        text=f"（群里问的）{plain}",
                        message_id="",
                        mentioned=True,
                        reply_ctx={"user_openid": self._dm_chats[-1]},
                    )
                    await self._dispatch(dm)
                    return
                logger.info("QQ 群内调试类问题，但主人还没私聊过本鱼，只能群里回")
            if event in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE") and self.group_reply == "master_only":
                who_m = str((d.get("author") or {}).get("member_openid") or "")
                allowed = who_m in self.master_members
                if event == "GROUP_AT_MESSAGE_CREATE" and not allowed:
                    if now - self._last_skip_log > 5:  # 便于主人第一次 @ 时从日志里认领 openid
                        self._last_skip_log = now
                        logger.info(
                            "QQ 群里非主人 @ 了本机，按 master_only 沉默：group=%s member_openid=%s text=%r",
                            d.get("group_openid"), who_m, (plain or "")[:20],
                        )
                    self._record_group_line(d, who_m, plain or content)
                    self.stats["skip_notcalled"] += 1
                    return
                if event == "GROUP_MESSAGE_CREATE":
                    # 群里不插话：既不看氛围也不抢话，只在被主人 @ 时出现
                    if not allowed or not _mentions_bot(d):
                        self._record_group_line(d, who_m, plain or content)
                        self.stats["skip_notcalled"] += 1
                        return
            if event in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
                who = str((d.get("author") or {}).get("member_openid") or "")
                key = f"{d.get('group_openid') or ''}:{who}"
                prev = self._recent_asks.get(key)
                ask = (plain or "").strip()
                if ask and prev and prev[0] == ask and now - prev[1] < self.same_text_window:
                    self.stats["skip_dup"] += 1
                    logger.info("QQ 同句重复（%s），不重复回", ask[:16])
                    return
                if ask:
                    self._recent_asks[key] = (ask, now)
            if event == "GROUP_MESSAGE_CREATE":
                self._remember_group_line(d)
            group_id = str(d.get("group_openid") or "")
            if event == "GROUP_MESSAGE_CREATE" and not _mentions_bot(d) and not self._should_chime(
                group_id, plain or str(d.get("content") or "")
            ) and not self._keyword_hit(plain or str(d.get("content") or "")):
                self._record_group_line(d, str((d.get("author") or {}).get("member_openid") or ""), plain or content)
                self.stats["skip_notcalled"] += 1
                if now - self._last_skip_log > 60:  # 每分钟最多打印一条，避免刷屏
                    self._last_skip_log = now
                    logger.info(
                        "QQ 群消息没叫本鱼也没命中关键词，按策略不回（本分钟第 %d 条）text=%r",
                        self.stats["skip_notcalled"], (plain or content)[:24],
                    )
                else:
                    logger.debug("QQ 群消息不接话，忽略")
                return
            images: list = []
            own_urls = self._image_urls(d) if has_image else []
            all_urls = list(dict.fromkeys(own_urls + list(quote_img_urls)))
            if all_urls:
                images = await self._image_payloads(all_urls)
            if has_image and not plain:
                content = "（发了张图，帮我看看）" if images else "（发了张图，但本鱼没拿到）"
            if has_image and not images:
                img_text = await self._image_text(d)
                if img_text:
                    content = f"{content}（图上文字：{img_text}）" if plain else f"（发来一张图，图上文字：{img_text}）"
            if quoted_any:
                # 把被引用的内容并进正文，模型才知道"这个/这张图"指什么
                q_bits = []
                if quote_text:
                    q_bits.append(quote_text)
                if quote_img_urls:
                    q_bits.append(f"（{len(quote_img_urls)} 张图）")
                elif quote_atts:
                    q_bits.append("（附件）")
                q_line = " ".join(q_bits) or "（空）"
                content = f"（引用了一条消息：{q_line}）\n{content}" if content else f"（引用了这条消息，帮我看看：{q_line}）"
                logger.info("QQ 识别到引用消息 type=%s 文本=%r 图 %d 张",
                            d.get("message_type"), quote_text[:24], len(quote_img_urls))
            if event in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
                group = str(d.get("group_openid") or "")
                member = str((d.get("author") or {}).get("member_openid") or "")
                if not group:
                    return
                remember_group(group, event=event)
                if self.group_policy == "allowlist" and group not in self.allowed_groups:
                    logger.info("QQ 群 %s 不在白名单，忽略", group)
                    return
                inbound = Inbound(
                    channel=self.name,
                    chat_id=group,
                    chat_type="group",
                    user_id=member,
                    user_name=self.member_name(member),
                    text=content,
                    message_id=msg_id,
                    mentioned=True,
                    reply_ctx={
                        "group_openid": group,
                        "msg_id": msg_id,
                        "ambient": self._recent_group_lines(group),
                    },
                    images=images,
                )
            elif event == "C2C_MESSAGE_CREATE":
                user = str((d.get("author") or {}).get("user_openid") or "")
                if not user:
                    return
                if user not in self._dm_chats:
                    self._dm_chats.append(user)
                    self._dm_chats = self._dm_chats[-3:]
                inbound = Inbound(
                    channel=self.name,
                    chat_id=user,
                    chat_type="dm",
                    user_id=user,
                    user_name=f"鱼友{user[-4:]}",
                    text=content,
                    message_id=msg_id,
                    mentioned=True,
                    reply_ctx={"user_openid": user, "msg_id": msg_id},
                    images=images,
                )
            elif event in ("AT_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"):
                if not self.reply_in_guild:
                    return
                channel = str(d.get("channel_id") or "")
                if not channel:
                    return
                author = str((d.get("author") or {}).get("id") or "")
                inbound = Inbound(
                    channel=self.name,
                    chat_id=channel,
                    chat_type="dm" if event == "DIRECT_MESSAGE_CREATE" else "group",
                    user_id=author,
                    user_name=f"频道友{author[-4:]}",
                    text=content,
                    message_id=msg_id,
                    mentioned=True,
                    reply_ctx={"channel_id": channel, "msg_id": msg_id},
                )
            else:
                return
            logger.info(
                "QQ 入站 event=%s chat=%s user=%s text=%r",
                event, inbound.chat_id[:8], inbound.user_id[-6:], content[:40],
            )
            await self._dispatch(inbound)
        except Exception as exc:  # noqa: BLE001
            logger.error("处理 QQ 消息失败: %s", exc)

    # ── 出站 ──
    async def send(self, inbound: Inbound, text: str) -> bool:
        if not self._http:
            return False
        self.stats["reply"] += 1
        ctx = inbound.reply_ctx or {}
        if inbound.chat_type == "group":
            path = f"/v2/groups/{ctx.get('group_openid')}/messages"
        elif inbound.chat_type == "dm" and "user_openid" in ctx:
            path = f"/v2/users/{ctx.get('user_openid')}/messages"
        else:
            path = f"/channels/{ctx.get('channel_id')}/messages"
        for attempt in range(2):
            body: Dict[str, Any] = {"content": text[:1500], "msg_type": 0}
            if ctx.get("msg_id"):
                body["msg_id"] = ctx["msg_id"]
                # 每次重试换 msg_seq：平台按 msg_id+msg_seq 去重，复用会被判重复而丢弃
                body["msg_seq"] = random.randint(1, 65535)
            started = time.time()
            try:
                token = await self._ensure_token()
                resp = await self._http.post(
                    API_BASE + path,
                    headers={"Authorization": f"QQBot {token}", "Content-Type": "application/json"},
                    json=body,
                )
                if resp.status_code in (401, 403):
                    self._token = ""
                    self._token_exp = 0.0
                    await asyncio.sleep(1)
                    continue
                if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    logger.warning(
                        "QQ 发送暂时失败 HTTP %s（%.0fms），1s 后重试",
                        resp.status_code, (time.time() - started) * 1000,
                    )
                    await asyncio.sleep(1)
                    continue
                if resp.status_code >= 400:
                    logger.warning(
                        "QQ 发送失败 HTTP %s（%.0fms）: %s",
                        resp.status_code, (time.time() - started) * 1000, resp.text[:200],
                    )
                    return False
                logger.info("QQ 发送成功 %.0fms len=%d", (time.time() - started) * 1000, len(text))
                await self._maybe_sticker(inbound, text)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("QQ 发送异常（第 %d 次）: %s", attempt + 1, exc)
                await asyncio.sleep(1)
        return False

    async def _maybe_sticker(self, inbound: Inbound, text: str) -> None:
        from .. import stickers

        try:
            cfg = self.cfg
            if inbound.chat_type != "group" or not stickers._cfg(cfg).get("enabled"):
                return
            # 表情包只是配菜：话太短/没情绪就别发，绝不用图代替说话
            if len((text or "").strip()) < int(stickers._cfg(cfg).get("min_text_chars", 6) or 6):
                return
            if random.random() > float(stickers._cfg(cfg).get("prob", 0.25) or 0):
                return
            pick = stickers.pick(cfg, text, inbound.chat_id)
            if pick:
                await self.send_sticker(inbound, pick["url"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("表情包流程出错（忽略）: %s", exc)

    async def send_sticker(self, inbound: Inbound, url: str) -> bool:
        """发表情包：先上传富媒体拿 file_info，再用 msg_type=7 发图。"""
        ctx = inbound.reply_ctx or {}
        if not self._http or inbound.chat_type != "group":
            return False
        group = ctx.get("group_openid")
        if not group:
            return False
        try:
            token = await self._ensure_token()
            up = await self._http.post(
                f"{API_BASE}/v2/groups/{group}/files",
                headers={"Authorization": f"QQBot {token}"},
                params={"file_type": 1, "srv_send_msg": "false"},
                json={"file_type": 1, "srv_send_msg": False, "url": url},
            )
            if up.status_code >= 400:
                logger.warning("表情包上传失败 HTTP %s: %s", up.status_code, up.text[:160])
                return False
            file_info = (up.json() or {}).get("file_info")
            if not file_info:
                logger.warning("表情包上传没拿到 file_info: %s", up.text[:160])
                return False
            body: Dict[str, Any] = {"content": " ", "msg_type": 7, "media": {"file_info": file_info}}
            if ctx.get("msg_id"):
                body["msg_id"] = ctx["msg_id"]
                body["msg_seq"] = random.randint(1, 65535)
            resp = await self._http.post(
                f"{API_BASE}/v2/groups/{group}/messages",
                headers={"Authorization": f"QQBot {token}", "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code >= 400:
                logger.warning("表情包发送失败 HTTP %s: %s", resp.status_code, resp.text[:160])
                return False
            logger.info("QQ 表情包已发 url=%s", url.rsplit("/", 1)[-1])
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("表情包发送异常: %s", exc)
            return False