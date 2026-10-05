"""L0 全局会话索引：index/sessions.jsonl（append-only）。

sessions.jsonl 采用追加写，同一 session_id 后出现的记录覆盖先前的；
rebuild 会扫描全部 meta.json 重新生成，用于去重与修复。
"""

import json
import os
from typing import Any, Dict, List, Optional

from . import compat, paths

__all__ = ["upsert", "rebuild", "list_sessions", "find_session", "iter_session_dirs"]


def index_file(root: str) -> str:
    return paths.index_path(root)


def iter_session_dirs(root: str) -> List[str]:
    out: List[str] = []
    projects_root = os.path.join(root, "projects")
    if not os.path.isdir(projects_root):
        return out
    for slug in sorted(os.listdir(projects_root)):
        pdir = os.path.join(projects_root, slug)
        if not os.path.isdir(pdir):
            continue
        for sid in sorted(os.listdir(pdir)):
            sd = os.path.join(pdir, sid)
            if os.path.isdir(sd):
                out.append(sd)
    return out


def list_sessions(root: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    p = index_file(root)
    if not os.path.exists(p):
        return out
    with compat.open_text(p, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def upsert(root: str, meta: Dict[str, Any]) -> None:
    compat.mkdirs(os.path.dirname(index_file(root)))
    compat.append_text(index_file(root), json.dumps(meta, ensure_ascii=False) + "\n")


def rebuild(root: str) -> int:
    """扫描全部会话目录重建索引，返回条数。"""
    from . import store

    rows: Dict[str, Dict[str, Any]] = {}
    for sd in iter_session_dirs(root):
        meta = store.read_meta(sd)
        if not meta.get("session_id"):
            continue
        sid = meta["session_id"]
        meta["slug"] = os.path.basename(os.path.dirname(sd))
        meta["chunks"] = store.count_chunks(sd)
        meta["has_rolling"] = os.path.exists(store.rolling_path(sd))
        rows[sid] = meta

    ordered = sorted(rows.values(), key=lambda m: int(m.get("updated_at") or 0))
    lines = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in ordered)
    compat.mkdirs(os.path.dirname(index_file(root)))
    compat.atomic_write_text(index_file(root), lines)
    return len(ordered)


def find_session(root: str, session_id: str) -> Optional[str]:
    """按 session_id 定位会话目录。"""
    projects_root = os.path.join(root, "projects")
    if not os.path.isdir(projects_root):
        return None
    for slug in sorted(os.listdir(projects_root)):
        cand = os.path.join(projects_root, slug, session_id)
        if os.path.isdir(cand):
            return cand
    return None


def sessions_for_project(root: str, slug: str, cwd: str = "") -> List[str]:
    """按 project 定位会话目录；slug 为空时用 cwd 编码兜底。"""
    if not slug and cwd:
        slug = paths.encode_project_slug(cwd)
    if not slug:
        return []
    pdir = os.path.join(root, "projects", slug)
    if not os.path.isdir(pdir):
        return []
    return [os.path.join(pdir, d) for d in sorted(os.listdir(pdir)) if os.path.isdir(os.path.join(pdir, d))]
