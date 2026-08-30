# Trinity OPERATIONS 运维手册（2026-09，EXECUTION 165）

> 供任何维护者快速上手的运维指南。目标：让第二个维护者能接手。

## 一、系统组成
- **服务**：trinity-pg（Windows 服务，Automatic）+ API(8001)/MCP(8000,8003)/gateway(8002)/memstream(8010)
- **数据**：PG 主存储（D 盘，32k 记忆）+ SQLite 镜像（D 盘）+ 感知/自省记忆
- **模型**：Ollama（bge-m3 嵌入，supervisor 保活）+ DeepSeek API（LLM，本地 qwen 降级）

## 二、每日任务（autostart 03:00）
| 任务 | 作用 | 失败影响 |
|---|---|---|
| dcpm-consolidate | System2 信念→schema | 夜间归纳暂停 |
| replay-consolidate | 重放+对比训练 | 权重强化暂停 |
| integrity-monitor | 数据完整性自愈 | 缺失向量累积 |
| self-reflect | 会话自省 | 反思记忆暂停 |
| perception-scan | 日志/文件感知 | 环境感知暂停 |
| cognition-check | 认知能力自检 | 退化无告警 |
| web-perception | RSS 订阅感知 | 网络质料暂停 |
| web-search | Bing 主动搜索 | 主动获取暂停 |
| drift-check | 配置漂移检测 | 漂移无告警 |

## 三、故障排查步骤
1. **服务挂了**：看 supervisor 日志 → 手动重启 API（cwd 必须 D:\trinity-code，HF_HUB_OFFLINE=1）
2. **检索慢（>5s）**：查 Ollama bge-m3 是否常驻（保活）；冷载 6s 属正常
3. **任务 SKIP**：查 with_lease detail（164 轮已加打印）——显式 --db 已修复路径问题
4. **审计链 false**：重算链（幂等脚本）；写入路径 timestamp 已修复（123 轮）
5. **数据缺失向量**：integrity-monitor 自动回填；脚本场景用 wait_backfill=True

## 四、已知边界
- 短进程异步回填不可靠 → 脚本用 wait_backfill=True（165 轮）
- 冷启动首查 5-30s（预热窗口）
- C 盘 store 残留被 Harness 持有（保留不删，D/PG 已覆盖）
- RSS/搜索依赖网络（Bing/OSChina 等可达，BBC 被墙）

## 五、日常检查
```powershell
powershell -File dsh-ops/trinity-dsh-maintenance.ps1 -Tasks health,integrity-monitor,cognition-check,drift-check
```
