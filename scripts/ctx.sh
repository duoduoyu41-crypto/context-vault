#!/usr/bin/env bash
# context-vault 统一入口。
# Windows 上钩子强制用 Git Bash 执行，因此所有调用都走本脚本：
#   1) 探测可用的 Python 解释器（开源版不能假设 python3 在 PATH）
#   2) 关闭 MSYS 路径自动转换，避免 /c/... 与 C:/... 被错误改写
#   3) 统一 UTF-8，避免中文输出抛 UnicodeEncodeError

set -uo pipefail

export MSYS_NO_PATHCONV=1
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Git Bash 下 pwd 返回 POSIX 形式（/c/Users/...），而 Windows 原生 Python 会把它
# 解析成「相对当前盘符」的路径（在 D 盘运行时变成 d:\c\Users\...）导致找不到脚本。
# 仅在 MSYS/MinGW/Cygwin 环境下转换为宿主形式（C:/Users/...）。
to_host_path() {
  local p="$1"
  case "$(uname -s 2>/dev/null)" in
    MSYS*|MINGW*|CYGWIN*)
      if command -v cygpath >/dev/null 2>&1; then
        local win
        if win="$(cygpath -m "$p" 2>/dev/null)" && [ -n "$win" ]; then
          printf '%s' "$win"; return 0
        fi
      fi
      # 兜底：/c/foo -> C:/foo（cygpath 不可用时）
      case "$p" in
        /[a-zA-Z]/*) printf '%s:/%s' "${p:1:1}" "${p:3}" ;;
        *) printf '%s' "$p" ;;
      esac
      ;;
    *) printf '%s' "$p" ;;
  esac
}

CTX_PY="$(to_host_path "$SCRIPT_DIR")/ctx.py"

for PY in "${CONTEXT_VAULT_PYTHON:-}" python3 python py \
          python3.13 python3.12 python3.11 python3.10 python3.9 python3.8; do
  [ -n "$PY" ] || continue
  command -v "$PY" >/dev/null 2>&1 || continue
  if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    exec "$PY" "$CTX_PY" "$@"
  fi
done

# 兜底：已知的解释器安装位置（按平台常见路径，失败则继续）
for PY_CANDIDATE in \
  "$HOME/.workbuddy/binaries/python/versions/3.13.12/python" \
  "/usr/bin/python3" \
  "/usr/local/bin/python3" \
  "/opt/homebrew/bin/python3"; do
  [ -x "$PY_CANDIDATE" ] || continue
  exec "$PY_CANDIDATE" "$CTX_PY" "$@"
done

echo "context-vault: 未找到 Python >= 3.8，请安装后重试，或设置 CONTEXT_VAULT_PYTHON=/path/to/python" >&2
exit 1
