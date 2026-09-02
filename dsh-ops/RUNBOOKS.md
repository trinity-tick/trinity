# 运维运行手册（2026-09-01 补充）

## PG 服务恢复（2026-09-01 修复）
supervisor 的 PG 恢复现为三级：提权助手（dsh-ops/elevated-pg-start.ps1，RunAs 静默提权，
UAC ConsentPromptBehaviorAdmin=0）→ 直连 Start-Service → pg_ctl fallback。
验证：PG 停止演练后 supervisor 在下一轮自动恢复（12:23 停 → 12:23:48 检测 → 拉起）。
注意：pg_ctl 拉起的实例服务状态显示 Stopped（非服务托管），重启机器后服务 Auto 自动恢复。

## 提权通道（终止提权启动的服务）
本机 UAC: ConsentPromptBehaviorAdmin=0 → Start-Process -Verb RunAs 静默提权。
模板: 写 payload ps1（按命令行匹配目标进程）→ Start-Process powershell -Verb RunAs -File payload -Wait
（任务计划 /rl HIGHEST 注册会被 Medium 完整性拒绝，勿用）

## 维护窗口流程（pool-sync/单写主迁移用）
1. 提权终止 api（trinity.api.server）
2. 执行窗口任务（pool-sync 等，API 在线时 SKIP 的设计）
3. supervisor pass 拉起 api；验证 /health + 端口

## 混沌演练（季度，建议）
- 杀 worker：taskkill python engine_worker → 插件应自动重连（观察 trinity_ping 恢复）
- 断 PG：Stop-Service trinity-pg → supervisor 探测 :5432 → Start-Service 兜底（观察日志）
- 磁盘满：向 D: 写占位文件至 95% → observe/backup 行为 → 清理
- 演练后必跑: quality-gate + reconcile + backup

## 记忆市场试点（2-3 agent）
- 协议: docs/MEMORY_MARKET_PROTOCOL.md（11 端点）
- 凭证: /audit/receipt/{id} 作 provenance 回执
- 建议首个试点: DSH 会话 ↔ 旺店通 WMS bot ↔ 知识采集器

## 多机同步（sync-agent）
- 配置: ~/.trinity/sync-agent.yaml（server.url 必须为远端，非环回）
- 安全守卫: 脚本拒绝推回本机聚合池
- 部署文档: dsh-ops/SYNC_AGENT_DEPLOY.md
- 异地备份: trinity-backup.ps1 产物（7.1GB/日）→ 加密外传 NAS/对象存储

## 告警激活
- TRINITY_ALERT_WEBHOOK 环境变量（或凭证文件同名项）设置后，
  supervisor 的 WARN/ERROR（含水位 STALE）与 maintenance 的 Send-Alert 自动推送。
## 事件驱动巩固（2026-09-01，大脑化第三阶段）
- 触发链: supervisor 每轮查 event_volume.py（近 1h dsh_events 数）→ ≥10 且距上次 >25min
  且当日 <24 次 → Start-Process maintenance -Tasks consolidate-recent
- consolidate-recent = sleep_consolidation --recent-days 1 --facts 10 --min-importance 0.3
- 重要: 事件钩子在主 Save-State 之后运行——Add-Member 后必须手动 Save-State，
  否则 lastConsolidateAt/consolidateCount 不落盘，间隔与上限全部失效
- 状态文件: .trinity/logs/dsh-supervisor-state.json（lastConsolidateAt/consolidateDay/consolidateCount）

## ps1 纪律（2026-09-01 三次事故教训）
1. 必须 UTF-8 BOM + CRLF：PS5.1 无 BOM 按 ANSI 读 → 中文注释字节被误解为引号 →
   here-string 断裂 → 整脚本解析崩溃。修复模板：
   ReadAllText → [regex]::Replace($c, '(?<!\r)\n', "\r\n") → WriteAllText($c, UTF8Encoding::new($true))
2. 路径一律正斜杠（从 JS/模板写入 ps1 时 \s / \e 会被吞成 $TrinityRootscripts...）
3. 行内注释不得出现在数组表达式中间（# 吞掉右括号）
4. 修后必验: powershell -File <script> 真实解析（.NET PSParser 按 UTF-8 读会漏掉 ANSI 误读）

## 存储加密盲区（2026-09-01）
- content 列 AES-256-GCM 密文（enc:v1:）——任何直连 SQL 读 content 的脚本
  （聚合/衰减/评测）必须解密（adapter._cipher.decrypt），否则 LLM 只见密文提取恒 0
- 症状特征: Phase2 done 1 秒完成 + extracted=0 + resolved to real

## 元认知行动化（2026-09-01）
- 门禁 FAIL → 写 ~/.trinity/.quality-strict → run_decay_compress λ×0.8 保守遗忘
- 门禁 PASS → 清除标记 → 恢复
- corrections_log 消费: analyze 中 PASS 观察（type=quality_gate + gate_ok=True）→ 同源
  open correction 标记 resolved

## 预测→验证链（2026-09-01）
- brain_report 每轮: 读 bench-results/metrics-history.json → 验证上轮预测（误差）→
  线性趋势预测下次 AnswerAcc/R@5 → 追加历史
- 第一个真实闭环: 预测 0.702 vs 实际 0.702（误差 0.000 ×2）

## 记忆市场生态（2026-09-01）
- 供给: maintenance -Tasks market-list（高价值记忆自动上架，幂等去重）→ 日链第 21 项
- 需求: 插件工具 trinity_market_search / trinity_market_buy（buyer=当前会话 agent）
- 持久化: ~/.trinity/memory_market_{orderbook,reputation,trust_exchange}.json（自 08-16）
  —— 验证持久化看底层 _orders（get_order_book 只返回 is_active）
- 归因: quality_gate --ablate（100q）→ keyword/hybrid 0.97 持平；search_hybrid_rrf
  裸引擎 0.0（聚合器环境才有语义通道价值）

## 审计链与用量（2026-09-01）
- 审计写路径: 单写主迁移后 audit 在 **PG audit_log**（活跃）；SQLite audit_log 冻结于
  08-27（镜像缺口，pg-backfill 不覆盖审计——如需镜像恢复另行加）
- 用量基线: PG audit 24h ≈ search 16 / search_hybrid 16 / create 12（脑检周报 5.7 段）
- 用量观察: AGENTS.md 注入（2026-09-01 20:0x 起）后的调用增长 = 阶段 A 观察窗口
- 注意: PG audit_log.timestamp 为 text，24h 过滤须 ::timestamptz 显式 cast


## 同仓多会话协作纪律（EXECUTION 458C，2026-09-02 事故教训）
- 共享仓库 C:/D: 上**禁止 git reset --hard origin/main**（会静默丢弃他人已提交的本地 commit——458b 曾被误删，靠 cherry-pick 救回）；
- 提交前先 git fetch origin && git status 确认无漂移；落后时用 rebase/merge 而非 reset；
- 并行会话共用 EXECUTION.md：新增轮次用顺序编号（457/458/459…），落笔前先看文件尾；
- 推送失败（网络抖动）时：本地 commit 保留 + D: 用 git fetch C:/ 本地同步兜底，勿删本地历史。
