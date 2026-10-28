"""本地知识卡：按关键词命中的小抄，命中才注入，省 token 又不用训练。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Tuple

from . import settings

logger = logging.getLogger(__name__)

_CACHE: Dict[str, Tuple[float, List[Tuple[List[str], str]]]] = {}


def _dirs() -> List[Path]:
    """先看当前档案的 knowledge/，再看仓库根的 knowledge/（公共知识）。"""
    out = [settings.ROOT / "knowledge"]
    repo = Path(__file__).resolve().parent.parent / "knowledge"
    if repo != out[0]:
        out.append(repo)
    return out


def parse(text: str) -> List[Tuple[List[str], str]]:
    """把 markdown 切成 (关键词表, 正文)。约定：

    ## 标题
    别名: A、B、C
    正文若干行
    """
    entries: List[Tuple[List[str], str]] = []
    title, aliases, body = None, [], []

    def flush() -> None:
        nonlocal title, aliases, body
        if title:
            keys = [title] + aliases
            content = " ".join(x.strip() for x in body if x.strip()).strip()
            if content:
                entries.append(([k.strip().lower() for k in keys if k and k.strip()], f"{title}：{content}"))
        title, aliases, body = None, [], []

    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            flush()
            title = line[3:].strip()
        elif line.lower().startswith(("别名:", "别名：")):
            sep = ":" if ":" in line else "："
            aliases = [x.strip() for x in line.split(sep, 1)[1].replace("、", ",").split(",") if x.strip()]
        elif title is not None:
            body.append(line)
    flush()
    return entries


def load() -> List[Tuple[List[str], str]]:
    """读所有 knowledge/*.md，按 mtime 缓存。"""
    merged: List[Tuple[List[str], str]] = []
    for d in _dirs():
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md")):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            key = str(path)
            if key not in _CACHE or _CACHE[key][0] != mtime:
                try:
                    _CACHE[key] = (mtime, parse(path.read_text(encoding="utf-8")))
                except OSError:
                    continue
            merged += _CACHE[key][1]
    return merged


def lookup(cfg: dict, text: str, limit: int | None = None) -> str:
    """按命中数量排序取前 N 条，拼成一段可直接塞进 system 的小抄。"""
    kn = (cfg or {}).get("knowledge") or {}
    if not kn.get("enabled", True):
        return ""
    low = (text or "").lower()
    if not low:
        return ""
    top = int(limit if limit is not None else kn.get("max_entries", 2) or 2)
    hits = [(sum(1 for k in keys if k in low), content) for keys, content in load()]
    hits = [h for h in hits if h[0] > 0]
    if not hits:
        return ""
    hits.sort(key=lambda x: -x[0])
    budget = int(kn.get("max_chars", 400) or 400)
    out: List[str] = []
    for _, content in hits[:top]:
        if len("\n".join(out + [content])) > budget:
            break
        out.append(content)
    return "\n".join(out)
