# context-vault 使用说明

面向 WorkBuddy / CodeBuddy（含 CLI 内核 CodeBuddy Code）。
解决两件事：**上下文被压缩后早期内容全忘**，以及 **开 Max 模式额度消耗过快**。

---

## 1. 三分钟上手

```bash
S=~/.workbuddy/skills/context-vault/scripts   # CodeBuddy 用户把 .workbuddy 换成 .codebuddy

"$S/ctx.sh" init-storage --dry-run   # ① 看看本机哪些盘适合放归档
"$S/ctx.sh" init-storage             # ② 初始化（或用 --pick <路径> 指定）
"$S/ctx.sh" archive                  # ③ 归档当前会话
"$S/ctx.sh" doctor                   # ④ 自检，应全绿
```

归档完之后就能检索了：

```bash
"$S/ctx.sh" search "关键词" --limit 5            # 看看在哪一轮提到过
"$S/ctx.sh" recall "关键词" --budget 4000        # 把相关原文取回到眼前
```

想全自动就再加一步（可选）：

```bash
"$S/ctx.sh" install-hooks --home both
```

> 钩子写入的是受保护文件 `settings.json`，**必须到 `/hooks` 菜单审查确认后才会生效**。

---

## 2. 命令一览

| 命令 | 作用 | 常用参数 |
|---|---|---|
| `init-storage` | 探测并初始化归档位置 | `--dry-run` 只看候选 · `--pick <路径>` 指定 · `--yes` 自动选最高分 |
| `archive` | 增量归档 | `--transcript <路径>` · `--all` 全部历史 · `--force` 跳过节流 · `--cwd <工作区>` |
| `search` | 检索归档 | `query` · `--session` · `--project` · `--kind` · `--role` · `--regex` · `--limit` · `--json` |
| `recall` | 取回片段用于回灌 | `query` · `--budget <token>` · `--hook`（输出注入用 JSON） |
| `summarize` | 生成/更新滚动摘要 | `--session` · `--from-host <摘要>` · `--write-back <文本>` · `--mark-done` |
| `install-hooks` | 开启自动归档 | `--home both\|workbuddy\|codebuddy` · `--dry-run` |
| `uninstall-hooks` | 关闭自动归档 | — |
| `hooks-status` | 查看安装状态 | — |
| `doctor` | 自检 | `--rebuild` 顺带重建索引 |
| `migrate` | 迁移归档位置 | `--to <路径>`（旧目录改名保留，**不删除**） |

退出码：`0` 成功 · `1` 非致命错误 · `3` 存储根未初始化。
**脚本永不返回 2** —— 2 会阻止压缩、阻塞你的发言。

---

## 3. 四个典型场景

### 3.1 首次安装

1. `init-storage --dry-run` 打印候选盘评分表（盘符 / 可用空间 / 是否系统盘 / 得分）。
2. 确认位置，或用 `--yes` 自动选最高分。评分偏好「剩余空间大 + 非系统盘」；
   若某盘已有归档标记 `.context-vault-marker`，会被强优先复用。
3. `init-storage` 完成初始化，配置写入 `<home>/.workbuddy/context-vault/config.json`。
4. `install-hooks --home both`（可选）。
5. **到 `/hooks` 菜单审查确认**。
6. `doctor` 确认全绿。

### 3.2 日常使用

- **装了钩子**：全自动。`UserPromptSubmit` 节流增量 → `PreCompact` 强制全量 → `Stop`/`SessionEnd` 收尾。
- **没装钩子**：想起来时手动 `archive --cwd "<工作区>"`，或 `archive --all`。
- 归档是**增量**的，只读取上次游标之后的新字节，重复执行几乎零开销。

### 3.3 上下文被压缩后，怎么回想

1. 先看 `rolling.md`（滚动摘要）—— 装了钩子的话它会在 `SessionStart` 自动注入。
2. 需要细节就 `recall "<关键词>" --budget 4000`，按 token 预算打包片段。
3. 还不确定在哪一轮，就先 `search "<关键词>" --limit 10` 定位。

**检索技巧**：

- 中文**不用分词**，子串直接匹配；**2–4 字的短词召回最好**（如"盘符"优于"Windows 盘符探测的实现细节"）。
- 想按代码/文件路径找，用 `--regex` 走正则。
- 结果里用户消息优先、越新越相关。

### 3.4 换机器 / 迁移

- **新机器**：装好技能跑 `init-storage`，自动探测该机候选盘；
  插上旧归档盘（含 `.context-vault-marker`）会被自动识别复用。
- **迁移位置**：`migrate --to <新路径>`，旧目录改名保留，**绝不删除**。
- **新项目**无需额外配置：归档按 transcript 所在目录自动分项目存放。

---

## 4. 关于"上下文窗口"那个数字

这是最容易误解的一点，分三层：

| 层级 | 说明 | 能否调整 |
|---|---|---|
| 模型上下文窗口（如 200k） | 模型硬上限 | 不能，任何方式都改不了 |
| `autoCompactWindow`（默认 200k） | 触发自动压缩的基准值 | **可调**，范围 [100k, 1M] |
| context-vault | 压缩后把原文取回来 | 已装好 |

`autoCompactWindow` 的官方约束：触发点 = `min(设定值, 模型有效预算) × 阈值`，
**clamp 到 [100k, 1M] 且不超过模型实际上下文窗口**。
所以设成 1M 的含义是"用满模型能力"，**不会真的变成 1M**——模型只有 200k 时设了也是 200k。

四种改法，任选其一：

```bash
/autocompact 1m                                   # ① 会话内斜杠命令，即时生效（最省事）
codebuddy --autocompact 1m                        # ② 启动参数，也接受 400k / auto
export CODEBUDDY_AUTO_COMPACT_WINDOW=1000000      # ③ 环境变量，优先级最高
# ④ settings.json 顶层加 "autoCompactWindow": 1000000（需先完全退出宿主再改）
```

顺带把 `showContextUsage` 设为 `true`，界面会常驻显示上下文使用率，不必等到告警阈值。

**推荐组合**：调高 `autoCompactWindow` 推迟压缩 + 装钩子保证压缩时零丢失 + 事后 `recall` 取回。
这样不必开 Max 模式，也就没有额度问题。

---

## 5. 归档库结构

```
archive/
├── .context-vault-marker     归档标记，换机器靠它自动识别
├── config.json               该归档根的配置副本
├── index/sessions.jsonl      L0 会话索引
├── logs/                     运行日志
└── projects/<工作区>/<会话ID>/
    ├── meta.json             会话元信息
    ├── state.json            增量游标（断点续传用）
    ├── rolling.md            L1 滚动摘要，压缩后常驻上下文
    ├── chunks.jsonl          L2 检索单位，关键词命中的就是它
    ├── pending_summary.json  待模型补写的摘要任务
    └── raw/                  L3 原文字节分片，用于精确回放与审计
        ├── main/part-00001.jsonl
        └── subagents/<agent>/part-00001.jsonl
```

四层都留着的原因：L0 快速发现、L1 常驻上下文、L2 精准检索、L3 兜底审计。
任意一层损坏都能从 L3 重建。

---

## 6. 故障排查

**`doctor` 各项含义**

| 检查项 | 不通过时怎么办 |
|---|---|
| `python` | 需 Python ≥ 3.8；或设 `CONTEXT_VAULT_PYTHON=/path/to/python` |
| `transcript_root` | 宿主尚无会话历史，先跑一轮对话 |
| `storage_root` | 跑 `init-storage` |
| `free_space` | 剩余空间不足 2 GB，换盘或 `migrate` |
| `schema_compat` | 未知类型占比高说明 transcript 格式变了，请提 issue |
| `hooks` | WARN 表示未装钩子，手动模式可用；想自动就 `install-hooks` |

**写入 `settings.json` 报 WinError 5**
宿主进程正占用该文件。这不是本技能的问题，原子写保护会保证原文件不被破坏。
解决：改用 `/autocompact` 斜杠命令或环境变量（见第 4 节），或等宿主完全退出后再手改。

**检索不到内容**
先确认已 `archive`（`doctor` 看 `chunks` 数），再换更短的关键词。

**Windows 上命令报 `d:\c\Users\...` 找不到脚本**
Git Bash 的路径转换问题，已在 `ctx.sh` 中用 `to_host_path()` 处理。
若仍出现，请更新到最新版本。

---

## 7. 隐私与配额

- **默认纯本地**，不联网、不上传。
- 敏感信息默认脱敏（OpenAI key、GitHub token、AWS key、私钥、Bearer token、密码类键值）。
- 云端同步默认关闭；即便开启也只传索引与摘要，不传原始对话字节。
- 配额默认 `max_total_gb: 20`，可在配置中调整。

---

## 8. 卸载

```bash
"$S/ctx.sh" uninstall-hooks                 # 移除钩子
rm -rf ~/.workbuddy/skills/context-vault    # 删除技能
```

归档数据目录**不会被删除**，需自行处理（建议先备份）。
