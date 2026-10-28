"""表情包：本地文件夹 + 公网镜像 URL。

QQ 群发图（v2 `msg_type: 7`）要求先把图片「上传富媒体」，而上传只吃公网 URL、
腾讯服务器自己去拉——所以每张图除了本地文件，还必须有一个公网可达地址（manifest 里记）。
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import settings

logger = logging.getLogger(__name__)

_LAST_SENT: Dict[str, float] = {}


def _cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return (cfg.get("stickers") or {}) if isinstance(cfg, dict) else {}


def _root(cfg: Dict[str, Any]) -> Path:
    rel = str(_cfg(cfg).get("dir") or "stickers")
    p = Path(rel)
    return p if p.is_absolute() else settings.ROOT / p


def load(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """读 manifest.json；每条至少要 file 或 url。"""
    path = _root(cfg) / "manifest.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("表情包 manifest 读不动：%s", exc)
        return []
    items = data.get("items") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "").strip()
        if not url:
            continue
        out.append({
            "file": str(it.get("file") or ""),
            "url": url,
            "desc": str(it.get("desc") or ""),
            "tags": [str(t) for t in (it.get("tags") or [])],
            "weight": float(it.get("weight") or 1),
        })
    return out


# 回复里的情绪词 → 标签；两边对上才允许发（不知道内容就不发）
_MOODS = {
    "开心": ("开心", "高兴", "好耶", "不错", "棒"),
    "笑": ("哈哈", "笑", "好笑", "绷不住", "乐"),
    "得意": ("哼", "当然", "本鱼厉害", "聪明", "牛", "稳"),
    "夸": ("厉害", "优秀", "真好", "爱你", "棒"),
    "委屈": ("委屈", "难过", "呜呜", "被凶"),
    "哭": ("哭", "呜呜", "落泪"),
    "生气": ("生气", "气死", "烦"),
    "无语": ("无语", "离谱", "什么鬼", "绷"),
    "吐槽": ("吐槽", "离谱", "这就", "绝了"),
    "嘴硬": ("才不", "就不", "不理你", "无所谓", "怎么啦"),
    "摆烂": ("摆烂", "不想动", "随便", "躺平"),
    "拒绝": ("不给", "不说", "不告诉", "别问"),
    "道歉": ("对不起", "抱歉", "我错了", "对不起啦"),
    "慌": ("糟了", "糟糕", "完了", "救命", "慌张"),
    "黏人": ("陪我", "别走", "在呢", "一起"),
    "讨饭": ("喂饭", "米饭", "投喂", "饿", "要饭", "加钱"),
    "吃": ("米饭", "吃", "饭", "干饭"),
    "累": ("累", "困", "加班", "打工人", "没力气"),
    "调皮": ("嘿嘿", "骗你", "逗你", "才不告诉你"),
    "被吐槽": ("胖", "吃白饭", "没用", "笨蛋"),
}


def moods_in(text: str) -> List[str]:
    """从回复文本里读出情绪标签（可多个）。"""
    body = text or ""
    return [m for m, words in _MOODS.items() if any(w in body for w in words)]


def pick(cfg: Dict[str, Any], text: str = "", chat_id: str = "") -> Optional[Dict[str, Any]]:
    """只在「表情包内容已知且跟当前情绪对得上」时才挑；对不上宁可不发。"""
    c = _cfg(cfg)
    if not c.get("enabled"):
        return None
    items = load(cfg)
    if not items:
        return None
    gap = float(c.get("min_seconds", 180) or 0)
    last = _LAST_SENT.get(chat_id or "-", 0.0)
    if gap and time.time() - last < gap:
        return None
    moods = moods_in(text)
    if not moods:
        if c.get("require_tag", True):
            return None
        pool = [i for i in items if i["tags"]]
    else:
        pool = [i for i in items if any(t in i["tags"] for t in moods)]
    if not pool:
        return None
    weights = [max(0.1, i["weight"]) for i in pool]
    chosen = random.choices(pool, weights=weights, k=1)[0]
    logger.info("表情包选中 %s（%s）情绪=%s", chosen["file"], chosen["desc"], ",".join(moods))
    _LAST_SENT[chat_id or "-"] = time.time()  # 占冷却位；发送失败也不连发
    return chosen


def mark_sent(chat_id: str) -> None:
    _LAST_SENT[chat_id or "-"] = time.time()
