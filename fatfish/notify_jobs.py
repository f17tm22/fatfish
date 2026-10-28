"""定时通知任务：按 cron 表达式跑一条命令，把 stdout 原样推到微信（0 token，不经过模型）。

设计目标：接替 hermes 里 `deliver: weixin` 的 cron 任务（纳指哨兵、opencode 监控），
让这些推送走大肥鱼这条通道，避免 hermes 没有微信平台后静默失败。

配置文件：`notify_jobs.yaml`
    jobs:
      - name: nasdaq
        schedule: "30 9 * * *"
        command: "python3 ~/.hermes/scripts/nasdaq-drawdown-sentinel.py"
        enabled: false
        timeout: 300

行为约定：
- stdout 为空 = 静默，不推送（与原 cron 的 no_agent 语义一致）。
- 退出码非 0 时把 stderr 尾部一起推出去，别让故障静默。
- 同一分钟内不会重复触发（按 `notify:<name>:last` 记分钟）。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shlex
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import yaml

from . import settings

logger = logging.getLogger("fatfish.notify")

JOBS_PATH = settings.ROOT / "notify_jobs.yaml"

_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 6))


def _parse_field(spec: str, low: int, high: int) -> set[int]:
    """解析单个 cron 字段，支持 `*`、`*/n`、`a`、`a-b`、`a-b/n`、逗号列表。"""
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("空的 cron 字段")
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            step = int(raw_step)
            if step <= 0:
                raise ValueError(f"步长必须为正: {spec}")
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start > end:
            start, end = end, start
        if start < low or end > high:
            raise ValueError(f"字段超出范围 {low}-{high}: {spec}")
        result.update(range(start, end + 1, step))
    if not result:
        raise ValueError(f"无法解析 cron 字段: {spec}")
    return result


def parse_cron(expr: str) -> List[set[int]]:
    fields = (expr or "").split()
    if len(fields) != 5:
        raise ValueError(f"cron 表达式需要 5 段，收到 {len(fields)}: {expr!r}")
    return [_parse_field(f, low, high) for f, (_, low, high) in zip(fields, _FIELDS)]


def cron_match(expr: str, when: Optional[dt.datetime] = None) -> bool:
    when = when or dt.datetime.now()
    minute, hour, day, month, weekday = parse_cron(expr)
    cron_dow = (when.weekday() + 1) % 7  # Python 周一=0 → cron 周日=0
    return (
        when.minute in minute
        and when.hour in hour
        and when.day in day
        and when.month in month
        and cron_dow in weekday
    )


def load_jobs(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = path or JOBS_PATH
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return []
    out: List[Dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "").strip()
        schedule = str(job.get("schedule") or "").strip()
        command = str(job.get("command") or "").strip()
        if not (name and schedule and command):
            logger.warning("通知任务缺少 name/schedule/command，已跳过: %r", job)
            continue
        try:
            parse_cron(schedule)
        except ValueError as exc:
            logger.warning("通知任务 %s 的 cron 非法（%s），已跳过", name, exc)
            continue
        out.append(
            {
                "name": name,
                "schedule": schedule,
                "command": command,
                "enabled": bool(job.get("enabled", True)),
                "timeout": float(job.get("timeout", 300) or 300),
            }
        )
    return out


async def run_command(job: Dict[str, Any]) -> tuple[int, str, str]:
    argv = shlex.split(job["command"])
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=job["timeout"])
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"命令超时（{job['timeout']:.0f}s）"
    return proc.returncode or 0, out_b.decode("utf-8", "replace"), err_b.decode("utf-8", "replace")


def compose_text(code: int, out: str, err: str) -> str:
    """决定要不要推、推什么。空 stdout 且成功 → 返回空串表示静默。"""
    body = out.strip()
    if code != 0:
        tail = "\n".join((err or out).strip().splitlines()[-6:])
        head = f"[{code}] " if code > 0 else "超时/失败 "
        return (head + tail).strip()[:1200]
    return body[:1200]


class NotifyScheduler:
    """每 20 秒扫一次时钟，命中 cron 就跑命令并推送。"""

    def __init__(
        self,
        send: Callable[[str], Awaitable[bool]],
        *,
        store=None,
        path: Optional[Path] = None,
        tick_seconds: float = 20.0,
    ) -> None:
        self._send = send
        self._store = store
        self._path = path or JOBS_PATH
        self._tick = tick_seconds
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("通知任务调度已启动（%s）", self._path)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("通知任务调度异常: %s", exc)
            await asyncio.sleep(self._tick)

    async def tick(self, when: Optional[dt.datetime] = None) -> List[str]:
        """返回本轮触发的任务名，便于测试。"""
        when = when or dt.datetime.now()
        slot = when.strftime("%Y-%m-%d %H:%M")
        fired: List[str] = []
        for job in load_jobs(self._path):
            if not job["enabled"]:
                continue
            try:
                if not cron_match(job["schedule"], when):
                    continue
            except ValueError:
                continue
            key = f"notify:{job['name']}:last"
            if self._store is not None and self._store.kv_get(key) == slot:
                continue
            if self._store is not None:
                self._store.kv_set(key, slot)
            fired.append(job["name"])
            asyncio.create_task(self.fire(job))
        return fired

    async def fire(self, job: Dict[str, Any]) -> bool:
        code, out, err = await run_command(job)
        text = compose_text(code, out, err)
        if not text:
            logger.info("通知任务 %s 无输出，静默", job["name"])
            return True
        ok = await self._send(text)
        logger.info("通知任务 %s 推送%s len=%d", job["name"], "成功" if ok else "失败", len(text))
        return ok