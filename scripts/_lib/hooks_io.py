"""钩子 IO：stdin 解析与 stdout 注入。

铁律：
  - 永不返回退出码 2。2 对 PreCompact = 阻止压缩，对 UserPromptSubmit = 阻塞用户发言。
  - 统一用 hookSpecificOutput.additionalContext 结构化注入（与事件无关的官方机制）。
  - 日志默认写文件，stdout 保持干净，避免污染上下文。
"""

import json
import os
import sys
from typing import Any, Dict, Optional

__all__ = ["read_hook_input", "emit", "emit_empty", "EXIT_OK", "EXIT_ERR"]

EXIT_OK = 0
EXIT_ERR = 1          # 非致命：写 stderr + 日志，绝不阻塞对话
EXIT_UNINIT = 3       # 未初始化存储根
# 注意：永远不要使用 2


def read_hook_input() -> Dict[str, Any]:
    """读取钩子 stdin JSON。失败返回空 dict（钩子绝不能因解析失败崩溃）。"""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def emit(context_text: str = "", code: int = EXIT_OK, system_message: str = "",
         event_name: str = "") -> None:
    """输出结构化结果并退出。

    context_text 非空时输出单行 JSON：
      {"hookSpecificOutput": {"hookEventName": <事件名>, "additionalContext": "..."}}
    """
    if context_text:
        payload: Dict[str, Any] = {
            "hookSpecificOutput": {
                "hookEventName": event_name or os.environ.get("CONTEXT_VAULT_HOOK_EVENT", ""),
                "additionalContext": context_text,
            }
        }
        if system_message:
            payload["systemMessage"] = system_message
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()
    sys.exit(code)


def emit_empty(code: int = EXIT_OK) -> None:
    sys.stdout.flush()
    sys.exit(code)
