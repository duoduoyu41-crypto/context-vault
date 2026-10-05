"""检索：字节级粗筛 + 精解析打分。

设计要点：
  - 中文不分词。中文是连续字符串，子串匹配天然命中且召回更全；
    仅对 2-6 字短查询加 bigram 增强。不引入 jieba（标准库约束）。
  - 先做字节级 `qb in raw_line` 粗筛，命中行才 json.loads，10 万行时快 50x+。
"""

import json
import os
import re
from typing import Any, Dict, List, Optional

from . import compat, index, paths, store

__all__ = ["search", "bigrams", "format_hits"]

_KIND_WEIGHT = {
    "user": 3.0, "assistant": 2.0, "tool_call": 2.0,
    "tool_result": 1.0, "reasoning": 1.0, "title": 2.0, "meta": 0.0, "text": 2.0,
}


def bigrams(q: str) -> List[str]:
    q = q.strip()
    if 2 <= len(q) <= 6:
        return [q[i:i + 2] for i in range(len(q) - 1)]
    return []


def _candidate_session_dirs(root: str, session_id: str = "", project: str = "", cwd: str = "") -> List[str]:
    dirs: List[str] = []
    if session_id:
        sd = index.find_session(root, session_id)
        if sd:
            dirs.append(sd)
    if project or cwd:
        for d in index.sessions_for_project(root, project, cwd):
            if d not in dirs:
                dirs.append(d)
    if not dirs:  # 全局
        dirs = index.iter_session_dirs(root)
    else:
        # 当前会话/项目优先，其后补充同项目其他会话，最后全局
        for d in index.iter_session_dirs(root):
            if d not in dirs:
                dirs.append(d)
    return dirs


def _scan_file(path: str, needles: List[bytes], limit: int,
               kind_filter: Optional[str], role_filter: Optional[str],
               regex: Optional["re.Pattern"], after_ts: int) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return hits
    with open(path, "rb") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            matched = False
            if regex is not None:
                matched = True  # 正则无法字节粗筛，交由精解析判定
            else:
                for nb in needles:
                    if nb in raw:
                        matched = True
                        break
            if not matched:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            if kind_filter and obj.get("kind") != kind_filter:
                continue
            if role_filter and obj.get("role") != role_filter:
                continue
            if after_ts and int(obj.get("ts") or 0) < after_ts:
                continue
            text = obj.get("text") or ""
            if regex is not None:
                if not regex.search(text):
                    continue
                hits.append(obj)
                continue
            cnt = sum(text.count(n.decode("utf-8", "replace")) for n in needles if n)
            obj["_hits"] = cnt
            hits.append(obj)
    if len(hits) > limit * 4:
        hits.sort(key=lambda h: -(h.get("_hits") or 1))
        hits = hits[: limit * 4]
    return hits


def search(root: str, query: str, session_id: str = "", project: str = "", cwd: str = "",
           kind: str = "", role: str = "", use_regex: bool = False, limit: int = 20,
           after_ts: int = 0, context: int = 0) -> List[Dict[str, Any]]:
    needles = [query.encode("utf-8")]
    needles += [b.encode("utf-8") for b in bigrams(query)]
    regex = re.compile(query) if use_regex else None

    dirs = _candidate_session_dirs(root, session_id=session_id, project=project, cwd=cwd)
    scored: List[Dict[str, Any]] = []
    for sd in dirs:
        sid = os.path.basename(sd)
        slug = os.path.basename(os.path.dirname(sd))
        for h in _scan_file(store.chunks_path(sd), needles, limit, kind, role, regex, after_ts):
            ts = int(h.get("ts") or 0)
            score = 2.0 * float(h.get("_hits") or 1)
            score += _KIND_WEIGHT.get(h.get("kind"), 1.0)
            score -= float(h.get("chars") or 0) / 10000.0
            if ts:
                score += min(ts / 1e13, 1.0)  # 新鲜度（毫秒量级归一化）
            h["_score"] = round(score, 3)
            h["_session"] = sid
            h["_project"] = slug
            scored.append(h)

    scored.sort(key=lambda h: -h["_score"])
    top = scored[:limit]
    if context:
        top = _expand_context(dirs, top, context)
    return top


def _expand_context(dirs: List[str], hits: List[Dict[str, Any]], radius: int) -> List[Dict[str, Any]]:
    """按 seq 取相邻 chunk，补上下文。"""
    by_sid: Dict[str, List[int]] = {}
    for h in hits:
        by_sid.setdefault(h["_session"], []).append(int(h.get("seq") or 0))
    extra: List[Dict[str, Any]] = []
    have = {(h["_session"], h.get("seq")) for h in hits}
    for sid, seqs in by_sid.items():
        sd = next((d for d in dirs if os.path.basename(d) == sid), None)
        if not sd:
            continue
        wanted = set()
        for s in seqs:
            for k in range(s - radius, s + radius + 1):
                if k > 0:
                    wanted.add(k)
        for c in store.read_chunks(sd):
            if c.get("seq") in wanted and (sid, c.get("seq")) not in have:
                c["_session"] = sid
                c["_project"] = os.path.basename(os.path.dirname(sd))
                c["_score"] = -1.0
                extra.append(c)
    return hits + extra


def format_hits(hits: List[Dict[str, Any]], query: str = "") -> str:
    if not hits:
        return "未命中任何归档内容。可尝试更短的关键词，或先执行 archive 归档当前会话。"
    lines = ["命中 %d 条：\n" % len(hits)]
    for h in hits:
        ts = h.get("ts") or 0
        when = compat.utc_iso(int(ts)) if ts else "?"
        head = "[%s] %s/%s · %s · %s" % (
            h.get("chunk_id") or "?", h.get("_session") or "?", h.get("seq"),
            h.get("role") or "?", h.get("kind") or "?")
        if h.get("tool"):
            head += " · tool=%s" % h["tool"]
        lines.append("## %s  (%s)" % (head, when))
        text = (h.get("text") or "").strip()
        if len(text) > 1200:
            text = text[:1200] + "\n…[截断]"
        lines.append(text)
        lines.append("")
    return "\n".join(lines)
