# 存储后端

## 定位

**本地磁盘是主归档层，云端仅作辅助且默认关闭。**

## 本地磁盘（推荐，默认）

### 探测优先级链

1. 环境变量 `CONTEXT_VAULT_HOME`
2. `<home>/.workbuddy/context-vault/config.json` 的 `root`（marker 失效则跳过）
3. 已存在 `.context-vault-marker` 的目录（换机/插移动硬盘自动找回，得分 +1000）
4. 自动探测：枚举挂载点 → 过滤 → 打分
5. 回落 `<home>/.workbuddy/context-vault/archive`

### 打分

```
score = min(free_gb, 500)
      + 30            （非系统盘）
      + 1000          （已有归档标记）
      - 500           （落在用户家目录下，兜底降权）
```

淘汰条件：剩余空间 < `min_free_gb`（默认 2GB）；类型属于可移动盘 / 网络盘 / 光驱 / RAM / 伪文件系统。

### 平台差异（实测坑）

- **Windows**：`GetLogicalDriveStringsW` 在某些会话下只返回 `C:\`，D/E/F 全部漏掉。
  必须叠加 `A-Z:\` 逐个 `os.path.isdir()` 兜底，否则探测必然只看到系统盘。
- **Linux**：读 `/proc/mounts` 取 fstype，扫描 `/mnt`、`/media`、`/data`。
- **macOS**：执行 `mount` 解析 fstype，扫描 `/Volumes`。

### 可写性

必须**实写实测**：建目录 → 写文件 → 读回比对 → 删除。
`os.access()` 会被权限、配额、只读挂载欺骗。

## 云端（可选）

| 后端 | 适用性 | 说明 |
|---|---|---|
| 云数据库（PostgreSQL） | 可用，需先开通 | Agent 侧写入通道是 `workbuddy_cloudservice_db_exec_sql`；TEXT 单值理论上限约 1GB；有配额/计费，数据归属具体应用 |
| 云对象存储 | **当前不适用** | 未暴露 Agent 侧上传工具，只能在应用运行时用 SDK；无公开直链 |
| 资料库（library） | 适合作检索层 | 可写个人空间，支持块级语义检索；适合存摘要/索引，不适合存原始字节流 |
| 项目网盘（tdrive） | 仅手动备份 | 写操作必须先经用户确认，会打断自动归档；上传为「申请 URL → curl PUT → 完成」三步 |

### 云端同步原则（P2）

- 默认关闭，需显式开启。
- **只同步 L0 索引与 L1 摘要**（单会话 < 200KB），不同步原始字节。
- 强制开启脱敏。

## 配额与清理

- `max_total_gb`（默认 20）：超限时按 LRU 清理最老会话的 **L3 原始字节**，
  **保留 L1 摘要与 L2 片段**，确保摘要与检索能力不丢。
- 剩余空间低于 512MB 时停止归档并写入 `<root>/ALERT`。
- 迁移与清理一律**改名**为 `.old-<ts>` / `.superseded-<ts>`，**绝不删除**。
