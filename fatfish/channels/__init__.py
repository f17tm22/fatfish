"""渠道工厂：按 config.yaml 装配已启用的渠道。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from .base import Channel, Handler, Inbound

logger = logging.getLogger("fatfish.channels")


def build_channels(cfg: Dict[str, Any], on_message: Handler, store: Any = None) -> List[Channel]:
    channels_cfg = cfg.get("channels", {}) or {}
    built: List[Channel] = []

    if (channels_cfg.get("console") or {}).get("enabled"):
        from .console import ConsoleChannel

        built.append(ConsoleChannel(cfg, on_message, store))
        logger.info("已启用渠道: console")

    if (channels_cfg.get("weixin") or {}).get("enabled", True):
        try:
            from .weixin import WeixinChannel

            built.append(WeixinChannel(cfg, on_message, store))
            logger.info("已启用渠道: weixin")
        except Exception as exc:  # noqa: BLE001
            logger.error("微信渠道初始化失败: %s", exc)

    if (channels_cfg.get("qq") or {}).get("enabled", True):
        try:
            from .qq import QQChannel

            built.append(QQChannel(cfg, on_message, store))
            logger.info("已启用渠道: qq")
        except Exception as exc:  # noqa: BLE001
            logger.error("QQ 渠道初始化失败: %s", exc)

    return built


__all__ = ["Channel", "Inbound", "Handler", "build_channels"]