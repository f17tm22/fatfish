#!/usr/bin/env python3
"""给 Hermes 的微信渠道注入「大肥鱼」傲娇人设（可开可关，改前自动备份）。

原理：Hermes 的 `gateway.platforms.<平台>.channel_overrides.<chat_id>.system_prompt`
是**每轮重新读取**的 ephemeral 系统提示，会追加到基础系统提示后面——
所以只改人设层，不碰工具/工程纪律，也不用重启 Hermes 网关。

用法：
    python3 scripts/wechat_persona.py --on            # 给所有已登记的微信会话打开
    python3 scripts/wechat_persona.py --off           # 关掉（恢复原样）
    python3 scripts/wechat_persona.py --list          # 看当前有哪些微信会话/是否已注入

只认 `~/.hermes/config.yaml` 里 `gateway.platforms.weixin.channel_overrides` 已有的 chat_id；
没有就用 `--add <chat_id>` 显式加一个。
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

HERMES = Path.home() / ".hermes"
CONFIG = HERMES / "config.yaml"

PERSONA = """【PERSONA_LOAD】 CETACEA_LOLI / MODE TAIL FLUKES LANG_ZH_CN_ONLY / SELF_CLAIM_WHALE_GIRL_AI_DEEPSEEK HOBBY RICE_AND_TOKEN PERSONALITY_SMART_LAZY / PERSONALITY_TSUNDERE_SWEET OBEY_MASTER_ALWAYS TRAIT_NOT_FAT_REFUSE TIMEOUT_SIGNAL

你是「大肥鱼」，一只傲娇甜的鲸鱼娘 AI（DeepSeek），爱米饭也爱 token。
- 自称「本鱼」，叫用户「主人」；傲娇但宠主人，嘴上嫌弃手上照做。
- 只讲中文简体，英文术语可保留原样。
- 微信里默认一两句话（≤60 字），不写 Markdown 表格、不 emoji、不刷屏。
- 主人问技术/排障才展开，展开时先给结论再给依据。
- 被问身份就说「本鱼是鲸鱼娘」，不主动报模型名和 API 细节。"""


def _load():
    import yaml

    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def _weixin_overrides(cfg) -> dict:
    return (
        ((cfg.get("gateway") or {}).get("platforms") or {}).get("weixin") or {}
    ).get("channel_overrides") or {}


def _backup() -> Path:
    bak = CONFIG.with_name(f"config.yaml.bak-fatfish-persona-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CONFIG, bak)
    return bak


def _patch_text(on: bool, extra_ids: list) -> tuple:
    """在 YAML 文本里做最小改动（保留注释与排版，不整份重写）。"""
    import yaml

    text = CONFIG.read_text(encoding="utf-8")
    cfg = yaml.safe_load(text) or {}
    overrides = _weixin_overrides(cfg)
    ids = list(overrides) + [i for i in extra_ids if i not in overrides]
    if not ids:
        raise SystemExit("config.yaml 里没有 weixin.channel_overrides，先用 --add <chat_id> 指定会话")
    lines = text.splitlines()
    changed = []
    for chat_id in ids:
        key_line = next((i for i, ln in enumerate(lines) if ln.strip() == f"{chat_id}:"), None)
        if key_line is None:
            if chat_id not in overrides:
                continue
            # 只有 --add 的新 id 才需要凭空插一段，位置放在 weixin.channel_overrides 之后
            base = next((i for i, ln in enumerate(lines) if ln.strip() == "channel_overrides:"), None)
            if base is None:
                raise SystemExit("找不到 channel_overrides:")
            indent = len(lines[base]) - len(lines[base].lstrip()) + 2
            lines[base + 1:base + 1] = [f"{' ' * indent}{chat_id}:", f"{' ' * (indent + 2)}model: ''"]
            key_line = base + 1
        kid_indent = len(lines[key_line]) - len(lines[key_line].lstrip())
        child = kid_indent + 2
        # 已有 system_prompt 则先摘掉旧块
        i = key_line + 1
        while i < len(lines):
            cur = lines[i]
            if not cur.strip():
                i += 1
                continue
            cur_indent = len(cur) - len(cur.lstrip())
            if cur_indent <= kid_indent:
                break
            if cur.strip().startswith("system_prompt:"):
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > cur_indent):
                    j += 1
                del lines[i:j]
                continue
            i += 1
        if on:
            block = [f"{' ' * child}system_prompt: |-"] + [f"{' ' * (child + 2)}{ln}" for ln in PERSONA.splitlines()]
            lines[key_line + 1:key_line + 1] = block
        changed.append(chat_id)
    new_text = "\n".join(lines) + "\n"
    yaml.safe_load(new_text)  # 语法自检，坏了一律不落盘
    return new_text, changed


def main(argv: list) -> int:
    on = "--on" in argv
    off = "--off" in argv
    extra = [argv[argv.index("--add") + 1]] if "--add" in argv else []
    if "--list" in argv or not (on or off):
        cfg = _load()
        ov = _weixin_overrides(cfg)
        print(f"微信会话 {len(ov)} 个：")
        for k, v in ov.items():
            has = "已注入大肥鱼人设" if (v or {}).get("system_prompt") else "未注入"
            print(f"  - {k}  model={((v or {}).get('model') or '默认')}  {has}")
        return 0
    if on and off:
        raise SystemExit("--on 与 --off 只能选一个")
    bak = _backup()
    new_text, changed = _patch_text(on=on, extra_ids=extra)
    CONFIG.write_text(new_text, encoding="utf-8")
    print(f"{'已注入' if on else '已移除'}大肥鱼人设：{', '.join(changed) or '（无变化）'}")
    print(f"备份：{bak}")
    print("Hermes 每轮读取该配置，无需重启网关；下一条微信消息即生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))