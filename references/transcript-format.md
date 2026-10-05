# transcript JSONL 真实格式（实测）

数据源位置（按候选顺序探测）：

```
<home>/.workbuddy/projects/<工作区slug>/<session-uuid>.jsonl
<home>/.workbuddy/projects/<工作区slug>/<session-uuid>/subagents/agent-<16位hex>.jsonl
```

`<home>` 可能是 `~/.workbuddy`、`~/.codebuddy`、`~/.agents` 之一，**必须逐个探测**，
不能假设只有 `.codebuddy`。判定依据：`projects` 子目录存在的那个即当前宿主目录。

## 工作区 slug 编码规则

盘符转小写 → 去掉 `:` → `\` 替换为 `-`，其余字符（含中文、空格）原样保留。

```
D:\Workbuddy上下文加长Skill   ->   d-Workbuddy上下文加长Skill
C:\Users\Admin\dev\proj        ->   c-Users-Admin-dev-proj
```

**实践中不要自己编码**：钩子的 `transcript_path` 是绝对路径，
`Path(transcript_path).parent.name` 即为 slug，零出错。编码函数只用于无钩子的手工扫描场景。

## 顶层字段

| 字段 | 说明 |
|---|---|
| `type` | 记录类型，见下表 |
| `id` | 记录 UUID。**是 `id` 不是 `uuid`** |
| `parentId` | 父记录。**是 `parentId` 不是 `parentUuid`** |
| `timestamp` | 毫秒 epoch |
| `role` | `user` / `assistant`，**仅 `type:"message"` 有** |
| `content` | block 数组，也可能是字符串或 dict |
| `sessionId` | 会话 UUID，只在 `session-meta` 行（文件名即 sessionId） |
| `cwd` | 工作区绝对路径 |
| `providerData` | provider 侧信息（`model` / `messageId` 等） |
| `meta` | 附加元数据 |

## `type` 取值

| type | 含义 | 解析要点 |
|---|---|---|
| `session-meta` | 会话元信息 | 取 `meta`；`sessionId` 在此 |
| `message` | 一轮对话 | `role` 在此；文本在 `content` block 里 |
| `reasoning` | 思考过程 | 文本可能在 `rawContent` 或 `content` |
| `function_call` | 工具调用 | `name` / `callId` / `arguments` |
| `function_call_result` | 工具结果 | `output`（可能是 dict，取 `text`） |
| `ai-title` | 会话标题 | 取 `aiTitle` |
| `file-history-snapshot` | 文件快照 | **噪声，默认丢弃**（体积大、无检索价值） |
| `text` / `list` | 其他 | 兜底处理 |

## content block

`content` 为数组时，每个 block 形如：

```json
{"type": "input_text", "text": "..."}
{"type": "output_text", "text": "..."}
{"type": "reasoning_text", "text": "..."}
```

抽取规则：依次尝试 `text` → `content` → `output`；
若取到的是 list/dict 则 `json.dumps(ensure_ascii=False)` 序列化。

## 解析陷阱（务必遵守）

1. **不要照抄 Claude Code 的解析代码**：那里是 `uuid`/`parentUuid`，这里是 `id`/`parentId`。
2. **中文路径**：Windows 下 slug 常含中文，所有 IO 必须显式 `encoding="utf-8", errors="replace"`。
3. **增量读取必须按行对齐**：从 offset 读字节后，只保留到最后一个 `\n`，避免读到半行。
4. **源文件可能被重写或截断**：若 `size < offset` 或 mtime 回退，判定为重写，
   旧分片改名 `.superseded-<ts>` 保留，**不得删除**，然后从 0 重建。
5. **未知 type 必须兜底**：整行序列化存为 `unknown:<type>`，保证格式演进时不丢数据。
   `doctor` 会统计 `unknown:*` 占比，超过 20% 提示升级。
6. **子 agent 是独立文件**，在 `subagents/` 子目录，归档需递归处理。
7. 文件会持续增长（实测主 transcript 238KB、子 agent 超 1MB），**禁止全量重拷**。
