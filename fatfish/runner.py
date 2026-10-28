"""进程入口：装配日志/账本/大脑/渠道，热读 config.yaml，优雅退出。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from logging.handlers import RotatingFileHandler

from . import settings
from .brain import Brain
from .channels import build_channels
from .channels.base import Inbound
from .llm import LLM
from .store import day_key
from .notify_jobs import NotifyScheduler
from .store import Store

logger = logging.getLogger("fatfish")


def setup_logging(level: str = "INFO") -> None:
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("fatfish")
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(
        settings.LOG_DIR / "fatfish.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


async def run() -> int:
    cfg = settings.load_config()
    setup_logging((cfg.get("logging", {}) or {}).get("level", "INFO"))
    store = Store()
    llm = LLM(cfg, store)
    brain = Brain(cfg, store, llm)
    channels = build_channels(cfg, brain.handle, store)
    if not channels:
        logger.error("没有启用的渠道，退出（检查 config.yaml 的 channels.*.enabled）")
        return 1

    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings.PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    logger.info("大肥鱼启动 pid=%s 渠道=%s", os.getpid(), ",".join(c.name for c in channels))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    tasks = [asyncio.create_task(ch.start(), name=f"start-{ch.name}") for ch in channels]
    last_mtime = settings.config_mtime()

    rt_cfg = cfg.get("runtime", {}) or {}
    hb_seconds = float(rt_cfg.get("heartbeat_seconds", 600) or 600)

    warned_spend = {"day": "", "warned": False}

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(hb_seconds)
            try:
                spent, cap = llm.spent_today(), llm.cap()
                logger.info(
                    "心跳 今日花费 ¥%.4f/¥%.2f 渠道=%s", spent, cap, ",".join(c.name for c in channels),
                )
                # 预算页的「花费预警线」原来只写进配置没人读，这里让它真的响一次/天
                warn_line = float((cfg.get("budget", {}) or {}).get("low_balance_warn_cny", 0) or 0)
                day = day_key()
                if warned_spend["day"] != day:
                    warned_spend["day"], warned_spend["warned"] = day, False
                if warn_line > 0 and spent >= warn_line and not warned_spend["warned"]:
                    warned_spend["warned"] = True
                    logger.warning("花费预警：今日已花 ¥%.4f，超过预警线 ¥%.2f（上限 ¥%.2f）",
                                   spent, warn_line, cap)
            except Exception:  # noqa: BLE001
                pass
            # 这 10 分钟的收发与"跳过原因"，专治"它怎么不回话"
            try:
                qq = next((c for c in channels if c.name == "qq"), None)
                st = getattr(qq, "stats", None)
                if st:
                    logger.info(
                        "近 %.0f 分钟 qq 收到 群 %d 私聊 %d，回 %d；跳过原因：没叫 %d / 同句重复 %d / 冷却 %d / 限流 %d / 空回复兜底 %d",
                        hb_seconds / 60, st.get("group", 0), st.get("c2c", 0), st.get("reply", 0),
                        st.get("skip_notcalled", 0), st.get("skip_dup", 0),
                        getattr(brain, "skipped_cooldown", 0), getattr(brain, "skipped_rate", 0),
                        getattr(brain, "empty_fallbacks", 0),
                    )
                    for k in st:
                        st[k] = 0
                    brain.skipped_cooldown = brain.skipped_rate = brain.empty_fallbacks = 0
            except Exception:  # noqa: BLE001
                pass

    def _dump_effective(*_args) -> None:
        """kill -USR1 <pid>：把当前进程真实在用的参数打到日志，用来验证 GUI 改动是否生效。"""
        b = brain
        p_ = b.cfg.get("persona", {}) or {}
        l_ = b.cfg.get("llm", {}) or {}
        q_ = (b.cfg.get("channels", {}) or {}).get("qq", {}) or {}
        _th = l_.get("thinking")
        _th_on = _th.get("enabled") if isinstance(_th, dict) else _th
        logger.info(
            "生效参数 max_reply_chars=%s 群冷却=%s 私聊冷却=%s 上下文=%s 模型=%s 思考=%s 预算=%.2f 接话=%s 概率=%s 调试转私聊=%s",
            p_.get("max_reply_chars"), p_.get("cooldown_seconds"), p_.get("dm_cooldown_seconds"),
            p_.get("context_messages"), l_.get("model"), _th_on,
            float((b.cfg.get("budget", {}) or {}).get("daily_cap_cny", 0) or 0),
            (q_.get("chime") or {}).get("enabled"), (q_.get("chime") or {}).get("random_prob"),
            (b.cfg.get("features", {}) or {}).get("debug_to_dm"),
        )
        try:
            import json as _json

            eff = {
                "persona.self_name": p_.get("self_name"),
                "persona.master_name": p_.get("master_name"),
                "persona.max_reply_chars": p_.get("max_reply_chars"),
                "persona.cooldown_seconds": p_.get("cooldown_seconds"),
                "persona.dm_cooldown_seconds": p_.get("dm_cooldown_seconds"),
                "persona.context_messages": p_.get("context_messages"),
                "persona.max_replies_per_minute": p_.get("max_replies_per_minute"),
                "persona.interrupt_prob": p_.get("interrupt_prob"),
                "llm.model": l_.get("model"),
                "llm.thinking": _th_on,
                "llm.reasoning_effort": l_.get("reasoning_effort"),
                "llm.max_tokens": l_.get("max_tokens"),
                "llm.temperature": l_.get("temperature"),
                "llm.timeout_seconds": l_.get("timeout_seconds"),
                "budget.daily_cap_cny": (b.cfg.get("budget", {}) or {}).get("daily_cap_cny"),
                "budget.reserve_cny": (b.cfg.get("budget", {}) or {}).get("reserve_cny"),
                "triggers.keywords": (b.cfg.get("triggers", {}) or {}).get("keywords"),
                "triggers.reply_in_dm": (b.cfg.get("triggers", {}) or {}).get("reply_in_dm"),
                "triggers.reply_on_mention": (b.cfg.get("triggers", {}) or {}).get("reply_on_mention"),
                "triggers.group_keyword_reply": (b.cfg.get("triggers", {}) or {}).get("group_keyword_reply"),
                **{
                    f"features.{k}": (b.cfg.get("features", {}) or {}).get(k)
                    for k in ("fortune", "feed", "pet", "repeat", "balance", "help", "auto_reply")
                },
                "channels.qq.group_policy": q_.get("group_policy"),
                "channels.qq.allowed_groups": q_.get("allowed_groups"),
                "channels.qq.chime.enabled": (q_.get("chime") or {}).get("enabled"),
                "channels.qq.chime.random_prob": (q_.get("chime") or {}).get("random_prob"),
                "channels.qq.proactive.enabled": (q_.get("proactive") or {}).get("enabled"),
                "runtime.same_text_window_seconds": (b.cfg.get("runtime", {}) or {}).get("same_text_window_seconds"),
                "runtime.msg_dedup_seconds": (b.cfg.get("runtime", {}) or {}).get("msg_dedup_seconds"),
                "runtime.zombie_silent_seconds": (b.cfg.get("runtime", {}) or {}).get("zombie_silent_seconds"),
                "runtime.heartbeat_seconds": (b.cfg.get("runtime", {}) or {}).get("heartbeat_seconds"),
            }
            # 渠道实例上的真实值（热更新的落点），比配置文件更可信
            for ch in channels:
                if ch.name == "qq":
                    eff["channels.qq.group_policy"] = getattr(ch, "group_policy", eff["channels.qq.group_policy"])
                    eff["channels.qq.chime.random_prob"] = getattr(ch, "chime_prob", None)
                    eff["channels.qq.chime.enabled"] = getattr(ch, "chime_enabled", None)
                    eff["channels.qq.proactive.enabled"] = getattr(ch, "proactive_enabled", None)
                    eff["runtime.same_text_window_seconds"] = getattr(ch, "same_text_window", None)
            logger.info("生效参数JSON %s", _json.dumps(eff, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            pass

    try:
        import signal as _signal

        asyncio.get_running_loop().add_signal_handler(_signal.SIGUSR1, _dump_effective)
    except (NotImplementedError, RuntimeError, ValueError):
        pass

    hb_task = asyncio.create_task(heartbeat(), name="heartbeat")

    weixin = next((c for c in channels if c.name == "weixin"), None)
    master_ids = list(((cfg.get("persona") or {}).get("master_ids") or []))
    master = next((m for m in master_ids if str(m).endswith("@im.wechat")), master_ids[0] if master_ids else "")

    async def notify_master(text: str) -> bool:
        """定时任务推送：直接走微信通道发给主人，0 token。"""
        if weixin is None or not master:
            logger.warning("通知任务无法推送：微信渠道未启用或未配置 master_ids")
            return False
        inbound = Inbound(
            channel="weixin",
            chat_id=master,
            chat_type="dm",
            user_id=master,
            user_name="定时任务",
            text=text,
            message_id="",
            mentioned=True,
            reply_ctx={"to": master},
        )
        return await weixin.send(inbound, text)

    scheduler = NotifyScheduler(notify_master, store=store)
    try:
        await scheduler.start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("通知任务调度启动失败: %s", exc)
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            mtime = settings.config_mtime()
            if mtime != last_mtime:
                last_mtime = mtime
                try:
                    new_cfg = settings.load_config()
                    brain.reload(new_cfg)
                    reloaded = []
                    for ch in channels:
                        fn = getattr(ch, "reload", None)
                        if callable(fn):
                            fn(new_cfg)
                            reloaded.append(ch.name)
                    logger.info("配置已热更新（模型/预算/触发/人格 + 渠道:%s）", ",".join(reloaded) or "无")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("配置热更新失败: %s", exc)
            for task in tasks:
                if task.done() and task.exception():
                    logger.error("渠道启动异常: %s", task.exception())
    finally:
        for ch in channels:
            try:
                await ch.stop()
            except Exception:  # noqa: BLE001
                pass
        hb_task.cancel()
        await scheduler.stop()
        for task in tasks:
            task.cancel()
        try:
            settings.PID_PATH.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        logger.info("大肥鱼已退出")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())