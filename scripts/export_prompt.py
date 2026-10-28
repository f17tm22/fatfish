"""把「现在正在用的提示词」打成一个 markdown 包：人格原文 + 拼装后的 system prompt + 知识库 + 关键参数。

用法：
  python3 scripts/export_prompt.py                 # 打进 exports/
  python3 scripts/export_prompt.py --dir /tmp/xx   # 指定目录
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fatfish import persona as persona_mod  # noqa: E402
from fatfish import settings  # noqa: E402


def mask_uid(uid: str) -> str:
    uid = str(uid)
    return f"{uid[:6]}…{uid[-4:]}（{len(uid)} 位）" if len(uid) > 12 else uid


def build(cfg: dict, root: Path, title: str) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p = cfg.get("persona", {}) or {}
    llm = cfg.get("llm", {}) or {}
    budget = cfg.get("budget", {}) or {}
    out: list[str] = []
    add = out.append

    add(f"# {title} · 提示词打包")
    add("")
    add(f"> 导出时间：{now}　来源：`{root}`　模型：`{llm.get('model')}`（thinking={llm.get('thinking')}）")
    add("> 本文件是只读快照，不含任何 API Key。")
    add("")

    add("## 1. 拼装后的 system prompt（群聊场景，主人本人）")
    add("")
    add("```text")
    add(persona_mod.build_system_prompt(cfg, scene="群聊", speaker="晓墨", channel="qq", is_master=True))
    add("```")
    add("")

    add("## 2. 拼装后的 system prompt（私聊场景，非主人）")
    add("")
    add("```text")
    add(persona_mod.build_system_prompt(cfg, scene="私聊", speaker="群友示例", channel="qq", is_master=False))
    add("```")
    add("")

    add("## 3. persona.md 原文")
    add("")
    add("```markdown")
    add(persona_mod.read_persona())
    add("```")
    add("")

    work = persona_mod.read_work_prompts(cfg)
    add("## 4. 工作手册同步段（work_prompt_files）")
    add("")
    if work:
        add("```markdown")
        add(work)
        add("```")
    else:
        add("（未配置 `persona.work_prompt_files`，这一段为空）")
    add("")

    add("## 5. 知识库（knowledge/*.md，按话题注入）")
    add("")
    add("> 下面列出全部知识文件原文；运行时并不会整包塞进提示词——`fatfish/knowledge.py` 只在话题命中时把那一段拼进 system。")
    add("")
    kdir = root / "knowledge"
    files = sorted(kdir.glob("*.md")) if kdir.is_dir() else []
    if not files:
        add("（没有知识库文件）")
    for f in files:
        add(f"### {f.name}")
        add("")
        add("```markdown")
        add(f.read_text(encoding="utf-8").strip())
        add("```")
        add("")

    add("## 6. 影响语气的参数")
    add("")
    add("| 参数 | 值 |")
    add("|---|---|")
    for key in ("self_name", "master_name", "max_reply_chars", "context_messages",
                "cooldown_seconds", "dm_cooldown_seconds", "max_replies_per_minute", "interrupt_prob"):
        add(f"| persona.{key} | `{p.get(key)}` |")
    add(f"| llm.max_tokens | `{llm.get('max_tokens')}` |")
    add(f"| llm.temperature | `{llm.get('temperature')}` |")
    add(f"| budget.daily_cap_cny | `{budget.get('daily_cap_cny')}` |")
    add("")

    add("## 7. 成员称呼表（openid 已打码）")
    add("")
    names = ((cfg.get("channels") or {}).get("qq") or {}).get("member_names") or {}
    if names:
        add("| openid | 名字 |")
        add("|---|---|")
        for uid, name in names.items():
            add(f"| {mask_uid(uid)} | {name} |")
    else:
        add("（还没配置 member_names）")
    add("")

    add("## 8. 运行时拼装顺序（代码视角）")
    add("")
    add("1. `fatfish/persona.py::read_persona()` —— 读 `persona.md` 全文；")
    add("2. `read_work_prompts()` —— 按 `persona.work_prompt_files` 附工作手册，带冲突收敛句；")
    add("3. `## 运行时` —— 当前北京时间、场景（群聊/私聊）、对话对象、心情轮换、说话方式轮换、本轮编号；")
    add("4. `## 回复格式（硬规则）` —— 字数、句式轮换、语言、禁止臆造时间线等；")
    add("5. 主人追加设定（`设定：…` 写进 kv，运行时以 `## 主人追加的设定` 追加）；")
    add("6. 知识库按话题命中后作为 system 前缀一起发送（`fatfish/knowledge.py`）。")
    add("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ROOT / "exports"))
    ap.add_argument("--no-claude", action="store_true")
    args = ap.parse_args()

    outdir = Path(args.dir)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")

    cfg = settings.load_config()
    main_txt = build(cfg, ROOT, "大肥鱼（fatfish 主机器人）")
    f1 = outdir / f"提示词打包-大肥鱼-{stamp}.md"
    f1.write_text(main_txt, encoding="utf-8")
    print(f1)

    if not args.no_claude:
        croot = ROOT / "profiles" / "claude"
        if (croot / "config.yaml").exists():
            old_root = settings.ROOT
            try:
                settings.ROOT = croot
                settings.CONFIG_PATH = croot / "config.yaml"
                settings.PERSONA_PATH = croot / "persona.md"
                c_cfg = settings.load_config()
                c_txt = build(c_cfg, croot, "claude 大小姐（profiles/claude）")
                f2 = outdir / f"提示词打包-claude-{stamp}.md"
                f2.write_text(c_txt, encoding="utf-8")
                print(f2)
            finally:
                settings.ROOT = old_root
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
