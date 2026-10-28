"""微信（iLink Bot）渠道：长轮询 getupdates，文本收发，context_token 回传。

协议沿用 ~/.hermes/hermes-agent/gateway/platforms/weixin.py（iLink bot token），仅保留文本。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from .. import settings
from .base import Channel, Handler, Inbound

logger = logging.getLogger("fatfish.weixin")

ILINK_BASE = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND = "ilink/bot/sendmessage"

STATE_DIR = settings.DATA_DIR / "weixin"
SYNC_PATH = STATE_DIR / "sync.json"
CTX_PATH = STATE_DIR / "context_tokens.json"
OUTBOX_PATH = STATE_DIR / "outbox.json"


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class WeixinChannel(Channel):
    name = "weixin"

    def __init__(self, cfg: Dict[str, Any], on_message: Handler):
        super().__init__(cfg, on_message)
        w = cfg.get("channels", {}).get("weixin", {}) or {}
        self.account_id = str(w.get("account_id") or settings.get("WEIXIN_ACCOUNT_ID") or "").strip()
        self.base_url = str(w.get("base_url") or settings.get("WEIXIN_BASE_URL") or ILINK_BASE).rstrip("/")
        self.group_policy = str(w.get("group_policy", "disabled") or "disabled")
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._send_lock = asyncio.Lock()
        self._seen: Dict[str, float] = {}
        self._ctx: Dict[str, str] = {}
        self._sync_buf = ""
        self._load_state()
        self.token = self._load_token()

    # ── 状态 ──
    def _load_state(self) -> None:
        try:
            if SYNC_PATH.exists():
                self._sync_buf = str((json.loads(SYNC_PATH.read_text(encoding="utf-8")) or {}).get("buf") or "")
            if CTX_PATH.exists():
                data = json.loads(CTX_PATH.read_text(encoding="utf-8")) or {}
                self._ctx = {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v}
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取微信状态失败: %s", exc)

    def _save_sync(self) -> None:
        try:
            _atomic_write(SYNC_PATH, {"buf": self._sync_buf})
        except Exception as exc:  # noqa: BLE001
            logger.warning("写 sync_buf 失败: %s", exc)

    def _save_ctx(self) -> None:
        try:
            _atomic_write(CTX_PATH, self._ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写 context_token 失败: %s", exc)

    def _load_token(self) -> str:
        token = settings.get("WEIXIN_TOKEN")
        if token:
            return token
        path = Path.home() / ".hermes" / "weixin" / "accounts" / f"{self.account_id}.json"
        try:
            if path.exists():
                return str((json.loads(path.read_text(encoding="utf-8")) or {}).get("token") or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ── 协议 ──
    def _headers(self, body: str) -> Dict[str, str]:
        uin = base64.b64encode(str(int.from_bytes(secrets.token_bytes(4), "big")).encode()).decode()
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Content-Length": str(len(body.encode("utf-8"))),
            "X-WECHAT-UIN": uin,
            "iLink-App-Id": APP_ID,
            "iLink-App-ClientVersion": str(CLIENT_VERSION),
            "Authorization": f"Bearer {self.token}",
        }

    async def _post(self, endpoint: str, payload: Dict[str, Any], timeout: float = 35.0) -> Dict[str, Any]:
        assert self._session is not None
        body = json.dumps(
            {**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        url = f"{self.base_url}/{endpoint}"
        async with self._session.post(
            url, data=body, headers=self._headers(body), timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            raw = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"iLink {endpoint} HTTP {resp.status}: {raw[:200]}")
            return json.loads(raw) if raw else {}

    # ── 生命周期 ──
    # ── 未送达队列：会话未就绪时先缓存，等下次入站补发 ──
    def _load_outbox(self) -> Dict[str, List[str]]:
        try:
            if OUTBOX_PATH.exists():
                data = json.loads(OUTBOX_PATH.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    return {str(k): [str(x) for x in v][-3:] for k, v in data.items() if isinstance(v, list)}
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _save_outbox(self, data: Dict[str, List[str]]) -> None:
        try:
            _atomic_write(OUTBOX_PATH, {k: v[-3:] for k, v in data.items() if v})
        except Exception as exc:  # noqa: BLE001
            logger.warning("outbox 写入失败: %s", exc)

    def _queue_undelivered(self, to: str, text: str) -> None:
        data = self._load_outbox()
        data.setdefault(to, []).append(text[:500])
        self._save_outbox(data)
        logger.warning("微信未送达，已入队 to=%s pending=%d", to[:10], len(data[to]))

    def _take_undelivered(self, to: str) -> List[str]:
        data = self._load_outbox()
        pending = data.pop(to, [])
        if pending:
            self._save_outbox(data)
        return pending

    async def _flush_undelivered(self, inbound: Inbound) -> None:
        to = str(inbound.reply_ctx.get("to") or inbound.chat_id)
        for text in self._take_undelivered(to):
            try:
                if not await self.send(inbound, text):
                    self._queue_undelivered(to, text)
                    break
                logger.info("补发历史未送达消息 to=%s len=%d", to[:10], len(text))
            except Exception as exc:  # noqa: BLE001
                logger.warning("补发失败: %s", exc)
                self._queue_undelivered(to, text)
                break

    async def start(self) -> None:
        if not self.account_id or not self.token:
            logger.error("微信未配置 account_id/token（WEIXIN_ACCOUNT_ID / WEIXIN_TOKEN），跳过")
            return
        self._session = aiohttp.ClientSession(trust_env=False)
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("微信已连接 account=%s base=%s", self.account_id[:8], self.base_url)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _poll_loop(self) -> None:
        backoff = 2.0
        while self._running:
            try:
                resp = await self._post(EP_GET_UPDATES, {"get_updates_buf": self._sync_buf})
                ret = resp.get("ret")
                errcode = resp.get("errcode")
                if ret not in (0, None) or errcode not in (0, None):
                    if ret == -14 or errcode == -14:
                        logger.error("微信会话失效（-14），暂停 10 分钟")
                        await asyncio.sleep(600)
                        backoff = 2.0
                        continue
                    logger.warning("微信 getupdates 失败 ret=%s errcode=%s errmsg=%s", ret, errcode, resp.get("errmsg"))
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
                backoff = 2.0
                buf = resp.get("get_updates_buf")
                if buf:
                    self._sync_buf = str(buf)
                    self._save_sync()
                for msg in resp.get("msgs") or []:
                    asyncio.create_task(self._handle_message(msg))
            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("微信轮询异常: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ── 入站 ──
    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        try:
            sender = str(msg.get("from_user_id") or "").strip()
            if not sender or sender == self.account_id:
                return
            if msg.get("msg_type") == 2:  # bot 自己的回显
                return
            mid = str(msg.get("message_id") or "").strip()
            now = time.time()
            self._seen = {k: v for k, v in self._seen.items() if now - v < 300}
            if mid and mid in self._seen:
                return
            if mid:
                self._seen[mid] = now
            item_list = msg.get("item_list") or []
            text = ""
            for item in item_list:
                if item.get("type") == 1:
                    text = str((item.get("text_item") or {}).get("text") or "").strip()
                    if text:
                        break
            if not text:
                return
            ctx = str(msg.get("context_token") or "").strip()
            if ctx:
                self._ctx[sender] = ctx
                self._save_ctx()
            room = str(msg.get("room_id") or msg.get("chat_room_id") or "").strip()
            to_uid = str(msg.get("to_user_id") or "").strip()
            is_group = bool(room) or (bool(to_uid) and to_uid != self.account_id)
            chat_type = "group" if is_group else "dm"
            if chat_type == "group" and self.group_policy == "disabled":
                logger.info("忽略微信群消息（group_policy=disabled）")
                return
            chat_id = room or (to_uid if is_group else "") or sender
            if ctx:
                self._ctx[chat_id] = ctx
                self._save_ctx()
            logger.info(
                "微信入站 from=%s chat=%s type=%s len=%d text=%r",
                sender[:10], chat_id[:10], chat_type, len(text), text[:40],
            )
            inbound = Inbound(
                channel=self.name,
                chat_id=chat_id,
                chat_type=chat_type,
                user_id=sender,
                user_name=sender[:12],
                text=text,
                message_id=mid,
                mentioned=True,
                reply_ctx={"context_token": ctx or self._ctx.get(chat_id, ""), "to": chat_id},
            )
            await self._flush_undelivered(inbound)
            await self._dispatch(inbound)
        except Exception as exc:  # noqa: BLE001
            logger.error("处理微信消息失败: %s", exc)

    # ── 出站 ──
    async def send(self, inbound: Inbound, text: str) -> bool:
        to = str(inbound.reply_ctx.get("to") or inbound.chat_id)
        ctx = str(inbound.reply_ctx.get("context_token") or self._ctx.get(to) or "")
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": uuid.uuid4().hex,
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            }
        }
        if ctx:
            payload["msg"]["context_token"] = ctx
        async with self._send_lock:
            for attempt in range(3):
                try:
                    resp = await self._post(EP_SEND, payload, timeout=30.0)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("微信发送异常 attempt=%d: %s", attempt, exc)
                    await asyncio.sleep(2)
                    continue
                ret, errcode = resp.get("ret"), resp.get("errcode")
                if ret in (0, None) and errcode in (0, None):
                    return True
                logger.warning("微信发送失败 ret=%s errcode=%s errmsg=%s", ret, errcode, resp.get("errmsg"))
                if int(ret or 0) in (-14, -2) or int(errcode or 0) in (-14, -2):
                    if ctx:
                        ctx = ""
                        payload["msg"].pop("context_token", None)
                        continue  # 无 token 重发一次
                    await asyncio.sleep(30)
            self._queue_undelivered(to, text)
            return False