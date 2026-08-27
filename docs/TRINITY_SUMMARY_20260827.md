# Trinity 总览（2026-08-27 最终版）

## 一、作用（能做什么）

### 对 Agent（记忆核心）
- 记住：事实/偏好/决策/轨迹（加密 AES-256-GCM + 审计链 + 版本链）
- 回忆：混合检索（FTS/语义/图谱/页树/reason 判题）
- 治理：更新/删除/冲突解决/遗忘
- 自我认知：查询自身进化状态/目标/评测

### 对用户/人
- 记忆流 Web UI（:8010）：时间线/类别过滤/检索高亮/统计/热门查询
- 知识周报（每日自动生成）
- 合规报告（一键导出）

### 对开发者
- 自进化引擎（目标→评测→达标/受阻自动判定，记忆/系统/代码三类指标）
- 评测护栏（eval 断言 12 项）+ 官方 LongMemEval-S 基准
- 实验审阅（自动对比归因）+ 使用反馈（热门/高频/闲置）

### 对运维/系统
- 每日维护链 36 任务（decay/tiers/同步/备份/周报/合规/审计/自检）
- 事件驱动自动化（8 规则 + 全编排：if/delay/retries/审批状态机/定时）
- supervisor 自愈（API/MCP/GATEWAY/UI 掉线自动拉起）
- ps1 三件套自检（audit-ps1）

### 对多 agent 协作
- AgentMesh：委托总线（create/claim/complete/expired + 订阅 + 分解 + 配额）

### 对业务（WMS 领域）
- 198 知识源（185 文件 + 结构化 2646 条）全量可检索
- 知识健康度治理（0 stale，过期自动告警+重新摄入）

## 二、性能（实测数字）

| 项 | 数字 |
|---|---|
| 检索延迟 | 毫秒级（FTS）/ reason 判题缓存+蒸馏 |
| judge 蒸馏 | 启发式 0.55 阈值，LLM 调用 8→0（holdout 不降） |
| 页树构建 | 全量 100s → 增量 1.2s + 向量增量 0.2s |
| 官方基准 | 升级口径 300q：Session R@10 0.99 / Turn 0.9433 / QA 0.4667 |
| 独立验证 | 50q：0.94 / 0.92 / 0.48（可复现） |
| 评测成本 | 缓存+蒸馏后显著下降 |
| WAL/锁 | 0 锁 0 膨胀（每日监控） |

## 三、功能清单（入口）

| 功能 | 入口 |
|---|---|
| 记忆读写 | MCP / REST :8001 / CLI |
| 混合检索 | /memory/search/hybrid |
| 知识问答 | knowledge_search |
| RAG 服务 | POST :8002/v1/retrieval |
| 进化状态 | /evolution/status |
| 审计回执 | /audit/receipt/{id} |
| 记忆流 UI | http://127.0.0.1:8010 |
| 自动化 | rules.yaml + emit |
| 维护链 | trinity-dsh-maintenance.ps1 -Tasks all |
| 联邦同步 | -Tasks federation-sync（TRINITY_FED_TARGET） |
| 周报/合规 | -Tasks produce |
| 遗忘治理 | -Tasks forgetting |
| 页树 | -Tasks pagetree（每日增量+周日全量） |

## 四、运行情况（当前）

- 服务 5/5 全在线（8000/8001/8002/8003/8010）
- 记忆 27959 行 / active 11600 / 审计 59k+ / 完整性 ok / WAL 0
- 自动化：emitted 46 / matched 10 / executed 10 / failed 0
- 维护链 36 任务三件套齐全（巡检 ALL OK）
- 测试 46+ 全绿；git 工作区干净（300+ commits 可回滚）

## 五、运行步骤

### 启动（supervisor 全自动）
1. `powershell -File dsh-ops\trinity-supervisor.ps1`（每 5 分钟循环守护）
2. 服务自动拉起：API :8001 / MCP :8000/:8003 / GATEWAY :8002 / UI :8010

### 每日维护（自动）
- 每日 03:00 维护链 36 任务自动运行（含周报/合规/审计/备份）

### 常用操作
```
# 健康检查
curl http://127.0.0.1:8001/health

# 检索
python -m trinity search --query "..." --top-k 5

# 评测
python scripts/run_evals.py --all

# 维护（手动）
powershell -File dsh-ops\trinity-dsh-maintenance.ps1 -Tasks all

# 记忆流
浏览器打开 http://127.0.0.1:8010

# RAG 接入
POST http://127.0.0.1:8002/v1/retrieval {"query": "...", "top_k": 5}
```

### 升级/回滚
- git 工作区干净；任何改动可 git checkout 回滚
- audit-ps1 每日自检三件套完整性

*生成 2026-08-27*

﻿---

## 六、闭环情况（最核心的运行特征）

### 1. 自进化闭环（95 轮实证）
目标(goal_create) → 评测(default_metrics: 记忆/系统/代码) → 达标/受阻自动判定
→ 经验沉淀(decision 记忆) → 使用反馈(usage_feedback) → 下一轮目标
当前：4 complete（0.752 / 0.6632 / 1.0 / 1.0）+ 1 blocked（MS 0.2375）

### 2. 自动化事件闭环（实测运转）
事件(emit) → 规则匹配(8 规则) → 动作(notify/exec) → rollout 审计(全链)
→ 失败告警(automation.failed) → 修复
实测：emitted 46 / matched 10 / executed 10 / failed 0

### 3. 知识生命周期闭环（E2E 验证）
知识源采集(kb_harvest) → 健康度监控(build_sources emit_stale)
→ stale 检测(>30 天, 动态阈值) → 自动告警 + 自动重新摄入(--stale-only)
→ 知识更新 → 检索使用

### 4. 记忆生命周期闭环（认知治理）
写入 → 使用(access_count) → 价值分(memory_value) → 遗忘决策(forgetting_score 每日)
→ 高价值豁免(>=0.7 不归档) → 检索降权(stale 后置) → 归档/版本链

### 5. 维护闭环（自检自愈）
每日 36 任务 → audit-ps1 三件套巡检 → 发现问题(报告) → 修复 → 再巡检
supervisor 5 分钟自愈(服务掉线自动拉起)

### 6. 使用反馈闭环
检索/写入使用 → usage_feedback(热门查询/高频记忆/闲置) → 进化输入
→ 优化 → 使用变化 → 再反馈
实测：3,300+ 次搜索/7 天，反馈闭环运转

### 7. 多 agent 协作闭环（AgentMesh 实战验证）
委托创建(decompose) → 订阅通知(delegation.notify) → 认领(claim 原子)
→ 完成(complete 回写) → 结果验证(inbox) → 配额治理(agent_quota)

### 8. 验证闭环（防自证）
基准运行 → manifest 防口径漂移 → 独立 fresh 环境验证(seed 777)
→ 可复现确认(sess_R 0.94-1.00 / QA 0.45-0.48) → 目标判定依据

### 9. 认知闭环（层感知）
查询 → _infer_layer(时间→episodic/知识→semantic) → 层过滤 → 结果
→ 审计回放(query/hits/ms/layer) → 决策可证明

### 10. 健康闭环
system_health 指标(ps1 三件套/WAL/备份/API) → 目标跟踪(1.0 complete)
→ 异常自动暴露 → 修复 → 指标回绿

**总结**：Trinity 不是"记忆库"，而是一套**十环相扣的自运行系统**——数据从写入到使用到遗忘到证明，每个环节都有闭环监控与自动动作，且全部实测运转。

