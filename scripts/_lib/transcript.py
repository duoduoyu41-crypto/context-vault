"""transcript(JSONL) 解析。

数据源：<home>/.workbuddy/projects/<工作区slug>/<session-uuid>.jsonl
子 agent：<session-uuid>/subagents/agent-<hex>.jsonl

实测格式要点（与 Claude Code 不同，切勿照抄网上解析代码）：
  - 主键是 id / parentId，不是 uuid / parentUuid
  - role 只在 type == "message" 上
  - content 是 block 数组，也可能是 str / dict
  - function_call / function_call_result 是顶层记录类型，不只是 content block
  - timestamp 是毫秒 epoch
"""

import json
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

__all__ = [
    "iter_content_text",
    "extract_records",
    "discover_sources",
    "read_new_bytes",
    "iter_json_lines",
]

# file-history-snapshot 是文件快照噪声，默认丢弃（占体积且无检索价值）
_NOISE_TYPES = {"file-history-snapshot"}


def iter_content_text(content: Any) -> Iterator[Tuple[str, str]]:
    """从 content（str / dict / block 数组）中抽取 (kind, text)。"""
    if isinstance(content, str):
        yield ("text", content)
        return
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, str):
            yield ("text", block)
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type") or "text"
        text = block.get("text") or block.get("content") or block.get("output")
        if isinstance(text, (list, dict)):
            text = json.dumps(text, ensure_ascii=False)
        if text:
            yield (kind, str(text))


def _norm_ts(obj: Dict[str, Any]) -> int:
    ts = obj.get("timestamp")
    try:
        return int(ts)
    except Exception:
        return 0


def extract_records(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """一行 JSONL -> 0..n 个归一化事件。未知类型兜底序列化，保证格式演进不丢数据。"""
    rtype = obj.get("type")
    ts = _norm_ts(obj)
    rid = obj.get("id")
    parent = obj.get("parentId")
    out: List[Dict[str, Any]] = []

    if rtype == "session-meta":
        meta = obj.get("meta") or {}
        return [{"kind": "meta", "role": "system", "ts": ts, "id": rid, "parent": parent,
                 "text": json.dumps(meta, ensure_ascii=False)}]

    if rtype == "ai-title":
        return [{"kind": "title", "role": "system", "ts": ts, "id": rid, "parent": parent,
                 "text": str(obj.get("aiTitle") or "")}]

    if rtype == "message":
        role = obj.get("role") or "unknown"
        for kind, text in iter_content_text(obj.get("content")):
            out.append({"kind": kind, "role": role, "ts": ts, "id": rid, "parent": parent, "text": text})
        if not out:  # 空 content 的 message 也要留痕，避免丢轮次
            out.append({"kind": "text", "role": role, "ts": ts, "id": rid, "parent": parent, "text": ""})
        return out

    if rtype == "reasoning":
        src = obj.get("rawContent") or obj.get("content")
        for _, text in iter_content_text(src):
            out.append({"kind": "reasoning", "role": "assistant", "ts": ts, "id": rid,
                        "parent": parent, "text": text})
        return out

    if rtype == "function_call":
        args = obj.get("arguments")
        if isinstance(args, str):
            try:
                args = json.dumps(json.loads(args), ensure_ascii=False)
            except Exception:
                pass
        if isinstance(args, (dict, list)):
            args = json.dumps(args, ensure_ascii=False)
        return [{"kind": "tool_call", "role": "assistant", "tool": obj.get("name"),
                 "call_id": obj.get("callId"), "ts": ts, "id": rid, "parent": parent,
                 "text": args or ""}]

    if rtype == "function_call_result":
        o = obj.get("output")
        if isinstance(o, dict):
            text = o.get("text") or o.get("output") or json.dumps(o, ensure_ascii=False)
        elif o is None:
            text = ""
        else:
            text = str(o)
        return [{"kind": "tool_result", "role": "tool", "tool": obj.get("name"),
                 "call_id": obj.get("callId"), "status": obj.get("status"), "ts": ts,
                 "id": rid, "parent": parent, "text": text}]

    if rtype in _NOISE_TYPES:
        return []

    # 未知类型：整行兜底，doctor 会统计 unknown:* 占比
    return [{"kind": "unknown:%s" % rtype, "role": "system", "ts": ts, "id": rid,
             "parent": parent, "text": json.dumps(obj, ensure_ascii=False)[:20000]}]


def iter_json_lines(raw: bytes) -> Iterator[Dict[str, Any]]:
    """按行解析 JSON 字节，坏行跳过。raw 应为完整的行集合（以 \\n 结尾）。"""
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            continue
        if isinstance(obj, dict):
            yield obj


def discover_sources(transcript_path: str) -> List[Dict[str, str]]:
    """给定主 transcript 路径，返回全部数据源（含子 agent）。

    source 键用于 state.json 分区：
      "main"                                -> 主 transcript
      "subagents/agent-<hex>"               -> 子 agent transcript
    """
    transcript_path = os.path.abspath(transcript_path)
    if not os.path.exists(transcript_path):
        return []

    stem = os.path.splitext(os.path.basename(transcript_path))[0]
    base = os.path.dirname(transcript_path)
    sources = [{"key": "main", "path": transcript_path}]

    sub_root = os.path.join(base, stem, "subagents")
    if os.path.isdir(sub_root):
        try:
            for name in sorted(os.listdir(sub_root)):
                if not name.endswith(".jsonl"):
                    continue
                full = os.path.join(sub_root, name)
                if os.path.isfile(full):
                    sources.append({"key": "subagents/%s" % os.path.splitext(name)[0], "path": full})
        except Exception:
            pass
    return sources


def read_new_bytes(path: str, offset: int, max_bytes: int) -> Tuple[bytes, int, int]:
    """从 offset 读取新字节，返回 (完整行字节, 新 offset, 源文件大小)。

    关键：最后一行若不以 \\n 结尾说明还在写，留给下次，避免读到半行。
    """
    try:
        size = os.path.getsize(path)
    except Exception:
        return b"", offset, 0
    if size <= offset:
        return b"", offset, size

    end = min(size, offset + max_bytes)
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            buf = fh.read(end - offset)
    except Exception:
        return b"", offset, size

    nl = buf.rfind(b"\n")
    if nl == -1:
        # 单次限制内没有换行：放宽限制再读一次，仍无则等待
        if end - offset >= max_bytes:
            try:
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    buf = fh.read(min(size - offset, max_bytes * 8))
            except Exception:
                return b"", offset, size
            nl = buf.rfind(b"\n")
            if nl == -1:
                return b"", offset, size
        else:
            return b"", offset, size

    complete = buf[: nl + 1]
    return complete, offset + len(complete), size
