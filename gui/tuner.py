"""大肥鱼 · 本地调参台（Tkinter）。

运行：python3 gui/tuner.py
改 config.yaml / persona.md；查看今日花费与近 14 天用量；启停机器人；测模型连通。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fatfish import settings  # noqa: E402
from fatfish import affinity as affinity_mod  # noqa: E402
from fatfish import names as names_mod  # noqa: E402
from fatfish.persona import read_persona  # noqa: E402
from fatfish.store import Store  # noqa: E402



ICON_PNG = ROOT / "assets" / "avatar_whalegirl_close_256.png"

# ── 纯函数区：不碰 Tk，可单测；主线程只负责画 ──
LOG_PARAM_MARKER = "生效参数JSON "
FEATURE_KEYS = ("fortune", "feed", "pet", "repeat", "balance", "help", "auto_reply")


def flatten_cfg(cfg: dict) -> dict:
    """把配置摊平成和 `生效参数JSON` 同名的键，便于逐项比对。"""
    p = cfg.get("persona", {}) or {}
    llm = cfg.get("llm", {}) or {}
    b = cfg.get("budget", {}) or {}
    t = cfg.get("triggers", {}) or {}
    q = (cfg.get("channels", {}) or {}).get("qq", {}) or {}
    chime = q.get("chime") or {}
    pro = q.get("proactive") or {}
    rt = cfg.get("runtime", {}) or {}
    th = llm.get("thinking")
    out = {
        "persona.self_name": p.get("self_name"),
        "persona.master_name": p.get("master_name"),
        "persona.max_reply_chars": p.get("max_reply_chars"),
        "persona.cooldown_seconds": p.get("cooldown_seconds"),
        "persona.dm_cooldown_seconds": p.get("dm_cooldown_seconds"),
        "persona.context_messages": p.get("context_messages"),
        "persona.max_replies_per_minute": p.get("max_replies_per_minute"),
        "persona.interrupt_prob": p.get("interrupt_prob"),
        "llm.model": llm.get("model"),
        "llm.thinking": th.get("enabled") if isinstance(th, dict) else th,
        "llm.reasoning_effort": llm.get("reasoning_effort"),
        "llm.max_tokens": llm.get("max_tokens"),
        "llm.temperature": llm.get("temperature"),
        "llm.timeout_seconds": llm.get("timeout_seconds"),
        "budget.daily_cap_cny": b.get("daily_cap_cny"),
        "budget.reserve_cny": b.get("reserve_cny"),
        "triggers.keywords": t.get("keywords"),
        "triggers.reply_in_dm": t.get("reply_in_dm"),
        "triggers.reply_on_mention": t.get("reply_on_mention"),
        "triggers.group_keyword_reply": t.get("group_keyword_reply"),
        "channels.qq.group_policy": q.get("group_policy"),
        "channels.qq.allowed_groups": q.get("allowed_groups"),
        "channels.qq.chime.enabled": chime.get("enabled"),
        "channels.qq.chime.random_prob": chime.get("random_prob"),
        "channels.qq.proactive.enabled": pro.get("enabled"),
        "runtime.same_text_window_seconds": rt.get("same_text_window_seconds"),
        "runtime.msg_dedup_seconds": rt.get("msg_dedup_seconds"),
        "runtime.zombie_silent_seconds": rt.get("zombie_silent_seconds"),
        "runtime.heartbeat_seconds": rt.get("heartbeat_seconds"),
    }
    for k in FEATURE_KEYS:
        if k in (cfg.get("features", {}) or {}):
            out[f"features.{k}"] = (cfg.get("features") or {}).get(k)
    return out


def param_equal(a, b) -> bool:
    """60 与 60.0、True 与 "true"、同内容的列表都算一致。"""
    if isinstance(a, list) and isinstance(b, list):
        return [str(x) for x in a] == [str(x) for x in b]
    if a is None or b is None:
        return a is b or str(a) == str(b)
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


def compare_params(want: dict, live: dict) -> list:
    """逐项比对，返回 [(键, 配置值, 运行值, 是否一致)]。"""
    rows = []
    for key in sorted(want):
        got = live.get(key, "—")
        rows.append((key, str(want[key]), str(got), param_equal(want[key], got)))
    return rows


def read_log_tail(path, max_bytes: int = 200_000, max_lines: int = 400) -> list:
    """只读日志尾巴：以前每次 refresh_log 都整文件 read_text，几十 MB 必然转圈。"""
    try:
        with Path(path).open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # 首行大概率被切断
    return lines[-max_lines:]


def parse_param_line(line: str) -> dict:
    if LOG_PARAM_MARKER not in line:
        return {}
    try:
        return json.loads(line.split(LOG_PARAM_MARKER, 1)[1])
    except ValueError:
        return {}


class _NoParams(Exception):
    """机器人没打过快照。"""


def wait_live_params(log_path, pid: int, timeout: float = 3.0, interval: float = 0.25) -> tuple:
    """在后台线程里跑：让机器人打一份「正在用的参数」，等它出现新的那行。

    返回 (参数 dict, 是否本次新打的)。绝不在主线程调用。
    """
    def snap() -> list:
        return [ln for ln in read_log_tail(log_path, max_bytes=400_000, max_lines=3000)
                if LOG_PARAM_MARKER in ln]

    before = len(snap())
    if not pid:
        old = snap()
        return (parse_param_line(old[-1]) if old else {}), False
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError:
        old = snap()
        return (parse_param_line(old[-1]) if old else {}), False

    deadline = time.monotonic() + timeout
    last = {}
    while True:
        lines = snap()
        if len(lines) > before:  # 出现新行，说明是这一秒的真实值
            last = parse_param_line(lines[-1])
            if last:
                return last, True
        if time.monotonic() >= deadline:
            if last:
                return last, False
            return (parse_param_line(lines[-1]) if lines else {}), False
        time.sleep(interval)


def filter_chat_rows(rows, time_kw: str = "", role: str = "全部", name_kw: str = "",
                     text_kw: str = "", newest_first: bool = True) -> list:
    """会话列表：按列过滤（子串、忽略大小写）再按时间排序。空条件＝不过滤。"""
    def hit(value, kw: str) -> bool:
        kw = (kw or "").strip().lower()
        return not kw or kw in str(value or "").lower()

    picked = []
    for row in rows:
        label = "用户" if row.get("role") == "user" else "大肥鱼"
        if role not in ("全部", "", None) and label != role:
            continue
        if not hit(row.get("when", ""), time_kw):
            continue
        if not hit(row.get("name", ""), name_kw):
            continue
        if not hit(row.get("text", ""), text_kw):
            continue
        picked.append(row)
    picked.sort(key=lambda r: r.get("ts", 0), reverse=bool(newest_first))
    return picked


def bot_pid() -> int:
    try:
        return int(settings.PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return 0



class Tuner(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        # 窗口/Dock 图标（.app 里还有 AppIcon.icns，这里是窗口级兜底）
        try:
            self._icon_img = tk.PhotoImage(file=str(ICON_PNG))
            self.iconphoto(True, self._icon_img)
        except Exception:  # noqa: BLE001
            pass
        self.bot_name = self._bot_label()
        self.title(f"{self.bot_name} · 调参台")
        self.geometry("1000x780+120+60")
        self.minsize(900, 700)
        self.cfg = settings.load_config()
        self.store = Store()
        self._apply_busy = False   # 核对参数是否占用中
        self._save_busy = False    # 保存按钮是否已禁用
        self._bg_q: queue.Queue = queue.Queue()  # 工作线程 → 主线程的结果通道
        self._build_header()
        # 底栏必须先占住底部：Notebook 用 expand 撑满，后 pack 的底栏会被挤出可视区
        self._footer = ttk.Frame(self)
        self._footer.pack(side="bottom", fill="x", padx=10, pady=(0, 8))
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self._build_persona_tab()
        self._build_model_tab()
        self._build_budget_tab()
        self._build_trigger_tab()
        self._build_channel_tab()
        self._build_chat_tab()
        self._build_affinity_tab()
        self._build_log_tab()
        self._build_push_tab()
        self._build_apply_tab()
        self._build_footer()
        self.refresh_status()
        self.refresh_usage()
        self._pump_bg()  # 后台结果泵：Tk 调用只在主线程

    # ── 顶栏 ──
    def _build_header(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=8)
        ttk.Label(bar, text=f"{self.bot_name} · 水群机器人", font=("PingFang SC", 16, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="状态：读取中…")
        ttk.Label(bar, textvariable=self.status_var).pack(side="right")
        # 多机器人切换：选另一个 profile 就换一个窗口（新进程带 FATFISH_ROOT），互不干扰
        names = self._profile_names()
        if len(names) > 1:
            self.profile_var = tk.StringVar(value=self._current_profile() or names[0])
            box = ttk.Frame(bar)
            box.pack(side="right", padx=(0, 12))
            ttk.Label(box, text="机器人").pack(side="left")
            picker = ttk.Combobox(box, textvariable=self.profile_var, values=names, width=12, state="readonly")
            picker.pack(side="left", padx=4)
            picker.bind("<<ComboboxSelected>>", lambda _e: self._switch_profile(self.profile_var.get()))

    @staticmethod
    def _repo_root() -> Path:
        """仓库根：无论当前跑在主机器人还是某个 profile 里，都要回到同一层。"""
        root = settings.ROOT
        return root.parent.parent if root.parent.name == "profiles" else root

    def _profile_names(self) -> list:
        """可调参的机器人：主机器人 + profiles/ 下未封存的子机器人（永远相对仓库根找）。

        封存 = profiles/<名>/.sealed 存在（里面写原因）。封存的不在下拉里出现，
        但命令行 `./run.sh --profile <名> ...` 照旧可用；维护时设
        FATFISH_GUI_SHOW_SEALED=1 可临时显示全部。
        """
        names = ["大肥鱼"]
        base = self._repo_root() / "profiles"
        if base.is_dir():
            show_sealed = os.environ.get("FATFISH_GUI_SHOW_SEALED") == "1"
            for d in sorted(base.iterdir()):
                if not (d / "config.yaml").exists():
                    continue
                if (d / ".sealed").exists() and not show_sealed:
                    continue
                names.append(d.name)
        return names

    def _current_profile(self) -> str:
        return settings.ROOT.name if settings.ROOT.parent.name == "profiles" else "大肥鱼"

    def _profile_root(self, name: str) -> Path | None:
        """切到某个机器人时该用的 FATFISH_ROOT；主机器人返回 None（用仓库根）。"""
        if name == "大肥鱼":
            return None
        return self._repo_root() / "profiles" / name

    def _bot_label(self) -> str:
        cfg_p = (settings.load_config().get("persona") or {}).get("self_name")
        return f"{cfg_p or '机器人'}（{self._current_profile()}）"

    def _switch_profile(self, name: str) -> None:
        """换一个机器人调参：另开一个窗口（新进程带 FATFISH_ROOT），关掉当前窗口。

        失败（新进程立刻退出）时不关窗口，只在标题栏报错并把下拉框拨回去，
        免得点一下界面就消失了、还回不来。
        """
        if name == self._current_profile():
            if hasattr(self, "profile_var"):
                self.profile_var.set(name)
            return
        root = self._profile_root(name)
        if root is not None and not (root / "config.yaml").exists():
            self.msg_var.set(f"没有这个机器人：{root}")
            self.profile_var.set(self._current_profile())
            return
        env = dict(os.environ)
        if root is None:
            env.pop("FATFISH_ROOT", None)
        else:
            env["FATFISH_ROOT"] = str(root)
        # 新进程不一定从仓库目录启动，PYTHONPATH 显式给上，避免 import fatfish 失败后秒退
        repo = str(self._repo_root())
        env["PYTHONPATH"] = repo + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve())],
                env=env,
                cwd=repo,
                start_new_session=True,
            )
        except OSError as exc:
            self.msg_var.set(f"启动失败：{exc}")
            self.profile_var.set(self._current_profile())
            return
        time.sleep(1.0)
        if proc.poll() is not None:  # 新窗口立刻退了，当前窗口保住
            code = getattr(proc, "returncode", "?")
            self.msg_var.set(f"切换 {name} 失败（新窗口退出码 {code}）")
            self.profile_var.set(self._current_profile())
            return
        self.destroy()

    # ── 定时推送 ──
    def _build_push_tab(self) -> None:
        from fatfish.notify_jobs import JOBS_PATH, load_jobs

        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="定时推送")
        ttk.Label(
            tab,
            text="命令的 stdout 原样推到主人微信（0 token）；stdout 为空则不推。启用后请关掉 hermes 里同名 cron，避免双推。",
            foreground="#666",
        ).pack(anchor="w", padx=10, pady=6)
        self.push_vars: dict = {}
        self.push_jobs = load_jobs()
        if not self.push_jobs:
            ttk.Label(tab, text=f"没有配置任务：{JOBS_PATH}", foreground="#a33").pack(anchor="w", padx=10)
        for job in self.push_jobs:
            row = ttk.Frame(tab)
            row.pack(fill="x", padx=10, pady=3)
            var = tk.BooleanVar(value=job["enabled"])
            self.push_vars[job["name"]] = var
            ttk.Checkbutton(row, text=job["name"], variable=var).pack(side="left")
            ttk.Label(row, text=job["schedule"], width=16, foreground="#666").pack(side="left", padx=8)
            ttk.Label(row, text=job["command"][:64], foreground="#888").pack(side="left")
            ttk.Button(
                row, text="试跑", command=lambda n=job["name"]: self.run_push_job(n)
            ).pack(side="right", padx=4)
        ttk.Button(tab, text="保存推送开关", command=self.save_push_jobs).pack(anchor="w", padx=10, pady=8)
        self.push_msg_var = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.push_msg_var, foreground="#666").pack(anchor="w", padx=10)

    def save_push_jobs(self) -> None:
        """按行改 `enabled:`，保留文件里的注释。"""
        from fatfish.notify_jobs import JOBS_PATH

        if not JOBS_PATH.exists():
            self.push_msg_var.set(f"找不到 {JOBS_PATH}")
            return
        lines = JOBS_PATH.read_text(encoding="utf-8").splitlines()
        current = ""
        changed = 0
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("- name:"):
                current = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("enabled:") and current in self.push_vars:
                want = "true" if self.push_vars[current].get() else "false"
                new_line = f"{line.split('enabled:')[0]}enabled: {want}"
                if new_line != line:
                    lines[idx] = new_line
                    changed += 1
        JOBS_PATH.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
        self.push_msg_var.set(f"已保存（改动 {changed} 处），进程 20 秒内生效")

    def run_push_job(self, name: str) -> None:
        import queue
        import threading

        if not hasattr(self, "_diag_q"):
            self._diag_q = queue.Queue()
        self.push_msg_var.set(f"试跑 {name} …")
        self.after(300, self._poll_diag)

        def work() -> None:
            proc = subprocess.run(
                [sys.executable, "-m", "fatfish.cli", "cron", "run", name],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self._diag_q.put(f"[试跑 {name}]\n" + (proc.stdout or "") + (proc.stderr or ""))

        threading.Thread(target=work, daemon=True).start()

    def _build_footer(self) -> None:
        bar = self._footer
        self.save_btn = ttk.Button(bar, text="💾 保存并应用（热更新）", command=self.save_all)
        self.save_btn.pack(side="left")
        ttk.Button(bar, text="▶ 启动", command=self.start_bot).pack(side="left", padx=6)
        ttk.Button(bar, text="■ 停止", command=self.stop_bot).pack(side="left")
        ttk.Button(bar, text="↻ 生效确认", command=self._refresh_apply).pack(side="left", padx=6)
        self.msg_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.msg_var).pack(side="left", padx=12)
        self.bind_all("<Command-s>", lambda _e: self.save_all())
        self.bind_all("<Control-s>", lambda _e: self.save_all())

    # ── 人格 ──
    def _build_persona_tab(self) -> None:
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="人格")
        top = ttk.Frame(tab)
        top.pack(fill="x", padx=6, pady=6)
        p = self.cfg.get("persona", {})

        self.vars: dict[str, tk.Variable] = {}
        def add_row(parent, row, label, key, kind="entry", width=10, values=None):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=3)
            var = tk.StringVar(value=str(p.get(key, "")))
            self.vars[key] = var
            if kind == "spin":
                w = ttk.Spinbox(parent, from_=0, to=10_000, textvariable=var, width=width)
            else:
                w = ttk.Entry(parent, textvariable=var, width=width)
            w.grid(row=row, column=1, sticky="w", padx=4, pady=3)
            return w

        add_row(top, 0, "自称", "self_name")
        add_row(top, 1, "对主人称呼", "master_name")
        add_row(top, 2, "最长回复字数", "max_reply_chars", "spin")
        add_row(top, 3, "群聊冷却（秒）", "cooldown_seconds", "spin")
        add_row(top, 4, "私聊冷却（秒）", "dm_cooldown_seconds", "spin")
        add_row(top, 5, "上下文条数", "context_messages", "spin")
        add_row(top, 6, "每分钟最多回复", "max_replies_per_minute", "spin")

        ttk.Label(top, text="随机插话概率").grid(row=0, column=2, sticky="w", padx=12)
        self.interrupt_var = tk.DoubleVar(value=float(p.get("interrupt_prob", 0.0)))
        ttk.Spinbox(top, from_=0.0, to=1.0, increment=0.01, textvariable=self.interrupt_var, width=8).grid(
            row=0, column=3, sticky="w"
        )

        ttk.Label(tab, text="人格提示词（persona.md，core 段建议别动）").pack(anchor="w", padx=6)
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.persona_text = tk.Text(frame, wrap="word", font=("PingFang SC", 12))
        sb = ttk.Scrollbar(frame, command=self.persona_text.yview)
        self.persona_text.configure(yscrollcommand=sb.set)
        self.persona_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.persona_text.insert("1.0", read_persona())

    # ── 模型 ──
    def _build_model_tab(self) -> None:
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="模型")
        box = ttk.Frame(tab)
        box.pack(fill="x", padx=10, pady=10)
        llm = self.cfg.get("llm", {})

        ttk.Label(box, text="模型").grid(row=0, column=0, sticky="w", pady=4)
        self.model_var = tk.StringVar(value=str(llm.get("model", "deepseek-flash")))
        ttk.Combobox(
            box, textvariable=self.model_var, width=24, state="readonly",
            values=["deepseek-flash", "deepseek-v4-pro"],
        ).grid(row=0, column=1, sticky="w", padx=8)

        ttk.Label(box, text="思考模式").grid(row=1, column=0, sticky="w", pady=4)
        self.thinking_var = tk.StringVar(value=str(llm.get("thinking", "disabled")))
        ttk.Combobox(
            box, textvariable=self.thinking_var, width=24, state="readonly",
            values=["disabled", "enabled"],
        ).grid(row=1, column=1, sticky="w", padx=8)

        ttk.Label(box, text="思考强度（仅 enabled 生效）").grid(row=2, column=0, sticky="w", pady=4)
        self.effort_var = tk.StringVar(value=str(llm.get("reasoning_effort", "low")))
        ttk.Combobox(
            box, textvariable=self.effort_var, width=24, state="readonly",
            values=["low", "high", "max"],
        ).grid(row=2, column=1, sticky="w", padx=8)

        ttk.Label(box, text="max_tokens").grid(row=3, column=0, sticky="w", pady=4)
        self.max_tokens_var = tk.StringVar(value=str(llm.get("max_tokens", 240)))
        ttk.Spinbox(box, from_=32, to=2000, textvariable=self.max_tokens_var, width=10).grid(
            row=3, column=1, sticky="w", padx=8
        )

        ttk.Label(box, text="temperature").grid(row=4, column=0, sticky="w", pady=4)
        self.temp_var = tk.DoubleVar(value=float(llm.get("temperature", 0.9)))
        ttk.Scale(box, from_=0.0, to=1.5, variable=self.temp_var, orient="horizontal", length=220).grid(
            row=4, column=1, sticky="w", padx=8
        )

        ttk.Label(box, text="超时（秒）").grid(row=5, column=0, sticky="w", pady=4)
        self.timeout_var = tk.StringVar(value=str(llm.get("timeout_seconds", 60)))
        ttk.Spinbox(box, from_=5, to=300, textvariable=self.timeout_var, width=10).grid(
            row=5, column=1, sticky="w", padx=8
        )

        ttk.Label(
            box,
            text="提示：日常水群保持「disabled + max_tokens 240」，最省钱；\n只有需要算题/长推理时临时开 enabled + low。",
            foreground="#666",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=10)

        ttk.Button(box, text="测一句（会花几分钱）", command=self.test_llm).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=6
        )
        self.test_out = tk.Text(tab, height=6, wrap="word", font=("PingFang SC", 12))
        self.test_out.pack(fill="x", padx=10, pady=(0, 10))

    # ── 预算 ──
    def _build_budget_tab(self) -> None:
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="预算")
        box = ttk.Frame(tab)
        box.pack(fill="x", padx=10, pady=10)
        b = self.cfg.get("budget", {})

        ttk.Label(box, text="每日上限（元）").grid(row=0, column=0, sticky="w", pady=4)
        self.cap_var = tk.StringVar(value=str(b.get("daily_cap_cny", 5.0)))
        ttk.Spinbox(box, from_=0.1, to=1000, increment=0.5, textvariable=self.cap_var, width=10).grid(
            row=0, column=1, sticky="w", padx=8
        )

        ttk.Label(box, text="预留安全垫（元）").grid(row=1, column=0, sticky="w", pady=4)
        self.reserve_var = tk.StringVar(value=str(b.get("reserve_cny", 0.10)))
        ttk.Spinbox(box, from_=0.0, to=10, increment=0.05, textvariable=self.reserve_var, width=10).grid(
            row=1, column=1, sticky="w", padx=8
        )

        ttk.Label(box, text="低余额提醒（元）").grid(row=2, column=0, sticky="w", pady=4)
        self.warn_var = tk.StringVar(value=str(b.get("low_balance_warn_cny", 0.50)))
        ttk.Spinbox(box, from_=0.0, to=10, increment=0.05, textvariable=self.warn_var, width=10).grid(
            row=2, column=1, sticky="w", padx=8
        )

        self.spend_label = ttk.Label(tab, text="", font=("PingFang SC", 13, "bold"))
        self.spend_label.pack(anchor="w", padx=10, pady=(0, 4))
        self.progress = ttk.Progressbar(tab, maximum=100, length=460)
        self.progress.pack(anchor="w", padx=10, pady=(0, 10))

        ttk.Label(tab, text="近 14 天用量").pack(anchor="w", padx=10)
        cols = ("day", "calls", "cost", "out")
        self.usage_tree = ttk.Treeview(tab, columns=cols, show="headings", height=12)
        for c, title, w in zip(cols, ("日期", "调用", "花费(元)", "输出 tokens"), (140, 80, 120, 140)):
            self.usage_tree.heading(c, text=title)
            self.usage_tree.column(c, width=w, anchor="center")
        self.usage_tree.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        ttk.Button(tab, text="刷新", command=self.refresh_usage).pack(anchor="w", padx=10, pady=(0, 8))

    # ── 触发与功能 ──
    def _build_trigger_tab(self) -> None:
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="触发/功能")
        box = ttk.Frame(tab)
        box.pack(fill="x", padx=10, pady=10)
        t = self.cfg.get("triggers", {})

        ttk.Label(box, text="唤醒词（逗号分隔）").grid(row=0, column=0, sticky="w")
        self.keywords_var = tk.StringVar(value=", ".join(t.get("keywords") or []))
        ttk.Entry(box, textvariable=self.keywords_var, width=70).grid(row=0, column=1, sticky="w", padx=8)

        self.reply_dm_var = tk.BooleanVar(value=bool(t.get("reply_in_dm", True)))
        self.reply_mention_var = tk.BooleanVar(value=bool(t.get("reply_on_mention", True)))
        self.reply_keyword_var = tk.BooleanVar(value=bool(t.get("group_keyword_reply", True)))
        ttk.Checkbutton(box, text="私聊都回", variable=self.reply_dm_var).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Checkbutton(box, text="群里 @ 就回", variable=self.reply_mention_var).grid(row=1, column=1, sticky="w")
        ttk.Checkbutton(box, text="群里命中唤醒词也回", variable=self.reply_keyword_var).grid(row=2, column=0, sticky="w")

        ttk.Separator(tab).pack(fill="x", padx=10, pady=8)
        ttk.Label(tab, text="水群小功能（模板回复，不花 token）").pack(anchor="w", padx=10)
        fbox = ttk.Frame(tab)
        fbox.pack(fill="x", padx=10, pady=6)
        f = self.cfg.get("features", {})
        self.feature_vars: dict[str, tk.BooleanVar] = {}
        labels = {
            "fortune": "抽签/运势",
            "feed": "喂饭/饿了吗",
            "pet": "贴贴/摸头",
            "repeat": "复读",
            "balance": "余额查询（仅主人）",
            "affinity": "好感度/好感榜",
            "help": "功能说明",
            "auto_reply": "自定义自动回复",
        }
        for i, (k, label) in enumerate(labels.items()):
            var = tk.BooleanVar(value=bool(f.get(k, True)))
            self.feature_vars[k] = var
            ttk.Checkbutton(fbox, text=label, variable=var).grid(row=i // 3, column=i % 3, sticky="w", padx=4, pady=3)

        qq_cfg = (self.cfg.get("channels", {}) or {}).get("qq", {}) or {}
        chime = qq_cfg.get("chime") or {}
        chime_box = ttk.LabelFrame(tab, text="群内接话（不 @ 也搭话）")
        chime_box.pack(fill="x", padx=10, pady=(8, 4))
        self.chime_enabled_var = tk.BooleanVar(value=bool(chime.get("enabled", False)))
        ttk.Checkbutton(chime_box, text="启用接话", variable=self.chime_enabled_var).grid(
            row=0, column=0, sticky="w", padx=6, pady=2
        )
        ttk.Label(chime_box, text="随机概率").grid(row=0, column=1, sticky="e")
        self.chime_prob_var = tk.StringVar(value=f"{float(chime.get('random_prob') or 0):.2f}")
        ttk.Entry(chime_box, textvariable=self.chime_prob_var, width=6).grid(row=0, column=2, padx=4)
        ttk.Label(chime_box, text="冷却(秒)").grid(row=0, column=3, sticky="e")
        self.chime_cd_var = tk.StringVar(value=str(int(chime.get("cooldown_seconds") or 60)))
        ttk.Entry(chime_box, textvariable=self.chime_cd_var, width=6).grid(row=0, column=4, padx=4)
        ttk.Label(chime_box, text="每小时上限").grid(row=0, column=5, sticky="e")
        self.chime_max_var = tk.StringVar(value=str(int(chime.get("max_per_hour") or 30)))
        ttk.Entry(chime_box, textvariable=self.chime_max_var, width=6).grid(row=0, column=6, padx=4)
        ttk.Label(
            chime_box,
            text=f"叫名字 {len(chime.get('names') or [])} 个、指令词 {len(chime.get('commands') or [])} 个（改词请在 config.yaml 里）",
            foreground="#666",
        ).grid(row=1, column=0, columnspan=7, sticky="w", padx=6)

        rt = self.cfg.get("runtime", {}) or {}
        rt_box = ttk.LabelFrame(tab, text="运行窗口（原来写死在代码里，改这里就热更新）")
        rt_box.pack(fill="x", padx=10, pady=(8, 4))
        self.rt_vars: dict[str, tk.StringVar] = {}
        for i, (key, label, width) in enumerate((
            ("same_text_window_seconds", "同句去重(秒)", 6),
            ("msg_dedup_seconds", "消息去重(秒)", 6),
            ("zombie_silent_seconds", "假连接判定(秒)", 6),
            ("heartbeat_seconds", "心跳/统计(秒)", 6),
        )):
            ttk.Label(rt_box, text=label).grid(row=0, column=i * 2, sticky="e", padx=(6, 2), pady=2)
            var = tk.StringVar(value=str(rt.get(key, "")))
            self.rt_vars[key] = var
            ttk.Entry(rt_box, textvariable=var, width=width).grid(row=0, column=i * 2 + 1, padx=(0, 6))

        pr = (self.cfg.get("channels", {}).get("qq", {}) or {}).get("proactive") or {}
        pro_box = ttk.LabelFrame(tab, text="冷场主动冒泡（占平台主动消息配额）")
        pro_box.pack(fill="x", padx=10, pady=(4, 4))
        self.pro_enabled_var = tk.BooleanVar(value=bool(pr.get("enabled", False)))
        ttk.Checkbutton(pro_box, text="启用冒泡", variable=self.pro_enabled_var).grid(
            row=0, column=0, sticky="w", padx=6, pady=2
        )
        ttk.Label(pro_box, text="冷场(分钟)").grid(row=0, column=1, sticky="e")
        self.pro_idle_var = tk.StringVar(value=str(int(pr.get("idle_minutes") or 120)))
        ttk.Entry(pro_box, textvariable=self.pro_idle_var, width=6).grid(row=0, column=2, padx=4)
        ttk.Label(pro_box, text="每天上限").grid(row=0, column=3, sticky="e")
        self.pro_max_var = tk.StringVar(value=str(int(pr.get("max_per_day") or 3)))
        ttk.Entry(pro_box, textvariable=self.pro_max_var, width=6).grid(row=0, column=4, padx=4)

        auto = f.get("auto_replies") or []
        pairs = "、".join(
            f"{a.get('key')}→{str(a.get('reply'))[:10]}…"
            for a in auto if isinstance(a, dict) and a.get("key")
        )
        ttk.Label(
            tab,
            text=f"自动回复 {len(auto)} 条：" + (pairs or "（在 config.yaml 的 features.auto_replies 里加）"),
            foreground="#666",
        ).pack(anchor="w", padx=10)

    # ── 渠道 ──
    def _build_channel_tab(self) -> None:
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="渠道")
        c = self.cfg.get("channels", {})
        w = c.get("weixin", {})
        q = c.get("qq", {})

        wx_box = ttk.LabelFrame(tab, text="微信（iLink Bot · 私聊为主）")
        wx_box.pack(fill="x", padx=10, pady=8)
        self.wx_enabled_var = tk.BooleanVar(value=bool(w.get("enabled", True)))
        ttk.Checkbutton(wx_box, text="启用", variable=self.wx_enabled_var).grid(row=0, column=0, sticky="w", padx=6)
        ttk.Label(wx_box, text="account").grid(row=1, column=0, sticky="w", padx=6)
        self.wx_account_var = tk.StringVar(value=str(w.get("account_id") or settings.get("WEIXIN_ACCOUNT_ID")))
        ttk.Entry(wx_box, textvariable=self.wx_account_var, width=44).grid(row=1, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(wx_box, text="群策略").grid(row=2, column=0, sticky="w", padx=6)
        self.wx_policy_var = tk.StringVar(value=str(w.get("group_policy", "disabled")))
        ttk.Combobox(wx_box, textvariable=self.wx_policy_var, width=20, state="readonly",
                     values=["disabled", "allow_all"]).grid(row=2, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(wx_box, text="iLink bot 身份通常进不了普通微信群，群聊以 QQ 为主。", foreground="#666").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=6, pady=4)

        qq_box = ttk.LabelFrame(tab, text="QQ 官方机器人（群 @ 触发）")
        qq_box.pack(fill="x", padx=10, pady=8)
        self.qq_enabled_var = tk.BooleanVar(value=bool(q.get("enabled", True)))
        ttk.Checkbutton(qq_box, text="启用", variable=self.qq_enabled_var).grid(row=0, column=0, sticky="w", padx=6)
        ttk.Label(qq_box, text="AppID").grid(row=1, column=0, sticky="w", padx=6)
        self.qq_appid_var = tk.StringVar(value=str(q.get("app_id") or settings.get("QQ_APP_ID")))
        ttk.Entry(qq_box, textvariable=self.qq_appid_var, width=44).grid(row=1, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(qq_box, text="群策略").grid(row=2, column=0, sticky="w", padx=6)
        self.qq_policy_var = tk.StringVar(value=str(q.get("group_policy", "allow_all")))
        ttk.Combobox(qq_box, textvariable=self.qq_policy_var, width=20, state="readonly",
                     values=["allow_all", "allowlist"]).grid(row=2, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(qq_box, text="白名单群 openid（逗号分隔）").grid(row=3, column=0, sticky="w", padx=6)
        self.qq_allow_var = tk.StringVar(value=", ".join(q.get("allowed_groups") or []))
        ttk.Entry(qq_box, textvariable=self.qq_allow_var, width=44).grid(row=3, column=1, sticky="w", padx=6, pady=3)

        diag_bar = ttk.Frame(tab)
        diag_bar.pack(anchor="w", padx=10, pady=2)
        ttk.Button(diag_bar, text="QQ 通道诊断", command=self.run_diagnose).pack(side="left")
        self.diag_var = tk.StringVar(value="")
        ttk.Label(diag_bar, textvariable=self.diag_var, foreground="#666").pack(side="left", padx=8)

        ttk.Label(
            tab,
            text="渠道开关改动后需要「停止 → 启动」；其他参数保存后自动热更新。",
            foreground="#666",
        ).pack(anchor="w", padx=10, pady=6)

    def run_diagnose(self) -> None:
        import queue
        import threading

        if not hasattr(self, "_diag_q"):
            self._diag_q = queue.Queue()
        self.diag_var.set("诊断中…")
        self.after(300, self._poll_diag)

        def work() -> None:
            proc = subprocess.run(
                [sys.executable, "-m", "fatfish.cli", "diagnose"],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self._diag_q.put((proc.stdout or "") + (proc.stderr or ""))

        threading.Thread(target=work, daemon=True).start()

    def _poll_diag(self) -> None:
        import queue

        try:
            out = self._diag_q.get_nowait()
        except queue.Empty:
            self.after(300, self._poll_diag)
            return
        self.diag_var.set("诊断完成，输出见「日志」页签")
        self._append_diag(out)

    def _append_diag(self, text: str) -> None:
        self.nb.select(6)  # 日志页签
        self.log_text.insert("end", "\n=== QQ 诊断 ===\n" + text + "\n")
        self.log_text.see("end")

    # ── 会话 ──
    def _build_chat_tab(self) -> None:
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="会话")

        # 第一行：会话选择 + 排序 + 刷新
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(bar, text="会话").pack(side="left")
        self.chat_filter_var = tk.StringVar(value="全部")
        self.chat_picker = ttk.Combobox(bar, textvariable=self.chat_filter_var, width=44, state="readonly")
        self.chat_picker.pack(side="left", padx=6)
        self.chat_picker.bind("<<ComboboxSelected>>", lambda _e: self.refresh_chats())
        ttk.Button(bar, text="刷新", command=self.refresh_chats).pack(side="left")

        self.chat_desc_var = tk.BooleanVar(value=True)
        self.chat_sort_btn = ttk.Button(bar, text="↓ 最新在前", width=12, command=self._toggle_chat_order)
        self.chat_sort_btn.pack(side="left", padx=(10, 2))
        ttk.Button(bar, text="清空筛选", command=self._clear_chat_filters).pack(side="left", padx=2)

        # 第二行：列筛选（改一个字就实时过一遍，纯内存，不查库）
        fbar = ttk.Frame(tab)
        fbar.pack(fill="x", padx=6, pady=(0, 4))
        self.chat_time_var = tk.StringVar(value="")
        self.chat_name_var = tk.StringVar(value="")
        self.chat_text_var = tk.StringVar(value="")
        self.chat_role_var = tk.StringVar(value="全部")

        ttk.Label(fbar, text="时间").pack(side="left")
        ttk.Entry(fbar, textvariable=self.chat_time_var, width=10).pack(side="left", padx=(2, 8))
        ttk.Label(fbar, text="角色").pack(side="left")
        ttk.Combobox(
            fbar, textvariable=self.chat_role_var, values=["全部", "用户", "大肥鱼"], width=7, state="readonly"
        ).pack(side="left", padx=(2, 8))
        ttk.Label(fbar, text="说话人").pack(side="left")
        self.chat_name_picker = ttk.Combobox(fbar, textvariable=self.chat_name_var, width=12)
        self.chat_name_picker.pack(side="left", padx=(2, 8))
        ttk.Label(fbar, text="内容").pack(side="left")
        ttk.Entry(fbar, textvariable=self.chat_text_var, width=28).pack(side="left", padx=2)
        self.chat_count_var = tk.StringVar(value="")
        ttk.Label(fbar, textvariable=self.chat_count_var, foreground="#666").pack(side="left", padx=8)

        for var in (self.chat_time_var, self.chat_name_var, self.chat_text_var, self.chat_role_var):
            var.trace_add("write", lambda *_a: self._chat_filters_changed())

        cols = ("time", "role", "name", "text")
        self.chat_tree = ttk.Treeview(tab, columns=cols, show="headings", height=20)
        for c, title, w, anchor_ in (
            ("time", "时间", 130, "center"),
            ("role", "角色", 70, "center"),
            ("name", "说话人", 120, "w"),
            ("text", "内容", 560, "w"),
        ):
            self.chat_tree.heading(c, text=title)
            self.chat_tree.column(c, width=w, anchor=anchor_)
        self.chat_tree.heading("time", command=self._toggle_chat_order)  # 点表头也能切时间顺序
        sb = ttk.Scrollbar(tab, command=self.chat_tree.yview)
        self.chat_tree.configure(yscrollcommand=sb.set)
        self.chat_tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        sb.pack(side="right", fill="y", pady=(0, 6), padx=(0, 6))

        self._chat_rows: list = []      # 原始行（含 ts），刷新时重取
        self._chat_job = None           # 输入去抖的 after id
        self.refresh_chats()

    def _toggle_chat_order(self) -> None:
        self.chat_desc_var.set(not self.chat_desc_var.get())
        self.chat_sort_btn.configure(text="↓ 最新在前" if self.chat_desc_var.get() else "↑ 最早在前")
        self._render_chats()

    def _clear_chat_filters(self) -> None:
        self.chat_time_var.set("")
        self.chat_name_var.set("")
        self.chat_text_var.set("")
        self.chat_role_var.set("全部")
        self.chat_desc_var.set(True)
        self.chat_sort_btn.configure(text="↓ 最新在前")
        self._render_chats()

    def _chat_filters_changed(self) -> None:
        """每敲一个字都过一遍会卡（300 行 × 4 列），150ms 去抖。"""
        if self._chat_job is not None:
            try:
                self.after_cancel(self._chat_job)
            except tk.TclError:
                pass
        self._chat_job = self.after(150, self._render_chats)

    def refresh_chats(self) -> None:
        """只负责取数据（查库）；筛选和排序在内存里做。"""
        import datetime as _dt

        chats = self.store.history_chats(30)
        opts = ["全部"] + [f"{c['chat_id']}" for c in chats]
        self.chat_picker["values"] = opts
        if self.chat_filter_var.get() not in opts:
            self.chat_filter_var.set("全部")
        want = self.chat_filter_var.get()

        rows = []
        names: set = set()
        for r in self.store.history_all(600):
            if want != "全部" and r["chat_id"] != want:
                continue
            names.add(r["name"])
            rows.append(
                {
                    "ts": r["ts"],
                    "when": _dt.datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S"),
                    "role": r["role"],
                    "name": r["name"],
                    "text": r["text"],
                }
            )
        self._chat_rows = rows
        self.chat_name_picker["values"] = ["全部"] + sorted(names)
        self._render_chats()

    def _render_chats(self) -> None:
        self._chat_job = None
        if not hasattr(self, "chat_tree"):
            return
        name_kw = self.chat_name_var.get()
        if name_kw == "全部":
            name_kw = ""
        rows = filter_chat_rows(
            self._chat_rows,
            time_kw=self.chat_time_var.get(),
            role=self.chat_role_var.get(),
            name_kw=name_kw,
            text_kw=self.chat_text_var.get(),
            newest_first=bool(self.chat_desc_var.get()),
        )
        self.chat_tree.delete(*self.chat_tree.get_children())
        for r in rows:
            self.chat_tree.insert(
                "", "end",
                values=(r["when"], "用户" if r["role"] == "user" else "大肥鱼", r["name"], r["text"]),
            )
        total = len(self._chat_rows)
        self.chat_count_var.set(f"{len(rows)}/{total} 条" if len(rows) != total else f"{total} 条")

    # ── 好感度 ──
    def _build_affinity_tab(self) -> None:
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="好感度")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(bar, text="群/会话").pack(side="left")
        self.aff_chat_var = tk.StringVar(value="")
        self.aff_picker = ttk.Combobox(bar, textvariable=self.aff_chat_var, width=44, state="readonly")
        self.aff_picker.pack(side="left", padx=6)
        self.aff_picker.bind("<<ComboboxSelected>>", lambda _e: self.refresh_affinity())
        ttk.Button(bar, text="刷新", command=self.refresh_affinity).pack(side="left")
        ttk.Label(bar, text="改名").pack(side="left", padx=(10, 2))
        self.aff_rename_var = tk.StringVar(value="")
        ttk.Entry(bar, textvariable=self.aff_rename_var, width=10).pack(side="left")
        ttk.Button(bar, text="设为此名", command=self.rename_affinity).pack(side="left", padx=2)

        for label, delta in (("＋1", 1), ("＋5", 5), ("－1", -1)):
            ttk.Button(
                bar, text=label, width=4,
                command=lambda d=delta: self.adjust_affinity(d),
            ).pack(side="left", padx=2)
        self.aff_hint = ttk.Label(tab, text="手动加减分只改本地账本，不动人格", foreground="#666")
        self.aff_hint.pack(anchor="w", padx=8, pady=(0, 2))

        cols = ("name", "score", "level", "count", "last")
        self.aff_tree = ttk.Treeview(tab, columns=cols, show="headings", height=18)
        for c, title, w, anchor_ in (
            ("name", "群友", 160, "w"),
            ("score", "好感分", 90, "center"),
            ("level", "等级", 110, "center"),
            ("count", "互动次数", 100, "center"),
            ("last", "最近互动", 150, "center"),
        ):
            self.aff_tree.heading(c, text=title)
            self.aff_tree.column(c, width=w, anchor=anchor_)
        self.aff_tree.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        self.aff_legend = ttk.Label(tab, text="", foreground="#555")
        self.aff_legend.pack(anchor="w", padx=8, pady=(0, 6))
        self.refresh_affinity()

    def _chat_options(self) -> dict:
        """chat_id 太长看不清：下拉里显示「…末6位 · 消息数 · 最近时间」，映射回真实 id。"""
        opts: dict = {}
        for c in self.store.history_chats(30):
            cid = str(c["chat_id"])
            when = time.strftime("%m-%d %H:%M", time.localtime(int(c.get("last_ts") or 0)))
            opts[f"{cid[-6:]} · {c.get('msgs', 0)}条 · {when}"] = cid
        return opts

    def _aff_chat(self) -> str:
        opts = self._chat_options()
        labels = list(opts)
        if list(self.aff_picker["values"]) != labels:
            self.aff_picker["values"] = labels
        cur = opts.get(self.aff_chat_var.get(), "")
        if not cur and labels:
            self.aff_chat_var.set(labels[0])
            cur = opts[labels[0]]
        return cur

    def refresh_affinity(self) -> None:
        chat = self._aff_chat()
        if not hasattr(self, "aff_tree"):
            return
        self.aff_tree.delete(*self.aff_tree.get_children())
        rows = affinity_mod.board(self.store, chat, 200) if chat else []
        counts = self.store.kv_prefix(f"affn:{chat}:") if chat else {}
        lasts = self.store.kv_prefix(f"afflast:{chat}:") if chat else {}
        for r in rows:
            uid = str(r["uid"])
            when = ""
            try:
                when = time.strftime("%m-%d %H:%M", time.localtime(int(lasts.get(f"afflast:{chat}:{uid}", "0") or 0)))
            except (ValueError, OSError):
                when = "—"
            self.aff_tree.insert(
                "", "end",
                iid=uid,
                values=(
                    names_mod.resolve(self.store, self.cfg, uid),
                    r["score"], r["level"], counts.get(f"affn:{chat}:{uid}", "0"), when or "—",
                ),
            )
        self.aff_hint.configure(
            text=f"{len(rows)} 人有好感记录" if chat else "还没有会话记录，等群里聊两句再看"
        )
        ladder = "等级：" + " → ".join(f"{need}分 {name}" for need, name, _n in affinity_mod.LEVELS)
        self.aff_legend.configure(text=ladder + f"（每天最多涨 {affinity_mod.DAILY_GAIN_CAP} 分）")

    def rename_affinity(self) -> None:
        """把选中的群友绑定成固定名字（写本地 kv，群消息里立刻生效）。"""
        chat = self._chat_options().get(self.aff_chat_var.get(), "")
        sel = self.aff_tree.selection()
        name = self.aff_rename_var.get().strip()
        if not sel:
            self.aff_hint.configure(text="先点一位群友，再输入名字")
            return
        if not name:
            self.aff_hint.configure(text="名字不能空")
            return
        uid = sel[0]
        owner = names_mod.owner_of(self.store, self.cfg, name)
        if owner and owner != uid:
            self.aff_hint.configure(text=f"「{name}」已被 {owner[-6:]} 占用")
            return
        names_mod.bind(self.store, uid, name)
        self.aff_hint.configure(text=f"已把 {uid[-6:]} 固定叫「{name}」")
        self.refresh_affinity()

    def adjust_affinity(self, delta: int) -> None:
        chat = self._chat_options().get(self.aff_chat_var.get(), "")
        sel = self.aff_tree.selection()
        if not chat or not sel:
            self.aff_hint.configure(text="先在表里点一位群友，再点加减分")
            return
        uid = sel[0]
        before = affinity_mod.score_of(self.store, chat, uid)
        after = min(999, max(affinity_mod.MIN_SCORE, before + delta))
        self.store.kv_set(affinity_mod.score_key(chat, uid), str(after))
        self._idx, name, _note = affinity_mod.level_for(after)
        self.aff_hint.configure(text=f"已把群友{uid[-4:]} 从 {before} 调到 {after}（{name}）")
        self.refresh_affinity()

    # ── 日志 ──
    def _build_log_tab(self) -> None:
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="日志")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="刷新", command=self.refresh_log).pack(side="left")
        self.autolog_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="每 3 秒自动刷新", variable=self.autolog_var).pack(side="left", padx=8)
        ttk.Label(bar, text="过滤").pack(side="left", padx=(10, 2))
        self.log_filter_var = tk.StringVar(value="全部")
        ttk.Combobox(
            bar,
            textvariable=self.log_filter_var,
            values=["全部", "QQ 事件", "入站/回复", "接话/群", "报错", "图片/OCR"],
            width=12,
            state="readonly",
        ).pack(side="left")
        ttk.Button(bar, text="一键排查", command=self.run_diagnose).pack(side="left", padx=8)
        ttk.Button(bar, text="清空显示", command=lambda: self.log_text.delete("1.0", "end")).pack(side="left")
        self.log_state_var = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.log_state_var, foreground="#666").pack(anchor="w", padx=8)
        self.log_text = tk.Text(tab, wrap="none", font=("Menlo", 10))
        self.log_text.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.refresh_log()
        self.after(3000, self._auto_log_tick)

    def _auto_log_tick(self) -> None:
        if self.autolog_var.get():
            self.refresh_log()
        self.after(3000, self._auto_log_tick)

    # ── 行为 ──
    def _run_bg(self, work, done=None) -> None:
        """所有要等的活都丢线程。

        Tk 不是线程安全的：`after` 只能从主线程调（从工作线程调会
        `RuntimeError: main thread is not in main loop`），所以结果写队列，
        由主线程的 _pump_bg 定时取走再画。
        """
        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - 线程里出错也要回主线程报
                result = exc
            self._bg_q.put((done, result))

        threading.Thread(target=runner, daemon=True).start()

    def _pump_bg(self) -> None:
        """主线程队列泵：每 80ms 取一次后台结果，Tk 调用全在主线程。"""
        while True:
            try:
                done, result = self._bg_q.get_nowait()
            except queue.Empty:
                break
            if done is not None:
                try:
                    done(result)
                except tk.TclError:
                    pass
        try:
            self.after(80, self._pump_bg)
        except tk.TclError:
            pass

    def save_all(self) -> None:
        """写盘是毫秒级；核对放到后台，点完立刻能继续操作。"""
        if self._save_busy:
            return
        try:
            self._save_all_checked()  # 只做内存组装 + 落盘
        except ValueError as exc:
            self.msg_var.set(f"保存失败：{exc}")
            return
        stamp = time.strftime("%H:%M:%S")
        self.saved_at = stamp
        self._save_busy = True
        self.save_btn.configure(state="disabled")
        self.msg_var.set(f"已保存 {stamp} · 正在核对是否生效…")
        self._refresh_apply(stamp=stamp)

    # ── 生效确认 ──
    def _build_apply_tab(self) -> None:
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="生效确认")
        top = ttk.Frame(tab)
        top.pack(fill="x", padx=10, pady=(8, 4))
        self.apply_hint = ttk.Label(
            top,
            text="保存后按这里，和运行中的机器人对一遍参数：一致＝真的生效，不一致＝要重启。"
                 "（app_id / 渠道开关 / 微信配置改完仍要重启才生效）",
            foreground="#555",
        )
        self.apply_hint.pack(side="left")
        self._apply_btn = ttk.Button(top, text="↻ 刷新", command=self._refresh_apply)
        self._apply_btn.pack(side="right")
        cols = ("item", "file", "live", "state")
        self.apply_tree = ttk.Treeview(tab, columns=cols, show="headings", height=18)
        for c, title, w in zip(cols, ("参数", "配置文件里的值", "机器人正在用的值", "状态"), (250, 250, 250, 120)):
            self.apply_tree.heading(c, text=title)
            self.apply_tree.column(c, width=w, anchor="w")
        self.apply_tree.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.apply_tree.tag_configure("bad", foreground="#b3261e")
        self.apply_tree.tag_configure("ok", foreground="#1f5f2f")

    def _set_apply_busy(self, busy: bool) -> None:
        self._apply_busy = busy
        if busy:
            self._apply_btn.configure(state="disabled")
        else:
            try:
                self._apply_btn.configure(state="normal")
            except tk.TclError:
                pass
        if not busy and self._save_busy:
            self._save_busy = False
            try:
                self.save_btn.configure(state="normal")
            except tk.TclError:
                pass

    def _refresh_apply(self, stamp: str = "") -> None:
        """后台要一份「机器人正在用的参数」再逐项比对（SIGUSR1 + 等日志，最长 3s）。"""
        if self._apply_busy:
            return
        self._set_apply_busy(True)
        self.apply_hint.configure(text="正在向机器人要当前生效参数…")

        def work():  # 线程内：读配置 + 发信号 + 轮询日志；一致性重试也在这里等
            want = flatten_cfg(settings.load_config())
            log = settings.LOG_DIR / "fatfish.log"
            live, fresh = wait_live_params(log, bot_pid(), timeout=1.5)
            if not live:
                return want, live, fresh
            deadline = time.monotonic() + 6.0  # 机器人 3s 轮询 mtime，给它几轮
            while not all(param_equal(want[k], live.get(k, "—")) for k in want):
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.4)
                nxt, nf = wait_live_params(log, bot_pid(), timeout=1.5)
                if not nxt:
                    break
                live, fresh = nxt, nf or fresh
            return want, live, fresh

        def done(res):  # 回主线程画
            self._set_apply_busy(False)
            if isinstance(res, Exception):
                self.apply_hint.configure(text=f"核对失败：{res}")
                if stamp:
                    self.msg_var.set(f"已保存 {stamp} · 核对失败：{res}")
                return
            want, live, fresh = res
            self._render_apply(want, live, fresh, stamp)

        self._run_bg(work, done)

    # 老名字保留：自检和历史调用点还在用
    def _verify_effective(self) -> tuple:
        self._refresh_apply()
        return self._apply_busy, ""

    def _render_apply(self, want: dict, live: dict, fresh: bool, stamp: str = "") -> None:
        for row in self.apply_tree.get_children():
            self.apply_tree.delete(row)
        if not live:
            self.apply_hint.configure(
                text="机器人没在运行（或还没打过快照）：现在保存的内容会在下次启动时加载。"
            )
            for key, val in sorted(want.items()):
                self.apply_tree.insert("", "end", values=(key, val, "—", "未运行"))
            if stamp:
                self.msg_var.set(f"已保存 {stamp} · 机器人未运行，重启后生效")
            return

        rows = compare_params(want, live)
        bad = [key for key, _w, _l, same in rows if not same]
        for key, wv, lv, same in rows:
            self.apply_tree.insert(
                "", "end", values=(key, wv, lv, "一致" if same else "需重启"),
                tags=("ok" if same else "bad",),
            )
        total = len(rows)
        if bad:
            more = "…" if len(bad) > 6 else ""
            self.apply_hint.configure(
                text=f"{total - len(bad)}/{total} 项已生效；这些要重启机器人：" + "、".join(bad[:6]) + more
            )
            note = f"{total - len(bad)}/{total} 项已生效，{len(bad)} 项需重启"
        else:
            tail = "（本次新快照）" if fresh else "（沿用上次快照）"
            self.apply_hint.configure(text=f"全部 {total} 项与运行中的机器人一致{tail}。")
            note = f"全部 {total} 项已生效"
        if stamp:
            self.msg_var.set(f"已保存 {stamp} · {note}")

    def _save_all_checked(self) -> None:
        c = self.cfg
        persona = c.setdefault("persona", {})
        for key in ("self_name", "master_name", "max_reply_chars", "cooldown_seconds",
                    "dm_cooldown_seconds", "context_messages", "max_replies_per_minute"):
            if key in self.vars:
                val = self.vars[key].get().strip()
                persona[key] = int(val) if key not in ("self_name", "master_name") and val.isdigit() else val
        persona["interrupt_prob"] = round(float(self.interrupt_var.get()), 3)

        llm = c.setdefault("llm", {})
        llm["model"] = self.model_var.get()
        llm["thinking"] = self.thinking_var.get()
        llm["reasoning_effort"] = self.effort_var.get()
        def _num(var, name: str, default: float, low: float, high: float) -> float:
            raw = str(var.get()).strip()
            if not raw:
                return default
            try:
                val = float(raw)
            except ValueError:
                raise ValueError(f"{name} 需要数字，当前是「{raw}」") from None
            if not (low <= val <= high):
                raise ValueError(f"{name} 应在 {low}~{high} 之间，当前 {val}")
            return val

        llm["max_tokens"] = int(_num(self.max_tokens_var, "max_tokens", 240, 32, 2000))
        llm["temperature"] = round(_num(self.temp_var, "temperature", 0.9, 0.0, 2.0), 2)
        llm["timeout_seconds"] = int(_num(self.timeout_var, "timeout_seconds", 60, 5, 300))

        budget = c.setdefault("budget", {})
        budget["daily_cap_cny"] = _num(self.cap_var, "每日上限", 5.0, 0.1, 1000.0)
        budget["reserve_cny"] = _num(self.reserve_var, "安全垫", 0.1, 0.0, 100.0)
        budget["low_balance_warn_cny"] = _num(self.warn_var, "余额提醒线", 0.5, 0.0, 100.0)
        if budget["reserve_cny"] >= budget["daily_cap_cny"]:
            raise ValueError("安全垫必须小于每日上限，否则永远调不动模型")

        triggers = c.setdefault("triggers", {})
        triggers["keywords"] = [k.strip() for k in self.keywords_var.get().split(",") if k.strip()]
        triggers["reply_in_dm"] = bool(self.reply_dm_var.get())
        triggers["reply_on_mention"] = bool(self.reply_mention_var.get())
        triggers["group_keyword_reply"] = bool(self.reply_keyword_var.get())

        features = c.setdefault("features", {})
        for k, var in self.feature_vars.items():
            features[k] = bool(var.get())

        channels = c.setdefault("channels", {})
        wc = channels.setdefault("weixin", {})
        wc["enabled"] = bool(self.wx_enabled_var.get())
        wc["account_id"] = self.wx_account_var.get().strip()
        wc["group_policy"] = self.wx_policy_var.get()
        qc = channels.setdefault("qq", {})
        qc["enabled"] = bool(self.qq_enabled_var.get())
        qc["app_id"] = self.qq_appid_var.get().strip()
        qc["group_policy"] = self.qq_policy_var.get()
        qc["allowed_groups"] = [g.strip() for g in self.qq_allow_var.get().split(",") if g.strip()]
        chime = qc.setdefault("chime", {})
        chime["enabled"] = bool(self.chime_enabled_var.get())
        chime["random_prob"] = round(_num(self.chime_prob_var, "接话随机概率", 0.03, 0.0, 1.0), 3)
        chime["cooldown_seconds"] = int(_num(self.chime_cd_var, "接话冷却", 60, 0, 3600))
        chime["max_per_hour"] = int(_num(self.chime_max_var, "接话每小时上限", 30, 0, 600))
        runtime = c.setdefault("runtime", {})
        for key, var in getattr(self, "rt_vars", {}).items():
            runtime[key] = int(_num(var, f"运行窗口 {key}", 1, 1, 86400))
        pro = qc.setdefault("proactive", {})
        pro["enabled"] = bool(self.pro_enabled_var.get())
        pro["idle_minutes"] = int(_num(self.pro_idle_var, "冒泡冷场分钟", 120, 1, 1440))
        pro["max_per_day"] = int(_num(self.pro_max_var, "冒泡每日上限", 3, 0, 50))

        text = self.persona_text.get("1.0", "end").rstrip() + "\n"
        if "【PERSONA_LOAD】" not in text:
            raise ValueError("人格 core 段（【PERSONA_LOAD】…）被删了，拒绝保存")
        settings.save_config(c)
        settings.PERSONA_PATH.write_text(text, encoding="utf-8")

    def _bot_env(self) -> dict:
        """启停机器人时的环境：窗口是哪个机器人就操作哪个，别错启主机器人。"""
        env = dict(os.environ)
        root = self._profile_root(self._current_profile())
        if root is None:
            env.pop("FATFISH_ROOT", None)
        else:
            env["FATFISH_ROOT"] = str(root)
        env["PYTHONPATH"] = str(self._repo_root())
        return env

    def _call_bot(self, verb: str) -> None:
        env, cwd, name = self._bot_env(), str(self._repo_root()), self._current_profile()
        self.msg_var.set(f"正在{('启动' if verb == 'start' else '停止')} {name}…")

        def work():
            out = subprocess.run(
                [sys.executable, "-m", "fatfish.cli", verb],
                cwd=cwd, env=env, capture_output=True, text=True,
            )
            tail = (out.stdout or out.stderr).strip().splitlines()
            return tail[-1] if tail else "见日志"

        def done(res):
            if isinstance(res, Exception):
                self.msg_var.set(f"{verb} 失败：{res}")
            else:
                self.msg_var.set(f"已{('启动' if verb == 'start' else '停止')} {name}：{res}")
            self.refresh_status()

        self._run_bg(work, done)

    def start_bot(self) -> None:
        self.save_all()
        self._call_bot("start")

    def stop_bot(self) -> None:
        self._call_bot("stop")

    def refresh_status(self) -> None:
        pid = None
        if settings.PID_PATH.exists():
            try:
                pid = int(settings.PID_PATH.read_text().strip())
            except Exception:  # noqa: BLE001
                pid = None
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        cfg = settings.load_config()
        cap = float(cfg.get("budget", {}).get("daily_cap_cny", 5))
        spent = self.store.spend_day()
        state = f"运行中 pid={pid}" if alive else "未运行"
        last = "最近无消息"
        try:
            chats = self.store.history_chats(1)
            if chats:
                import datetime as _dt

                last = "最近消息 " + _dt.datetime.fromtimestamp(chats[0]["last_ts"]).strftime("%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            pass
        self.status_var.set(f"{state} · 今日 ¥{spent:.4f} / ¥{cap:.2f} · {last}")
        self.spend_label.configure(text=f"今日已花 ¥{spent:.4f} / 上限 ¥{cap:.2f}")
        self.progress["value"] = min(100.0, spent / cap * 100 if cap else 0)
        self.after(5000, self.refresh_status)

    def refresh_usage(self) -> None:
        for item in self.usage_tree.get_children():
            self.usage_tree.delete(item)
        for row in self.store.daily_totals(14):
            self.usage_tree.insert("", "end", values=(row["day"], row["calls"], f"{(row['cost'] or 0):.4f}", int(row["out_tokens"] or 0)))

    def refresh_log(self) -> None:
        path = settings.LOG_DIR / "fatfish.log"
        if not path.exists():
            self.log_text.delete("1.0", "end")
            self.log_text.insert("1.0", "（还没有日志，启动后生成 logs/fatfish.log）")
            return
        try:
            st = path.stat()
        except OSError as exc:
            lines = [f"读取日志失败: {exc}"]
        else:
            sig = (st.st_mtime_ns, st.st_size)
            if sig == getattr(self, "_log_sig", None) and self.log_text.get("1.0", "end").strip():
                return  # 日志没变就别重画控件
            self._log_sig = sig
            lines = read_log_tail(path, max_bytes=400_000, max_lines=200) or ["（日志暂时读不到）"]
        pats = {
            "QQ 事件": ("QQ 事件", "QQ READY", "群变更", "重连"),
            "入站/回复": ("QQ 入站", "已回复", "发送成功", "推送"),
            "接话/群": ("接话", "冒泡", "群消息不接话"),
            "报错": ("ERROR", "WARNING", "失败", "异常"),
            "图片/OCR": ("图片", "OCR", "vision"),
        }
        want = self.log_filter_var.get() if hasattr(self, "log_filter_var") else "全部"
        if want in pats:
            lines = [ln for ln in lines if any(k in ln for k in pats[want])]
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", "\n".join(lines[-300:]))
        self.log_text.see("end")
        try:
            events = {}
            for ln in lines:
                for m in __import__("re").findall(r"(?:QQ 事件 t=|QQ 入站 event=)(\w+)", ln):
                    events[m] = events.get(m, 0) + 1
            cap = float(self.cfg.get("budget", {}).get("daily_cap_cny", 5) or 5)
            self.log_state_var.set(
                f"{'运行中' if self._alive() else '未运行'}　今日 ¥{self.store.spend_day():.3f}/{cap:.2f}　"
                f"本次日志事件：{events or '无'}"
            )
        except Exception:  # noqa: BLE001
            pass

    def _alive(self) -> bool:
        if not settings.PID_PATH.exists():
            return False
        try:
            os.kill(int(settings.PID_PATH.read_text().strip()), 0)
            return True
        except Exception:  # noqa: BLE001
            return False

    def run_diagnose(self) -> None:
        try:
            out = subprocess.run(
                [sys.executable, "-m", "fatfish.cli", "diagnose"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout
        except Exception as exc:  # noqa: BLE001
            out = f"排查失败: {exc}"
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", out)

    def test_llm(self) -> None:
        self.save_all()
        self.test_out.delete("1.0", "end")
        self.test_out.insert("1.0", "测试中…")

        def worker() -> None:
            try:
                from fatfish.llm import LLM
                cfg = settings.load_config()
                llm = LLM(cfg, self.store)
                before = llm.spent_today()
                msgs = [
                    {"role": "system", "content": "你是大肥鱼，傲娇鲸鱼娘，中文口语，一句话。"},
                    {"role": "user", "content": "在吗？打个招呼"},
                ]
                reply = asyncio.run(llm.chat(msgs, channel="gui", chat_id="gui-test"))
                after = llm.spent_today()
                out = f"{reply.text}\n\n[{reply.model} 输入{reply.prompt_tokens} 输出{reply.completion_tokens} tokens，本次 ¥{after - before:.4f}]"
            except Exception as exc:  # noqa: BLE001
                out = f"失败：{exc}"
            self.after(0, lambda: (self.test_out.delete("1.0", "end"), self.test_out.insert("1.0", out)))

        threading.Thread(target=worker, daemon=True).start()


def main() -> None:
    app = Tuner()
    # 从终端/脚本拉起时窗口常常藏在别的 app 后面，主动抬到最前
    try:
        app.deiconify()
        app.lift()
        app.attributes("-topmost", True)
        app.focus_force()
        app.after(1500, lambda: app.attributes("-topmost", False))
    except Exception:  # noqa: BLE001
        pass
    app.mainloop()


if __name__ == "__main__":
    main()