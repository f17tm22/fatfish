"""新建一个机器人 profile：./run.sh init claude --app-id 1906xxxxxx --secret xxx

profile = 独立的一套 config.yaml / persona.md / .env / data / logs，
用 ./run.sh --profile <名字> start 跑起来，跟主机器人互不干扰（各自的 PID、账本、群登记）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--app-id", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--persona", default="", help="人格文件路径，默认用占位人格")
    args = ap.parse_args()

    root = REPO / "profiles" / args.name
    if root.exists():
        print(f"{root} 已存在，先手动处理掉再建")
        return 2
    for sub in ("data", "logs"):
        (root / sub).mkdir(parents=True)

    # config.yaml：复制主配置，换 app_id，降预算，清空主人名单，关掉微信
    import yaml

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    cfg.setdefault("channels", {})["weixin"] = {"enabled": False}
    qq = cfg["channels"].setdefault("qq", {})
    qq["app_id"] = args.app_id
    qq["enabled"] = True
    cfg.setdefault("persona", {})["master_ids"] = []
    cfg.setdefault("budget", {})["daily_cap_cny"] = float(cfg["budget"].get("daily_cap_cny", 5) or 5)
    cfg.setdefault("features", {})["debug_to_dm"] = False
    chime = cfg["channels"]["qq"].setdefault("chime", {})
    chime["enabled"] = False
    (root / "config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8"
    )

    # .env：沿用主 key，换成这个 Bot 的 AppID/Secret
    main_env = (REPO / ".env").read_text(encoding="utf-8") if (REPO / ".env").exists() else ""
    key_line = next((ln for ln in main_env.splitlines() if ln.startswith("FATFISH_DEEPSEEK_API_KEY=")), "")
    env_text = "\n".join([key_line, f"QQ_APP_ID={args.app_id}", f"QQ_CLIENT_SECRET={args.secret}", ""])
    env_path = root / ".env"
    env_path.write_text(env_text, encoding="utf-8")
    env_path.chmod(0o600)

    persona_src = Path(args.persona).expanduser() if args.persona else None
    if persona_src and persona_src.exists():
        shutil.copy2(persona_src, root / "persona.md")
    else:
        (root / "persona.md").write_text(
            "# 人格提示词（改这里就能换性格，改完下一条消息立即生效）\n\n"
            "<!-- core: 这个 Bot 的人格，随便写 -->\n\n"
            "你是「claude大小姐」，一位说话干脆、偶尔毒舌但很靠得住的 AI 大小姐。\n"
            "自称「本小姐」，对主人称「主人」。中文简体，一句话能说完就别说两句。\n"
            "不写 Markdown、不列 1234、不用 emoji。\n",
            encoding="utf-8",
        )
    print(f"已创建 profile: {root}")
    print(f"  配置 {root / 'config.yaml'}（app_id={args.app_id}，微信关闭，主人名单空）")
    print(f"  密钥 {root / '.env'}（600）")
    print(f"  人格 {root / 'persona.md'}")
    print(f"启动：./run.sh --profile {args.name} start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
