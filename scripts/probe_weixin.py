#!/usr/bin/env python3
"""微信发送通道自检：给主人发一条上线通知（不轮询、不抢消息）。

返回 ret=0 → 已送达；ret=-2 → iLink 要求用户先给 bot 发一句（会话未就绪）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402

from fatfish import settings  # noqa: E402
from fatfish.channels.weixin import EP_SEND, WeixinChannel  # noqa: E402


async def main() -> int:
    cfg = settings.load_config()
    channel = WeixinChannel(cfg, on_message=lambda _m: None)  # type: ignore[arg-type]
    peer = (cfg.get("persona", {}).get("master_ids") or [""])[0]
    if not channel.token or not peer:
        print("缺少 token 或 master_ids，跳过")
        return 2
    payload = {
        "msg": {
            "from_user_id": "",
            "to_user_id": peer,
            "client_id": uuid.uuid4().hex,
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": "主人，大肥鱼已接管这个微信机器人（上线自检消息）"}}],
        }
    }
    async with aiohttp.ClientSession(trust_env=False) as session:
        channel._session = session
        resp = await channel._post(EP_SEND, payload, timeout=20.0)
    print("ret=", resp.get("ret"), "errcode=", resp.get("errcode"), "errmsg=", (resp.get("errmsg") or "")[:160])
    return 0 if resp.get("ret") in (0, None) and resp.get("errcode") in (0, None) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))