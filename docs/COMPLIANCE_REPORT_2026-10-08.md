# Trinity 合规报告（2026-10-08）

## 记忆数据
- 记忆总数: 115742 | 活跃: 28733
- 审计日志: 392124 条（近 7 天 80471）

## 检索决策（近 7 天 8153 次——样本）

- search: query=Trinity 记忆操作系统 架构 | hits=0 | ms=392.5 | layer=None
- search_hybrid: query=Trinity 记忆系统 架构 | hits=3 | ms=None | layer=None
- search_hybrid: query=Trinity 记忆系统 架构 | hits=3 | ms=None | layer=None

## 自动化动作
- stats: {"emitted": 124, "matched": 123, "executed": 113, "failed": 10}

## 可验证性
- 存储加密（AES-256-GCM）**仅 SQLite 镜像启用**（原写「默认开启」，2026-09-29 勘误：对生产 PostgreSQL 不成立——PG 侧 7,675/51,242 行即 15.0% 为明文；SQLite 的 tokenized_content 为明文影子列 97.3%）；每条记忆 SHA-256 哈希 + 版本链可独立重算
- 审计回执: GET /audit/receipt/{memory_id}；全链: GET /audit/integrity