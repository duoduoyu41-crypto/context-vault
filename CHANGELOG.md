# Changelog

## 0.1.0

- P0 最小可用版本
- 增量归档 transcript（含子 agent 递归）到 L2 片段与 L3 原文
- 关键词检索（中文不分词，子串匹配 + 短词 bigram 增强）
- `recall` 按 token 预算回灌，支持 `--hook` 输出注入用 JSON
- 滚动摘要：确定性骨架 + 宿主 `compact_summary` 合流
- 存储根跨平台自动探测（含 Windows 盘符 A-Z 兜底）
- `install-hooks` / `uninstall-hooks` / `hooks-status`，幂等且保留备份
- `doctor` 自检、`migrate` 迁移、`index-rebuild` 重建索引
- 敏感信息默认脱敏
