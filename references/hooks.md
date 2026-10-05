# 钩子机制（实测 + 官方）

## 配置位置

三级，同级合并**不覆盖**：

| 级别 | 路径 |
|---|---|
| user | `<home>/.workbuddy/settings.json` 或 `<home>/.codebuddy/settings.json` |
| project | `<工作区>/.workbuddy/settings.json` |
| project-local | `<工作区>/.workbuddy/settings.local.json` |

### 两套 home 目录的处理

实测环境中 `~/.workbuddy/`（含完整配置与 `projects/`）与 `~/.codebuddy/`（可能只有 `enabledPlugins`）并存。
策略：

- 安装钩子时**默认双写**（`--home both`），幂等且无害。
- 真实宿主目录判定：`projects` 子目录存在的那个优先 —— transcript 在那里。
- 读取配置时按顺序探测，取第一个存在的。

## 事件与 matcher

| 事件 | matcher | 本技能用途 | 超时 |
|---|---|---|---|
| `UserPromptSubmit` | 无 | 节流后的增量归档 + 待补写摘要提醒 | 10s |
| `PreCompact` | `auto` / `manual` | **抢在压缩前归档原文** + 生成骨架摘要 | 30s |
| `PostCompact` | `auto` / `manual` | 接住 `compact_summary` 写入 rolling.md | 20s |
| `SessionStart` | `startup` / `resume` / `clear` / `compact` | 注入已有摘要 | 20s |
| `SessionEnd` | `clear` / `logout` / `prompt_input_exit` / `other` | 收尾全量归档 | 60s |
| `Stop` | 无 | 收尾全量归档（含子 agent） | 60s |

## stdin 输入

公共字段：`hook_event_name` / `session_id` / `transcript_path` / `cwd` / `permission_mode`

事件特有：

- `UserPromptSubmit`：`prompt`
- `PreCompact`：`trigger`（`auto`|`manual`）、`custom_instructions`
- `PostCompact`：`trigger`、`compact_summary`
- `SessionStart`：`source`

**`transcript_path` 是核心**：直接给出当前会话 JSONL 的绝对路径，无需自己拼。

## stdout 输出

```json
{"hookSpecificOutput": {"hookEventName": "PreCompact", "additionalContext": "要注入的文本"}}
```

必须是**单行 JSON**。另有 `systemMessage`（只给用户看）可用。

## 退出码（关键）

| 码 | 含义 |
|---|---|
| 0 | 成功。`UserPromptSubmit` 与 `SessionStart` 的 stdout 会加入上下文 |
| **2** | **阻塞**：PreCompact 下=阻止压缩；UserPromptSubmit 下=阻止提问并清除提示词 |
| 其他 | 非阻塞错误 |

**本技能永不返回 2**。钩子失败必须静默降级，绝不能干扰正常对话。

## 生效条件

`settings.json` 是受保护文件。脚本写入后**不会立即生效**，
需用户在 `/hooks` 菜单中审查并确认。安装脚本必须打印该提示。

## Skill frontmatter 里声明钩子？

不支持本场景。SKILL.md 的 `hooks` 字段仅 `context: fork` 生效，
而 fork 上下文**不访问对话历史**，且需 `allowUntrustedFrontmatterHooks: true`。
因此本技能采用「`install-hooks` 命令写 settings.json」的方式，默认零侵入。

## Windows 特有问题

钩子命令**强制 Git Bash 执行**，会触发 MSYS 路径转换（把 `/c/...`、`C:/...` 改写）。
对策：`ctx.sh` 首行 `export MSYS_NO_PATHCONV=1`，且命令中用双引号包裹 `${CODEBUDDY_SKILL_DIR}`。

## 幂等归属判定

安装/卸载时用命令字符串中的 `context-vault` 作为标记：

```python
any("context-vault" in h.get("command", "") for h in group.get("hooks", []))
```

只增删带该标记的条目，用户已有的其他钩子**一律不动**。
