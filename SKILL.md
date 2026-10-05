---
name: context-vault
description: >
  长对话上下文的本地磁盘归档与按需取回（上下文保险库，简称 ctx）。
  当出现以下情况时使用：上下文被压缩或即将压缩（compact / 快满了 / 上下文不够用 / Max 模式太烧额度）；
  需要回想被压缩掉的早期内容（"之前说过什么"、"那个路径是什么"、"上次的结论是"、"我们之前讨论过"）；
  需要把超长对话、大文件历史、子 agent 过程持久化到本地空间充裕的硬盘；
  需要跨会话、跨项目检索历史讨论内容；用户提到 归档 / 存档 / 回看历史 / 上下文保险库 /
  context vault / ctx / 长上下文 / 上下文不够 / 别让我重复说。
  提供子命令：archive（增量归档）、search（关键词检索）、recall（回灌上下文）、
  summarize（滚动摘要）、init-storage（存储位置探测与初始化）、install-hooks（按需开启自动归档）、
  doctor（自检）、migrate（迁移存储根）。
version: 0.1.0
agent_created: true
allowed-tools: Bash(python3:*), Bash(python:*), Bash(py:*), Bash(sh:*), Bash(bash:*), Read, Write, Edit, Glob, Grep
user-invocable: true
disable: false
---

# Context Vault —— 长上下文的本地归档与按需取回

## 用途

把对话过程实时归档到本地磁盘，使上下文被压缩后仍能完整回想起早期内容，
从而不必反复重复同一件事，也不必为了保留上下文而长期开启 Max 模式。

核心机制：宿主的对话历史本身就在实时落盘为 JSONL（`~/.workbuddy/projects/<工作区slug>/<session-uuid>.jsonl`），
本技能在**上下文被压缩丢弃之前**（`PreCompact`）把它增量复制到本地归档盘，
压缩后用「滚动摘要 + 关键词检索」按需取回。

四层数据：
| 层 | 内容 | 用途 |
|---|---|---|
| L0 | `index/sessions.jsonl` | 会话发现与统计 |
| L1 | `rolling.md` | 滚动摘要，压缩后常驻上下文（token 占用小） |
| L2 | `chunks.jsonl` | 检索单位，关键词命中后取回片段 |
| L3 | `raw/**/part-NNNNN.jsonl` | 原文字节，用于精确回放与审计 |

## 何时使用

- 上下文被压缩、或即将压缩（收到 compact 提示 / 上下文告警）
- 需要回想早期被压缩掉的内容
- 需要把超长对话/复杂项目历史持久化到本地大容量硬盘
- 用户要求归档、回看历史、或抱怨上下文不够用

## 调用方式

所有命令统一入口。优先用 shell 包装（它负责探测 Python 解释器并处理 Windows/Git Bash 路径）：

```bash
"${CODEBUDDY_SKILL_DIR}/scripts/ctx.sh" <子命令> [参数]
```

若 shell 包装不可用，直接调用 Python（需 ≥3.8）：

```bash
python3 "${CODEBUDDY_SKILL_DIR}/scripts/ctx.py" <子命令> [参数]
```

退出码：`0` 成功 · `1` 非致命错误 · `3` 存储根未初始化。
**脚本永不返回 2**（2 对 PreCompact 意味着阻止压缩，对 UserPromptSubmit 意味着阻塞用户发言）。

## 子命令

| 命令 | 作用 | 常用参数 |
|---|---|---|
| `init-storage` | 探测并初始化存储根 | `--dry-run` 只看候选 · `--pick <路径>` 指定 · `--yes` 自动选最高分 |
| `archive` | 增量归档 | `--transcript <路径>` · `--all` 全部历史 · `--force` 跳过节流 · `--cwd <工作区>` |
| `search` | 检索归档 | `query` · `--session` · `--project` · `--kind` · `--role` · `--regex` · `--limit` · `--json` |
| `recall` | 取回片段用于回灌 | `query` · `--budget <token>` · `--hook`（输出注入用 JSON） |
| `summarize` | 生成/更新滚动摘要 | `--session` · `--from-host <摘要>` · `--write-back <文本>` · `--mark-done` |
| `install-hooks` | 开启自动归档 | `--home both\|workbuddy\|codebuddy` · `--dry-run` |
| `uninstall-hooks` | 关闭自动归档 | — |
| `hooks-status` | 查看安装状态 | — |
| `doctor` | 自检 | `--rebuild` 顺带重建索引 |
| `migrate` | 迁移存储根 | `--to <路径>` |
| `hook <事件>` | 钩子回调（宿主调用，勿手动） | — |

## 工作流

### 1. 首次安装

依次执行并**向用户展示每步结果**：

1. `init-storage --dry-run` —— 打印候选盘评分表（盘符/可用空间/是否系统盘/得分）。
2. 由用户确认位置，或 `--yes` 自动选最高分。评分优先「剩余空间大 + 非系统盘」，
   若某盘已存在归档标记（`.context-vault-marker`）则强优先复用。
3. `init-storage` 完成初始化。配置写入 `<home>/.workbuddy/context-vault/config.json`。
4. `install-hooks --home both` —— 备份并合并写入 `settings.json` 的 `hooks` 键。
5. **必须提示用户**：`settings.json` 是受保护文件，需在 `/hooks` 菜单中审查确认后才会生效。
6. `doctor` 确认全绿。

若用户不希望改配置文件，跳过第 4 步，改为手动执行 `archive`；此时 `doctor` 会显示 hooks 为 WARN。

### 2. 日常归档

- 装了钩子后自动进行：`UserPromptSubmit` 节流增量、`PreCompact` 强制全量、`Stop`/`SessionEnd` 收尾。
- 未装钩子时手动：`archive --cwd "<工作区>"`（自动定位最近 transcript）或 `archive --all`。
- 归档是增量的：只读取上次游标之后的新字节，重复执行开销极小。

### 3. 上下文被压缩后回想

1. 先读已有的 `rolling.md`（L1 摘要）—— 若钩子已装，它会在 `SessionStart` 自动注入；
   否则执行 `recall "<关键词>" --budget 4000`。
2. 需要更多细节时，用 `search "<关键词>" --limit 10` 定位，再用 `recall` 取回原文片段。
3. 用户消息优先、越新越相关；中文无需分词，直接子串匹配即可。

### 4. 换机器 / 新项目

- 新机器：安装本技能后执行 `init-storage`，自动探测该机的候选盘；
  若插入的旧归档盘含 `.context-vault-marker`，会被自动识别并复用。
- 新项目无需额外配置：归档按 `transcript` 所在目录自动分项目存放。
- 迁移：`migrate --to <新路径>`（旧目录改名保留，**不删除**）。

## 关键约束

- **不要硬编码盘符或用户目录**：存储位置一律由 `init-storage` 探测或环境变量 `CONTEXT_VAULT_HOME` 指定。
- **不要在钩子路径里省略引号**：skill 目录可能含空格，命令必须写作 `"${CODEBUDDY_SKILL_DIR}/scripts/ctx.sh"`。
- **归档失败不要重试到阻塞对话**：脚本异常一律返回 1 并静默降级。
- **原文默认复制**（`copy_raw: true`）：宿主会清理/轮转 transcript，只存偏移量会丢数据。
- **敏感信息默认脱敏**（`redact: true`）：密钥类内容替换为占位符。云端同步默认关闭。
- 检索不到时，先确认是否已 `archive`，再换更短的关键词（中文 2–4 字召回更好）。

## 与项目记忆的关系

`MEMORY.md` 存**结论**（有 200 行/25KB 上限），本技能存**证据链**（完整对话过程，无上限、按需检索）。
压缩后先从本技能找回证据，再把精炼结论回写 `MEMORY.md`。

## 参考

- `references/transcript-format.md` —— transcript JSONL 的真实字段格式与解析陷阱
- `references/hooks.md` —— 钩子事件、退出码、两套 home 目录的处理
- `references/storage.md` —— 存储后端对比与云端扩展位
