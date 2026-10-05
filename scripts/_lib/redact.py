"""敏感信息脱敏。

默认开启。归档时会把明显属于密钥/令牌的内容替换为占位符，
避免长期落盘的对话原文成为泄露源（尤其在开启云端同步时）。
"""

import re
from typing import List, Tuple

__all__ = ["redact", "PATTERNS"]

# (名称, 正则, 替换)
_PATTERNS: List[Tuple[str, "re.Pattern", str]] = [
    ("openai_key", re.compile(r"sk-[A-Za-z0-9\-_]{20,}"), "«REDACTED:openai_key»"),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "«REDACTED:github_token»"),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}"), "«REDACTED:aws_key»"),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "«REDACTED:private_key»"),
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"), "«REDACTED:bearer»"),
    ("kv_secret", re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|token|password|passwd|secret)\b\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',}]{8,}"),
     "«REDACTED:kv_secret»"),
]

PATTERNS = _PATTERNS


def redact(text: str) -> str:
    if not text:
        return text
    for _name, pattern, repl in _PATTERNS:
        try:
            text = pattern.sub(repl, text)
        except Exception:
            continue
    return text
