# Trinity 知识周报（2026-10-08）

> 自动生成：高价值决策/知识/总结记忆聚合（近 1 天）

## knowledge（18 条）

- 进化周期 #112 于 2026-09-09 21:19 沉淀，包含 frequent_search 查询关于初次成为高级软件工程师时团队规模的问题
- doc:summary:Trinity 状态为 supported，基于 15 条证据，最新证据时间为 2026-09-16 16:22:17
- procedural:偏好 状态为 supported，基于 6 条证据，最新证据时间为 2026-09-16 04:25:39
- knowledge:语料预热 状态为 supported，基于 35 条证据，最新证据时间为 2026-10-05 20:10:55
- episodic:task 状态为 supported，基于 22 条证据，最新证据时间为 2026-10-07 02:37:13
- episodic:attempt 状态为 supported，基于 25 条证据，最新证据时间为 2026-10-07 00:16:03
- 存在 graphql 闭环相关记录，编号包括 1791319498、1791231740、1791145173、1791058781、1790972444、1790887124、1790799923、1790713302、1790627094
- n=548 复现显示 B2 显著优于 A2（p=2e-6），A2 也显著优于 B1，说明胜出的是「正文级词法索引」而非「词法」本身。
- Trinity 是按传输层分流的双库架构：HTTP REST :8001 全部端点路由到 SQLite（C:\Users\Administrator\.trinity\store-restored\trinity_store.db，2.71GB，memories 114,488 行 / active 27,55x）；任
- Trinity 加固第三轮 + 提交（2026-10-06）：提交 4501716，领先 origin/main 451（未 push）；现场 20 checks/0 FAIL；定向回归 267 passed；全量单测 4152 passed/38 failed；新建 trinity/agents/aggregator
- Trinity 加固第三轮 + 提交（2026-10-06）：提交 4501716，领先 origin/main 451（未 push）；现场 20 checks/0 FAIL；定向回归 267 passed；全量单测 4152 passed/38 failed。新建 trinity/agents/aggregator
- 文档级词法重排（trinity/retrieval/doc_lexical_rerank.py）已存在、已接线、已插桩，但在本部署下运行时是死的（empty_index）；实现为按 metadata.source_file 聚合的文档级 BM25 + 章节标题加权 + 中文 bigram，无 LLM。
- doc_lexical_rerank 并非因 PG 不可用而静默死掉——PG 实际是通的；此前「PG-only 导致 empty_index」的断言被 in-process 调 _build_pg 实测推翻。
- Trinity 审计链存在「双链外锚」缺陷：scripts/audit_anchor.py 只锚 PostgreSQL 链（127.0.0.1:5432/trinity，audit_log≈123,755 行），而 scripts/audit_fullchain_verify.py 校验的是 HTTP REST :80
- Trinity 审计链「双链外锚」修复：scripts/audit_anchor.py 只锚 PostgreSQL 链（127.0.0.1:5432/trinity，audit_log≈123,755 行），而 scripts/audit_fullchain_verify.py 校验的是 HTTP REST :8001
- 向量通道/融合这条线已查到头：在生产入口上独立复现了预注册负结果；启用向量通道不是改善，而是把 top-10 整批换掉（E1 补齐分量后与现状重合 0/10）。
- 零依赖词法在四套题集上都胜过 A2；n=548 复现显示 B2 显著优于 A2（p=2e-6），A2 也显著优于 B1，说明胜出的是「正文级词法索引」而非「词法」本身。
- A 的近零召回是语料作用域问题而非检索差：A（全库、无作用域）275 题全部返回满 10 条，但 2,750 个槽位中 1,892 个（69%）文档基名为空（缺 metadata.source_file），doc 级 R@k 必然算不中。
