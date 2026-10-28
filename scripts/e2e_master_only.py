"""验证 group_reply=master_only：群里只有主人的 @ 会被回应，其余一切沉默。"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fatfish import settings  # noqa: E402
from fatfish.brain import Brain  # noqa: E402
from fatfish.channels.qq import QQChannel  # noqa: E402
from fatfish.llm import LLM  # noqa: E402
from fatfish.store import Store  # noqa: E402

POSTS = []


class FakeResp:
    status_code = 200

    async def json(self):
        return {}

    async def text(self):
        return ""


class FakeHTTP:
    async def post(self, url, headers=None, json=None, **kw):  # noqa: A002
        POSTS.append({"url": url, "body": json or {}})
        return FakeResp()

    async def aclose(self):
        return None


RUN = uuid.uuid4().hex[:6]
BOT_ID = "4016688322"


def frame(member: str, text: str, group: str, msg_id: str, event: str = "GROUP_AT_MESSAGE_CREATE"):
    content = f"<@!{BOT_ID}> {text}" if event.endswith("AT_MESSAGE_CREATE") else text
    return {
        "op": 0,
        "s": 1,
        "t": event,
        "d": {"id": msg_id, "content": content, "group_openid": group,
              "author": {"member_openid": member, "username": "群友"}},
    }


async def main() -> int:
    cfg = settings.load_config()
    qq = ((cfg.get("channels") or {}).get("qq") or {})
    masters = [str(x) for x in (qq.get("master_members") or [])]
    groups = [str(x) for x in (qq.get("allowed_groups") or [])]
    if not masters or not groups:
        print("config 里还没有 master_members / allowed_groups，先认主再跑")
        return 1
    master, group = masters[0], groups[0]
    stranger = f"STR-{RUN}"

    store = Store()
    ch = QQChannel(cfg, Brain(cfg, store, LLM(cfg, store)).handle)
    ch._token, ch._token_exp = "sim-token", 10 ** 12
    ch._http = FakeHTTP()

    async def feed(fr: dict, wait: float = 0.6) -> None:
        await ch._dispatch_ws(fr)
        await asyncio.sleep(wait)

    checks = []

    def ck(name, ok, detail=""):
        checks.append((name, ok, detail))

    await feed(frame(stranger, "抽签", group, f"S1-{RUN}"))
    ck("陌生人 @ 被沉默", len(POSTS) == 0, f"posts={len(POSTS)}")

    await feed(frame(stranger, "抽签", f"OTHER-{RUN}", f"S2-{RUN}"))
    ck("别的群也不理", len(POSTS) == 0, f"posts={len(POSTS)}")

    await feed(frame(master, "说说你们在聊啥", group, f"S3-{RUN}", event="GROUP_MESSAGE_CREATE"))
    ck("主人不 @ 也不插话", len(POSTS) == 0, f"posts={len(POSTS)}")

    await feed(frame(master, "抽签", group, f"S4-{RUN}"))
    body = (POSTS[-1].get("body") or {}) if POSTS else {}
    ck("主人 @ 有回复", len(POSTS) == 1, f"posts={len(POSTS)}")
    ck("回复走群通道", str((POSTS[-1] if POSTS else {}).get("url", "")).endswith(f"/v2/groups/{group}/messages"))
    ck("内容是签文", "【" in str(body.get("content") or ""), str(body.get("content"))[:20])

    width = max(len(n) for n, _, _ in checks)
    bad = 0
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
        bad += 0 if ok else 1
    print(f"\n{len(checks) - bad}/{len(checks)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
