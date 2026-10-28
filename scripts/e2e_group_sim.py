"""QQ 群消息端到端模拟：假 WS 帧 → 真渠道 → 真大脑 → 断言出站 POST。

用途：平台还没把群 @ 推过来时，先证明「一旦来了一条群 @，回复一定能正确发出」。
不发真实网络请求（token/网关/发送全部打桩），不花 token（用 0 token 模板功能）。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fatfish import settings  # noqa: E402
from fatfish.channels import qq as qq_mod  # noqa: E402
from fatfish.brain import Brain  # noqa: E402
from fatfish.channels.qq import QQChannel  # noqa: E402
from fatfish.llm import LLM  # noqa: E402
from fatfish.store import Store  # noqa: E402

import tempfile  # noqa: E402

SIM_DB = Path(tempfile.mkdtemp()) / "sim.sqlite3"  # 别把模拟消息写进真实聊天记录

POSTS: list[dict] = []
qq_mod.remember_group = lambda *a, **k: None  # 模拟不污染真实 data/qq/groups.json


class FakeResp:
    def __init__(self, code: int = 200) -> None:
        self.status_code = code
        self.text = ""


class FakeHTTP:
    async def post(self, url, headers=None, json=None, **kw):  # noqa: A002
        POSTS.append({"url": url, "body": json or {}})
        return FakeResp(200)

    async def aclose(self) -> None:
        return None


RUN = uuid.uuid4().hex[:6]
BOT_ID = "4016688322"  # 每次跑用独立会话，避开 10s 冷却导致的假失败


def group_frame(chat="大肥鱼水群", text="大肥鱼抽签", msg_id="SIM1", group=None, member=None):
    group = group or f"GRP-SIM-{RUN}"
    member = member or f"MEM-SIM-{RUN}"
    return {
        "op": 0,
        "s": 1,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "id": msg_id,
            "content": f"<@!{BOT_ID}> {text}",
            "group_openid": group,
            "author": {"member_openid": member, "username": chat},
        },
    }


async def main() -> int:
    cfg = settings.load_config()
    store = Store(path=str(SIM_DB))
    llm = LLM(cfg, store)
    brain = Brain(cfg, store, llm)

    ch = QQChannel(cfg, brain.handle)
    ch._token, ch._token_exp = "sim-token", 10 ** 12
    ch._http = FakeHTTP()

    before = store.spend_day()

    async def feed(frame: dict, expect_new: int = 1) -> None:
        """真实链路是 async task 处理事件，等它真跑完（出站条数达标）再断言。"""
        want = len(POSTS) + expect_new
        await ch._dispatch_ws(frame)
        for _ in range(40):
            await asyncio.sleep(0.05)
            if len(POSTS) >= want:
                break

    await feed(group_frame())
    reply_post = POSTS[-1] if POSTS else {}

    checks = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    ck("出站有且只有一条", len(POSTS) == 1, f"posts={len(POSTS)}")
    ck("走群通道 URL", str(reply_post.get("url", "")).endswith(f"/v2/groups/GRP-SIM-{RUN}/messages"),
       str(reply_post.get("url", ""))[-40:])
    body = reply_post.get("body") or {}
    ck("带 msg_id 被动回复", body.get("msg_id") == "SIM1", str(body.get("msg_id")))
    ck("带 msg_seq", isinstance(body.get("msg_seq"), int), str(body.get("msg_seq")))
    ck("msg_type=0 文本", body.get("msg_type") == 0, str(body.get("msg_type")))
    ck("回复内容非空", bool(str(body.get("content") or "").strip()), str(body.get("content"))[:20])
    ck("0 token 功能不花钱", abs(store.spend_day() - before) < 1e-9, f"{before:.4f}->{store.spend_day():.4f}")

    # 重复帧应被去重，不再发第二条
    await feed(group_frame(), expect_new=0)
    ck("重复 msg_id 被去重", len(POSTS) == 1, f"posts={len(POSTS)}")

    # 引用消息（message_type=103）：只引用没自己打字，也要接话，并把被引用原文带进去
    quote_frame = group_frame(text="", msg_id="SIMQ1", group=f"GRP-SIMQ-{RUN}")
    quote_frame["d"]["message_type"] = 103
    quote_frame["d"]["msg_elements"] = [{"content": "刚才那张图是啥", "attachments": []}]
    await feed(quote_frame)
    ck("引用消息也能接话", any((x.get("body") or {}).get("msg_id") == "SIMQ1" for x in POSTS),
       f"posts={len(POSTS)}")

    # 引用里带图：被引用的图片要一起下载给模型看
    _seen_urls: list = []
    _orig_payloads = ch._image_payloads

    async def _spy_payloads(urls):
        _seen_urls.extend(urls)
        return ["data:image/png;base64,QUJD"]

    ch._image_payloads = _spy_payloads
    qimg_frame = group_frame(text="这是啥", msg_id="SIMQ2", group=f"GRP-SIMQ2-{RUN}")
    qimg_frame["d"]["message_type"] = 103
    qimg_frame["d"]["msg_elements"] = [
        {"content": "", "attachments": [{"url": "https://gchat.qpic.cn/quoted/sim.png", "content_type": "image/png"}]}
    ]
    await feed(qimg_frame)
    ck("引用里的图片被下载", any("quoted/sim.png" in u for u in _seen_urls), str(_seen_urls)[:60])
    ck("引图那条也回了一条", any((x.get("body") or {}).get("msg_id") == "SIMQ2" for x in POSTS),
       f"posts={len(POSTS)}")
    ch._image_payloads = _orig_payloads

    # 换一个只有 @ 没有正文的帧，应兜底成“在吗”并回复
    # 换一个群（新会话）避开群冷却，验证「只 @ 不打字」也兜底回复
    await feed(group_frame(text="", msg_id="SIM2", group=f"GRP-SIM2-{RUN}"))
    ck("空 @ 也有回复", any((x.get("body") or {}).get("msg_id") == "SIM2" for x in POSTS),
       f"posts={len(POSTS)}")

    width = max(len(n) for n, _, _ in checks)
    bad = 0
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
        bad += 0 if ok else 1
    print(f"\n{len(checks) - bad}/{len(checks)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))