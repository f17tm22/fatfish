"""按日志把丢掉的群聊天记录补回 history（日志里有入站文本与回复文本，够还原对话）。

用法：
  python3 scripts/backfill_history.py --dry-run          # 只看能补多少
  python3 scripts/backfill_history.py --apply            # 真写库（先自动备份 sqlite）
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fatfish import names as nm  # noqa: E402
from fatfish import settings  # noqa: E402
from fatfish.store import Store  # noqa: E402

IN_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO fatfish\.qq: QQ 入站 "
    r"event=(?P<ev>GROUP\w*|C2C\w*) chat=(?P<chat>\w+) user=(?P<user>\w+) text='(?P<text>.*)'$"
)
OUT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO fatfish\.channel: \[qq\] 已回复 "
    r"chat=(?P<chat>\w+) len=\d+ text='(?P<text>.*)'$"
)


def parse(log_path: Path, since: float, until: float, chat_prefix: str) -> list:
    rows = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = IN_RE.match(line)
        role, who = "user", ""
        if not m:
            m = OUT_RE.match(line)
            role, who = "assistant", ""
        if not m:
            continue
        ts = time.mktime(time.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"))
        if not (since <= ts <= until):
            continue
        if not m.group("chat").startswith(chat_prefix):
            continue
        rows.append({"ts": ts, "role": role, "suffix": m.groupdict().get("user") or "", "text": m.group("text")})
    rows.sort(key=lambda r: r["ts"])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(settings.LOG_DIR / "fatfish.log"))
    ap.add_argument("--chat", default="F8EA69A5", help="群 chat_id 前缀")
    ap.add_argument("--since", default="2026-10-27 16:00")
    ap.add_argument("--until", default="2026-10-27 21:30")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    since = time.mktime(time.strptime(args.since, "%Y-%m-%d %H:%M"))
    until = time.mktime(time.strptime(args.until, "%Y-%m-%d %H:%M"))
    rows = parse(Path(args.log), since, until, args.chat)
    if not rows:
        print("日志里没有可补的记录")
        return 0

    store = Store()
    cfg = settings.load_config()
    db_chat = None
    con = sqlite3.connect(settings.DB_PATH)
    row = con.execute("SELECT chat_id FROM history WHERE chat_id LIKE ? LIMIT 1", (args.chat + "%",)).fetchone()
    db_chat = row[0] if row else args.chat
    con.close()

    existing = {(r["role"], r["text"]) for r in store.history_recent(db_chat, 10**6)}
    todo = []
    for r in rows:
        if (r["role"], r["text"]) in existing:
            continue
        existing.add((r["role"], r["text"]))
        name = (
            _name_for_suffix(store, cfg, r["suffix"])
            if r["role"] == "user"
            else cfg.get("persona", {}).get("self_name", "大肥鱼")
        )
        todo.append({**r, "name": name, "chat": db_chat})

    print(f"日志命中 {len(rows)} 条，其中 {len(todo)} 条库里没有")
    for r in todo[:10]:
        print(f"  {time.strftime('%H:%M:%S', time.localtime(r['ts']))} {r['name']}：{r['text'][:30]}")
    if not args.apply:
        print("\n（dry-run，没有写库；加 --apply 才真写）")
        return 0

    bak = Path(str(settings.DB_PATH) + f".bak-backfill-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(settings.DB_PATH, bak)
    for r in todo:
        store.history_add(r["chat"], r["role"], r["name"], r["text"], ts=r["ts"])
    print(f"已补 {len(todo)} 条 → {db_chat[:12]}…（备份 {bak.name}）")
    return 0


def _name_for_suffix(store: Store, cfg: dict, suffix: str) -> str:
    """日志只有 openid 后 6 位：依次查 config 名字表、预挂名字、见过的 openid。"""
    if not suffix:
        return "群友"
    suffix = suffix.upper()
    for uid, fixed in (names_config(cfg)).items():
        if uid.upper().endswith(suffix):
            return fixed
    for n in (len(suffix), 6, 4):
        if len(suffix) >= n:
            pending = str(store.kv_get(f"pendname:{suffix[-n:]}", "") or "").strip()
            if pending:
                return pending
    return nm.resolve(store, cfg, _full_uid(store, suffix) or f"GROUPFRIEND{suffix}")


def names_config(cfg: dict) -> dict:
    from fatfish import names as _n

    return _n.config_map(cfg)


def _full_uid(store: Store, suffix: str) -> str:
    """日志只有 openid 后 6 位，去 seen: 里找完整 openid；找不到就用假 uid（只影响取名）。"""
    if not suffix:
        return ""
    for key, _val in store.kv_prefix("seen:").items():
        uid = key[len("seen:"):]
        if uid.endswith(suffix):
            return uid
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
