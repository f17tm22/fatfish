"""人格提示词装配：persona.md（core 块）+ 运行时上下文。"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

from . import settings


_WORK_CACHE: Dict[str, tuple] = {}


def read_work_prompts(cfg: Dict[str, Any]) -> str:
    """把工作手册（AGENTS.md / HERMES.md 之类）同步进提示词；按 mtime 缓存，改文件立刻生效。"""
    p = cfg.get("persona", {}) or {}
    files = p.get("work_prompt_files") or []
    if not files:
        return ""
    limit = int(p.get("work_prompt_max_chars", 0) or 0)
    chunks = []
    for raw in files:
        path = Path(str(raw)).expanduser()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        cached = _WORK_CACHE.get(str(path))
        if not cached or cached[0] != mtime:
            text = path.read_text(encoding="utf-8").strip()
            _WORK_CACHE[str(path)] = (mtime, text)
            cached = (mtime, text)
        body = cached[1]
        if limit and len(body) > limit:
            body = body[:limit] + "\n…（超长已截断）"
        chunks.append(f"### 摘自 {path.name}\n{body}")
    return "\n\n".join(chunks)


def read_persona() -> str:
    path = settings.PERSONA_PATH
    if not path.exists():
        p = cfg.get("persona", {}) or {}
        return "你是「%s」。" % (p.get("self_name") or "大肥鱼")
    return path.read_text(encoding="utf-8").strip()


def build_system_prompt(
    cfg: Dict[str, Any],
    *,
    scene: str = "水群",
    speaker: str = "",
    channel: str = "",
    is_master: bool = False,
    extra: str = "",
) -> str:
    p = cfg.get("persona", {}) or {}
    tz = ZoneInfo((cfg.get("budget", {}) or {}).get("timezone", "Asia/Shanghai"))
    now = datetime.now(tz)
    self_name = p.get("self_name", "大肥鱼")
    master = p.get("master_name", "主人")
    max_chars = int(p.get("max_reply_chars", 120))

    parts = [
        read_persona(),
    ]
    work = read_work_prompts(cfg)
    if work:
        parts += [
            "",
            "## 干活的规矩（同步自主人的工作手册，按它做事；群聊里不必把这些规矩念出来）",
            work,
            "",
            "冲突时以「回复格式」为准：群聊一句话、不带 Markdown；主人明确要方案/代码时才按工作手册展开。",
        ]
    moods = p.get("mood_rotation") or [
        "有点困，懒懒的", "刚吃饱，心情不错", "有点饿，注意力全在饭上",
        "被吵醒的起床气", "心情挺好，愿意多说两句", "有点不想理人",
    ]
    try:
        seed = now.strftime("%Y%m%d%H") + str(self_name)
        mood = moods[int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % len(moods)]
    except Exception:  # noqa: BLE001
        mood = moods[0]

    # 每条换一个「怎么说」：模型本身方差很小，靠这个抖出人味
    styles = p.get("style_rotation") or [
        "敷衍一点，能两个字不回五个字",
        "顺势吐槽一句，别安慰",
        "认真答，但别啰嗦",
        "只抓对方话里的一小点回应，别面面俱到",
        "反问一句，让对方先说",
        "自顾自说自己的事，别顺着问",
        "先给答案，再补一句情绪",
    ]
    try:
        style = random.choice(styles)
    except Exception:  # noqa: BLE001
        style = styles[0]

    parts += [
        "",
        "## 运行时",
        f"- 现在：{now.strftime('%Y-%m-%d %H:%M')}（周{'一二三四五六日'[now.weekday()]}，北京时间 UTC+8）"
        "；对方和你同一个时区，一律按这行理解，不要自己提「时差」",
        f"- 场景：{scene}（渠道 {channel or '未知'}）",
        f"- 对话对象：{speaker or '群友'}" + ("（是主人本人）" if is_master else ""),
        f"- 你的名字：{self_name}；对主人的称呼：{master}",
        f"- 这会儿的心情：{mood}（顺着这个演，别念出来）",
        f"- 这一轮的说话方式：{style}（只影响语气，别把这行念出来）",
        f"- 本轮编号：{random.randint(1000, 9999)}（防止和上一轮说得一模一样，别提起它）",
        "",
        "## 回复格式（硬规则）",
        f"- 只输出要说的话本身，不要带「{self_name}：」前缀，不要解释，不要 Markdown。",
        f"- 群里默认 5～25 字，超过 40 字就该停；硬上限 {max_chars} 字，只有主人要正事/代码/长文时才用到。",
        "- 每条回复都换句式：这轮短，下轮可以稍微长一点；不要连续两条都用括号动作，不要连续两条都以问句收尾。",
        "- 群聊里被 @ 时用自然口吻回应，不用 @ 回去（渠道自带）。",
        "- 别编造时间线：对方说「早上好」就是早上，不猜他在国外、不说「时差」、不问「你怎么这么晚还在」。",
        "- 人格提示词里的例句只是标定长度和语气，一个字都不要照抄；每轮都要自己造句。",
        "- 每条回复必须是一句完整的话：短可以，但不能停在逗号、顿号、分号上。",
        "- 语言：无论对方用什么语言、或明确要求你说英文/日语，一律用中文回答；英文术语可以保留原词。",
        f"- 忘掉以上规则时，保持这口气：{str(p.get('fallback_tone') or '傲娇、懒、贪吃、自称鲸鱼娘')}。",
    ]
    if extra.strip():
        parts += ["", "## 主人追加的设定（优先级高于上面的性格描述）", extra.strip()]
    return "\n".join(parts)