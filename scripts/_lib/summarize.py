"""L1 滚动摘要。

难点：PreCompact 是 shell 命令，无法调用 LLM。解法是三路合流：
  ① 确定性骨架抽取（PreCompact 同步，零依赖，保命基线）
  ② PostCompact 的 compact_summary（宿主已算好，成本为 0，质量最高）
  ③ 下一轮模型补写（SessionStart(source=compact)，填「事实/决策/待办」三节）

诚实降级：① 阶段写不出的语义小节留空，由 ② 或 ③ 填充，绝不伪造内容。
"""

import json
import os
import re
from typing import Any, Dict, List, Optional

from . import compat, store

__all__ = [
    "build_skeleton", "render_rolling_md", "write_pending", "read_pending",
    "clear_pending", "apply_compact_summary", "extract_file_paths", "trim_rolling",
]

_FENCE = "<!-- context-vault:rolling -->"

_PATH_RE = re.compile(r'"(?:file_path|path|notebook_path)"\s*:\s*"([^"]{2,300})"')
_CMD_PATH_RE = re.compile(r"([A-Za-z]:[\\/][^\s\"'|<>]{2,200}|(?:/[\w.\-]+){2,10})")


def extract_file_paths(text: str) -> List[str]:
    out: List[str] = []
    for m in _PATH_RE.finditer(text or ""):
        out.append(m.group(1))
    if not out:
        for m in _CMD_PATH_RE.finditer(text or ""):
            out.append(m.group(1))
    return out


_XML_TAG_RE = re.compile(r"</?[A-Za-z][\w.:-]*[\s/>]")


_UQ_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S)
_SR_RE = re.compile(r"<system-reminder\b.*?</system-reminder>", re.S)

# 宿主会把工具参数/结果以 "Arguments: {...}" 的形式塞进 user 记录，
# 只按 role 过滤会误把它们当成用户诉求。
_TOOLISH_PREFIX = ("Arguments:", "Command:", "Stdout:", "Stderr:", "Result:",
                   "Exit Code:", "Tool ran without output")


def extract_user_text(text: str) -> str:
    """从 user 记录中提取真实提问。

    优先级：① `<user_query>` 标签内的原文（最可靠） ② 剥掉 system-reminder 后的剩余文本。
    """
    t = text or ""
    m = _UQ_RE.search(t)
    if m:
        return m.group(1).strip()
    return _SR_RE.sub("", t).strip()


def _is_noise(text: str) -> bool:
    """过滤系统注入与工具参数/结果，它们不是用户真实诉求。"""
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("<system-reminder") or t.startswith("<system-"):
        return True
    if t.startswith(_TOOLISH_PREFIX):
        return True
    # 序列化后的工具参数残片（含 \" 或 \n 转义、常伴 mojibake）不是自然语言
    if '\\"' in t or ('\\n' in t and len(t) > 200):
        return True
    head = t[:400]
    for marker in ('data-role="user-context"', "You are working within a project",
                   "<project-tool-routing>", "<tdrive-root", "_Fill this in when",
                   "# BOOTSTRAP.md", "# SOUL.md", "# USER.md", "# IDENTITY.md"):
        if marker in head:
            return True
    # 密集 XML 标签 = 系统提示/结构化注入，不是自然语言诉求
    if len(_XML_TAG_RE.findall(head)) >= 3:
        return True
    return False


def _dedup(items: List[str], limit: int, each: int = 200) -> List[str]:
    seen = set()
    out = []
    for it in items:
        it = " ".join((it or "").split())
        if not it or _is_noise(it):
            continue
        key = it[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(it[:each] + ("…" if len(it) > each else ""))
        if len(out) >= limit:
            break
    return out


def build_skeleton(sd: str, cfg: Dict[str, Any]) -> str:
    """从 L2 chunks 确定性抽取骨架。不调用 LLM。"""
    max_chars = int(cfg.get("rolling_max_chars") or 4000)
    chunks = store.read_chunks(sd)
    session_id = os.path.basename(sd)

    def prefer_main(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """子 agent 的 prompt 不是用户诉求：优先取主 transcript 的记录。"""
        m = [c for c in items if c.get("source") == "main"]
        return m or items

    titles = [c for c in chunks if c.get("kind") == "title"]
    users = prefer_main([c for c in chunks if c.get("role") == "user"
                         and c.get("kind") in ("input_text", "text")])
    if not users:
        users = prefer_main([c for c in chunks if c.get("role") == "user"])
    assistants = prefer_main([c for c in chunks if c.get("role") == "assistant"])
    tools = [c for c in chunks if c.get("kind") == "tool_call"]

    files: List[str] = []
    for c in tools:
        files.extend(extract_file_paths(c.get("text") or ""))
        if c.get("tool"):
            pass
    tool_names: List[str] = []
    for c in tools:
        n = c.get("tool")
        if n and n not in tool_names:
            tool_names.append(str(n))

    title = titles[-1]["text"].strip() if titles else "(未命名会话)"
    tail_src = [c for c in chunks if c.get("role") == "assistant"
                and c.get("kind") in ("output_text", "text")]
    return render_rolling_md(
        session_id=session_id,
        title=title,
        goals=_dedup([extract_user_text(c.get("text", "")) for c in users], 8),
        facts=[], decisions=[], doing=[], todos=[],
        files=_dedup(files, 20, 160),
        tools=tool_names[:12],
        tail=_dedup([c.get("text", "") for c in (prefer_main(tail_src) or tail_src)[-3:]], 3, 400),
        max_chars=max_chars,
        turns=len(chunks),
        source="pre-compact(skeleton)",
    )


def render_rolling_md(session_id: str, title: str, goals: List[str], facts: List[str],
                      decisions: List[str], doing: List[str], todos: List[str],
                      files: List[str], tools: List[str], tail: List[str],
                      max_chars: int, turns: int = 0, source: str = "",
                      host_summary: str = "") -> str:
    def sec(name: str, items: List[str]) -> str:
        if not items:
            return "## %s\n- （待补写）\n" % name
        return "## %s\n%s\n" % (name, "\n".join("- %s" % i for i in items))

    body = "\n".join([
        "# 会话滚动摘要 · %s" % session_id[:8],
        "",
        "> %s · 覆盖 %d 个片段 · 生成方式：%s" % (compat.utc_iso(), turns, source or "unknown"),
        "",
        "## 标题", title,
        "",
        sec("目标", goals),
        "",
        sec("已确认事实", facts),
        "",
        sec("关键决策", decisions),
        "",
        sec("进行中", doing),
        "",
        sec("待办", todos),
        "",
        sec("关键文件", files),
        "",
        "## 主要工具",
        (", ".join(tools) if tools else "- （无）"),
        "",
        sec("最近进展", tail),
    ])
    if host_summary:
        body += "\n## 宿主压缩摘要\n%s\n" % host_summary.strip()
    body += "\n## 检索提示\n- 需要细节时执行：`ctx.py recall \"<关键词>\" --budget 4000`\n"

    header = "%s\n" % _FENCE
    out = header + body
    if len(out) > max_chars:
        out = out[:max_chars] + "\n…[摘要已达上限，细节请用 recall 检索]\n"
    return out


def trim_rolling(sd: str, max_chars: int) -> None:
    text = store.read_rolling(sd)
    if len(text) > max_chars:
        store.write_rolling(sd, text[:max_chars] + "\n…[已裁剪]\n")


# ---------------------------------------------------------------- pending 队列

def write_pending(sd: str, session_id: str, trigger: str = "auto",
                  from_turn: int = 0, to_turn: int = 0) -> str:
    payload = {
        "session_id": session_id,
        "trigger": trigger,
        "compact_at": compat.now_ms(),
        "from_turn": from_turn,
        "to_turn": to_turn,
        "status": "pending",
        "instructions": (
            "用 ≤200 字补全该会话 rolling.md 的『已确认事实』『关键决策』『待办』三节，"
            "然后执行 summarize --mark-done。" ),
    }
    compat.atomic_write_json(store.pending_path(sd), payload)
    return store.pending_path(sd)


def read_pending(sd: str) -> Dict[str, Any]:
    return compat.read_json(store.pending_path(sd), {}) or {}


def clear_pending(sd: str) -> None:
    p = store.pending_path(sd)
    if os.path.exists(p):
        try:
            os.remove(p)
        except Exception:
            pass


def apply_compact_summary(sd: str, summary: str, cfg: Dict[str, Any]) -> bool:
    """把宿主 PostCompact 提供的 compact_summary 并入 rolling.md。

    返回是否成功写入。summary 为空则保持 pending，交由下一轮模型补写。
    """
    if not (summary or "").strip():
        return False
    existing = store.read_rolling(sd)
    if not existing.strip():
        existing = build_skeleton(sd, cfg)
    if "## 宿主压缩摘要" in existing:
        head, _, _tail = existing.partition("## 宿主压缩摘要")
        merged = head
    else:
        merged = existing.rstrip() + "\n\n"
    merged += "## 宿主压缩摘要\n%s\n" % summary.strip()
    max_chars = int(cfg.get("rolling_max_chars") or 4000)
    if len(merged) > max_chars * 2:
        merged = merged[: max_chars * 2] + "\n…[已裁剪]\n"
    store.write_rolling(sd, merged)
    return True
