"""配置与路径：~/fatfish 为根；config.yaml 可热改，.env 存密钥。"""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Any, Dict

import yaml

# 多机器人：FATFISH_ROOT 指向某个 profile 目录（含 config.yaml/persona.md/.env/data/logs），
# 不设就用仓库根。第二个 Bot（例如只做私聊的「claude大小姐」）各跑一个 profile。
ROOT = Path(os.environ.get("FATFISH_ROOT") or Path(__file__).resolve().parent.parent).expanduser().resolve()
CONFIG_PATH = ROOT / "config.yaml"
PERSONA_PATH = ROOT / "persona.md"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
PID_PATH = DATA_DIR / "fatfish.pid"
DB_PATH = DATA_DIR / "fatfish.sqlite3"
ENV_PATH = ROOT / ".env"
HERMES_ENV = Path.home() / ".hermes" / ".env"

# 允许从 ~/.hermes/.env 兜底的键（大肥鱼自己的 .env 优先）
FALLBACK_KEYS = {
    "WEIXIN_ACCOUNT_ID",
    "WEIXIN_TOKEN",
    "WEIXIN_BASE_URL",
    "QQ_APP_ID",
    "QQ_CLIENT_SECRET",
}

DEFAULTS: Dict[str, Any] = {
    "persona": {
        "file": "persona.md",
        "scene": "水群",
        "master_name": "主人",
        "self_name": "大肥鱼",
        "master_ids": [],
        "interrupt_prob": 0.0,
        "cooldown_seconds": 15,
        "dm_cooldown_seconds": 2,
        "max_reply_chars": 120,
        "context_messages": 8,
        "max_replies_per_minute": 8,
    },
    "llm": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "FATFISH_DEEPSEEK_API_KEY",
        "model": "deepseek-flash",
        "thinking": "disabled",
        "reasoning_effort": "low",
        "max_tokens": 240,
        "temperature": 0.9,
        "timeout_seconds": 60,
    },
    "budget": {
        "daily_cap_cny": 5.0,
        "reserve_cny": 0.10,
        "timezone": "Asia/Shanghai",
        "low_balance_warn_cny": 0.50,
        "prices": {
            "deepseek-flash": {
                "input_cache_hit_peak": 0.04,
                "input_cache_hit_offpeak": 0.02,
                "input_miss_peak": 2.0,
                "input_miss_offpeak": 1.0,
                "output_peak": 8.0,
                "output_offpeak": 4.0,
            }
        },
    },
    "runtime": {
        # 以前这几个窗口是写死在代码里的，调参台改不动 → 提到配置层
        "same_text_window_seconds": 60,   # 同一个人同一句话多少秒内只回一次
        "msg_dedup_seconds": 300,         # 平台重复推送同一条消息的去重窗口
        "zombie_silent_seconds": 90,      # WS 静默多久判定假连接并重连
        "heartbeat_seconds": 600,         # 日志心跳/统计汇总间隔
    },
    "triggers": {
        "keywords": ["大肥鱼", "胖鱼", "鲸鱼", "小鲸鱼", "肥鱼"],
        "reply_in_dm": True,
        "reply_on_mention": True,
        "group_keyword_reply": True,
    },
    "channels": {
        "console": {"enabled": False},
        "weixin": {"enabled": True, "account_id": "", "base_url": "https://ilinkai.weixin.qq.com", "group_policy": "disabled"},
        "qq": {"enabled": True, "app_id": "", "markdown": False, "group_policy": "allow_all", "allowed_groups": [], "reply_in_guild": False},
    },
    "features": {
        "fortune": True,
        "feed": True,
        "pet": True,
        "repeat": True,
        "balance": True,
        "help": True,
        "affinity": True,
    },
    "logging": {"level": "INFO"},
}

_lock = threading.Lock()
_env: Dict[str, str] | None = None


def _parse_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


def env() -> Dict[str, str]:
    """合并 .env：fatfish 的 .env 优先，~/.hermes/.env 只兜底白名单键。"""
    global _env
    with _lock:
        if _env is None:
            merged: Dict[str, str] = {
                k: v for k, v in _parse_env_file(HERMES_ENV).items() if k in FALLBACK_KEYS
            }
            merged.update(_parse_env_file(ENV_PATH))
            for k, v in os.environ.items():
                if k.startswith("FATFISH_") or k in merged:
                    merged[k] = v
            _env = merged
        return _env


def get(key: str, default: str = "") -> str:
    return env().get(key) or default


def reload_env() -> None:
    global _env
    with _lock:
        _env = None


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            cfg = _deep_merge(cfg, data)
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    """写回 config.yaml：优先用 ruamel 保留注释与顺序，失败再退回 PyYAML（并留备份）。"""
    import shutil
    import time as _time

    bak = CONFIG_PATH.with_name(f"config.yaml.bak-{_time.strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copy2(CONFIG_PATH, bak)
    except OSError:
        pass
    # 调参台可能一天点几十次保存，只留最近 10 份备份（本机已见 26 份）
    try:
        olds = sorted(CONFIG_PATH.parent.glob("config.yaml.bak-*"))
        for old in olds[:-10]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        from ruamel.yaml import YAML

        ry = YAML()
        ry.preserve_quotes = True
        ry.indent(mapping=2, sequence=4, offset=2)
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            doc = ry.load(fh) or {}
        def _deep_update(dst, src) -> None:
            for k, v in src.items():
                if isinstance(v, dict) and hasattr(dst, "get") and isinstance(dst.get(k), dict):
                    _deep_update(dst[k], v)
                else:
                    dst[k] = v

        _deep_update(doc, cfg)
        with CONFIG_PATH.open("w", encoding="utf-8") as fh:
            ry.dump(doc, fh)
        return
    except Exception:  # noqa: BLE001 - 缺 ruamel/解析失败退回 PyYAML
        pass
    CONFIG_PATH.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def config_mtime() -> float:
    return CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0

def set_env_value(key: str, value: str, env_path: Path | None = None) -> Path:
    """就地更新 .env 里的一个键（不存在就追加），保持 600 权限，返回值用于回显路径。"""
    import os as _os

    path = Path(env_path or ENV_PATH)
    if path.exists():
        import shutil as _shutil
        import time as _time

        bak = path.with_name(f"{path.name}.bak-{_time.strftime('%Y%m%d-%H%M%S')}")
        try:
            _shutil.copy2(path, bak)
        except OSError:
            pass
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    prefix = f"{key}="
    out, done = [], False
    for ln in lines:
        if ln.strip().startswith(prefix):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        _os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def set_qq_app_id(config_path: Path, app_id: str) -> None:
    """把 config.yaml 里 channels.qq.app_id 换成新值（只动这一行）。"""
    import re as _re

    text = config_path.read_text(encoding="utf-8")
    new, n = _re.subn(r"(\n\s{2}qq:\n(?:\s{4}.*\n)*?\s{4}app_id: )'?[^'\n]*'?",
                      lambda m: f"{m.group(1)}'{app_id}'", text, count=1)
    if n == 0:
        raise RuntimeError("config.yaml 里没找到 channels.qq.app_id")
    config_path.write_text(new, encoding="utf-8")


def add_master_id(config_path: Path, master_id: str) -> bool:
    """把一个 openid 加进 persona.master_ids（已存在则不动）。返回是否有改动。"""
    text = config_path.read_text(encoding="utf-8")
    if master_id in text:
        return False
    lines = text.splitlines()
    try:
        idx = next(
            i for i, ln in enumerate(lines)
            # 兼容三种写法：`master_ids:`、`master_ids: []`、`master_ids: ["x"]`
            if ln.strip() == "master_ids:" or ln.strip().startswith("master_ids: [")
        )
    except StopIteration:
        raise RuntimeError("config.yaml 里没找到 persona.master_ids") from None
    if lines[idx].strip() != "master_ids:":
        # 行内列表（[] 或 ["x", "y"]）先拆成块列表，否则往里插 - item 会破坏 YAML
        inline = lines[idx].split(":", 1)[1].strip()
        items = [x for x in inline.strip("[]").split(",") if x.strip()]
        lines[idx] = lines[idx].split(":", 1)[0] + ":"
        for i, it in enumerate(reversed(items)):
            lines.insert(idx + 1, "  - " + it.strip())
    # YAML 的列表项缩进可以等于或大于 key 的缩进，两种写法都要认
    key_indent = len(lines[idx]) - len(lines[idx].lstrip())
    insert_at, indent = idx + 1, None
    while insert_at < len(lines):
        cur = lines[insert_at]
        stripped = cur.strip()
        if not stripped:
            insert_at += 1
            continue
        cur_indent = len(cur) - len(cur.lstrip())
        if stripped.startswith("- ") and cur_indent >= max(0, key_indent - 2):
            indent = cur_indent
            insert_at += 1
            continue
        break
    if indent is None:
        indent = key_indent + 2
    lines.insert(insert_at, " " * indent + f"- {master_id}")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def remove_master_id(config_path: Path, master_id: str) -> bool:
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    kept = [ln for ln in lines if ln.strip() != f"- {master_id}"]
    if len(kept) == len(lines):
        return False
    config_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return True

def app_id_conflicts(app_id: str, current_root: Path | None = None) -> list:
    """找出还有谁在用同一个 QQ AppID（同一 AppID 被两个进程消费 → 消息双回、互相抢 WS）。

    只读扫描已知的 env 文件位置：所有 fatfish profile + Hermes。返回人类可读的描述列表。
    """
    app_id = str(app_id or "").strip()
    if not app_id:
        return []
    root = (current_root or ROOT).resolve()
    hits = []
    candidates = []
    base = root / "profiles" if (root / "profiles").is_dir() else root.parent
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if (d / ".env").exists():
                candidates.append(d / ".env")
    hermes = Path.home() / ".hermes"
    for name in (".env",):
        if (hermes / name).exists():
            candidates.append(hermes / name)
    for env_path in candidates:
        try:
            if env_path.resolve() == (root / ".env").resolve():
                continue
            text = env_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.startswith("QQ_APP_ID="):
                continue
            if line.split("=", 1)[1].strip() == app_id:
                owner = env_path.parent.name
                if env_path.parent.name == ".hermes":
                    owner = "Hermes（微信/qqbot 插件）"
                hits.append(f"{owner} 也用 AppID {app_id}（{env_path}）")
    return hits

