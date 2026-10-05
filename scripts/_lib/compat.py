"""跨平台兼容层：文件锁、原子写、时间、UTF-8 IO。

仅依赖 Python 标准库，兼容 Python 3.8+。
所有模块统一从这里取锁与原子写，避免各平台行为不一致。
"""

import json
import os
import sys
import time
from typing import Any, Dict, Optional

__all__ = [
    "IS_WINDOWS",
    "PY_VERSION",
    "now_ms",
    "utc_iso",
    "lock_file",
    "unlock_file",
    "atomic_write_text",
    "atomic_write_json",
    "append_text",
    "read_json",
    "open_text",
    "mkdirs",
    "ensure_utf8_stdio",
]

IS_WINDOWS = os.name == "nt"
PY_VERSION = sys.version_info[:3]

try:  # pragma: no cover - 平台分支
    import fcntl as _fcntl
except Exception:  # Windows
    _fcntl = None

try:  # pragma: no cover - 平台分支
    import msvcrt as _msvcrt
except Exception:  # POSIX
    _msvcrt = None


def ensure_utf8_stdio() -> None:
    """强制 stdout/stderr 使用 UTF-8。

    Windows 默认可能是 GBK/codecs，中文输出会抛 UnicodeEncodeError。
    每个入口脚本第一行都应调用本函数。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_iso(ms: Optional[int] = None) -> str:
    """epoch 毫秒 -> 本地可读时间字符串。

    存储一律用 epoch ms，只在展示时转字符串，避免时区与序列化歧义。
    """
    if ms is None:
        ms = now_ms()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000.0))


def mkdirs(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def open_text(path: str, mode: str = "r"):
    """所有文本 IO 必须走这里：显式 UTF-8 + errors=replace。"""
    return open(path, mode, encoding="utf-8", errors="replace", newline="\n" if "w" in mode or "a" in mode else None)


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open_text(path, "r") as fh:
            return json.load(fh)
    except Exception:
        return default


def atomic_write_text(path: str, text: str) -> None:
    """tmp 写入 + os.replace，避免进程被杀留下半截文件。"""
    mkdirs(os.path.dirname(path) or ".")
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open_text(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: str, obj: Any, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent))


def append_text(path: str, text: str) -> None:
    mkdirs(os.path.dirname(path) or ".")
    with open_text(path, "a") as fh:
        fh.write(text)


def lock_file(path: str, timeout_s: float = 5.0, stale_s: float = 30.0):
    """获取目录/文件锁，返回句柄；失败返回 None。

    锁文件写入 pid + 时间戳。超过 stale_s 视为死锁（进程被杀），可被抢占。
    调用方必须在 finally 里 unlock_file。
    """
    mkdirs(os.path.dirname(path) or ".")
    deadline = time.time() + timeout_s
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, ("%d %d" % (os.getpid(), now_ms())).encode("utf-8"))
            os.close(fd)
            return path
        except FileExistsError:
            pass
        except OSError:
            pass
        # 已存在：检查是否过期
        try:
            raw = open_text(path, "r").read().strip().split()
            age = (now_ms() - int(raw[1])) / 1000.0 if len(raw) >= 2 else stale_s + 1
            if age > stale_s:
                try:
                    os.remove(path)
                except Exception:
                    pass
                continue
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            continue
        if time.time() >= deadline:
            return None
        time.sleep(0.05)


def unlock_file(handle: Optional[str]) -> None:
    if not handle:
        return
    try:
        os.remove(handle)
    except Exception:
        pass


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def free_bytes(path: str) -> int:
    try:
        import shutil

        return shutil.disk_usage(path).free
    except Exception:
        return 0


def total_bytes(path: str) -> int:
    try:
        import shutil

        return shutil.disk_usage(path).total
    except Exception:
        return 0


def human_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fTB" % n


def load_state(path: str) -> Dict[str, Any]:
    return read_json(path, {}) or {}
