"""控制台渠道：本地调试用，stdin 一行 = 一条消息。"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Dict

from .base import Channel, Handler, Inbound


class ConsoleChannel(Channel):
    name = "console"

    def __init__(self, cfg: Dict[str, Any], on_message: Handler):
        super().__init__(cfg, on_message)
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while self._running:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                await asyncio.sleep(0.5)
                continue
            text = line.strip()
            if not text:
                continue
            inbound = Inbound(
                channel="console",
                chat_id="console",
                chat_type="dm",
                user_id="local",
                user_name="主人",
                text=text,
                message_id="",
            )
            await self._dispatch(inbound)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def send(self, inbound: Inbound, text: str) -> bool:
        print(f"大肥鱼> {text}")
        return True