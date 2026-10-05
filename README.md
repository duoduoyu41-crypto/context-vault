# context-vault

长对话上下文的**本地磁盘归档与按需取回**。专为 WorkBuddy / CodeBuddy（及其 CLI 内核 CodeBuddy Code）设计。

解决两个痛点：

- 上下文窗口不够用，被压缩后早期内容全忘，不得不反复重复同一件事；
- 开 Max 模式能保留完整上下文，但额度消耗明显更快。

## 原理

宿主的对话历史**本来就在实时落盘**为 JSONL。本技能在上下文被压缩丢弃**之前**
（`PreCompact` 钩子）把它增量复制到本地归档盘，之后通过「滚动摘要 + 关键词检索」按需取回。

不开 Max 模式，也不需要云端，信息零丢失。

## 安装

### 方式一：复制到 skills 目录（推荐）

```bash
# 用户级（所有项目可用）
cp -r context-vault ~/.workbuddy/skills/

# 项目级（随仓库共享）
cp -r context-vault <项目>/.workbuddy/skills/
```

安装目录按以下顺序探测，取第一个存在的即可：`~/.workbuddy/skills/` → `~/.codebuddy/skills/` → `~/.agents/skills/`。

### 方式二：指定位置后用环境变量

```bash
export CONTEXT_VAULT_HOME=/path/to/your/archive
```

## 快速开始

```bash
S=~/.workbuddy/skills/context-vault/scripts

# 1. 查看本机哪些盘适合存放归档
"$S/ctx.sh" init-storage --dry-run

# 2. 初始化（或用 --pick 指定位置）
"$S/ctx.sh" init-storage

# 3. 归档当前会话
"$S/ctx.sh" archive

# 4. 需要回想时
"$S/ctx.sh" recall "关键词" --budget 4000

# 5. 开启自动归档（可选，会写 settings.json）
"$S/ctx.sh" install-hooks --home both

# 6. 自检
"$S/ctx.sh" doctor
```

> 第 5 步写入的是受保护文件 `settings.json`，需到 `/hooks` 菜单审查确认后才会生效。

## 存储位置如何选

自动探测，优先级：

1. 环境变量 `CONTEXT_VAULT_HOME`
2. 配置文件里的 `root`（失效则跳过）
3. **已存在 `.context-vault-marker` 的目录** —— 换机器或插入旧归档盘时自动找回
4. 自动打分：剩余空间大 + 非系统盘优先
5. 回落用户家目录

打分 `min(可用GB, 500) + 30(非系统盘) + 1000(已有归档) - 500(家目录)`。
换机器时把整个归档目录拷过去即可，标记文件会被自动识别。

## 隐私

- **默认纯本地**，不联网、不上传。
- 敏感信息默认脱敏：OpenAI key、GitHub token、AWS key、私钥、Bearer token、密码类键值。
- 云端同步默认关闭，即便开启也只传索引与摘要，不传原始对话字节。

## 与 MEMORY.md 的分工

| | MEMORY.md | context-vault |
|---|---|---|
| 容量 | 200 行 / 25KB 上限 | 无上限（受磁盘配额） |
| 内容 | 长期结论、偏好、约定 | 完整对话过程（每轮、每次工具调用、子 agent） |
| 读取 | 每次会话全量注入 | 按需检索，默认不占 token |

压缩后先从 context-vault 找回证据，再把精炼结论回写 MEMORY.md。

## 要求

- Python ≥ 3.8（仅用标准库，无需 pip install）
- Windows / macOS / Linux 均可
- Windows 下钩子通过 Git Bash 执行，脚本已处理路径转换

## 卸载

```bash
"$S/ctx.sh" uninstall-hooks    # 移除钩子
rm -rf ~/.workbuddy/skills/context-vault
```

归档数据目录不会被删除，需自行处理（建议先备份）。

## 许可

MIT
