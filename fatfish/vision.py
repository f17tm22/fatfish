"""图片理解：用 macOS 自带 Vision 框架做 OCR（零额外密钥、离线）。

DeepSeek 的 deepseek-flash / deepseek-v4-pro 都是纯文本模型，看不见图，
所以群友发截图时先把图上的字读出来，再当文本喂给模型。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OCR_BIN = ROOT / "tools" / "ocr"
OCR_SRC = ROOT / "tools" / "ocr.swift"
MAX_DIM = 1600


def ensure_binary() -> bool:
    """tools/ocr 不存在时用 swiftc 现编一个（只需一次）。"""
    if OCR_BIN.exists():
        return True
    if not OCR_SRC.exists() or not shutil.which("swiftc"):
        return False
    try:
        subprocess.run(
            ["swiftc", "-O", str(OCR_SRC), "-o", str(OCR_BIN)],
            check=True,
            capture_output=True,
            timeout=180,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return OCR_BIN.exists()


def _downscale(path: Path) -> Path:
    """大图先缩到 1600px 内：Vision 对 2585px 的图要 30s，缩完 0.8s。"""
    try:
        out = Path(tempfile.gettempdir()) / f"fatfish_ocr_{path.stem[:20]}.png"
        subprocess.run(
            ["sips", "-Z", str(MAX_DIM), str(path), "--out", str(out)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return out if out.exists() else path
    except (subprocess.SubprocessError, OSError):
        return path


def ocr_image(path: str | Path, max_chars: int = 600) -> str:
    """返回图上文字（换行已合并成一行）；失败或无文字返回空串。"""
    p = Path(path)
    if not p.exists() or not ensure_binary():
        return ""
    small = _downscale(p)
    try:
        res = subprocess.run(
            [str(OCR_BIN), str(small)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    finally:
        if small != p:
            try:
                small.unlink()
            except OSError:
                pass
    if res.returncode != 0:
        return ""
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    text = " / ".join(lines)
    return text[:max_chars]
