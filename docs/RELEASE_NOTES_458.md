# Trinity Release Notes — EXECUTION 457-458（2026-09-02）

版本: v8.2.1（本机运行版）| 仓库: github.com/trinity-tick/trinity | 分支: main

## 本包新增（457 大脑化体检优化 + 458 下一步全执行）
- 情境持续上下文流（situation_stream：双写 JSON+PG ctx:brain，检索自动注入"当下"）——意识蓝图 82→85（情境 6→9）
- 语义级视觉（本地 qwen2.5vl:3b，vision.py 语义优先/特征降级；/memory/perceive 已接线）
- 第二 agent ops-bot 自治日循环（主题轮转→命名空间检索→决策记忆→市场上架）+ 记忆市场真实成交
- 持续感知流 perception_loop（inbox 截图→语义视觉→vision 通道感知记忆→情境流刷新；30 分钟调度）
- 图谱增密 graph_densify：entities 187→3,188 / relations 980→17,803（幂等日门）
- 核心级自进化阶段3试点：evolve_core_gate（allowlist→LLM 提案→AST→行为门禁→168 pytest→commit）首个补丁 crypto.is_encrypted 合入
- 联邦最小双实例验证：federation_mini_demo（A3/B2 → 双向传播+幂等去重 PASS）
- 自主调度入日链：replay,curiosity,proactive,cognition-agent,situation,opsbot-cycle,perception-continuous（26 任务）
- 修复：ToM focus 推断（session_context 无 agent_id）、persistence 测试 env 泄漏、runner 误连生产 PG（强制 sqlite 隔离 + WAL 提速）、vision 通道缺失、OLLAMA_HOST 0.0.0.0、多处 D: 硬编码

## 官方基准（已锁定）
- LongMemEval oracle 500 题：R@1/3/5/10 = 1.000/1.000/1.000/1.000（六类全绿，复现 EXIT=0）
- AnswerAcc 0.560（SS-U .986/KU .731/SS-A .679/TR .399/MS .391/SS-P .367，LLM judge $0.40）
- 生成侧弱项策略 A/B 结果见 .trinity/bench-official/qa_strategy_*.json

## 诚实差距（未做/待做）
- MCP registry/PyPI 上架与 GitHub Release 页：需账号与外部注册（本机已备发布清单，tag 随仓库推送）
- BEAM 1M/10M 档、官方 LongMemEval-M、真多机 WAN 联邦：外部条件（数据/第二台机器）未满足，如实标注
- LongMemEval-S 265MB cleaned 全量 runner 仍受 ingest 单线程限制（已提速补丁，建议后续并行化）
