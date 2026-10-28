"""好感度：喂饭/签到/猜中/被夸加分，被骂与刷屏扣分；等级给称号与一点点彩蛋。

全部走 kv 表，0 token，不新增 schema：
  aff:{chat_id}:{uid}        累计分数
  affn:{chat_id}:{uid}       互动次数（含扣分事件，方便调参台看活跃度）
  afflast:{chat_id}:{uid}    最近一次变动时间戳
  affday:{chat_id}:{uid}:{天} 当天已获得的正分（防刷）
"""

from __future__ import annotations

import time
from typing import Dict, List, Tuple

# (门槛分, 称号, 一句话彩蛋说明) —— 只增不减，改表要同步 docs
LEVELS: List[Tuple[int, str, str]] = [
    (0, "陌生", "本鱼连你叫什么都记不住"),
    (10, "眼熟", "有点印象，大概是喂过饭的那位"),
    (30, "熟人", "会主动搭理你，抽签容易转好一点"),
    (70, "朋友", "签到多赏一碗饭，尾巴给你晃两下"),
    (150, "自己人", "抽签更容易转好，本鱼会多看你两眼"),
    (300, "本鱼的菜", "签到一次三碗，还能叫本鱼「小肥」"),
]

# 事件 → 分值。正分受每日上限约束，负分不设限（下限 -100）
GAIN: Dict[str, int] = {
    "checkin": 2,
    "feed": 1,          # 每碗
    "fortune_good": 2,  # 抽到大吉/中吉
    "guess_win": 3,
    "pet": 1,
    "praise": 2,
    "scold": -2,
    "fat_name": -1,     # 叫胖鱼/死鱼
    "spam": -1,         # 触发限流/刷屏
}

DAILY_GAIN_CAP = 10     # 每天最多涨 10 分，防刷
MIN_SCORE = -100

_DELTA_TEXT = {
    "checkin": "签到",
    "feed": "喂饭",
    "fortune_good": "好运",
    "guess_win": "猜中",
    "pet": "贴贴",
    "praise": "夸本鱼",
    "scold": "凶本鱼",
    "fat_name": "叫胖鱼",
    "spam": "刷屏",
}


# 群里查规则就用这段（一行一句，QQ 群消息不会太长）
RULES_TEXT = (
    "好感规则：签到+2；喂饭+1/碗；抽到大吉中吉+2；猜数字猜中+3；贴贴+1；夸本鱼+2。"
    "凶本鱼-2；叫胖鱼/死鱼-1；好感没到 300 还叫「小肥」-1。每天最多涨 10 分。\n"
    "等级：0 陌生 → 10 眼熟 → 30 熟人（抽签差签 34% 抬一档）→ 70 朋友（签到 2 碗）"
    "→ 150 自己人（抬档概率 50%）→ 300 本鱼的菜（签到 3 碗、可叫小肥）。\n"
    "查自己：说「好感度」；看排行：说「好感榜」。"
)


def score_key(chat_id: str, uid: str) -> str:
    return f"aff:{chat_id}:{uid}"


def count_key(chat_id: str, uid: str) -> str:
    return f"affn:{chat_id}:{uid}"


def last_key(chat_id: str, uid: str) -> str:
    return f"afflast:{chat_id}:{uid}"


def day_key(chat_id: str, uid: str, day: str) -> str:
    return f"affday:{chat_id}:{uid}:{day}"


def level_for(score: int) -> Tuple[int, str, str]:
    """返回 (等级序号 0..N, 称号, 彩蛋说明)。"""
    idx = 0
    for i, (need, _name, _note) in enumerate(LEVELS):
        if score >= need:
            idx = i
    return idx, LEVELS[idx][1], LEVELS[idx][2]


def next_gap(score: int) -> Tuple[int, str]:
    """还差多少分到下一级；已满级返回 (0, "")。"""
    idx, _name, _note = level_for(score)
    if idx + 1 >= len(LEVELS):
        return 0, ""
    need = LEVELS[idx + 1][0]
    return max(0, need - int(score)), LEVELS[idx + 1][1]


def score_of(store, chat_id: str, uid: str) -> int:
    try:
        return int(store.kv_get(score_key(chat_id, uid), "0") or 0)
    except (TypeError, ValueError):
        return 0


def apply_event(store, chat_id: str, uid: str, event: str, day: str,
                times: int = 1, now: float | None = None) -> Dict[str, object]:
    """记一次好感事件。返回 before/after/actual/level 等，供回复里带一句。"""
    base = GAIN.get(event, 0) * (times or 1)
    before = score_of(store, chat_id, uid)
    actual = base
    if base > 0:
        used_key = day_key(chat_id, uid, day)
        try:
            used = int(store.kv_get(used_key, "0") or 0)
        except (TypeError, ValueError):
            used = 0
        room = max(0, DAILY_GAIN_CAP - used)
        actual = min(base, room)
        if actual:
            store.kv_set(used_key, str(used + actual))
    after = max(MIN_SCORE, before + actual)
    store.kv_set(score_key(chat_id, uid), str(after))
    try:
        count = int(store.kv_get(count_key(chat_id, uid), "0") or 0)
    except (TypeError, ValueError):
        count = 0
    store.kv_set(count_key(chat_id, uid), str(count + 1))
    store.kv_set(last_key(chat_id, uid), str(int(now or time.time())))
    idx, name, note = level_for(after)
    gap, next_name = next_gap(after)
    return {
        "event": event,
        "label": _DELTA_TEXT.get(event, event),
        "delta": actual,
        "capped": actual != base,
        "before": before,
        "after": after,
        "level": idx,
        "name": name,
        "note": note,
        "gap": gap,
        "next_name": next_name,
        "count": count + 1,
    }


def suffix(result: Dict[str, object]) -> str:
    """回复尾巴：`（好感 +2 → 眼熟 12，还差 18 分到熟人）`；没加分就不加尾巴。"""
    delta = int(result.get("delta") or 0)
    if not delta:
        if result.get("capped"):
            return "（今天好感涨满啦，明天再来）"
        return ""
    sign = "+" if delta > 0 else ""
    tail = f"（好感 {sign}{delta} → {result['name']} {result['after']}"
    if result.get("gap"):
        tail += f"，还差 {result['gap']} 分到{result['next_name']}"
    return tail + "）"


def board(store, chat_id: str, limit: int = 5) -> List[Dict[str, object]]:
    prefix = f"aff:{chat_id}:"
    rows = store.kv_prefix(prefix)
    out = []
    for key, val in rows.items():
        uid = key[len(prefix):]
        try:
            score = int(val)
        except (TypeError, ValueError):
            continue
        _idx, name, _note = level_for(score)
        out.append({"uid": uid, "score": score, "level": name})
    out.sort(key=lambda r: -r["score"])
    return out[:limit]


def rank_text(rows: List[Dict[str, object]], name_of=None) -> str:
    if not rows:
        return "还没有人对本鱼有好感，喂碗饭试试。"
    bits = [
        f"{i + 1}. {(name_of(r['uid']) if callable(name_of) else '群友' + str(r['uid'])[-4:])} "
        f"{r['score']}（{r['level']}）"
        for i, r in enumerate(rows)
    ]
    return "好感榜：" + "；".join(bits)
