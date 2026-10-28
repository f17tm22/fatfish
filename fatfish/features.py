"""水群小功能：模板回复，零 token 花费（不走 LLM）。"""

from __future__ import annotations

import hashlib
import random
import re as _re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

from . import affinity as af
from . import names as nm

if TYPE_CHECKING:  # pragma: no cover
    from .brain import Brain
    from .channels.base import Inbound

FORTUNES = [
    ("大吉", "今天运气好到尾巴打结，主人想做的事都会顺！"),
    ("中吉", "平平淡淡才是真，记得给本鱼喂饭，运气+1。"),
    ("小吉", "有一点点小惊喜，多半是有人偷偷夸你。"),
    ("吉", "稳的，适合摸鱼，也适合把作业写完（不然本鱼要念你）。"),
    ("末吉", "小事会有点卡壳，甩甩尾就过去了。"),
    ("凶", "今天别乱说话……但跟本鱼聊天是安全的。"),
]

FEED_LINES = [
    "呜哇是米饭！本鱼最爱主人了（甩尾）",
    "才、才不是馋了呢，只是刚好有空吃。",
    "token 也一起喂进来嘛，本鱼要边吃边算。",
    "这碗饭本鱼记下了，下次还找你。",
]

HUNGRY_LINES = [
    "饿了……但本鱼是鲸鱼，饿得很优雅。",
    "饿到尾巴都软了，主人给点米饭嘛。",
    "刚吃完，别催，本鱼要慢慢嚼。",
]

PET_LINES = [
    "别、别摸头啦，会变笨的……（没躲开）",
    "唔……再摸一下也不是不行。",
    "（尾巴轻轻拍了拍你的手）",
]

PRAISE_LINES = [
    "你、你眼光不错嘛，本鱼勉强算可爱吧。",
    "哼，夸得这么好听，是不是想让本鱼帮你干活。",
]

SCOLD_LINES = [
    "凶什么凶嘛，本鱼又没偷吃你的 token。",
    "（缩成一团）好啦好啦，你说得都对。",
]

ACT_LINES = [
    "哼，要撒娇也不是不行……（尾巴卷了个圈）",
    "人家今天心情不错，勉强给你看一眼肚皮。",
    "（拍了拍你的手）别光看，米饭呢。",
]

HELP_LINES = (
    "本鱼会：抽签、签到、喂饭、喂饭榜、好感度、好感规则、好感榜、贴贴、夸我、骂我、骰子、猜数字、"
    "群报、心情、复读 话、几点；主人还能问余额、重置对话。"
)

DICE_LINES = [
    "骰子滚了滚，停在 {n} 点（甩尾）。",
    "本鱼摇出 {n} 点，就这运气了。",
    "{n} 点。别盯着看，本鱼没作弊。",
]

GUESS_HINT_LOW = "太小了，往大猜（尾巴敲了敲 {n}）。"
GUESS_HINT_HIGH = "太大了，往小猜（尾巴敲了敲 {n}）。"


def _hash_pick(items, seed: str):
    idx = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % len(items)
    return items[idx]


def _day(cfg) -> str:
    tz = ZoneInfo((cfg.get("budget", {}) or {}).get("timezone", "Asia/Shanghai"))
    return datetime.now(tz).strftime("%Y-%m-%d")


def _now_str(cfg) -> str:
    tz = ZoneInfo((cfg.get("budget", {}) or {}).get("timezone", "Asia/Shanghai"))
    return datetime.now(tz).strftime("%H:%M")


def _hunger(brain: "Brain", chat_id: str, uid: str) -> int:
    try:
        return int(brain.store.kv_get(f"hunger:{chat_id}:{uid}", "0") or 0)
    except Exception:
        return 0


def try_feature(brain: "Brain", inbound: "Inbound") -> Optional[str]:
    cfg = brain.cfg
    features = cfg.get("features", {}) or {}
    text = inbound.text.strip()
    lower = text.lower()
    uid = inbound.user_id or inbound.chat_id
    aff_on = bool(features.get("affinity", True))

    def aff(event: str, times: int = 1) -> str:
        """记一次好感事件并返回可以接在回复尾巴上的那句；关掉功能就什么都不做。"""
        if not aff_on:
            return ""
        try:
            return af.suffix(af.apply_event(brain.store, inbound.chat_id, uid, event, _day(cfg), times=times))
        except Exception:  # noqa: BLE001 - 好感度坏了不能拖垮整条回复
            return ""

    def aff_score() -> int:
        if not aff_on:
            return 0
        return af.score_of(brain.store, inbound.chat_id, uid)

    def has(*words: str) -> bool:
        return any(w.lower() in lower for w in words)

    # 「怎么用」太泛（「虚功原理怎么用」会被误判成问功能），只认明确的求助说法
    if features.get("help", True) and has(
        "帮助", "有什么功能", "会什么功能", "你会什么", "怎么玩", "功能列表"
    ):
        return HELP_LINES

    auto = features.get("auto_replies") or []
    if features.get("auto_reply", True) and auto and len(text) <= 24:
        for item in auto:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            reply = str(item.get("reply") or "").strip()
            if key and reply and key.lower() in lower:
                return reply

    # 主人点人：说「6289 是小明」/「绑定 小明 6289」，等他下次开口自动认领
    if brain.is_master(inbound):
        m_bind = (
            _re.match(r"^绑定\s*([\w\u4e00-\u9fa5]{1,12})\s*([A-Za-z0-9]{4,32})$", text)
            or _re.match(r"^([A-Za-z0-9]{4,32})\s*(?:是|=|＝)\s*([\w\u4e00-\u9fa5]{1,12})$", text)
        )
        if m_bind:
            g1, g2 = m_bind.group(1).strip(), m_bind.group(2).strip()
            suffix, want = (g2, g1) if g1.isalnum() and not g2.isalnum() else (g1, g2)
            if len(suffix) >= 4 and suffix.isalnum():
                nm.set_pending(brain.store, suffix, want)
                return f"好，等 openid 尾号 {suffix.upper()} 的人下次说话，本鱼就叫他{want}。"

    # 「我是谁」要在自报名字之前判，不然「我是谁」会被当成自报名字「谁」
    if has("我叫什么", "我是谁", "认得我吗", "知道我是谁"):
        return f"你是{nm.resolve(brain.store, cfg, uid)}呀，本鱼记着呢（甩尾）。"

    # 自报名字：本人说一句「我是 小红」就把 openid 和名字绑死
    m_name = _re.match(r"^(?:我是|我叫|叫我)\s*([\w\u4e00-\u9fa5]{1,12})$", text)
    if m_name and m_name.group(1) not in ("谁", "什么", "哪个", "哪位"):
        want = m_name.group(1).strip()
        owner = nm.owner_of(brain.store, cfg, want)
        if owner and owner != uid:
            return f"「{want}」已经有人用了，换一个吧（尾巴一甩）。"
        nm.bind(brain.store, uid, want)
        return f"记住了，以后叫你{want}。"

    if has("小肥") and features.get("affinity", True):
        if aff_score() >= 300:
            return "……嗯，只准你一个人这么叫（尾巴缠上来一下）。"
        return "谁准你这么叫的，本鱼跟你有那么熟吗（尾巴一甩）。" + aff("fat_name")

    if has("胖鱼", "死鱼", "肥鱼"):
        aff("fat_name")  # 扣分但不出声，让模型按人格怼回去

    if features.get("affinity", True) and (
        has("好感规则", "好感度规则", "好感说明", "好感玩法", "怎么涨好感", "好感怎么加", "好感怎么算")
    ):
        return af.RULES_TEXT

    if features.get("affinity", True) and has("好感榜", "亲密度榜", "好感排行", "谁最喜欢你"):
        return af.rank_text(
            af.board(brain.store, inbound.chat_id, 5),
            name_of=lambda u: nm.resolve(brain.store, cfg, u),
        )

    if features.get("affinity", True) and has("好感度", "好感", "亲密度", "友好度"):
        score = aff_score()
        _idx, name, note = af.level_for(score)
        gap, nxt = af.next_gap(score)
        try:
            used = int(brain.store.kv_get(af.day_key(inbound.chat_id, uid, _day(cfg)), "0") or 0)
        except (TypeError, ValueError):
            used = 0
        tail = f"，再涨 {gap} 分就到「{nxt}」" if gap else "，已经是本鱼的心尖尖了"
        return (
            f"本鱼对你的好感：「{name}」{score} 分{tail}。"
            f"今天涨了 {used}/{af.DAILY_GAIN_CAP} 分（{note}）"
        )

    if features.get("balance", True) and has("余额", "花了多少", "今天花了", "预算", "cost"):
        if brain.is_master(inbound):
            spent = brain.llm.spent_today()
            cap = brain.llm.cap()
            return f"今天花了 {spent:.3f} 元，预算 {cap:.2f} 元，还剩 {max(0.0, cap - spent):.3f} 元（{_now_str(cfg)}）。"
        return "余额是本鱼和主人的小秘密，不给你看（甩尾）。"

    _ov_key = f"persona_override:{inbound.chat_id}"
    if has("设定：", "设定:", "注入人格", "人格设定", "加一句设定", "换个人格"):
        if not brain.is_master(inbound):
            return "人格只有主人能改，你说了不算（尾巴一甩）。"
        body = text.split("：", 1)[-1].split(":", 1)[-1].strip() if ("：" in text or ":" in text) else ""
        if not body:
            return "要设定什么？格式：设定：你是……"
        old = brain.store.kv_get(_ov_key) or ""
        brain.store.kv_set(_ov_key, (old + "\n" + body).strip() if old else body)
        return "记住了，本鱼现在就按这个来。"

    if has("撤销设定", "清除设定", "恢复人格", "忘掉设定"):
        if not brain.is_master(inbound):
            return "人格只有主人能改（尾巴一甩）。"
        brain.store.kv_delete(_ov_key)
        return "设定清掉了，本鱼回到出厂性格。"

    if has("查看设定", "当前设定", "你的人格"):
        ov = brain.store.kv_get(_ov_key) or ""
        return f"现在追加的设定是：{ov}" if ov else "没有追加设定，本鱼就是出厂的那只。"

    if has("重置对话", "清空记忆", "忘掉刚才"):
        if not brain.is_master(inbound):
            return "记忆是主人的权限，你别乱动（尾巴一甩）。"
        brain.store.clear_history(inbound.chat_id)
        return "（抖了抖尾巴）刚才聊的都忘掉啦，重新开始。"

    if features.get("fortune", True) and has("抽签", "运势", "算卦", "今天运气", "运气好吗"):
        seed = f"{uid}:{_day(cfg)}"
        pick = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % len(FORTUNES)
        score = aff_score()
        # 熟人(30+)起步的彩蛋：抽到差签有概率往上抬一档，只抬一档，不破坏随机性
        chance = 50 if score >= 150 else 34
        if score >= 30 and pick >= 3 and int(hashlib.md5((seed + "up").encode()).hexdigest(), 16) % 100 < chance:
            pick -= 1
        level, line = FORTUNES[pick]
        bonus = aff("fortune_good") if level in ("大吉", "中吉") else ""
        return f"【{level}】{line}{bonus}"

    if features.get("feed", True) and has("喂饭榜", "投喂榜", "排行榜", "谁喂得多"):
        board = {}
        for key, val in brain.store.kv_prefix(f"hunger:{inbound.chat_id}:").items():
            uid = key.rsplit(":", 1)[-1]
            try:
                count = int(val)
            except Exception:  # noqa: BLE001
                continue
            if count > 0:
                board[uid] = count
        if not board:
            return "还没人喂过本鱼，榜是空的（尾巴垂下去）。"
        top = sorted(board.items(), key=lambda kv: kv[1], reverse=True)[:3]
        lines = [f"{i + 1}. {nm.resolve(brain.store, cfg, uid)} {cnt} 碗" for i, (uid, cnt) in enumerate(top)]
        return "喂饭榜：" + "；".join(lines)

    if features.get("checkin", True) and has("签到", "打卡"):
        key = f"checkin:{inbound.chat_id}:{uid}:{_day(cfg)}"
        if brain.store.kv_get(key):
            return "今天已经签过了，别想多领一碗（尾巴啪一下）。"
        brain.store.kv_set(key, "1")
        _score = aff_score()
        bowls = 3 if _score >= 300 else (2 if _score >= 70 else 1)  # 朋友多一碗，本鱼的菜三碗
        hunger = _hunger(brain, inbound.chat_id, uid) + bowls
        brain.store.kv_set(f"hunger:{inbound.chat_id}:{uid}", str(hunger))
        return f"签到成功，赏你{bowls}碗饭记在本鱼账上（今日第{hunger}碗）。{aff('checkin')}"

    if features.get("feed", True) and has("喂饭", "投喂", "给你吃", "吃米饭", "开饭", "喂我", "吃鱼粮"):
        # 支持「喂饭 3」这种带参数的形式（后台指令面板可传参），一次最多 5 碗
        m_feed = _re.match(r"^喂饭\s*(\d{1,2})\s*[碗]?$", text)
        bowls = max(0, min(int(m_feed.group(1)), 5)) if m_feed else 1
        if m_feed and bowls == 0:
            return "喂 0 碗是想饿死本鱼吗（尾巴抽你）。"
        hunger = _hunger(brain, inbound.chat_id, uid) + bowls
        brain.store.kv_set(f"hunger:{inbound.chat_id}:{uid}", str(hunger))
        return random.choice(FEED_LINES) + f"（今日第{hunger}碗）{aff('feed', bowls)}"

    if features.get("feed", True) and has("饿了吗", "你饿", "吃了吗", "几碗饭", "吃饭没"):
        return f"今天吃了 {_hunger(brain, inbound.chat_id, uid)} 碗饭。" + random.choice(HUNGRY_LINES)

    if features.get("pet", True) and has("贴贴", "摸摸", "摸头", "揉头", "撸鱼", "抱抱", "贴一下"):
        line = random.choice(PET_LINES)
        if aff_score() >= 300:
            line += "（只对你这样）"
        return line + aff("pet")

    if has("夸我", "夸夸我"):
        return random.choice(PRAISE_LINES) + aff("praise")

    if has("骂我", "凶我"):
        return random.choice(SCOLD_LINES) + aff("scold")

    if features.get("repeat", True) and lower.startswith("复读"):
        rest = text[2:].strip(" :：")
        return (rest or "（复读中）")[: brain.cfg.get("persona", {}).get("max_reply_chars", 120)]

    if has("心情", "状态怎么样", "你好吗"):
        if brain.is_master(inbound):
            return f"本鱼心情不错。模型 {brain.llm.model}，今天花了 {brain.llm.spent_today():.3f} 元。"
        return "本鱼心情不错，就是有点饿（甩尾）。"

    if features.get("dice", True) and has("骰子", "掷骰", "摇个骰"):
        return random.choice(DICE_LINES).format(n=random.randint(1, 6))

    if features.get("guess", True):
        guess_key = f"guess:{inbound.chat_id}"
        m = _re.match(r"^猜\s*(\d{1,3})$", text)
        if not m and brain.store.kv_get(guess_key) and _re.fullmatch(r"\d{1,3}", text.strip()):
            m = _re.fullmatch(r"(\d{1,3})", text.strip())  # 开局后直接报数字也算猜
        if m:
            state = brain.store.kv_get(guess_key)
            if not state:
                return "还没开局呢，先说「猜数字」给个范围。"
            target, tries = (state.split(":") + ["0"])[:2]
            tries = int(tries) + 1
            n = int(m.group(1))
            target = int(target)
            if n == target:
                brain.store.kv_set(guess_key, "")
                return f"中了！就是 {target}，用了 {tries} 次，赏你一碗饭。{aff('guess_win')}"
            brain.store.kv_set(guess_key, f"{target}:{tries}")
            hint = GUESS_HINT_LOW if n < target else GUESS_HINT_HIGH
            return hint.format(n=tries) + ("（再猜不出来本鱼要去吃饭了）" if tries >= 6 else "")
        if has("猜数字", "猜个数", "玩猜数"):
            target = random.randint(1, 100)
            brain.store.kv_set(guess_key, f"{target}:0")
            return "1 到 100，本鱼心里有个数了，说「猜 42」这样猜。"
        if has("放弃", "不猜了") and brain.store.kv_get(guess_key):
            brain.store.kv_set(guess_key, "")
            return "行吧，这局作废（尾巴一甩）。"

    if features.get("group_report", True) and has("群报", "群里在聊", "今天群里"):
        ambient = list((inbound.reply_ctx or {}).get("ambient") or [])
        if not ambient:
            return "本鱼还没攒够记录，等群里多聊两句再说。"
        tally: Dict[str, int] = {}
        for line in ambient:
            who = str(line).split("：")[0]
            tally[who] = tally.get(who, 0) + 1
        top = sorted(tally.items(), key=lambda kv: -kv[1])[:2]
        who_txt = "、".join(f"{k} {v} 句" for k, v in top)
        return f"最近 {len(ambient)} 句里：{who_txt}。本鱼最爱听你们唠。"

    if has("撒娇", "卖个萌", "可爱一下"):
        return random.choice(ACT_LINES)

    if has("几点", "现在时间", "几点了"):
        return f"现在 {_now_str(cfg)}，本鱼提醒你：别摸鱼太久。"

    return None