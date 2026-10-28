"""渠道抽象：统一的入站消息与发送接口。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, Optional, List


@dataclass(slots=True)
class Inbound:
    channel: str
    chat_id: str
    chat_type: str  # dm | group | guild
    user_id: str
    user_name: str
    text: str
    message_id: str = ""
    mentioned: bool = False
    reply_ctx: Dict[str, Any] = field(default_factory=dict)
    images: List[str] = field(default_factory=list)  # data:image/...;base64,xxx，原生多模态直读


Handler = Callable[[Inbound], Coroutine[Any, Any, Optional[str]]]


class Channel:
    name = "base"

    def __init__(self, cfg: Dict[str, Any], on_message: Handler, store: Any = None):
        self.cfg = cfg
        self.on_message = on_message
        self.store = store  # 群友称呼等本地数据要用；单测里可以不传

    async def start(self) -> None:  # pragma: no cover - 接口
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover - 接口
        raise NotImplementedError

    async def send(self, inbound: Inbound, text: str) -> bool:  # pragma: no cover - 接口
        raise NotImplementedError

    async def _dispatch(self, inbound: Inbound) -> None:
        reply = await self.on_message(inbound)
        if reply:
            ok = await self.send(inbound, reply)
            _log = logging.getLogger("fatfish.channel")
            if ok:
                _log.info("[%s] 已回复 chat=%s len=%d text=%r", self.name, inbound.chat_id[:12], len(reply), reply[:40])
            else:
                _log.warning("[%s] 回复发送失败 chat=%s", self.name, inbound.chat_id)