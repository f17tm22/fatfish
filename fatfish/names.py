"""群成员固定称呼。

QQ 群事件只给 member_openid，没有昵称。优先级：
  1) config 的 `channels.qq.member_names`（手动写死，最稳）
  2) sqlite kv 里的 `nick:{openid}`（本人说「我是 akane」自报，或调参台里改）
  3) 兜底 `群友{openid 后 4 位}`
"""

from __future__ import annotations

from typing import Any, Dict, Optional

FALLBACK_PREFIX = "群友"
NICK_PREFIX = "nick:"


def config_map(cfg: Dict[str, Any]) -> Dict[str, str]:
    q = ((cfg.get("channels") or {}).get("qq") or {})
    raw = q.get("member_names") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if str(v).strip()}


def nick_key(uid: str) -> str:
    return f"{NICK_PREFIX}{uid}"


def self_named(store, uid: str) -> str:
    try:
        return str(store.kv_get(nick_key(uid), "") or "").strip()
    except Exception:  # noqa: BLE001 - 昵称坏了不能拖垮回复
        return ""


def resolve(store, cfg: Dict[str, Any], uid: str) -> str:
    uid = str(uid or "")
    if not uid:
        return FALLBACK_PREFIX
    fixed = config_map(cfg).get(uid)
    if fixed:
        return fixed
    named = self_named(store, uid)
    if named:
        return named
    return f"{FALLBACK_PREFIX}{uid[-4:]}"


def owner_of(store, cfg: Dict[str, Any], name: str) -> Optional[str]:
    """这个名字已经被谁占了（config 优先，再看 kv）。用来防冒名。"""
    name = (name or "").strip()
    if not name:
        return None
    for uid, fixed in config_map(cfg).items():
        if fixed == name:
            return uid
    try:
        rows = store.kv_prefix(NICK_PREFIX)
    except Exception:  # noqa: BLE001
        return None
    for key, val in rows.items():
        if str(val).strip() == name:
            return key[len(NICK_PREFIX):]
    return None


PENDING_PREFIX = "pendname:"   # 主人说「6289 是小羊」但那个 openid 还没见过 → 先挂着，等他说话自动认领


def set_pending(store, suffix: str, name: str) -> None:
    store.kv_set(f"{PENDING_PREFIX}{suffix.strip().upper()}", (name or "").strip()[:12])


def pending_for(store, uid: str) -> str:
    """按 openid 后 6 位/后 4 位找挂着待认领的名字。"""
    uid = str(uid or "").upper()
    for n in (6, 4):
        if len(uid) < n:
            continue
        try:
            got = str(store.kv_get(f"{PENDING_PREFIX}{uid[-n:]}", "") or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if got:
            return got
    return ""


def clear_pending(store, uid: str) -> None:
    uid = str(uid or "").upper()
    for n in (6, 4):
        if len(uid) >= n:
            try:
                store.kv_delete(f"{PENDING_PREFIX}{uid[-n:]}")
            except Exception:  # noqa: BLE001
                pass


def bind(store, uid: str, name: str) -> None:
    store.kv_set(nick_key(uid), (name or "").strip()[:12])
