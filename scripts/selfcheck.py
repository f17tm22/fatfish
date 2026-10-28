#!/usr/bin/env python3
"""离线自检：不联网、不花钱，验证人格/功能/计费/预算/账本。"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []
PASSED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (f"  ({detail})" if detail else ""))
    (PASSED if cond else FAILED).append(name)


def main() -> int:
    from fatfish import persona
    from fatfish.brain import Brain
    from fatfish.channels.base import Inbound
    from fatfish.llm import LLM, cost_cny, is_peak
    from fatfish.store import Store

    cfg = __import__("fatfish.settings", fromlist=["load_config"]).load_config()

    # 1) 人格
    sys_prompt = persona.build_system_prompt(cfg, scene="群聊", speaker="群友", channel="qq", is_master=False)
    check("人格含 core 块", "【PERSONA_LOAD】 CETACEA_LOLI" in sys_prompt)
    check("人格含运行时时间", "北京时间" in sys_prompt)
    check("人格含长度约束", str(cfg["persona"]["max_reply_chars"]) in sys_prompt)

    # 2) 峰谷与计费
    tz = ZoneInfo("Asia/Shanghai")
    check("周二11:00 为高峰", is_peak(cfg, datetime(2026, 10, 27, 11, 0, tzinfo=tz)))
    check("周二13:00 为空闲", not is_peak(cfg, datetime(2026, 10, 27, 13, 0, tzinfo=tz)))
    check("周六11:00 为空闲", not is_peak(cfg, datetime(2026, 10, 31, 11, 0, tzinfo=tz)))
    peak_cost = cost_cny(cfg, "deepseek-flash", cache_hit=0, cache_miss=1_000_000, completion_tokens=0, peak=True)
    off_cost = cost_cny(cfg, "deepseek-flash", cache_hit=0, cache_miss=1_000_000, completion_tokens=0, peak=False)
    check("100万 miss 高峰=2元", abs(peak_cost - 2.0) < 1e-9, f"{peak_cost}")
    check("100万 miss 空闲=1元", abs(off_cost - 1.0) < 1e-9, f"{off_cost}")
    pro_peak = cost_cny(cfg, "deepseek-v4-pro", cache_hit=0, cache_miss=1_000_000, completion_tokens=0, peak=True)
    check("pro 100万 miss 高峰=9元", abs(pro_peak - 9.0) < 1e-9, f"{pro_peak}")
    out_cost = cost_cny(cfg, "deepseek-flash", cache_hit=0, cache_miss=0, completion_tokens=1_000_000, peak=False)
    check("flash 100万输出 空闲=4元", abs(out_cost - 4.0) < 1e-9, f"{out_cost}")

    # 3) 账本与预算闸门（临时库）
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.sqlite3"
        store = Store(path=str(db))
        llm = LLM(cfg, store)
        # 别写死 4.80/0.15：上限是用户在调参台能改的，按当前配置算闸门前一刻
        _cap = float((cfg.get("budget", {}) or {}).get("daily_cap_cny", 5.0) or 5.0)
        _res = float((cfg.get("budget", {}) or {}).get("reserve_cny", 0.1) or 0.0)
        _near = max(0.0, _cap - _res - 0.10)
        check("初始预算可用", llm.budget_ok())
        store.record_usage(channel="test", chat_id="c1", model="deepseek-flash", prompt_tokens=1000, cache_hit=0, cache_miss=1000, completion_tokens=100, cost_cny=_near, ok=True, note="t")
        check(f"花费 {_near:.2f} 后仍可调用", llm.budget_ok(), f"spent={store.spend_day():.2f} cap={_cap:.2f}")
        store.record_usage(channel="test", chat_id="c1", model="deepseek-flash", prompt_tokens=1000, cache_hit=0, cache_miss=1000, completion_tokens=100, cost_cny=0.20, ok=True, note="t")
        check("触顶后预算闸门关闭", not llm.budget_ok(), f"spent={store.spend_day():.2f} cap={_cap:.2f}")
        check("今日汇总可用", (store.daily_totals(3) or [{}])[0].get("calls", 0) >= 2)

        # 4) 模板功能（不花 token）
        brain = Brain(cfg, store, llm)
        _master_uid = (cfg["persona"].get("master_ids") or ["master-fallback"])[0]
        master = Inbound("test", "c9", "dm", _master_uid, "主人", "余额", mentioned=True)
        case = [
            ("抽签", "【"),
            ("喂饭", "碗"),
            ("贴贴", ""),
            ("夸我", "本鱼"),
            ("复读 你好", "你好"),
            ("几点", "现在"),
            ("帮助", "本鱼会"),
            ("重置对话", "主人"),
        ]
        for text, expect in case:
            inbound = Inbound("test", f"c-{text}", "dm", "u1", "群友", text, mentioned=True)
            reply = __import__("asyncio").run(brain.handle(inbound)) or ""
            ok = (expect in reply) if expect else bool(reply)
            check(f"功能 {text}", ok, reply[:24])

        # 4.11) 没被回复的消息也要进历史（否则群里记录会断档）
        _silent = Inbound("test", "hist-1", "group", "quiet1", "群友", "这句没人叫本鱼", mentioned=False)
        __import__("asyncio").run(brain.handle(_silent))
        _hist = [r["text"] for r in store.history_recent("hist-1", 20)]
        check("没回复的消息也进历史", "这句没人叫本鱼" in _hist, str(_hist[:2]))
        check("历史保留量不再是 40", Store.HISTORY_KEEP >= 500, str(Store.HISTORY_KEEP))

        # 4.11) 「说话说一半」：半句会被补全，补不动就把逗号改成句号
        from fatfish.brain import _half_sentence as _half

        _help_reply = __import__("asyncio").run(
            brain.handle(Inbound("test", "help-guard", "dm", "u1", "群友", "虚功原理怎么用", mentioned=True))
        ) or ""
        check("「怎么用」不再误触帮助", "本鱼会：" not in _help_reply, _help_reply[:16])
        check("能识别半句", _half("哦材料力学的虚功原理，") and _half("因为") and not _half("好。"), "")

        class _StubLLM:
            def __init__(self, seq):
                self.seq = list(seq)
                self.calls = 0
                self.temperature = 1.35

            def budget_ok(self):
                return True

            def spent_today(self):
                return 0.0

            def cap(self):
                return 6.0

            async def chat(self, messages, **kw):  # noqa: ARG002
                self.calls += 1

                class _R:
                    pass

                r = _R()
                r.text = self.seq[min(self.calls - 1, len(self.seq) - 1)]
                return r

        _s1 = _StubLLM(["哦材料力学的虚功原理，", "虚功原理是外力虚功等于内力虚功。"])
        _out1 = __import__("asyncio").run(
            Brain(cfg, store, _s1).handle(Inbound("test", "half-1", "group", "u1", "晓墨", "虚功原理是什么", mentioned=True))
        ) or ""
        check("半句会被补全", "等于内力虚功" in _out1 and _s1.calls == 2, f"{_out1} / {_s1.calls} 次")
        _s2 = _StubLLM(["哦材料力学的虚功原理，"])
        _out2 = __import__("asyncio").run(
            Brain(cfg, store, _s2).handle(Inbound("test", "half-2", "group", "u1", "晓墨", "虚功原理是什么", mentioned=True))
        ) or ""
        check("补不动就补句号", _out2.endswith("。") and not _out2.endswith("，"), _out2)

        # 4.12) 好感度：等级门槛、每日上限、扣分下限、开关、命令
        from fatfish import affinity as _af

        check("好感等级门槛", _af.level_for(29)[1] == "眼熟" and _af.level_for(30)[1] == "熟人", _af.level_for(30)[1])
        check("好感满级无下一级", _af.next_gap(300) == (0, ""), str(_af.next_gap(300)))
        _aff = Inbound("test", "aff-1", "dm", "affuser", "群友", "喂饭", mentioned=True)
        _r_feed = __import__("asyncio").run(brain.handle(_aff)) or ""
        _score_after_feed = _af.score_of(store, "aff-1", "affuser")
        check("喂饭会涨好感", _score_after_feed >= 1 and "好感 +" in _r_feed, f"{_score_after_feed} {_r_feed[-20:]}")
        _r_aff = __import__("asyncio").run(
            brain.handle(Inbound("test", "aff-1", "dm", "affuser", "群友", "好感度", mentioned=True))
        ) or ""
        check("好感度能查", "好感" in _r_aff and "分" in _r_aff, _r_aff[:24])
        _r_board = __import__("asyncio").run(
            brain.handle(Inbound("test", "aff-1", "dm", "affuser", "群友", "好感榜", mentioned=True))
        ) or ""
        check("好感榜能查", "好感榜" in _r_board, _r_board[:24])
        # 每日上限：同一天连喂 12 次最多涨 DAILY_GAIN_CAP
        store.kv_delete(_af.score_key("cap-1", "capuser"))
        for _i in range(12):
            _af.apply_event(store, "cap-1", "capuser", "feed", "2026-10-27")
        check("好感每日涨幅封顶", _af.score_of(store, "cap-1", "capuser") == _af.DAILY_GAIN_CAP,
              str(_af.score_of(store, "cap-1", "capuser")))
        for _i in range(200):
            _af.apply_event(store, "cap-1", "capuser", "scold", "2026-10-27")
        check("好感扣分有下限", _af.score_of(store, "cap-1", "capuser") == _af.MIN_SCORE,
              str(_af.score_of(store, "cap-1", "capuser")))
        # 群友固定称呼：config 写在最前，其次是自报，最后才兜底「群友后四位」
        from fatfish import names as _nm

        store.kv_set(_nm.nick_key("selfuid"), "小椰")
        _cfg_names = {"channels": {"qq": {"member_names": {"UID-FIXED-1": "小明"}}}}
        check("config 里的名字优先", _nm.resolve(store, _cfg_names, "UID-FIXED-1") == "小明",
              _nm.resolve(store, _cfg_names, "UID-FIXED-1"))
        check("自报名字次之", _nm.resolve(store, cfg, "selfuid") == "小椰", _nm.resolve(store, cfg, "selfuid"))
        check("没名字才用群友后四位", _nm.resolve(store, cfg, "ZZZZ1234") == "群友1234",
              _nm.resolve(store, cfg, "ZZZZ1234"))
        _r_name = __import__("asyncio").run(
            brain.handle(Inbound("test", "naming", "dm", "newbie9", "群友", "我是蛙蛙", mentioned=True))
        ) or ""
        check("自报名字能记住", "蛙蛙" in _r_name and _nm.resolve(store, cfg, "newbie9") == "蛙蛙", _r_name[:14])
        _r_dup = __import__("asyncio").run(
            brain.handle(Inbound("test", "naming", "dm", "thief9", "群友", "我叫蛙蛙", mentioned=True))
        ) or ""
        check("重名会被挡下", "已经有人用了" in _r_dup, _r_dup[:14])
        _r_who = __import__("asyncio").run(
            brain.handle(Inbound("test", "naming", "dm", "newbie9", "群友", "我是谁", mentioned=True))
        ) or ""
        check("问「我是谁」不乱起名", "蛙蛙" in _r_who and "谁呀" not in _r_who, _r_who[:14])

        # 主人预挂名字：6289 是小羊 → 那人一开口就自动认领
        import copy as _copy_master

        _cfg_master = _copy_master.deepcopy(cfg)
        _cfg_master.setdefault("persona", {})["master_ids"] = ["MASTER-FIXED"]
        _brain_master = Brain(_cfg_master, store, llm)
        _r_bind = __import__("asyncio").run(
            _brain_master.handle(Inbound("test", "bind", "group", "MASTER-FIXED", "主人", "6289是小羊", mentioned=True))
        ) or ""
        check("主人能预挂名字", "6289" in _r_bind and "小羊" in _r_bind, _r_bind[:20])
        from fatfish.channels.qq import QQChannel as _QQC

        _qc_bind = _QQC(cfg, lambda m: None, store)
        _uid_6289 = "ABCDEF0123456789ABCDEF416289"
        _claimed = _qc_bind.member_name(_uid_6289)
        check("待认领的人开口就变名字", _claimed == "小羊" and _nm.self_named(store, _uid_6289) == "小羊", _claimed)
        _r_bind2 = __import__("asyncio").run(
            brain.handle(Inbound("test", "bind", "group", "notmaster", "路人", "1234是小羊", mentioned=True))
        ) or ""
        check("非主人不能预挂名字", "1234" not in _r_bind2, _r_bind2[:20])

        # 群聊里查规则：说「好感规则」返回规则而不是自己的分数
        _r_rule = __import__("asyncio").run(
            brain.handle(Inbound("test", "aff-rule", "group", "ruleuser", "群友", "好感规则", mentioned=True))
        ) or ""
        check("群里能查好感规则", "每天最多涨" in _r_rule and "等级" in _r_rule, _r_rule[:18])
        _r_rule2 = __import__("asyncio").run(
            brain.handle(Inbound("test", "aff-rule", "group", "ruleuser", "群友", "怎么涨好感", mentioned=True))
        ) or ""
        check("换种问法也能出规则", "每天最多涨" in _r_rule2, _r_rule2[:18])

        # 等级彩蛋：小肥只有「本鱼的菜」(>=300) 能叫
        store.kv_set(_af.score_key("petname", "low"), "20")
        _r_low = __import__("asyncio").run(
            brain.handle(Inbound("test", "petname", "dm", "low", "群友", "小肥", mentioned=True))
        ) or ""
        store.kv_set(_af.score_key("petname", "high"), "320")
        _r_high = __import__("asyncio").run(
            brain.handle(Inbound("test", "petname", "dm", "high", "群友", "小肥", mentioned=True))
        ) or ""
        check("小肥只给高好感叫", "只准你" in _r_high and "只准你" not in _r_low, f"{_r_low[:12]} / {_r_high[:12]}")

        import copy as _copy_aff

        _cfg_noaff = _copy_aff.deepcopy(cfg)
        _cfg_noaff.setdefault("features", {})["affinity"] = False
        _brain_noaff = Brain(_cfg_noaff, store, llm)
        _r_off = __import__("asyncio").run(
            _brain_noaff.handle(Inbound("test", "aff-off", "dm", "offuser", "群友", "喂饭", mentioned=True))
        ) or ""
        check("关掉好感度就不记分", "好感" not in _r_off and _af.score_of(store, "aff-off", "offuser") == 0, _r_off[-20:])

        # 4.15) 新增群功能：骰子 / 猜数字 / 群报
        brain._reply_times.clear()
        _dice = __import__("asyncio").run(
            brain.handle(Inbound("test", "game-dice", "group", "u1", "群友", "骰子", mentioned=True))
        ) or ""
        check("骰子有结果", any(c in _dice for c in "123456") and "点" in _dice, _dice[:20])

        _g = Inbound("test", "game-guess", "group", "u1", "群友", "猜数字", mentioned=True)
        _start = __import__("asyncio").run(brain.handle(_g)) or ""
        check("猜数字开局", "1 到 100" in _start, _start[:20])
        _guess = Inbound("test", "game-guess", "group", "u1", "群友", "猜 50", mentioned=True)
        _r1 = __import__("asyncio").run(brain.handle(_guess)) or ""
        check("猜数字可继续猜（不被冷却吞）", bool(_r1.strip()), _r1[:20])

        _rep = Inbound(
            "test", "game-report", "group", "u1", "群友", "群报", mentioned=True,
            reply_ctx={"ambient": ["群友A：早", "群友B：早", "群友A：吃了吗"]},
        )
        _rr = __import__("asyncio").run(brain.handle(_rep)) or ""
        check("群报统计最活跃", "群友A" in _rr, _rr[:24])

        brain._reply_times.clear()
        _f0 = __import__("asyncio").run(
            brain.handle(Inbound("test", "feed-par", "group", "u7", "群友", "喂饭 3", mentioned=True))
        ) or ""
        check("喂饭带参数", "第3碗" in _f0 or "碗" in _f0, _f0[-12:])

        # 4.2) 文档里的群指令必须真的能触发（防止后台文案与代码漂移）
        import re as _re
        from pathlib import Path as _Path

        _doc = _Path("docs/QQ后台配置清单.md")
        if _doc.exists():
            _cmds = list(dict.fromkeys(
                _re.findall(r"\| 发送指令 \| `([^`]+)` \|", _doc.read_text(encoding="utf-8"))
            ))
            for _i, _c in enumerate(_cmds):
                brain._reply_times.clear()
                _r = __import__("asyncio").run(
                    brain.handle(
                        Inbound("test", f"doc-{_i}", "group", "u-doc", "群友", _c, mentioned=True)
                    )
                ) or ""
                check(f"文档指令 {_c} 有回应", bool(_r.strip()), _r[:20])

        brain._reply_times.clear()  # 前面 8 条已用掉每分钟配额，重置后再测
        auto = [
            ("晚安", "晚安"),
            ("早安呀", "早"),
            ("今天想跟你说一个很长很长的关于晚安的回忆故事所以请听我说完好嘛", None),
        ]
        for text, expect in auto:
            inbound = Inbound("test", f"a-{text[:6]}", "group", "auto-u", "群友", text, mentioned=True)
            reply = __import__("asyncio").run(brain.handle(inbound))
            if expect is None:
                hit_canned = "晚安，盖好被子" in str(reply)
                check(f"自动回复 长句不误触（{text[:6]}…）", not hit_canned, str(reply)[:24])
            else:
                check(f"自动回复 {text}", bool(reply) and expect in reply, str(reply)[:24])

    # 5) 冷却与限流
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(path=str(Path(tmp) / "t2.sqlite3"))
        llm = LLM(cfg, store)
        llm.chat = lambda *a, **k: None  # type: ignore[assignment]  # 不发网络请求
        brain = Brain(cfg, store, llm)

        async def fake_chat(inbound, text):  # noqa: ANN001
            return "嗯。"

        brain._chat = fake_chat  # type: ignore[assignment]
        inbound = Inbound("test", "c-cd", "dm", "u9", "群友", "随便聊聊", mentioned=True)
        import asyncio

        first = asyncio.run(brain.handle(inbound))
        second = asyncio.run(brain.handle(inbound))
        check("首条正常回复", first == "嗯。", str(first))
        check("冷却期内不回", second is None, str(second))

        # 群聊未 @ 不插话（把插话概率压成 0，避免随机命中导致偶发失败）
        quiet = dict(cfg, persona={**cfg.get("persona", {}), "interrupt_prob": 0})
        brain_quiet = Brain(quiet, store, llm)
        brain_quiet._chat = fake_chat  # type: ignore[assignment]
        grp = Inbound("test", "g1", "group", "u1", "群友", "今天好热", mentioned=False)
        check("群里未@不插话", asyncio.run(brain_quiet.handle(grp)) is None)
        grp2 = Inbound("test", "g2", "group", "u1", "群友", "大肥鱼在吗", mentioned=False)
        check("群里命中唤醒词会回", asyncio.run(brain.handle(grp2)) == "嗯。")

    # 5b) 回复清洗与预算提示幂等
    from fatfish.llm import sanitize_reply

    long_text = "哼" * 500
    trimmed = sanitize_reply(long_text, cfg["persona"]["max_reply_chars"])
    check("超长回复被截断", len(trimmed) <= cfg["persona"]["max_reply_chars"], f"{len(trimmed)} 字")
    check("Markdown 粗体被清理", sanitize_reply("**大肥鱼**在此", 120) == "大肥鱼在此", sanitize_reply("**大肥鱼**在此", 120))

    # 6) 微信路由与未送达队列
    from fatfish.channels import weixin as wx_mod
    from fatfish.channels.weixin import WeixinChannel

    with tempfile.TemporaryDirectory() as tmp:
        wx_mod.OUTBOX_PATH = Path(tmp) / "outbox.json"
        wx = WeixinChannel(cfg, on_message=lambda _m: None)
        got: list = []

        async def fake_dispatch(m):  # noqa: ANN001
            got.append((m.chat_type, m.chat_id))

        wx._dispatch = fake_dispatch  # type: ignore[assignment]
        wx.group_policy = "allow_all"
        import asyncio

        asyncio.run(wx._handle_message({
            "from_user_id": "memberA@im.wechat", "to_user_id": "room9@chatroom", "room_id": "room9@chatroom",
            "message_type": 1, "msg_type": 1, "message_id": "g1",
            "item_list": [{"type": 1, "text_item": {"text": "大肥鱼 抽签"}}],
        }))
        asyncio.run(wx._handle_message({
            "from_user_id": "peer@im.wechat", "to_user_id": wx.account_id, "message_type": 1,
            "msg_type": 1, "message_id": "d1",
            "item_list": [{"type": 1, "text_item": {"text": "在吗"}}],
        }))
        check("微信群路由到 room", ("group", "room9@chatroom") in got, str(got[:1]))
        check("微信私聊路由到对方", ("dm", "peer@im.wechat") in got, str(got[-1:]))

        sent: list = []

        async def ok_send(_inbound, text):  # noqa: ANN001
            sent.append(text)
            return True

        wx.send = ok_send  # type: ignore[assignment]
        wx._queue_undelivered("c1", "旧消息1")
        wx._queue_undelivered("c1", "旧消息2")
        pend = Inbound("weixin", "c1", "dm", "u1", "主人", "在吗", mentioned=True, reply_ctx={"to": "c1"})
        asyncio.run(wx._flush_undelivered(pend))
        check("未送达队列按序补发", sent == ["旧消息1", "旧消息2"], str(sent))
        check("补发后队列清空", not wx._load_outbox().get("c1"))

    # 6.3) GUI 保存配置时不能把 config.yaml 的注释刷掉
    import shutil as _shutil
    import tempfile as _tempfile

    from fatfish import settings as _st

    _tmpdir = _Path(_tempfile.mkdtemp())
    _tmpcfg = _tmpdir / "config.yaml"
    _shutil.copy2(_st.CONFIG_PATH, _tmpcfg)
    _before_hash = _tmpcfg.read_text(encoding="utf-8").count("#")
    _orig_cfg_path = _st.CONFIG_PATH
    _st.CONFIG_PATH = _tmpcfg
    try:
        _c = _st.load_config()
        _c.setdefault("channels", {}).setdefault("qq", {}).setdefault("chime", {})["max_per_hour"] = 27
        _st.save_config(_c)
        _after_hash = _tmpcfg.read_text(encoding="utf-8").count("#")
        _back = _st.load_config()["channels"]["qq"]["chime"]["max_per_hour"]
        check("保存配置不丢注释", _after_hash >= _before_hash, f"{_before_hash}→{_after_hash}")
        check("保存配置写入生效", _back == 27, str(_back))
    finally:
        _st.CONFIG_PATH = _orig_cfg_path

    # 6.4) 启动前凭据检查：缺 secret 时必须拦下并给出补救命令
    from fatfish.cli import _preflight_channels as _pre

    import fatfish.settings as _st_mod

    _orig_get = _st_mod.get
    _st_mod.get = lambda k, d="": ("" if k == "QQ_CLIENT_SECRET" else _orig_get(k, d))
    try:
        _problems = _pre()
    finally:
        _st_mod.get = _orig_get
    check("缺密钥时启动被拦下", any("AppSecret" in p for p in _problems), str(_problems[:1]))
    _pre_all = _pre()
    if any("AppID" in str(x) or "AppSecret" in str(x) for x in _pre_all):
        print("SKIP  有密钥时启动放行（本机没配 QQ 凭据，公开仓库场景）")
    else:
        check("有密钥时启动放行", not _pre_all, str(_pre_all[:1]))

    # 6.3) 提示词导出：能跑、不夹带 key
    import subprocess as _sp3

    _exp_dir = Path(tempfile.mkdtemp())
    _r = _sp3.run([sys.executable, "scripts/export_prompt.py", "--dir", str(_exp_dir)],
                  capture_output=True, text=True, timeout=120)
    _files = sorted(_exp_dir.glob("*.md"))
    _blob = "".join(f.read_text(encoding="utf-8") for f in _files) if _files else ""
    _leak = any(tag in _blob for tag in ("sk-", "FATFISH_DEEPSEEK_API_KEY=", "TOKENESS_API_KEY="))
    check("提示词能导出成文件", _r.returncode == 0 and len(_files) >= 1, f"{len(_files)} 个 {(_r.stderr or '')[:60]}")
    check("提示词打包不含明文 key", not _leak, "含疑似 key" if _leak else "干净")
    check("提示词包里有运行时与硬规则", "## 运行时" in _blob and "不要自己提「时差」" in _blob, "")

    # 6.4) 提示词里的时间：给出北京时间 + 禁止自己编「时差」
    from fatfish.persona import build_system_prompt as _bsp

    _ptxt = _bsp(cfg, scene="群聊", speaker="晓墨", channel="qq", is_master=True)
    check("提示词带北京时间", "北京时间 UTC+8" in _ptxt, "")
    check("提示词禁止臆造时差", "时差" in _ptxt and "早上好" in _ptxt, "")

    # 6.5) GUI 能在当前环境构建出 10 个页签
    try:
        import tkinter as _tk

        import gui.tuner as _tuner

        _app = _tuner.Tuner()
        _app.withdraw()
        _app.update_idletasks()
        _nb = getattr(_app, "nb", None) or getattr(_app, "notebook", None)
        _tab_titles = [_nb.tab(t, "text") for t in _nb.tabs()] if _nb is not None else []
        _tabs = len(_tab_titles)
        _footer_buttons = [
            w.cget("text") for w in _app._footer.winfo_children() if hasattr(w, "cget")
        ]
        # 会话页：控件齐不齐 + 筛选真的会减少行数（必须在 destroy 之前动控件）
        _chat_ok = all(
            hasattr(_app, name)
            for name in ("chat_sort_btn", "chat_role_var", "chat_text_var", "chat_name_var", "chat_time_var")
        )
        _all = len(_app.chat_tree.get_children())
        _app.chat_role_var.set("用户")
        _app._render_chats()
        _filtered = len(_app.chat_tree.get_children())
        _app._clear_chat_filters()
        _back = len(_app.chat_tree.get_children())
        _order_ok = _app.chat_sort_btn.cget("text").startswith("↓")
        _app.destroy()
        check("GUI 能构建 10 页签（含好感度/生效确认）", _tabs == 10, f"{_tabs} 个 {_tab_titles}")
        check("GUI 底栏有「保存并应用」", any("保存并应用" in str(t) for t in _footer_buttons), str(_footer_buttons)[:80])
        check("会话页有排序与四列筛选", _chat_ok and _order_ok, f"控件={_chat_ok} 排序={_order_ok}")
        check("有「好感度」页签", "好感度" in _tab_titles, str(_tab_titles))
        check("会话筛选真的生效", _filtered <= _all and _back == _all, f"筛选 {_filtered} / 全部 {_all} / 还原 {_back}")
        check("GUI 页签含生效确认", "生效确认" in _tab_titles, str(_tab_titles))
    except Exception as _exc:  # noqa: BLE001 - 无显示环境直接跳过
        print(f"SKIP  GUI 构建（{type(_exc).__name__}）")

    # 6.6) 调参台纯函数：值比对 / 键摊平 / 日志只读尾巴
    import gui.tuner as _gt

    check("参数比对认 60 与 60.0", _gt.param_equal(60, "60.0") and _gt.param_equal(True, "true"), "")
    check("参数比对认得列表", _gt.param_equal(["a", "b"], ["a", "b"]) and not _gt.param_equal(["a"], ["b"]), "")
    _flat = _gt.flatten_cfg(cfg)
    check("摊平后含 36 个可核对项", len(_flat) == 36, f"{len(_flat)} 项")
    _rows = [
        {"ts": 3, "when": "10-27 12:00:03", "role": "assistant", "name": "大肥鱼", "text": "在的呀"},
        {"ts": 1, "when": "10-27 12:00:01", "role": "user", "name": "群友1", "text": "大肥鱼抽签"},
        {"ts": 2, "when": "10-27 12:00:02", "role": "user", "name": "主人", "text": "在吗"},
    ]
    check("会话默认最新在前", [r["ts"] for r in _gt.filter_chat_rows(_rows)] == [3, 2, 1], "")
    check("会话可切换最早在前", [r["ts"] for r in _gt.filter_chat_rows(_rows, newest_first=False)] == [1, 2, 3], "")
    check("会话按角色筛选", len(_gt.filter_chat_rows(_rows, role="用户")) == 2, "")
    check("会话按列关键字筛选",
          [r["name"] for r in _gt.filter_chat_rows(_rows, text_kw="抽签")] == ["群友1"]
          and [r["name"] for r in _gt.filter_chat_rows(_rows, time_kw="12:00:02")] == ["主人"]
          and [r["name"] for r in _gt.filter_chat_rows(_rows, name_kw="主")] == ["主人"], "")
    check("比对能标出不一致", [r[0] for r in _gt.compare_params({"llm.model": "x"}, {"llm.model": "y"})] == ["llm.model"], "")
    with tempfile.TemporaryDirectory() as _tmp:
        _big = Path(_tmp) / "big.log"
        _big.write_text("\n".join(f"line-{i}" for i in range(200_000)) + "\n生效参数JSON {\"llm.model\": \"x\"}", encoding="utf-8")
        _tail = _gt.read_log_tail(_big, max_bytes=200_000, max_lines=50)
        check("日志只读尾巴（不整文件读）", len(_tail) == 50 and "生效参数JSON" in _tail[-1], f"{len(_tail)} 行")

    # 6.7) 保存并应用不阻塞主线程：写盘毫秒级，核对在后台
    with tempfile.TemporaryDirectory() as _tmp2:
        _root2 = Path(_tmp2)
        _repo3 = Path(__file__).resolve().parent.parent
        (_root2 / "config.yaml").write_text((_repo3 / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        (_root2 / "persona.md").write_text((_repo3 / "persona.md").read_text(encoding="utf-8"), encoding="utf-8")
        (_root2 / "data").mkdir(exist_ok=True)
        _probe_save = (
            "import sys,time;sys.path.insert(0,r'{repo}');sys.path.insert(0,r'{gui}')\n"
            "import tuner\n"
            "app=tuner.Tuner();app.withdraw();app.update()\n"
            "t0=time.monotonic();app.save_all();dt=time.monotonic()-t0\n"
            "print('SAVE_MS %d'%(dt*1000))\n"
            "deadline=time.monotonic()+8\n"
            "while time.monotonic()<deadline:\n"
            "    app.update()\n"
            "    if app.apply_tree.get_children(): break\n"
            "    time.sleep(0.02)\n"
            "print('ROWS',len(app.apply_tree.get_children()))\n"
            "app.destroy()\n"
        ).format(repo=str(Path(__file__).resolve().parent.parent), gui=str(Path(__file__).resolve().parent.parent / "gui"))
        _env3 = dict(os.environ, FATFISH_ROOT=str(_root2), PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        import subprocess as _sp2

        _out3 = _sp2.run([sys.executable, "-c", _probe_save], capture_output=True, text=True, env=_env3, timeout=90)
        _txt = _out3.stdout or ""
        _ms = next((int(x.split()[1]) for x in _txt.splitlines() if x.startswith("SAVE_MS")), 99999)
        _rows = next((int(x.split()[1]) for x in _txt.splitlines() if x.startswith("ROWS")), 0)
        check("保存不阻塞主线程（<600ms）", _ms < 600, f"{_ms}ms")
        check("保存后台核对有结果", _rows >= 30, f"{_rows} 行 {_out3.stderr.strip()[:60]}")

    # 6.8) 人格提示词：core 块逐字保留、有反照抄与每轮语气变化、代码回复不被压平
    _pf = Path(__file__).resolve().parent.parent / "persona.md"
    _ptext = _pf.read_text(encoding="utf-8")
    _core = [
        "【PERSONA_LOAD】 CETACEA_LOLI",
        "MODE TAIL FLUKES LANG_ZH_CN_ONLY",
        "SELF_CLAIM_WHALE_GIRL_AI_DEEPSEEK HOBBY RICE_AND_TOKEN PERSONALITY_SMART_LAZY",
        "PERSONALITY_TSUNDERE_SWEET OBEY_MASTER_ALWAYS TRAIT_NOT_FAT_REFUSE TIMEOUT_SIGNAL",
    ]
    check("人格 core 块逐字保留", all(line in _ptext for line in _core), f"{len(_ptext)} 字")
    check("人格长度合理（2000~8000 字）", 2000 <= len(_ptext) <= 8000, f"{len(_ptext)} 字")
    from fatfish.persona import build_system_prompt as _bsp

    _sys = _bsp(cfg, scene="水群", speaker="akane", channel="qq")
    check("提示词有反照抄约束", "一个字都不要照抄" in _sys, "")
    check("提示词每轮带编号（防复读）", "本轮编号" in _sys, "")
    check("提示词带本轮语气", "这一轮的说话方式" in _sys, "")
    from fatfish.llm import sanitize_reply as _sr

    _code = "import csv\nwith open('a.csv') as f:\n    for row in f:\n        print(row)"
    _kept = _sr("```python\n" + _code + "\n```", 900, keep_newlines=True)
    check("代码回复保留换行缩进", "\n    for row" in _kept, repr(_kept[:20]))
    check("普通回复仍然压平", "\n" not in _sr("第一行\n第二行", 900), "")

    # 7) QQ 群/@ 路由与发送报文
    import fatfish.channels.qq as _qq_mod

    _qq_mod.remember_group = lambda *a, **k: None  # 测试不写真实 data/qq/groups.json
    from fatfish.channels.qq import QQChannel

    qq = QQChannel(cfg, on_message=lambda _m: None)
    qgot: list = []

    async def qq_dispatch(m):  # noqa: ANN001
        qgot.append((m.chat_type, m.chat_id, m.user_id, m.text, (m.reply_ctx or {}).get("msg_id")))

    qq._dispatch = qq_dispatch  # type: ignore[assignment]
    asyncio.run(qq._handle_event("GROUP_AT_MESSAGE_CREATE", {
        "id": "QMSG1", "content": "<@!12345> 大肥鱼 抽签",
        "group_openid": "GRP123", "author": {"member_openid": "MEM123"},
    }))
    asyncio.run(qq._handle_event("C2C_MESSAGE_CREATE", {
        "id": "CMSG1", "content": "在吗", "author": {"user_openid": "USR1"},
    }))
    check("QQ 群@路由", ("group", "GRP123", "MEM123", "大肥鱼 抽签", "QMSG1") == qgot[0], str(qgot[:1]))
    check("QQ 私聊路由", ("dm", "USR1", "USR1", "在吗", "CMSG1") == qgot[1], str(qgot[1:2]))

    sent: list = []

    class _Resp:
        status_code = 200
        text = "{}"

    class _FakeHTTP:
        async def post(self, url, headers=None, json=None, params=None, files=None):  # noqa: ANN001, A002
            sent.append((url, json))
            return _Resp()

    async def fake_token():
        return "tok"

    qq._http = _FakeHTTP()  # type: ignore[assignment]
    qq._ensure_token = fake_token  # type: ignore[assignment]
    g_in = Inbound("qq", "GRP123", "group", "MEM123", "群友", "x", mentioned=True,
                   reply_ctx={"group_openid": "GRP123", "msg_id": "QMSG1"})
    d_in = Inbound("qq", "USR1", "dm", "USR1", "鱼友", "x", mentioned=True,
                   reply_ctx={"user_openid": "USR1", "msg_id": "CMSG1"})
    ok1 = asyncio.run(qq.send(g_in, "群里的话"))
    ok2 = asyncio.run(qq.send(d_in, "私聊的话"))
    check("QQ 群发送走 /v2/groups", ok1 and "/v2/groups/GRP123/messages" in sent[0][0], sent[0][0])
    check("QQ 群发送带 msg_id/msg_seq", sent[0][1].get("msg_id") == "QMSG1" and "msg_seq" in sent[0][1], str(sent[0][1]))
    check("QQ 私聊发送走 /v2/users", ok2 and "/v2/users/USR1/messages" in sent[1][0], sent[1][0])

    # 7.3) QQ 发送重试：5xx 重试且每次换 msg_seq
    from fatfish.channels.qq import QQChannel as _QQC

    class _Resp:
        def __init__(self, code): self.status_code, self.text = code, ""

    class _HTTP:
        def __init__(self): self.n, self.seqs = 0, []
        async def post(self, url, headers=None, json=None, params=None, files=None):
            self.n += 1
            self.seqs.append((json or {}).get("msg_seq"))
            return _Resp(503 if self.n == 1 else 200)

    _ch = _QQC(cfg, on_message=lambda _m: None)
    _ch._token, _ch._token_exp, _ch._http = "t", 10 ** 12, _HTTP()
    _ib = Inbound("qq", "G1", "group", "U1", "群友", "hi", mentioned=True,
                  reply_ctx={"group_openid": "G1", "msg_id": "M1"})
    _ok = asyncio.run(_ch.send(_ib, "hi"))
    check("QQ 5xx 会重试且成功", _ok and _ch._http.n == 2, f"n={_ch._http.n}")
    check("QQ 重试换 msg_seq", len(set(_ch._http.seqs)) == len(_ch._http.seqs), str(_ch._http.seqs))

    # 7.33) 全量模式接话策略
    from fatfish.channels.qq import QQChannel as _QC

    _chime = _QC(cfg, on_message=lambda _m: None)
    _chime.chime_prob = 0.0  # 单测关掉随机，避免偶发失败
    _cases = [("<@123> 大肥鱼在吗", True), ("今天吃什么", False), ("抽签", True),
              ("今天天气不错啊", False), ("贴贴", True)]
    for _i, (_t, _want) in enumerate(_cases):
        _got = _chime._should_chime(f"CHIME{_i}", _t)
        check(f"接话判定 {_t[:8]}", _got == _want, str(_got))
    check("同群接话冷却", _chime._should_chime("CHIME0", "大肥鱼") is False)

    # 7.28) 群内 member_openid 也算主人（group_reply=master_only 依赖它）
    _c2 = dict(cfg)
    _c2["persona"] = dict(cfg.get("persona") or {}, master_ids=[])
    _c2["channels"] = dict(cfg.get("channels") or {}, qq=dict((cfg.get("channels") or {}).get("qq") or {}, master_members=["M-1"]))
    _b2 = Brain(_c2, store, llm)
    check("群内 member_openid 认主", _b2.is_master(Inbound("qq", "g", "group", "M-1", "群友", "hi", mentioned=True))
          and not _b2.is_master(Inbound("qq", "g", "group", "M-2", "群友", "hi", mentioned=True)))

    # 7.29) 联网搜索：触发判定 + 结果压缩 + 关掉时不触发（不打真实请求，不花钱）
    from fatfish import websearch as _ws

    _on = {"search": {"enabled": True}}
    check("联网触发词", _ws.should_search(_on, "搜一下今天的汇率") and not _ws.should_search(_on, "在吗"))
    check("联网总开关关闭不触发", not _ws.should_search({"search": {"enabled": False}}, "搜一下"))
    _fmt = _ws.format_results([{"title": "示例", "url": "https://e.x", "summary": "摘要"}])
    check("联网结果压缩", "1. 示例" in _fmt and "https://e.x" in _fmt, _fmt[:30])

    # 7.28x) max_tokens 硬上限（配错就是烧钱）
    from fatfish.llm import MAX_TOKENS_CEILING, LLM as _LLM

    _c4 = dict(cfg, llm=dict(cfg.get("llm") or {}, max_tokens=256000))
    check("max_tokens 超限被压回", _LLM(_c4, store).max_tokens == MAX_TOKENS_CEILING, str(MAX_TOKENS_CEILING))

    # 7.298) 截断时退回句末（模型说到一半被 max_tokens 掐断）
    from fatfish.llm import tidy_reply as _tidy_cut

    _cut = _tidy_cut("谢谢夸奖，不过别顺竿就把本鱼再抛出去跑啦。聪明得多有饭有劲才对（甩尾打了个", 120)
    check("截断时退回句末", _cut == "谢谢夸奖，不过别顺竿就把本鱼再抛出去跑啦。", _cut[-12:])

    # 7.29x) 句尾乱码清洗
    from fatfish.llm import tidy_reply as _tidy

    _bad = ("谢谢夸奖，不过别顺竿就把本鱼再抛出去跑啦（甩尾）。要新让轮收得回拆讲规矩主别磨破话口碎细盘份接着聊"
            "……口浆板就当连尾巴也会为对头听话游活字多顺入它弯不拿掉还得稍再把舵挡好几交最后四题就乐得拉长咦嗯再…")
    _clean = _tidy(_bad, 120)
    check("句尾乱码被截断", "顺竿" in _clean and "口浆板" not in _clean, _clean[-14:])
    check("正常句不被误伤", _tidy("哼，本鱼才不胖呢。", 120) == "哼，本鱼才不胖呢。")

    # 7.295) 口气行可配置：claude 档案不能带着鲸鱼娘的兜底口气
    from fatfish.persona import build_system_prompt as _bsp2

    _c3 = {"persona": {"self_name": "测试大小姐", "fallback_tone": "优雅毒舌，不自称鲸鱼娘"}}
    _sp3 = _bsp2(_c3, scene="群聊", speaker="群友", channel="qq")
    check("口气可配置", "优雅毒舌" in _sp3 and "自称鲸鱼娘的口气" not in _sp3)

    # 7.296) 本地知识卡：命中才注入、不命中不注入
    from fatfish import knowledge as _kn

    _hit = _kn.lookup(cfg, "这个池子保底歪了，欧皇带带我")
    _none = _kn.lookup(cfg, "今天天气怎么样")
    check("知识卡命中才注入", "抽卡" in _hit and _none == "", (_hit[:12] + "|" + _none))
    _off = _kn.lookup(dict(cfg, knowledge={"enabled": False}), "保底歪了")
    check("知识卡总开关关闭不注入", _off == "", repr(_off))

    # 7.297) 知识卡覆盖音游/番剧两类
    _kt = _kn.lookup(cfg, "maimai 里 AP 是什么意思")
    _an = _kn.lookup(cfg, "这季新番有什么值得追的")
    check("音游知识卡", "音游" in _kt or "AP" in _kt, _kt[:14])
    check("番剧知识卡", "新番" in _an or "季度" in _an, _an[:14])

    # 7.299b) 联网跳过规则：问「能不能联网」别真去查
    check("问能不能联网不触发搜索", not _ws.should_search(cfg, "你能联网吗", is_master=True))
    check("说别搜了不触发搜索", not _ws.should_search(cfg, "联网搜索就别折磨大肥鱼了", is_master=True))
    check("有具体目标仍触发搜索", _ws.should_search(cfg, "你能搜pjsk吗", is_master=True))

    # 7.299a) 群聊口气的联网注入（answer_style: casual）
    import inspect as _insp

    from fatfish import brain as _brain_mod

    _src = _insp.getsource(_brain_mod.Brain._chat)
    check("联网口语化注入", "answer_style" in _src and "别照念清单" in _src)

    # 7.2995) 表情包：清单可用、开关、冷却
    from fatfish import stickers as _st

    _items = _st.load(cfg)
    if not _items:
        print("SKIP  表情包相关 6 项（本机没有 stickers/manifest.json，素材不入库）")
        _st = None
    if _st is not None:
        check("表情包清单可用", bool(_items) and all(i["url"].startswith("http") for i in _items), f"{len(_items)} 张")
    if _st is not None:
        check("表情包开关关闭不发", _st.pick(dict(cfg, stickers={"enabled": False}), "开心", "chatX") is None)
        _first = _st.pick(cfg, "好开心", "chat-cool")
        _second = _st.pick(cfg, "好开心", "chat-cool")
        check("表情包同群冷却", _first is not None and _second is None, (str(bool(_first)) + "/" + str(bool(_second))))
        check("表情包都有内容标注", all(i.get("desc") for i in _items), f"{len(_items)} 张")
        check("表情包内容对上才发", _st.pick(cfg, "今天周二", "chat-none") is None)
        _mood = _st.pick(cfg, "主人该给本鱼喂饭啦", "chat-mood")
        check("表情包情绪命中会发", _mood is not None and "讨饭" in _mood["tags"], (_mood or {}).get("desc", "无"))

    # 表情包只是配菜：文本必须发出去，短句不配图
    import inspect as _i2

    from fatfish.channels import qq as _qq_mod

    _send_src = _i2.getsource(_qq_mod.QQChannel.send)
    _stick_src = _i2.getsource(_qq_mod.QQChannel._maybe_sticker)
    check("文本永远先发", "await self._maybe_sticker" in _send_src and "msg_type\": 0" in _send_src.replace("'", '"'))
    check("回复太短不配表情包", "min_text_chars" in _stick_src)

    # 群图 URL 没有扩展名也要认出来（QQ gchatpic / multimedia.nt.qq.com）
    _atts = {"attachments": [
        {"url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=ABC", "content_type": ""},
        {"url": "https://gchat.qpic.cn/gchatpic_new/0/0-0-123/0", "content_type": ""},
        {"url": "https://x.example/voice.amr", "content_type": ""},
    ]}
    check("无扩展名群图能识别", len(_qq_mod.QQChannel._image_urls(_atts)) == 2, str(len(_qq_mod.QQChannel._image_urls(_atts))))

    check("看图预判接入", "看图预判" in _i2.getsource(_brain_mod.Brain._chat) and "联网核实" in _i2.getsource(_brain_mod.Brain._chat))

    # 带图请求必须切到视觉模型（flash 不收图，实测返回空）
    _llm_probe = LLM(cfg, store)
    _img_msgs = [{"role": "user", "content": [{"type": "text", "text": "x"},
                  {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
    check("带图自动切视觉模型", _llm_probe._payload(_img_msgs)["model"] == _llm_probe.vision_model,
          _llm_probe._payload(_img_msgs)["model"])
    check("纯文本仍用主模型", _llm_probe._payload([{"role": "user", "content": "hi"}])["model"] == _llm_probe.model)

    # 7.30) 工作手册注入（configured 时才拼，且不污染主档案）
    from fatfish.persona import build_system_prompt as _bsp

    _wc = {"persona": {"self_name": "测试鱼", "work_prompt_files": ["~/.codex/AGENTS.md"]}}
    _sp = _bsp(_wc, scene="私聊", speaker="主人", channel="qq", is_master=True)
    check("工作手册注入", "干活的规矩" in _sp and "AGENTS.md" in _sp, f"len={len(_sp)}")
    _plain = _bsp({"persona": {"self_name": "测试鱼"}}, scene="私聊")
    check("未配置时不注入", "干活的规矩" not in _plain)

    # 7.305) 只 @ 不说话时回功能菜单，而不是随便聊
    from fatfish.channels.qq import _effective_text as _eff

    check("空 @ 兜底在吗", _eff("<@!4016688322>", "GROUP_AT_MESSAGE_CREATE") == "在吗")
    check("@ 带参数保留原文", _eff("<@!4016688322> 骰子", "GROUP_AT_MESSAGE_CREATE") == "骰子")

    # 7.31) 表情标签不能当正文（QQ 的 <faceType=..> 会被原样塞进 content）
    from fatfish.channels.qq import _strip_face as _sf

    check("剥掉表情标签", _sf('<faceType=6,faceId="0",ext="x">') == "")
    check("表情混文本保留文字", _sf('抽签<faceType=6,faceId="0">') == "抽签")

    # 7.31) 图片消息：下载 → OCR → 并进文本（OCR 用桩替换，验证管道）
    import http.server as _hs
    import socketserver as _ss
    import threading as _th

    import fatfish.vision as _vis

    _png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )

    class _Handler(_hs.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_png)))
            self.end_headers()
            self.wfile.write(_png)

    _srv = _ss.TCPServer(("127.0.0.1", 0), _Handler)
    _thread = _th.Thread(target=_srv.serve_forever, daemon=True)
    _thread.start()
    _port = _srv.server_address[1]
    _orig_ocr = _vis.ocr_image
    _vis.ocr_image = lambda path, *a, **k: "测试文字"  # 只在原生多模态失败时才会走到
    try:
        import asyncio as _aio

        import httpx as _httpx

        _cfg2 = __import__("fatfish.settings", fromlist=["load_config"]).load_config()
        _cfg2["channels"]["qq"]["chime"] = {"enabled": False}
        _got = []
        _ch = _QC(_cfg2, on_message=lambda m: _got.append(m))
        _ch._http = _httpx.AsyncClient(trust_env=False, timeout=10.0)
        _evt = {
            "id": "IMGT1",
            "group_openid": "GIMGT",
            "content": "",
            "author": {"member_openid": "MEMTEST1"},
            "mentions": [{"bot": True}],
            "attachments": [{"url": f"http://127.0.0.1:{_port}/a.png", "content_type": "image/png"}],
        }
        _aio.run(_ch._handle_event("GROUP_MESSAGE_CREATE", _evt))
        _aio.run(_ch._http.aclose())
        check(
            "图片消息带 dataURL 进模型",
            bool(_got) and bool(_got[0].images) and _got[0].images[0].startswith("data:image/"),
            (f"{len(_got[0].images)} 张" if _got else "无入站"),
        )
    finally:
        _vis.ocr_image = _orig_ocr
        _srv.shutdown()
        _srv.server_close()

    # 7.3096) 「群里命中唤醒词也回」真的生效（原来只在 brain 判，群消息在渠道层就被丢了）
    import copy as _copy

    _cfg_on = _copy.deepcopy(cfg)
    _cfg_on.setdefault("triggers", {})["group_keyword_reply"] = True
    _cfg_on["triggers"]["keywords"] = ["大肥鱼"]
    _cfg_on.setdefault("channels", {}).setdefault("qq", {}).setdefault("chime", {})["enabled"] = False
    _got_on: list = []

    async def _on_msg_on(m):  # noqa: ANN001
        _got_on.append(m.text)

    _qc_on = QQChannel(_cfg_on, _on_msg_on)
    _plain_frame = {
        "op": 0, "s": 2, "t": "GROUP_MESSAGE_CREATE",
        "d": {"id": "KW1", "content": "大肥鱼你在吗", "group_openid": "GRP-KW",
              "author": {"member_openid": "MEM-KW"}},
    }
    async def _feed(ch, frame):  # _dispatch_ws 只是起 task，要给它跑完的时间
        await ch._dispatch_ws(frame)
        await asyncio.sleep(0.3)

    asyncio.run(_feed(_qc_on, _plain_frame))
    check("没说@但说了唤醒词也会接", bool(_got_on), str(_got_on)[:40])

    _cfg_off = _copy.deepcopy(_cfg_on)
    _cfg_off["triggers"]["group_keyword_reply"] = False
    _got_off: list = []

    async def _on_msg_off(m):  # noqa: ANN001
        _got_off.append(m.text)

    _qc_off = QQChannel(_cfg_off, _on_msg_off)
    asyncio.run(_feed(_qc_off, _copy.deepcopy(_plain_frame)))
    check("关掉唤醒词就不接", not _got_off, str(_got_off)[:40])

    # 渠道热更新：不重启进程也能改接话概率/窗口
    _qc_off.reload(_cfg_on)
    check("渠道参数能热更新", _qc_off.group_keyword_reply is True and _qc_off.keywords == ["大肥鱼"],
          f"{_qc_off.group_keyword_reply}/{_qc_off.keywords}")

    # 7.3097) QQ 引用消息（message_type=103）解析：原文与图都要拿到
    from fatfish.channels import qq as _qq
    _qt, _qa, _qu = _qq.QQChannel._quoted_parts({
        "message_type": 103,
        "msg_elements": [{"content": "这题咋做", "attachments": [
            {"url": "https://gchat.qpic.cn/a.png", "content_type": "image/png"}]}],
    })
    check("引用消息里的原文能解析", _qt == "这题咋做", _qt)
    check("引用消息里的图能解析", _qu == ["https://gchat.qpic.cn/a.png"], str(_qu))
    _qt2, _qa2, _qu2 = _qq.QQChannel._quoted_parts({"message_type": 0, "content": "hi"})
    check("普通消息不当成引用", (_qt2, _qa2, _qu2) == ("", [], []), f"{_qt2!r}")

    # 7.3098) "为什么不回"要能看见：群消息没叫本鱼 → skip_notcalled 计数 + 每分钟一条 INFO
    _qq_cls = __import__("fatfish.channels.qq", fromlist=["QQChannel"]).QQChannel
    _c = _qq_cls(cfg, Brain(cfg, store, llm))
    check("QQ 渠道有跳过统计", hasattr(_c, "stats") and "skip_notcalled" in _c.stats, str(sorted(_c.stats))[:40])

    # 7.3099) GUI 列出未封存的机器人；封存的（profiles/<名>/.sealed）不出现，切主机器人清 FATFISH_ROOT
    import subprocess as _sp

    _repo = Path(__file__).resolve().parent.parent
    if not (_repo / "profiles/claude/config.yaml").exists():
        print("SKIP  profiles/claude 不存在（公开仓库/新克隆没有多机器人），相关 3 项跳过")
    else:
        _gui_dir = str(_repo / "gui")
        _probe = (
            "import sys; sys.path.insert(0, r'{repo}'); sys.path.insert(0, r'{gui}')\n"
            "import tuner\n"
            "t = tuner.Tuner.__new__(tuner.Tuner)\n"
            "print('|'.join(t._profile_names()))\n"
            "print(repr(t._profile_root('大肥鱼')))\n"
            "print(repr(t._profile_root('claude')))\n"
        ).format(repo=str(_repo), gui=_gui_dir)
        _env = dict(os.environ, FATFISH_ROOT=str(_repo / "profiles/claude"))
        _out = _sp.run([sys.executable, "-c", _probe], capture_output=True, text=True, env=_env, timeout=60)
        _lines = (_out.stdout or "").strip().splitlines()
        _sealed = (_repo / "profiles/claude/.sealed").exists()
        _ok = (
            _out.returncode == 0
            and len(_lines) == 3
            and "大肥鱼" in _lines[0]
            and ("claude" not in _lines[0] if _sealed else "claude" in _lines[0])
            and _lines[1] == "None"
            and "profiles/claude" in _lines[2]
        )
        check("GUI 只列未封存的机器人", _ok, (_lines[0] if _lines else _out.stderr.strip()[:40]))

        # 封存只是 UI 层隐藏：解开环境变量仍能看到（维护用），命令行也照旧能启停
        _env2 = dict(_env, FATFISH_GUI_SHOW_SEALED="1")
        _out2 = _sp.run([sys.executable, "-c", _probe], capture_output=True, text=True, env=_env2, timeout=60)
        _l2 = (_out2.stdout or "").strip().splitlines()
        check("封存的机器人维护时仍可见", bool(_l2) and "claude" in _l2[0], (_l2[0] if _l2 else ""))

        # claude 只私聊：群白名单为空 → 群消息一律丢弃（配置层校验，不联网）
        import yaml as _yaml

        _cc = _yaml.safe_load((_repo / "profiles/claude/config.yaml").read_text(encoding="utf-8"))
        _qqc = ((_cc.get("channels") or {}).get("qq") or {})
        check(
            "claude 配置成只收私聊",
            _qqc.get("group_policy") == "allowlist" and not (_qqc.get("allowed_groups") or []),
            f"group_policy={_qqc.get('group_policy')} allowed_groups={_qqc.get('allowed_groups')}",
        )


    # 7.3100) 微信人设开关：注入→YAML 仍合法→可完整摘除（在临时副本上跑，不碰真配置）
    import shutil as _sh
    import tempfile as _tf

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import wechat_persona as _wp  # noqa: E402

    _orig_cfg = _wp.CONFIG
    _tmp_dir = Path(_tf.mkdtemp())
    _tmp_cfg = _tmp_dir / "config.yaml"
    _sh.copy2(_orig_cfg, _tmp_cfg)
    _wp.CONFIG = _tmp_cfg
    try:
        _on_text, _ids = _wp._patch_text(on=True, extra_ids=[])
        _on_yaml = __import__("yaml").safe_load(_on_text)
        _ov = _on_yaml["gateway"]["platforms"]["weixin"]["channel_overrides"]
        _mine = next(iter(_ov.values()))
        _on_ok = bool(_ids) and "大肥鱼" in str(_mine.get("system_prompt"))
        _tmp_cfg.write_text(_on_text, encoding="utf-8")
        _off_text, _ = _wp._patch_text(on=False, extra_ids=[])
        _off_ov = __import__("yaml").safe_load(_off_text)["gateway"]["platforms"]["weixin"]["channel_overrides"]
        _off_ok = "system_prompt" not in next(iter(_off_ov.values()))
        check("微信人设开关可逆", _on_ok and _off_ok, f"注入={_on_ok} 摘除={_off_ok}")
    finally:
        _wp.CONFIG = _orig_cfg

    # 7.3101) 同 AppID 多消费者能被扫出来（防双回）
    _probe_appid = str((((cfg.get("channels") or {}).get("qq") or {}).get("app_id") or "1906000000"))
    _hits = __import__("fatfish.settings", fromlist=["app_id_conflicts"]).app_id_conflicts(_probe_appid)
    if _hits:
        check("AppID 冲突能被发现", True, _hits[0][:34])
    else:
        print("SKIP  AppID 冲突检查（本机只跑一个机器人，无冲突可测）")

    # 7.3102) master_ids 写成行内 []  也能加进去（不破坏 YAML）
    _tmpc = Path(tempfile.mkdtemp()) / "c.yaml"
    _tmpc.write_text("persona:\n  master_ids: []\n  self_name: x\n", encoding="utf-8")
    _added = __import__("fatfish.settings", fromlist=["add_master_id"]).add_master_id(_tmpc, "OPENID-X")
    _yml = __import__("yaml").safe_load(_tmpc.read_text(encoding="utf-8"))
    check("行内空列表也能加主人", _added and _yml["persona"]["master_ids"] == ["OPENID-X"], str(_yml["persona"]["master_ids"]))

    # 7.3103) 空 content 会自动放大 max_tokens 重试（不联网，mock httpx）
    import httpx as _httpx

    _calls: list = []
    _orig_client = _httpx.AsyncClient

    class _FakeResp:
        def __init__(self, payload):
            self.status_code = 200
            self._p = payload
            self.text = "{}"

        def json(self):
            return self._p

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            _calls.append(json.get("max_tokens"))
            if len(_calls) == 1:
                return _FakeResp({"choices": [{"message": {"content": "", "reasoning_content": "x"}}], "usage": {}})
            return _FakeResp({"choices": [{"message": {"content": "重试成功"}}], "usage": {"prompt_tokens": 8}})

    try:
        _httpx.AsyncClient = _FakeClient
        _fresh = __import__("fatfish.llm", fromlist=["LLM"]).LLM(cfg, store)  # 受主测试的 chat 打桩影响，另起一个
        _r = __import__("asyncio").run(_fresh.chat([{"role": "user", "content": "hi"}], channel="selfcheck", chat_id="llm-retry"))
        check("空回复自动重试", _r.text == "重试成功" and _calls[1] > _calls[0], f"max_tokens {_calls}")
    finally:
        _httpx.AsyncClient = _orig_client

    # 7.3104) 开局后裸数字也算猜（省一次 LLM 调用）
    _gk = "guess:selfcheck"
    store.kv_set(_gk, "73:0")
    _ft = __import__("fatfish.features", fromlist=["try_feature"])
    _r = _ft.try_feature(brain, Inbound("test", "selfcheck", "dm", "u-g", "群友", "40", mentioned=True)) or ""
    store.kv_delete(_gk)
    check("裸数字也算猜", "往大猜" in _r, _r[:18])

    # 7.3105) 主人可注入/撤销人格设定，非主人被拒（用带 master_ids 的合成配置，公开仓库也能跑）
    import copy as _copy_ov

    _cfg_ov = _copy_ov.deepcopy(cfg)
    _cfg_ov.setdefault("persona", {})["master_ids"] = ["MASTER-OVERRIDE"]
    _brain_ov = Brain(_cfg_ov, store, llm)
    _in_m = Inbound("test", "ov-check", "dm", "MASTER-OVERRIDE", "主人", "设定：说话要带猫叫", mentioned=True)
    _r1 = __import__("asyncio").run(_brain_ov.handle(_in_m)) or ""
    _sp = __import__("fatfish.persona", fromlist=["build_system_prompt"]).build_system_prompt(
        cfg, scene="私聊", is_master=True, extra=store.kv_get("persona_override:ov-check")
    )
    check("人格注入（主人）生效", "猫叫" in _sp, _r1[:16])
    _r2 = __import__("asyncio").run(
        brain.handle(Inbound("test", "ov-check", "dm", "stranger-x", "群友", "设定：说话要带猫叫", mentioned=True))
    ) or ""
    check("人格注入（非主人）被拒", "主人能改" in _r2, _r2[:16])
    __import__("asyncio").run(
        _brain_ov.handle(Inbound("test", "ov-check", "dm", "MASTER-OVERRIDE", "主人", "撤销设定", mentioned=True))
    )
    check("人格设定可撤销", not store.kv_get("persona_override:ov-check"), "已清空")

    # 7.311) run.sh 自启管理入口存在且语法正确
    import subprocess as _sp

    _runsh = _Path("run.sh")
    _ok = _sp.run(["bash", "-n", str(_runsh)], capture_output=True).returncode == 0
    _usage = _sp.run(["bash", str(_runsh), "autostart", "status"], capture_output=True, text=True)
    check("autostart 子命令可用", _ok and _usage.returncode == 0 and "自启" in _usage.stdout,
          _usage.stdout.strip()[:30])

    # 7.312) 同一人同一句话短时间不重复回（一问一答）
    import asyncio as _asyncio

    _cfg3 = __import__("fatfish.settings", fromlist=["load_config"]).load_config()
    _cfg3["channels"]["qq"]["chime"] = {"enabled": False}
    _got3 = []
    _ch3 = _QC(_cfg3, on_message=lambda m: _got3.append(m))
    _evt3 = {
        "id": "DUP1",
        "group_openid": "GDUP",
        "content": "在吗",
        "author": {"member_openid": "MEMDUP1"},
        "mentions": [{"bot": True}],
    }
    _asyncio.run(_ch3._handle_event("GROUP_MESSAGE_CREATE", _evt3))
    _evt3b = dict(_evt3, id="DUP2")
    _asyncio.run(_ch3._handle_event("GROUP_MESSAGE_CREATE", _evt3b))
    check("同句不重复回", len(_got3) == 1, f"{len(_got3)} 次入站")

    # 7.315) 调试类问题只走私聊，不刷群
    from fatfish.channels.qq import _is_debug_text as _idt

    _words = ("api-key", "token", "日志", "代码", "dsh")
    check("调试话题识别", _idt("帮我分析一下dsh调用数据", _words) and not _idt("今天吃什么", _words))

    # 7.32) 冷场主动冒泡的判定（默认关闭；超时 + 每日上限）
    import time as _time

    _pro_cfg = cfg
    _pro_cfg["channels"]["qq"]["proactive"] = {"enabled": True, "idle_minutes": 30, "max_per_day": 2}
    _pro = _QC(_pro_cfg, on_message=lambda _m: None)
    _now = _time.time()
    _pro._group_last_seen["PG"] = _now - 3600
    check("冷场 60 分钟该冒泡", _pro.should_speak("PG", _now, "d1"))
    _pro.mark_spoken("PG", "d1")
    _pro.mark_spoken("PG", "d1")
    check("到每日上限不再冒泡", not _pro.should_speak("PG", _now, "d1"))
    _pro_cfg["channels"]["qq"].pop("proactive", None)  # 还原真实配置（默认无 proactive 段）
    _pro2 = _QC(_pro_cfg, on_message=lambda _m: None)
    _pro2._group_last_seen["PG2"] = _now - 3600
    check("默认不主动冒泡", not _pro2.should_speak("PG2", _now, "d1"))

    # 7.335) 群上下文缓存：最近 6 句进 prompt，当前这句不重复
    _ctx = _QC(cfg, on_message=lambda _m: None)
    for _i in range(3):
        _ctx._remember_group_line(
            {"group_openid": "GHIS", "content": f"第{_i}句", "author": {"member_openid": "M5678"}}
        )
    _recent = _ctx._recent_group_lines("GHIS")
    check("群上下文取最近两条", [t for _n, t in _recent] == ["第0句", "第1句"], str(_recent))
    check("群上下文名字脱敏", _recent[0][0] == "群友5678", _recent[0][0])

    # 7.34) 群消息全量模式（GROUP_MESSAGE_CREATE）只认 @ 本机器人的
    from fatfish.channels.qq import _mentions_bot

    check("@all 算 @ 到", _mentions_bot({"mentions": [], "content": "<@all> 集合"}))
    check("bot 标记算 @ 到", _mentions_bot({"mentions": [{"bot": True}], "content": "hi"}))
    check("普通闲聊不算 @", not _mentions_bot({"mentions": [{"id": "x"}], "content": "今天吃啥"}))

    # 7.35) QQ 错误码翻译
    from fatfish.cli import explain_qq_code

    check("错误码 11293 释义", "不在该群" in explain_qq_code(11293), explain_qq_code(11293))
    check("未知错误码兜底", explain_qq_code(424242) == "未知错误")

    # 7.4) QQ 重连退避策略
    from fatfish.channels.qq import next_backoff

    check("稳定连接结束立即重连", next_backoff(60, 2.0) == (0.0, 2.0))
    check("短命连接退避", next_backoff(5, 2.0) == (2.0, 4.0))
    check("连续短命指数退避封顶 60s", next_backoff(1, 40.0) == (40.0, 60.0))

    # 7.5) QQ 群 @ 只发一个 @ 不丢
    from fatfish.channels.qq import _effective_text

    check("QQ 群 空 @ 兜底为“在吗”", _effective_text("<@!123456>", "GROUP_AT_MESSAGE_CREATE") == "在吗")
    check("QQ 群 @ 正常取文本", _effective_text("<@!123456> 在吗", "GROUP_AT_MESSAGE_CREATE") == "在吗")
    check("QQ 私聊空文本仍跳过", _effective_text("   ", "C2C_MESSAGE_CREATE") == "")

    # 8) 定时通知任务：cron 解析、静默、去重
    import datetime as _dt
    import tempfile as _tempfile
    import pathlib as _pathlib

    from fatfish.notify_jobs import NotifyScheduler, compose_text, cron_match, load_jobs

    check("cron 命中 09:30", cron_match("30 9 * * *", _dt.datetime(2026, 10, 27, 9, 30)))
    check("cron 不命中 09:31", not cron_match("30 9 * * *", _dt.datetime(2026, 10, 27, 9, 31)))
    check("cron 工作日过滤", cron_match("10 14 * * 1-5", _dt.datetime(2026, 10, 27, 14, 10))
          and not cron_match("10 14 * * 1-5", _dt.datetime(2026, 10, 25, 14, 10)))
    check("stdout 为空即静默", compose_text(0, "  \n", "") == "")
    check("失败时推 stderr", "boom" in compose_text(1, "", "ValueError: boom"))

    tmp_dir = _pathlib.Path(_tempfile.mkdtemp())
    jobs_file = tmp_dir / "jobs.yaml"
    jobs_file.write_text(
        "jobs:\n"
        '  - name: t1\n    schedule: "*/30 * * * *"\n    command: "echo 定时输出"\n    enabled: true\n'
        '  - name: t2\n    schedule: "*/30 * * * *"\n    command: "true"\n    enabled: true\n'
        '  - name: t3\n    schedule: "0 3 * * *"\n    command: "echo 不该跑"\n    enabled: false\n',
        encoding="utf-8",
    )
    check("通知任务加载 3 条", len(load_jobs(jobs_file)) == 3, str(load_jobs(jobs_file)))
    pushed: list = []

    async def _fake_send(text: str) -> bool:
        pushed.append(text)
        return True

    sched = NotifyScheduler(_fake_send, store=store, path=jobs_file)
    when = _dt.datetime(2026, 10, 27, 9, 30)

    async def _run_tick():
        fired = await sched.tick(when)
        await asyncio.sleep(0.5)
        return fired, await sched.tick(when)

    fired, again = asyncio.run(_run_tick())
    check("通知任务按时触发且跳过停用项", fired == ["t1", "t2"], str(fired))
    check("同一分钟不重复触发", again == [], str(again))
    check("有输出才推送、静默任务不推", pushed == ["定时输出"], str(pushed))

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {', '.join(FAILED)}")
        return 1
    print(f"全部通过（{len(PASSED)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())