# Trinity 知识周报（2026-10-07）

> 自动生成：高价值决策/知识/总结记忆聚合（近 1 天）

## decision（12 条）

- [★ 更正] 我上一轮说「doc_lexical_rerank 因 PG 不可用而静默死掉」是**错的**——PG 其实是通的（2026-10-06，session-f4924ca3）  ## 更正 我上一轮据此断言：`doc_lexical_rerank` 是 PG-only，本部署 SQLite + PG 口令不可
- [完整回归结果] tests/unit 3963 passed / 11 failed；其中**仅 1 个归因于我**，已修（2026-10-06，session-f4924ca3）  ## 完整 `tests/unit` 结果（65 分钟） `11 failed, 3963 passed, 20 skipped`。逐
- [★ 最重要发现] 文档级词法重排**早已存在、已接线、已插桩——但在本部署下运行时是死的**（`empty_index`）（2026-10-06，session-f4924ca3）  ## 发现 `trinity/retrieval/doc_lexical_rerank.py` **已经实现了**我这几个回合一直在重
- [已执行] `metadata.source_file` 回填 6,784 行：覆盖率 13.0% → 37.8%，健康未变，可回滚（2026-10-06，session-f4924ca3）  ## 执行内容与结果 用户明确确认后执行 `python scripts/backfill_source_file.py --
- [作用域根因·可修] 6,784 条真实文档行缺 `metadata.source_file`；并有配套审计/门控回填工具（**未执行实写**）（2026-10-06，session-f4924ca3）  ## 缺口是真数据缺口，不是设计如此 全库 active 27,381 行，**只有 3,569 行（13.0%）
- [根因查清 + 人工题集复现] A 的近零召回是**语料作用域**问题；零依赖词法在**四套题集**上都胜过 A2（2026-10-06，session-f4924ca3）  ## (1) 根因：A 不是"检索差"，是**语料作用域**问题 用评测产物逐题 `pages` 诊断（`temp/diagA.py`）： - 
- [★ 换生成输入复现] 「B2 更好」是**查询构造来源的产物**：标题生成的题集上 B1 反而显著胜过 B2（2026-10-05/06，session-f4924ca3）  ## 做法（换生成输入） `scripts/build_section_golden.py`（12 tests）：**只给 LLM 该文档的章
- [★ 区别特征获统计支持] B2 增益**全部集中在高碎片文档**；低碎片文档上 B2 反而更差（2026-10-05，session-f4924ca3）  ## 判据与结果（统计，不是比均值） `scripts/chunk_heterogeneity_test.py`（7 tests）。做法：按 target 文档 
- [★ 文档级切分推翻"可整体改默认"] B2 的增益**不均匀**：一半文档 +16pp(显著)、另一半 +2pp(不显著)（2026-10-05，session-f4924ca3）  ## 做法 对 n=548 的评测产物 `output/doc_retrieval_eval_n548_20261005_224624
- 内语(评估：关于『对「备份」的巡检判断』，我有相关记忆可参考；计划：可以先检索相关知识，再综合决策) [ops-bot 自治] 2026-10-06 主题「备份」巡检：自证 3 条 / 外部线索 2 条 | 自证: 内语(评估：关于『对「自证」的巡检判断』，我有相关记忆可参考；计划：可以先检索相关知识，再综合决策) [
- [n=548 复现完成] B2 显著优于 A2（p=2e-6）；但**A2 也显著优于 B1** ⇒ 胜出的是「正文级词法索引」而非「词法」本身（2026-10-05，session-f4924ca3）  ## 结果（同作用域、配对、n=548） `output/doc_retrieval_eval_n548_2026
- [两个前置条件执行结果] 配对检验证明 B2 优势**显著**(p=0.0001)；B2 退化曲线已实测（2026-10-05，session-f4924ca3）  ## (a) 同作用域配对显著性检验 —— **显著** 上一轮只有点值（A2 0.496 vs B2 0.616）。本轮用 `scripts/paire

## knowledge（30 条）

- n=548 复现实验显示 B2 显著优于 A2（p=2e-6），但 A2 也显著优于 B1，说明胜出的是「正文级词法索引」而非「词法」本身。
- n=548 复现显示 B2 显著优于 A2（p=2e-6），但 A2 也显著优于 B1，说明胜出的是「正文级词法索引」而非「词法」本身。
- Trinity 作用域补齐（scoped top-up）对 R@k 的贡献为0；历史 ΔR@10=+0.2167 的前提已不成立。
- 全库 active 27,381 行中只有 3,569 行（13.0%）有 metadata.source_file；6,784 条真实文档行缺该键，且已有配套审计/门控回填工具但未执行实写。
- 文档级词法重排（trinity/retrieval/doc_lexical_rerank.py）早已存在、已接线、已插桩，但在本部署下运行时是死的（empty_index）；其实现为按 metadata.source_file 聚合的文档级 BM25 + 章节标题加权 + 中文 bigram，无 LLM。
- Trinity 是按传输层分流的双库：HTTP REST :8001 全部端点路由到 SQLite（C:\Users\Administrator\.trinity\store-restored\trinity_store.db，2.71GB，memories 114,488 行 / active 27,55x）；任何读
- 全库 active 27,381 行中只有 3,569 行（13.0%）有 metadata.source_file；6,784 条真实文档行缺该键，且已有配套审计/门控回填工具但未执行实写（2026-10-06，session-f4924ca3）
- Trinity 是按传输层分流的双库：HTTP REST :8001 全部端点路由到 SQLite（C:\Users\Administrator\.trinity\store-restored\trinity_store.db，2.71GB，memories 114,488 行 / active 27,55x）；任何读
- 文档级词法重排（trinity/retrieval/doc_lexical_rerank.py）早已存在、已接线、已插桩，但在本部署下运行时是死的（empty_index）；其实现为按 metadata.source_file 聚合的文档级 BM25 + 章节标题加权 + 中文 bigram，无 LLM（2026-10
- 多模态融合感知显示主题『当下、当下的意思和含义、知乎』同时出现在 web(5)、log(5)、filesystem(5) 通道
- 多模态融合感知显示主题『当下』同时出现在 web(5)、log(5)、filesystem(5) 通道
- 存在复发主题：51 条记忆、21 天窗口，涉及偏好与 Trinity 文档摘要
- doc:summary:Trinity 状态为 supported，基于 15 条证据，最新时间 2026-09-16 16:22:17
- procedural:偏好 状态为 supported，基于 6 条证据，最新时间 2026-09-16 04:25:39
- 存在复发主题：41 条记忆、21 天窗口，涉及进化周期沉淀
- 进化周期 #112 沉淀于 2026-09-09 21:19，包含 frequent_search 查询关于初次成为 Senior Software Engineer 时的团队规模
- 存在复发主题：17 条记忆、21 天窗口，涉及用户偏好设置
- 存在复发主题：15 条记忆、21 天窗口，涉及 P04 补货流程
- SmartCos WMS 五维对标评价报告 V10（最深度重构AI主导终局版）中，流程 P04 补货的步骤链为 触发→计划→执行→确认，AI模式为 Auto，竞品参照为菜鸟，状态为 ✅
- 语料预热状态为 supported，基于 35 条证据，最新时间 2026-10-05 20:10:55
- 存在 graphql闭环 系列记忆条目，编号随时间递增（1790540062 至 1791231740）
- 接口调用规范：REST风格API，使用HTTP(S)发送POST请求，请求头Content-Type为text/xml，报文为UTF8编码的XML字符串，测试环境用HTTP、正式环境用HTTPS
- 接口调用规范：REST风格API，使用HTTP(S)发送POST请求，Content-Type为text/xml，报文为UTF8编码的XML字符串，测试环境用HTTP、正式环境用HTTPS，不支持表情符号等非法字符
- 接口调用规范：REST风格API，使用HTTP(S)发送POST请求，请求头Content-Type为text/xml，报文为UTF8编码的XML字符串，测试环境用HTTP、正式环境用HTTPS，不支持表情符号等非法字符
- doc_route开启后无实质变化；且生产混合检索A打不过零依赖BM25 B2（2026-10-05）
- 启用向量通道/补齐融合分量的真实后果不是改善，而是把top-10整批换掉（E1向量独有入top-10为10，与现状E0重合0/10）
- 向量通道/融合这条线已查到头：在生产入口上独立复现了预注册负结果，用线程局部预算作用域做决定性A/B（OFF早返回空，ON预加载20003行索引）
- 向量通道/融合这条线已查到头：在生产入口上独立复现了预注册负结果，OFF不预加载时_vector_search早返回空，ON预加载后索引可用
- doc_route开启后无实质变化；且生产混合检索A打不过零依赖BM25 B2（2026-10-05，session-f4924ca3）
- 向量通道/融合这条线已查到头：在生产入口上独立复现了预注册负结果（2026-10-05）
