# Trinity 合规报告（2026-08-27）

## 记忆数据
- 记忆总数: 27901 | 活跃: 11548
- 审计日志: 65067 条（近 7 天 51441）

## 检索决策（近 7 天 3753 次——样本）

- search: query=Trinity | hits=3 | ms=208.4 | layer=None
- search: query=Trinity | hits=3 | ms=451.3 | layer=None
- search_hybrid: query=WMS ?????? | hits=3 | ms=None | layer=None

## 自动化动作
- stats: {"emitted": 0, "matched": 0, "executed": 1, "failed": 0}

## 可验证性
- 存储加密（AES-256-GCM）默认开启；每条记忆 SHA-256 哈希 + 版本链可独立重算
- 审计回执: GET /audit/receipt/{memory_id}；全链: GET /audit/integrity