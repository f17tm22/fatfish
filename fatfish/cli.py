"""命令行：run / start / stop / status / usage / say / test-llm / persona。"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import settings


def _read_pid() -> Optional[int]:
    try:
        if settings.PID_PATH.exists():
            return int(settings.PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001
        return None
    return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_run(_: argparse.Namespace) -> int:
    from .runner import main

    return main()


def _preflight_channels() -> list[str]:
    """启动前的硬性检查：开着 QQ 就必须有 app_id + secret，否则直接说清楚怎么补。"""
    cfg = settings.load_config()
    problems = []
    ch = cfg.get("channels", {}) or {}
    qq = ch.get("qq", {}) or {}
    if qq.get("enabled"):
        app_id = str(qq.get("app_id") or settings.get("QQ_APP_ID") or "").strip()
        secret = str(settings.get("QQ_CLIENT_SECRET") or "").strip()
        if not app_id:
            problems.append("QQ 通道缺少 app_id（config.yaml channels.qq.app_id）")
        if not secret:
            problems.append(
                "QQ 通道缺少 AppSecret：python3 -m fatfish.cli set-qq "
                f"--app-id {app_id or '<AppID>'} --secret <AppSecret>"
            )
    wx = ch.get("weixin", {}) or {}
    if wx.get("enabled") and not settings.get("WEIXIN_TOKEN"):
        problems.append("微信通道缺少 WEIXIN_TOKEN")
    return problems


def cmd_start(_: argparse.Namespace) -> int:
    problems = _preflight_channels()
    if problems:
        print("启动前检查未通过：")
        for p in problems:
            print(f"  - {p}")
        return 2
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"已在运行 pid={pid}")
        return 0
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = settings.LOG_DIR / "fatfish.out"
    # profile 模式下 ROOT 不是仓库根，子进程要靠 PYTHONPATH 找到 fatfish 包
    pkg_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["FATFISH_ROOT"] = str(settings.ROOT)
    with open(out_path, "ab") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "fatfish.cli", "run"],
            cwd=str(settings.ROOT),
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    settings.PID_PATH.write_text(str(proc.pid), encoding="utf-8")
    time.sleep(1.5)
    if proc.poll() is not None:
        print(f"启动失败，退出码 {proc.returncode}，看 {out_path}")
        return 1
    print(f"已启动 pid={proc.pid}，日志 {settings.LOG_DIR / 'fatfish.log'}")
    return 0


def cmd_stop(_: argparse.Namespace) -> int:
    pid = _read_pid()
    if not pid or not _alive(pid):
        print("未在运行")
        settings.PID_PATH.unlink(missing_ok=True)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.5)
        if not _alive(pid):
            print(f"已停止 pid={pid}")
            return 0
    print(f"未在 10s 内退出，强制结束 pid={pid}")
    os.kill(pid, signal.SIGKILL)
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid and _alive(pid):
        uptime = ""
        try:
            out = subprocess.run(
                ["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True, timeout=5
            ).stdout.strip()
            uptime = f" 运行 {out}" if out else ""
        except Exception:  # noqa: BLE001
            pass
        print(f"运行中 pid={pid}{uptime}")
    else:
        print("未运行")
    from .llm import LLM
    from .store import Store

    cfg = settings.load_config()
    store = Store()
    llm = LLM(cfg, store)
    print(f"今日花费 ¥{llm.spent_today():.4f} / 预算 ¥{llm.cap():.2f}")
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    from .store import Store, day_key
    from .llm import LLM

    cfg = settings.load_config()
    store = Store()
    llm = LLM(cfg, store)
    print(f"今日（{day_key()}）¥{llm.spent_today():.4f} / ¥{llm.cap():.2f}")
    print(f"{'日期':<12}{'调用':>6}{'花费(元)':>10}{'输出tokens':>12}")
    for row in store.daily_totals(args.days):
        print(f"{row['day']:<12}{row['calls']:>6}{(row['cost'] or 0):>10.4f}{int(row['out_tokens'] or 0):>12}")
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    from .brain import Brain
    from .channels.base import Inbound
    from .llm import LLM
    from .store import Store

    cfg = settings.load_config()
    store = Store()
    llm = LLM(cfg, store)
    brain = Brain(cfg, store, llm)
    inbound = Inbound(
        channel="local",
        chat_id=args.chat or "local",
        chat_type=args.chat_type or "dm",
        user_id=args.user or "local",
        user_name=args.name or "主人",
        text=args.text,
        mentioned=True,
    )
    before = llm.spent_today()
    reply = asyncio.run(brain.handle(inbound))
    after = llm.spent_today()
    print(reply or "(无回复)")
    if after > before:
        print(f"[本次花费 ¥{after - before:.4f}，今日累计 ¥{after:.4f}/{llm.cap():.2f}]")
    return 0


def cmd_test_llm(_: argparse.Namespace) -> int:
    from .brain import Brain
    from .channels.base import Inbound
    from .llm import LLM
    from .store import Store

    cfg = settings.load_config()
    store = Store()
    llm = LLM(cfg, store)
    brain = Brain(cfg, store, llm)
    inbound = Inbound(
        channel="test", chat_id="test", chat_type="dm", user_id="test", user_name="主人",
        text="在吗？用一句话跟我打个招呼", mentioned=True,
    )
    reply = asyncio.run(brain.handle(inbound))
    print(reply or "(无回复)")
    print(f"今日累计 ¥{llm.spent_today():.4f} / ¥{llm.cap():.2f}")
    return 0


QQ_CODE_HINTS = {
    11293: "机器人不在该群（未被添加，或已退群）",
    11255: "群不存在或已注销（openid 无效）",
    11254: "机器人没有该群的操作权限",
    11244: "主动消息额度用尽（群机器人每月有上限）",
    11001: "不支持的调用（该接口对此类机器人未开放）",
    304023: "消息已过期，无法被动回复（5 分钟窗口）",
}


def explain_qq_code(code: int) -> str:
    """把 QQ 开放平台错误码翻成一句人话，便于排障。"""
    return QQ_CODE_HINTS.get(int(code), "未知错误")


def cmd_master(args: argparse.Namespace) -> int:
    """查看/增删主人 openid（换 QQ 应用后 openid 会变，需要重新认主）。"""
    from . import settings as _settings

    cfg = settings.load_config()
    ids = list((cfg.get("persona", {}) or {}).get("master_ids") or [])
    if args.action == "list" or not args.action:
        for i in ids:
            print(i)
        return 0
    if not args.master_id:
        print("需要给一个 openid；从日志里找 `QQ 入站 ... user=XXXX`")
        return 2
    if args.action == "add":
        changed = _settings.add_master_id(_settings.CONFIG_PATH, args.master_id)
        print("已加入主人名单" if changed else "已在名单里，无需重复添加")
    elif args.action == "remove":
        removed = _settings.remove_master_id(_settings.CONFIG_PATH, args.master_id)
        print("已移除" if removed else "名单里没有这个 openid")
    return 0


def cmd_set_qq(args: argparse.Namespace) -> int:
    """换 QQ 机器人应用：写 .env（600）与 config.yaml 的 app_id，不打印密钥。"""
    from . import settings as _settings

    app_id = str(args.app_id).strip()
    secret = str(args.secret).strip()
    if not app_id.isdigit() or not secret:
        print("用法：set-qq --app-id <数字AppID> --secret <AppSecret>")
        return 2
    env_path = _settings.set_env_value("QQ_APP_ID", app_id)
    _settings.set_env_value("QQ_CLIENT_SECRET", secret)
    _settings.set_qq_app_id(_settings.CONFIG_PATH, app_id)
    print(f"已写入 {env_path}（600）与 config.yaml，AppID {app_id}，密钥长度 {len(secret)}")
    print("下一步：./run.sh stop && ./run.sh start，然后在群里 @ 一次。")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    """打印最近 QQ 事件（可选持续跟随），调试群消息用。"""
    import time
    from pathlib import Path as _P

    log_path = _P(settings.DATA_DIR).parent / "logs" / "fatfish.log"
    if not log_path.exists():
        print(f"没有日志文件：{log_path}")
        return 1
    keys = ("QQ 事件", "QQ 群变更", "QQ 入站", "已回复", "QQ 发送")

    def hits(lines):
        return [ln.rstrip() for ln in lines if any(k in ln for k in keys)]

    recent = hits(log_path.read_text(errors="ignore").splitlines()[-args.tail:])
    for ln in recent:
        print(ln)
    if not args.follow:
        return 0

    print("--- 跟随中（Ctrl-C 退出）---")
    pos = log_path.stat().st_size
    try:
        while True:
            time.sleep(0.5)
            size = log_path.stat().st_size
            if size <= pos:
                pos = min(pos, size)
                continue
            with log_path.open(errors="ignore") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
            for ln in hits(chunk.splitlines()):
                print(ln, flush=True)
    except KeyboardInterrupt:
        return 0


def cmd_groups(args: argparse.Namespace) -> int:
    """列出记录过的群 openid 与最近事件。"""
    import json

    from .channels.qq import GROUPS_PATH

    data = {}
    if GROUPS_PATH.exists():
        try:
            data = json.loads(GROUPS_PATH.read_text()) or {}
        except Exception:  # noqa: BLE001
            data = {}
    if not data:
        print("还没有记录到任何群。等一条 GROUP_ADD_ROBOT / 群 @ 会自动记进来。")
        return 0
    for gid, meta in data.items():
        print(f"{gid}  {meta.get('event', '?'):<22} {meta.get('at', '')}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """用一条主动消息探测机器人是否在该群（群里会真的收到这条消息）。"""
    import aiohttp

    cfg = settings.load_config()
    app_id = cfg.get("channels", {}).get("qq", {}).get("app_id") or settings.get("QQ_APP_ID")
    secret = settings.get("QQ_CLIENT_SECRET")
    gid = args.group
    text = args.text

    async def _run() -> int:
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.post(
                "https://bots.qq.com/app/getAppAccessToken",
                json={"appId": app_id, "clientSecret": secret},
            ) as resp:
                token = (await resp.json()).get("access_token") or ""
            if not token:
                print("token 获取失败")
                return 2
            async with session.post(
                f"https://api.sgroup.qq.com/v2/groups/{gid}/messages",
                headers={"Authorization": f"QQBot {token}", "Content-Type": "application/json"},
                json={"content": text, "msg_type": 0},
            ) as r:
                body = await r.text()
                try:
                    import json as _json

                    code = int((_json.loads(body) or {}).get("code") or 0)
                except Exception:  # noqa: BLE001
                    code = 0
                if r.status < 400 and code == 0:
                    print(f"OK：消息已发进群 {gid}（机器人确实在群里）")
                    return 0
                print(f"HTTP {r.status} code={code}：{explain_qq_code(code)}")
                print(body[:300])
                return 1

    return asyncio.run(_run())


def cmd_diagnose(args: argparse.Namespace) -> int:
    """QQ 群通道排障：凭证、身份、intents、日志里的事件分布。"""
    import json
    import re

    import aiohttp

    from .channels.qq import INTENTS

    cfg = settings.load_config()
    app_id = cfg.get("channels", {}).get("qq", {}).get("app_id") or settings.get("QQ_APP_ID")
    secret = settings.get("QQ_CLIENT_SECRET")
    conflicts = settings.app_id_conflicts(str(app_id))

    async def _probe() -> None:
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.post(
                "https://bots.qq.com/app/getAppAccessToken",
                json={"appId": app_id, "clientSecret": secret},
            ) as resp:
                data = await resp.json()
            token = data.get("access_token") or ""
            print(f"凭证：HTTP {resp.status}，token {'有效' if token else '缺失'}，有效期 {data.get('expires_in')}s")
            for path in ("/users/@me", "/gateway"):
                async with session.get(
                    f"https://api.sgroup.qq.com{path}", headers={"Authorization": f"QQBot {token}"}
                ) as r:
                    body = await r.text()
                    if path == "/users/@me":
                        try:
                            j = json.loads(body)
                            print(f"身份：{j.get('username')} id={str(j.get('id'))[:12]}…")
                        except Exception:  # noqa: BLE001
                            print("身份：", body[:120])
                    else:
                        print(f"网关：HTTP {r.status} {body[:80]}")
                await asyncio.sleep(0)

    if conflicts:
        print("⚠ 同一 AppID 还有别人在用（会双回/抢 WS）：")
        for c in conflicts:
            print(f"  - {c}")
    asyncio.run(_probe())
    bits = [f"1<<{i}" for i in range(32) if INTENTS & (1 << i)]
    print(f"intents={INTENTS}（{', '.join(bits)}）")

    groups_path = settings.DATA_DIR / "qq" / "groups.json"
    if groups_path.exists():
        try:
            groups = json.loads(groups_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            groups = {}
        if groups:
            print(f"已知群机器人接入 {len(groups)} 个：")
            for gid, meta in groups.items():
                print(f"  {gid}  {meta.get('event')}  {meta.get('at')}")

    log = settings.LOG_DIR / "fatfish.log"
    if log.exists():
        text = log.read_text(encoding="utf-8", errors="replace")[-400_000:]
        # 只看本次启动之后的日志：换过 AppID 时旧应用的事件会把诊断带偏
        boot = text.rfind("fatfish: 大肥鱼启动")
        if boot > 0:
            text = text[boot:]
        ready = re.findall(r"QQ READY session=(\w+)", text)
        print(f"最近一次 QQ READY：{ready[-1] if ready else '无'}")
        counts: Dict[str, int] = {}
        for m in re.findall(r"QQ 事件 t=(\w+)", text):
            counts[m] = counts.get(m, 0) + 1
        for m in re.findall(r"QQ 入站 event=(\w+)", text):
            counts[m] = counts.get(m, 0) + 1
        print("日志里的事件分布：", counts or "（空）")
        added = counts.get("GROUP_ADD_ROBOT", 0) > 0
        got_at = counts.get("GROUP_AT_MESSAGE_CREATE", 0) > 0
        got_full = counts.get("GROUP_MESSAGE_CREATE", 0) > 0
        if not (added or got_at or got_full):
            print("换过 AppID 的话先注意：沙箱配置是「按应用」的，新应用要重新配一遍。")
            print("  0) 开发设置 → 消息推送方式必须是 WebSocket（选 Webhook 会推到你填的网址，不推网关）；")
            print("  0b) 开发设置 → 事件订阅 勾上「群消息/@机器人」相关事件；")
        if got_full or got_at:
            print("诊断：本次启动已收到群消息事件，群通道是通的。")
        elif added:
            print("提示：收到了加群事件但没收到群消息 → 平台没把消息推过来。")
            print("  1) 开发者后台 → 开发设置 → 沙箱配置：沙箱成员填你自己的 QQ 号、沙箱群填群号，两个都要加；")
            print("  2) 消息推送方式必须是 WebSocket；事件订阅勾上群消息/群 @ 机器人。")
        else:
            print("提示：从未收到任何 GROUP_* 事件 → 群通道在平台侧未开通。")
            print("  1) 群里请用「群设置 → 群机器人 → 添加机器人」加入，而不是普通邀请；")
            print("  2) 开发者后台 → 开发设置 → 沙箱配置 → 沙箱群 里加上该群；")
            print("  3) 机器人能力/场景里勾选 QQ 群。")
        if not (got_at or got_full) and added:
            print("诊断：已收到加群事件（机器人确实在群里），但一条群 @ 都没收到。")
            print("  机器人未上线时，只有「沙箱群」里的 @ 才会推送 →")
            print("  1) q.qq.com → 开发设置 → 沙箱配置：沙箱成员填你自己的 QQ 号、沙箱群填群号，两个都要加；")
            print("  2) 或把机器人提审发布上线，正式群才不受沙箱限制；")
            print("  3) 群里 @ 时必须选中机器人本体（@机器人），不是打「大肥鱼」三个字；")
            print("  4) 若机器人早已退群（可用 `groups` + `probe <openid>` 确认），重新加一次群。")
        elif got_full:
            print("诊断：收到的是全量群消息（GROUP_MESSAGE_CREATE），说明机器人开了「接收所有消息」，群通道是通的。")
    return 0


def push_master(text: str) -> tuple[bool, str]:
    """给主人微信推一条消息（cron/脚本可调用；会话需主人近期和 bot 说过话）。"""
    import json
    import uuid

    import aiohttp

    from .channels.weixin import CTX_PATH, EP_SEND, WeixinChannel

    cfg = settings.load_config()
    master_ids = cfg.get("persona", {}).get("master_ids") or [""]
    peer = next((m for m in master_ids if str(m).endswith("@im.wechat")), master_ids[0])
    if not peer:
        return False, "未配置 persona.master_ids，无法推送"
    channel = WeixinChannel(cfg, on_message=lambda _m: None)  # type: ignore[arg-type]
    ctx = ""
    try:
        if CTX_PATH.exists():
            ctx = str((json.loads(CTX_PATH.read_text(encoding="utf-8")) or {}).get(peer) or "")
    except Exception:  # noqa: BLE001
        ctx = ""
    payload = {
        "msg": {
            "from_user_id": "",
            "to_user_id": peer,
            "client_id": uuid.uuid4().hex,
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
    }
    if ctx:
        payload["msg"]["context_token"] = ctx

    async def _send() -> dict:
        async with aiohttp.ClientSession(trust_env=False) as session:
            channel._session = session
            return await channel._post(EP_SEND, payload, timeout=20.0)

    resp = asyncio.run(_send())
    ok = resp.get("ret") in (0, None) and resp.get("errcode") in (0, None)
    return ok, "" if ok else f"ret={resp.get('ret')} errmsg={resp.get('errmsg')}"


def cmd_notify(args: argparse.Namespace) -> int:
    ok, err = push_master(args.text)
    print("已推送" if ok else f"推送失败 {err}")
    return 0 if ok else 1


def cmd_cron(args: argparse.Namespace) -> int:
    """定时通知任务：列表 / 手动跑一条。"""
    import datetime as dt

    from .notify_jobs import JOBS_PATH, load_jobs, compose_text, cron_match, run_command

    jobs = load_jobs()
    if args.action == "list":
        if not jobs:
            print(f"没有配置任务（{JOBS_PATH}）")
            return 0
        now = dt.datetime.now()
        for job in jobs:
            state = "启用" if job["enabled"] else "停用"
            hit = "命中本分钟" if cron_match(job["schedule"], now) else ""
            print(f"{job['name']:18s} {job['schedule']:16s} {state}  {hit}  {job['command'][:60]}")
        return 0

    name = (args.name or "").strip()
    job = next((j for j in jobs if j["name"] == name), None)
    if job is None:
        print(f"没有名为 {name} 的任务；现有：{', '.join(j['name'] for j in jobs) or '无'}")
        return 2
    code, out, err = asyncio.run(run_command(job))
    text = compose_text(code, out, err)
    print(f"退出码 {code}，输出 {len(out)}B" + ("，静默（无内容不推送）" if not text else ""))
    if text:
        print("---- 将推送 ----")
        print(text)
    if args.send and text:
        ok, perr = push_master(text)
        print("已推送" if ok else f"推送失败 {perr}")
        return 0 if ok else 1
    return 0


def cmd_persona(_: argparse.Namespace) -> int:
    from . import persona

    cfg = settings.load_config()
    print(persona.build_system_prompt(cfg, scene="私聊", speaker="主人", channel="cli", is_master=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fatfish", description="大肥鱼 · 水群机器人")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="前台运行").set_defaults(func=cmd_run)
    sub.add_parser("start", help="后台启动").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="停止后台进程").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="进程与今日花费").set_defaults(func=cmd_status)
    p_usage = sub.add_parser("usage", help="近 N 天花费")
    p_usage.add_argument("--days", type=int, default=14)
    p_usage.set_defaults(func=cmd_usage)
    p_say = sub.add_parser("say", help="本地试一句（走完整流程）")
    p_say.add_argument("text")
    p_say.add_argument("--chat", default="local")
    p_say.add_argument("--chat-type", default="dm", choices=["dm", "group"])
    p_say.add_argument("--user", default="local")
    p_say.add_argument("--name", default="主人")
    p_say.set_defaults(func=cmd_say)
    sub.add_parser("test-llm", help="测试模型连通与花费").set_defaults(func=cmd_test_llm)
    sub.add_parser("persona", help="打印系统提示词").set_defaults(func=cmd_persona)
    sub.add_parser("diagnose", help="QQ 群通道排障").set_defaults(func=cmd_diagnose)
    sub.add_parser("groups", help="列出记录过的 QQ 群").set_defaults(func=cmd_groups)
    p_master = sub.add_parser("master", help="主人名单 list/add/remove")
    p_master.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove"])
    p_master.add_argument("master_id", nargs="?", default="")
    p_master.set_defaults(func=cmd_master)
    p_setqq = sub.add_parser("set-qq", help="换 QQ 机器人应用（写 .env 与 config）")
    p_setqq.add_argument("--app-id", required=True)
    p_setqq.add_argument("--secret", required=True)
    p_setqq.set_defaults(func=cmd_set_qq)
    p_events = sub.add_parser("events", help="最近的 QQ 事件，可跟随")
    p_events.add_argument("--tail", type=int, default=3000, help="从日志末尾回看多少行")
    p_events.add_argument("--follow", action="store_true", help="持续跟随新事件")
    p_events.set_defaults(func=cmd_events)
    p_probe = sub.add_parser("probe", help="主动消息探测机器人是否在某群")
    p_probe.add_argument("group", help="group_openid")
    p_probe.add_argument("--text", default="（大肥鱼调试）本鱼在这群里吗？")
    p_probe.set_defaults(func=cmd_probe)
    p_cron = sub.add_parser("cron", help="定时通知任务（list / run）")
    p_cron.add_argument("action", choices=["list", "run"])
    p_cron.add_argument("name", nargs="?", default="")
    p_cron.add_argument("--send", action="store_true", help="真的推送到微信")
    p_cron.set_defaults(func=cmd_cron)
    p_notify = sub.add_parser("notify", help="给主人微信推一条消息")
    p_notify.add_argument("text")
    p_notify.set_defaults(func=cmd_notify)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())