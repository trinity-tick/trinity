# Trinity ROADMAP（2026-08-30 梳理，EXECUTION 135）

> 遗留事项集中清单（此前散落 EXECUTION 各处）。状态：pending/in-progress/done。

## P0（数据安全）
- [ ] **C 盘残留 store 646MB 清理**（114 轮遗留；Harness 锁，D 有副本 + PG 主存储，低风险）
  - 前置：确认 Harness 不再持有 → 删除 C:\Users\Administrator\.trinity\store

## P1（运维可靠性）
- [ ] **with_lease 租约 SKIP(reason=error) 根因**（135 轮绕行：全任务移除 LeaseJob）
  - 现状：maintenance 子进程 acquire 报 error（手动模拟正常）；未影响功能（绕行）
  - 后续：如恢复租约机制需深挖子进程 env 差异
- [ ] **短进程异步回填可靠性**（131 轮：API 长驻 OK；测试脚本短进程线程被杀）
  - 方案：脚本场景显式调用同步回填（或 sleep 等待）
- [ ] **冷启动窗口 5-30s**（124 轮遗留：BM25/reranker 预热不完整）
  - 方案：预热顺序优化 / TRINITY_PREWARM_* 扩展

## P2（性能与效果）
- [ ] **稳态检索 780ms → 500ms**（124 轮目标；rerank 缓存）
- [ ] **rerank A/B 效果对比**（模型已就绪未正式 benchmark）
- [ ] **value_boost_k / synapse K1/K2 500q 校准**（122 轮确认保守值，未大样本）

## P3（基准与文档）
- [ ] **LongMemEval-V2 官方数据完整版**（130 轮；gated，HF 认证后可跑）
- [ ] **ARCHITECTURE.md 大脑化全景同步**（135 轮补）
- [ ] **大脑化机制测试背书**（14 项机制无 pytest 覆盖）

## Done（135 轮梳理时）
- [x] 维护链 18 个带租约任务假 SKIP（134-135 根治：全移除 LeaseJob）
- [x] 夜间整合任务（dcpm/replay）真实运行（134）
- [x] API 写入向量回填（131）
- [x] 数据完整性巡检入链（133）
