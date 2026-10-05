"""归档存储层：L0 索引 / L1 滚动摘要 / L2 片段 / L3 原文。

目录布局（每个会话）：
  projects/<slug>/<sessionId>/
    meta.json          L0  会话元数据
    rolling.md         L1  滚动摘要（常驻上下文，硬上限）
    chunks.jsonl       L2  检索单位
    state.json         增量游标
    pending_summary.json
    raw/main/part-NNNNN.jsonl        L3
    raw/subagents/<agent>/part-NNNNN.jsonl
"""

import json
import os
from typing import Any, Dict, List, Optional

from . import compat, paths, redact, transcript

__all__ = [
    "SCHEMA_VERSION",
    "state_path",
    "chunks_path",
    "meta_path",
    "rolling_path",
    "raw_dir",
    "pending_path",
    "load_state",
    "save_state",
    "ensure_dirs",
    "truncate_tool_result",
    "split_text",
    "records_to_chunks",
    "append_chunks",
    "append_raw",
    "write_meta",
    "read_meta",
    "read_rolling",
    "write_rolling",
    "read_chunks",
    "est_tokens",
    "count_chunks",
]

SCHEMA_VERSION = 1
PART_MAX_BYTES = 8 * 1024 * 1024  # 单个 L3 分片上限


# ---------------------------------------------------------------- 路径

def state_path(sd: str) -> str:
    return os.path.join(sd, "state.json")


def chunks_path(sd: str) -> str:
    return os.path.join(sd, "chunks.jsonl")


def meta_path(sd: str) -> str:
    return os.path.join(sd, "meta.json")


def rolling_path(sd: str) -> str:
    return os.path.join(sd, "rolling.md")


def pending_path(sd: str) -> str:
    return os.path.join(sd, "pending_summary.json")


def raw_dir(sd: str, source_key: str) -> str:
    return os.path.join(sd, "raw", source_key.replace("/", os.sep))


# ---------------------------------------------------------------- state

def load_state(sd: str) -> Dict[str, Any]:
    st = compat.read_json(state_path(sd), None)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("schema_version", SCHEMA_VERSION)
    st.setdefault("sources", {})
    st.setdefault("last_run_at", 0)
    st.setdefault("last_chunks", 0)
    st.setdefault("seq", 0)
    return st


def save_state(sd: str, st: Dict[str, Any]) -> None:
    compat.atomic_write_json(state_path(sd), st)


def ensure_dirs(sd: str) -> str:
    compat.mkdirs(sd)
    compat.mkdirs(os.path.join(sd, "raw"))
    return sd


# ---------------------------------------------------------------- 文本处理

def truncate_tool_result(text: str, limit: int) -> str:
    """截断工具输出：保留 head 2/3 + tail 1/3。

    尾部常含 Exit Code / Stderr，信息密度高，不能简单从头截。
    """
    if not text or limit <= 0 or len(text) <= limit:
        return text
    head_n = int(limit * 2 / 3)
    tail_n = limit - head_n
    return "%s\n…[已截断 %d 字符]…\n%s" % (text[:head_n], len(text) - limit, text[-tail_n:])


def split_text(text: str, max_chars: int) -> List[str]:
    """按段落/换行切分长文本，尽量不在语义中间断开。"""
    if len(text) <= max_chars:
        return [text]
    parts: List[str] = []
    buf = ""
    for para in text.split("\n"):
        while len(para) > max_chars:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(para[:max_chars])
            para = para[max_chars:]
        if len(buf) + len(para) + 1 > max_chars:
            parts.append(buf)
            buf = para
        else:
            buf = (buf + "\n" + para) if buf else para
    if buf:
        parts.append(buf)
    return [p for p in parts if p]


def records_to_chunks(records: List[Dict[str, Any]], cfg: Dict[str, Any],
                      session_id: str, source_key: str, seq_start: int) -> List[Dict[str, Any]]:
    """归一化事件 -> L2 chunk 列表。"""
    chunk_max = int(cfg.get("chunk_max_chars") or 4000)
    tool_max = int(cfg.get("tool_result_max_chars") or 8000)
    do_redact = bool(cfg.get("redact", True))
    chunks: List[Dict[str, Any]] = []
    seq = seq_start

    for rec in records:
        text = rec.get("text") or ""
        if not text.strip():
            continue
        if do_redact:
            text = redact.redact(text)
        if rec.get("kind") == "tool_result":
            text = truncate_tool_result(text, tool_max)
        pieces = split_text(text, chunk_max)
        for i, piece in enumerate(pieces):
            seq += 1
            chunks.append({
                "chunk_id": "c%06d" % seq,
                "session_id": session_id,
                "seq": seq,
                "ts": rec.get("ts") or 0,
                "role": rec.get("role") or "unknown",
                "kind": rec.get("kind") or "text",
                "tool": rec.get("tool"),
                "call_id": rec.get("call_id"),
                "source": source_key,
                "part": "%d/%d" % (i + 1, len(pieces)) if len(pieces) > 1 else None,
                "chars": len(piece),
                "tokens_est": est_tokens(piece),
                "text": piece,
            })
    return chunks


def est_tokens(text: str) -> int:
    """粗估 token：CJK 约 1 token/字，西文约 3.6 字符/token。足够用于预算裁剪。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)
    return int(cjk + (len(text) - cjk) / 3.6)


# ---------------------------------------------------------------- 写入

def append_chunks(sd: str, chunks: List[Dict[str, Any]]) -> int:
    if not chunks:
        return 0
    lines = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks)
    compat.append_text(chunks_path(sd), lines)
    return len(chunks)


def append_raw(sd: str, source_key: str, data: bytes) -> str:
    """追加原始字节到 L3 分片。返回写入的分片路径。"""
    d = compat.mkdirs(raw_dir(sd, source_key))
    existing = sorted(f for f in os.listdir(d) if f.startswith("part-") and f.endswith(".jsonl")) \
        if os.path.isdir(d) else []
    if existing:
        cur = os.path.join(d, existing[-1])
        try:
            if os.path.getsize(cur) + len(data) <= PART_MAX_BYTES:
                with open(cur, "ab") as fh:
                    fh.write(data)
                return cur
        except Exception:
            pass
    name = "part-%05d.jsonl" % (len(existing) + 1)
    cur = os.path.join(d, name)
    with open(cur, "ab") as fh:
        fh.write(data)
    return cur


def mark_superseded(sd: str, source_key: str) -> None:
    """源文件被重写/截断：旧分片改名保留，绝不删除。"""
    d = raw_dir(sd, source_key)
    if not os.path.isdir(d):
        return
    ts = compat.now_ms()
    for name in sorted(os.listdir(d)):
        if name.startswith("part-"):
            try:
                os.rename(os.path.join(d, name), os.path.join(d, name + ".superseded-%d" % ts))
            except Exception:
                pass


def write_meta(sd: str, meta: Dict[str, Any]) -> None:
    compat.atomic_write_json(meta_path(sd), meta)


def read_meta(sd: str) -> Dict[str, Any]:
    return compat.read_json(meta_path(sd), {}) or {}


def write_rolling(sd: str, text: str) -> None:
    compat.atomic_write_text(rolling_path(sd), text)


def read_rolling(sd: str) -> str:
    try:
        with compat.open_text(rolling_path(sd), "r") as fh:
            return fh.read()
    except Exception:
        return ""


def read_chunks(sd: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    p = chunks_path(sd)
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


def count_chunks(sd: str) -> int:
    n = 0
    p = chunks_path(sd)
    if not os.path.exists(p):
        return 0
    with compat.open_text(p, "r") as fh:
        for line in fh:
            if line.strip():
                n += 1
    return n


def build_meta(session_id: str, slug: str, cwd: str, state: Dict[str, Any],
               sd: str = "", title: str = "", chunks: int = 0) -> Dict[str, Any]:
    ts_list = []
    for src in state.get("sources", {}).values():
        if src.get("started_at"):
            ts_list.append(int(src["started_at"]))
    return {
        "session_id": session_id,
        "project": slug,
        "cwd": cwd,
        "title": title,
        "started_at": min(ts_list) if ts_list else compat.now_ms(),
        "updated_at": compat.now_ms(),
        "turns": sum(int(src.get("lines") or 0) for src in state.get("sources", {}).values()),
        "chunks": chunks,
        "sources": sorted(state.get("sources", {}).keys()),
        "has_rolling": bool(sd) and os.path.exists(rolling_path(sd)),
    }
