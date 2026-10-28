"""SQLite 账本：用量/花费、KV（sync_buf、context_token）、会话状态、短历史。"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import settings

TZ = ZoneInfo("Asia/Shanghai")


def day_key(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), TZ).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path: Optional[str] = None):
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = str(path or settings.DB_PATH)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self) -> None:
        with self._lock, self._conn:
            c = self._conn
            c.execute(
                """CREATE TABLE IF NOT EXISTS usage(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL, day TEXT NOT NULL,
                    channel TEXT DEFAULT '', chat_id TEXT DEFAULT '', model TEXT DEFAULT '',
                    prompt_tokens INTEGER DEFAULT 0, cache_hit INTEGER DEFAULT 0,
                    cache_miss INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
                    cost_cny REAL DEFAULT 0, ok INTEGER DEFAULT 1, note TEXT DEFAULT ''
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_usage_day ON usage(day)")
            c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
            c.execute(
                """CREATE TABLE IF NOT EXISTS chat_state(
                    chat_id TEXT PRIMARY KEY, last_reply_ts REAL DEFAULT 0,
                    last_msg_ts REAL DEFAULT 0, replies INTEGER DEFAULT 0, day TEXT DEFAULT ''
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS history(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
                    ts REAL NOT NULL, role TEXT NOT NULL, name TEXT DEFAULT '', text TEXT NOT NULL
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_hist_chat ON history(chat_id, id)")

    # ── KV ──
    def kv_get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def kv_delete(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM kv WHERE k=?", (key,))

    def kv_set(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, str(value)),
            )

    def kv_prefix(self, prefix: str) -> Dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT k, v FROM kv WHERE k LIKE ? ESCAPE '\\'",
                (prefix.replace("%", "\\%").replace("_", "\\_") + "%",),
            ).fetchall()
        return {r["k"]: r["v"] for r in rows}

    def history_all(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chat_id, role, name, text, ts FROM history ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 用量 ──
    def record_usage(
        self,
        *,
        channel: str,
        chat_id: str,
        model: str,
        prompt_tokens: int,
        cache_hit: int,
        cache_miss: int,
        completion_tokens: int,
        cost_cny: float,
        ok: bool = True,
        note: str = "",
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO usage(ts, day, channel, chat_id, model, prompt_tokens, cache_hit,
                       cache_miss, completion_tokens, cost_cny, ok, note)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(),
                    day_key(),
                    channel,
                    chat_id,
                    model,
                    int(prompt_tokens),
                    int(cache_hit),
                    int(cache_miss),
                    int(completion_tokens),
                    float(cost_cny),
                    1 if ok else 0,
                    note,
                ),
            )

    def spend_day(self, day: Optional[str] = None) -> float:
        day = day or day_key()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_cny), 0) AS s FROM usage WHERE day=?", (day,)
            ).fetchone()
        return float(row["s"] or 0.0)

    def usage_day(self, day: Optional[str] = None) -> List[Dict[str, Any]]:
        day = day or day_key()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM usage WHERE day=? ORDER BY id DESC LIMIT 200", (day,)
            ).fetchall()
        return [dict(r) for r in rows]

    def daily_totals(self, days: int = 14) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT day, COUNT(*) AS calls, SUM(cost_cny) AS cost,
                          SUM(completion_tokens) AS out_tokens
                   FROM usage GROUP BY day ORDER BY day DESC LIMIT ?""",
                (int(days),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 会话状态 ──
    def chat_state(self, chat_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM chat_state WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return dict(row) if row else {"chat_id": chat_id, "last_reply_ts": 0.0, "last_msg_ts": 0.0, "replies": 0, "day": ""}

    def touch_chat(self, chat_id: str, *, replied: bool) -> Dict[str, Any]:
        now = time.time()
        day = day_key()
        st = self.chat_state(chat_id)
        replies = st["replies"] if st.get("day") == day else 0
        if replied:
            replies += 1
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO chat_state(chat_id, last_reply_ts, last_msg_ts, replies, day)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     last_reply_ts=excluded.last_reply_ts,
                     last_msg_ts=excluded.last_msg_ts, replies=excluded.replies, day=excluded.day""",
                (chat_id, now if replied else st["last_reply_ts"], now, replies, day),
            )
        st.update({"last_reply_ts": now if replied else st["last_reply_ts"], "last_msg_ts": now, "replies": replies, "day": day})
        return st

    # ── 历史 ──
    def history_chats(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT chat_id, COUNT(*) AS msgs, MAX(ts) AS last_ts
                   FROM history GROUP BY chat_id ORDER BY last_ts DESC LIMIT ?""",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_history(self, chat_id: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM history WHERE chat_id=?", (chat_id,))
        return cur.rowcount

    HISTORY_KEEP = 800  # 每个会话保留多少条：以前写死 40，群里聊两小时就把「五点到八点」挤掉了

    def history_add(self, chat_id: str, role: str, name: str, text: str, ts: float | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO history(chat_id, ts, role, name, text) VALUES(?,?,?,?,?)",
                (chat_id, float(ts or time.time()), role, name, text),
            )
            self._conn.execute(
                "DELETE FROM history WHERE chat_id=? AND id NOT IN "
                "(SELECT id FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?)",
                (chat_id, chat_id, int(self.HISTORY_KEEP)),
            )

    def history_recent(self, chat_id: str, limit: int = 8) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, name, text FROM history WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, int(limit)),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]