"""路径解析与存储根探测。

这是本技能"可移植"的核心：绝不硬编码任何机器专属路径。

存储根优先级链：
  1. 环境变量 CONTEXT_VAULT_HOME
  2. <home>/.workbuddy/context-vault/config.json 的 root（校验 marker 有效）
  3. 已有 .context-vault-marker 的目录（换机/插移动硬盘时自动找回旧归档）
  4. 自动探测：枚举挂载点 -> 过滤 -> 打分
  5. 回落 <home>/.workbuddy/context-vault/archive
"""

import json
import os
import shutil
import string
import time
from typing import Any, Dict, List, Optional

from . import compat

__all__ = [
    "APP_NAME",
    "MARKER_NAME",
    "home_dir",
    "home_candidates",
    "skills_dir_candidates",
    "settings_file_candidates",
    "transcript_root_candidates",
    "config_path",
    "load_config",
    "save_config",
    "resolve_root",
    "probe_candidates",
    "encode_project_slug",
    "session_dir",
    "project_dir",
]

APP_NAME = "context-vault"
MARKER_NAME = ".context-vault-marker"

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "root": None,          # 绝对路径；为 None 表示尚未初始化
    "root_auto": True,     # True 时每次 resolve 都校验 marker，失效则重探测
    "min_free_gb": 2,
    "max_total_gb": 20,
    "copy_raw": True,      # L3：复制原始 transcript 字节（源文件会被宿主清理）
    "max_bytes_per_run": 4 * 1024 * 1024,
    "rolling_max_chars": 4000,
    "tool_result_max_chars": 8000,
    "chunk_max_chars": 4000,
    "keep_snapshots": False,
    "redact": True,
    "upload_cloud": False,
}

# Windows 盘符类型：0 未知 / 1 无根目录 / 2 可移动 / 3 固定 / 4 网络 / 5 光驱 / 6 RAM
_WIN_SKIP_TYPES = {0, 1, 2, 4, 5, 6}
_POSIX_SKIP_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "overlay", "aufs", "squashfs", "iso9660", "udf", "9p", "cifs", "nfs", "nfs4",
}


# ---------------------------------------------------------------- home / 宿主目录

def home_dir() -> str:
    return os.path.expanduser("~")


def home_candidates() -> List[str]:
    """宿主家目录下的配置目录候选（两套 home 并存，顺序即优先级）。"""
    h = home_dir()
    return [os.path.join(h, d) for d in (".workbuddy", ".codebuddy", ".claude", ".agents")]


def skills_dir_candidates() -> List[str]:
    """用户级 skills 安装目录候选。"""
    return [os.path.join(d, "skills") for d in home_candidates()]


def settings_file_candidates() -> List[str]:
    """宿主 settings.json 候选（hooks 写入目标）。"""
    return [os.path.join(d, "settings.json") for d in home_candidates()]


def transcript_root_candidates() -> List[str]:
    """transcript 落盘根目录候选。"""
    return [os.path.join(d, "projects") for d in home_candidates()]


def detect_transcript_root() -> Optional[str]:
    for p in transcript_root_candidates():
        if os.path.isdir(p):
            return p
    return None


def _detect_home_kind() -> str:
    """判定真实宿主目录：projects 存在的那个优先（transcript 在那里）。"""
    for d in home_candidates():
        if os.path.isdir(os.path.join(d, "projects")):
            return os.path.basename(d)
    return os.path.basename(home_candidates()[0])


# ---------------------------------------------------------------- config

def _config_dir() -> str:
    h = home_dir()
    kind = _detect_home_kind()
    return os.path.join(h, kind, APP_NAME)


def config_path() -> str:
    return os.path.join(_config_dir(), "config.json")


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(compat.read_json(config_path(), {}) or {})
    # 兼容：也读另一个 home 的旧配置
    if not cfg.get("root"):
        h = home_dir()
        for kind in (".workbuddy", ".codebuddy", ".claude", ".agents"):
            alt = os.path.join(h, kind, APP_NAME, "config.json")
            if os.path.exists(alt):
                cfg.update(compat.read_json(alt, {}) or {})
                break
    return cfg


def save_config(cfg: Dict[str, Any]) -> str:
    p = config_path()
    compat.mkdirs(os.path.dirname(p))
    compat.atomic_write_json(p, cfg)
    return p


# ---------------------------------------------------------------- 挂载点枚举

def _win_mount_points() -> List[Dict[str, Any]]:
    """Windows 挂载点。

    实测坑：GetLogicalDriveStringsW 在某些会话下只返回 C:\\，
    必须用 A-Z 逐个 isdir 兜底，否则探测必然只看到系统盘。
    """
    drives = set()
    try:  # 1) API（不可单独依赖）
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetLogicalDriveStringsW(1024, buf)
        drives.update(d for d in buf.value.split("\x00") if d)
    except Exception:
        pass
    for letter in string.ascii_uppercase:  # 2) 兜底（必须）
        drives.add("%s:\\" % letter)

    sysdrv = (os.environ.get("SystemDrive") or "C:").rstrip(":\\").upper() + ":"
    out = []
    for d in sorted(drives):
        if not os.path.isdir(d):
            continue
        dtype = 3
        try:
            import ctypes

            dtype = ctypes.windll.kernel32.GetDriveTypeW(d)
        except Exception:
            pass
        if dtype in _WIN_SKIP_TYPES:
            continue
        out.append({"path": d, "is_system": d.upper().startswith(sysdrv)})
    return out


def _read_mount_fstypes() -> Dict[str, str]:
    m: Dict[str, str] = {}
    try:  # Linux
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3:
                    m[parts[1]] = parts[2]
        return m
    except Exception:
        pass
    try:  # macOS
        import subprocess

        out = subprocess.run(["mount"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if " on " in line and "(" in line:
                mp = line.split(" on ", 1)[1].split(" (", 1)[0]
                fs = line.rsplit("(", 1)[-1].split(",")[0].strip(")")
                m[mp] = fs
    except Exception:
        pass
    return m


def _posix_mount_points() -> List[Dict[str, Any]]:
    fstypes = _read_mount_fstypes()
    home = home_dir()
    roots = ["/"]
    for base in ("/Volumes", "/mnt", "/media"):
        p = os.path.join("/", base.lstrip("/"))
        if os.path.isdir(p):
            try:
                roots += [os.path.join(p, c) for c in sorted(os.listdir(p))
                          if os.path.isdir(os.path.join(p, c))]
            except Exception:
                pass
    for extra in ("/data", "/srv", "/opt/data"):
        if os.path.isdir(extra):
            roots.append(extra)
    if home not in roots:
        roots.append(home)

    out = []
    for r in roots:
        real = os.path.realpath(r)
        fs = fstypes.get(real, "")
        if fs in _POSIX_SKIP_FS and real != "/":
            continue
        out.append({"path": r, "is_system": real in ("/", os.path.realpath(home))})
    return out


def _writable(path: str) -> bool:
    """可写性必须实写实测：os.access 会被权限/配额/只读挂载欺骗。"""
    try:
        compat.mkdirs(path)
        probe = os.path.join(path, ".wtest-%d-%d" % (os.getpid(), compat.now_ms()))
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        with open(probe, "r", encoding="utf-8") as fh:
            ok = fh.read() == "ok"
        os.remove(probe)
        return ok
    except Exception:
        return False


# ---------------------------------------------------------------- 探测与打分

def probe_candidates(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """枚举候选存储根并按分数降序返回。"""
    cfg = cfg or load_config()
    min_free_gb = float(cfg.get("min_free_gb") or 2)
    mounts = _win_mount_points() if compat.IS_WINDOWS else _posix_mount_points()
    home = home_dir()
    res: List[Dict[str, Any]] = []

    for mp in mounts:
        base = mp["path"]
        subs = [base]
        if not compat.IS_WINDOWS:
            subs += [os.path.join(base, "Users"), os.path.join(base, "home")]
        for s in subs:
            if not os.path.isdir(s):
                continue
            cand = os.path.join(s, APP_NAME)
            try:
                usage = shutil.disk_usage(s)
            except Exception:
                continue
            free_gb = usage.free / (1024.0 ** 3)
            if free_gb < min_free_gb:
                continue
            if not _writable(cand):
                continue
            score = min(free_gb, 500.0)
            if not mp["is_system"]:
                score += 30.0
            has_marker = os.path.exists(os.path.join(cand, MARKER_NAME))
            if has_marker:
                score += 1000.0  # 已有归档：换机/插盘自动找回
            try:
                if os.path.realpath(s) == os.path.realpath(home):
                    score -= 500.0
            except Exception:
                pass
            res.append({
                "root": cand,
                "free_gb": round(free_gb, 1),
                "total_gb": round(usage.total / (1024.0 ** 3), 1),
                "is_system": bool(mp["is_system"]),
                "has_marker": has_marker,
                "score": round(score, 1),
                "fallback": False,
            })

    fallback = os.path.join(home, _detect_home_kind(), APP_NAME, "archive")
    try:
        fb_free = shutil.disk_usage(home).free / (1024.0 ** 3)
    except Exception:
        fb_free = 0.0
    res.append({
        "root": fallback,
        "free_gb": round(fb_free, 1),
        "total_gb": 0.0,
        "is_system": True,
        "has_marker": os.path.exists(os.path.join(fallback, MARKER_NAME)),
        "score": -1000.0,
        "fallback": True,
    })
    res.sort(key=lambda x: -x["score"])
    return res


def _root_is_valid(root: Optional[str]) -> bool:
    return bool(root) and os.path.exists(os.path.join(str(root), MARKER_NAME))


def resolve_root(cfg: Optional[Dict[str, Any]] = None, auto_repair: bool = True) -> str:
    """解析当前生效的存储根，必要时自动重探测并写回配置。"""
    cfg = cfg or load_config()

    env = os.environ.get("CONTEXT_VAULT_HOME")
    if env:
        env = os.path.abspath(os.path.expanduser(env))
        if _writable(env):
            return env

    root = cfg.get("root")
    if root:
        root = os.path.abspath(os.path.expanduser(str(root)))
        if _root_is_valid(root) or not cfg.get("root_auto", True):
            return root
        # marker 失效（拔盘 / 换机）：重新发现或重探测
        if auto_repair:
            rediscovered = _rediscover()
            if rediscovered:
                cfg["root"] = rediscovered
                save_config(cfg)
                return rediscovered

    cands = probe_candidates(cfg)
    chosen = cands[0]["root"] if cands else os.path.join(home_dir(), _detect_home_kind(), APP_NAME, "archive")
    if auto_repair:
        cfg["root"] = chosen
        save_config(cfg)
    return chosen


def _rediscover() -> Optional[str]:
    """遍历候选挂载点，找含 marker 的已有归档目录。"""
    for c in probe_candidates():
        if c.get("has_marker"):
            return c["root"]
    return None


def init_root(root: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """创建存储根：写 marker + 备份 config + 建子目录。"""
    cfg = cfg or load_config()
    root = os.path.abspath(os.path.expanduser(root))
    compat.mkdirs(root)
    marker = {
        "app": APP_NAME,
        "version": 1,
        "created_at": compat.now_ms(),
        "created_at_iso": compat.utc_iso(),
        "machine": _machine_id(),
    }
    compat.atomic_write_json(os.path.join(root, MARKER_NAME), marker)
    compat.mkdirs(os.path.join(root, "index"))
    compat.mkdirs(os.path.join(root, "projects"))
    compat.mkdirs(os.path.join(root, "logs"))
    cfg["root"] = root
    compat.atomic_write_json(os.path.join(root, "config.json"), cfg)
    save_config(cfg)
    return marker


def _machine_id() -> str:
    """机器标识（仅用于提示，不用于鉴权）。"""
    raw = "%s|%s" % (os.environ.get("USERNAME") or os.environ.get("USER") or "?", home_dir())
    import hashlib

    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]


# ---------------------------------------------------------------- 归档布局

def encode_project_slug(cwd: str) -> str:
    """工作区路径 -> 宿主 transcript 目录名。

    实测：D:\\Workbuddy上下文加长Skill -> d-Workbuddy上下文加长Skill
    仅作兜底；优先使用 Path(transcript_path).parent.name（零出错）。
    """
    s = (cwd or "").replace("/", "\\")
    if s.startswith("\\\\"):  # UNC
        return s[2:].replace("\\", "-").lower()
    if len(s) >= 2 and s[1] == ":":
        s = s[0].lower() + s[1:]
    return s.replace(":", "").replace("\\", "-")


def project_dir(root: str, slug: str) -> str:
    return os.path.join(root, "projects", slug)


def session_dir(root: str, slug: str, session_id: str) -> str:
    return os.path.join(project_dir(root, slug), session_id)


def index_path(root: str) -> str:
    return os.path.join(root, "index", "sessions.jsonl")


def log_path(root: str) -> str:
    return os.path.join(root, "logs", "ctx-%s.log" % time.strftime("%Y-%m-%d"))


def log_line(root: str, msg: str) -> None:
    try:
        compat.append_text(log_path(root), "[%s] %s\n" % (compat.utc_iso(), msg))
    except Exception:
        pass
