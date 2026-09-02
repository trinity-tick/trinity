# DSH 发版兼容检查清单（2026-09-01，rc.7 断裂教训制度化）

> 背景：2026-08-24 DSH rc.6→rc.7 升级后，dsh-trinity 插件静默失效 8 天
> （插件加载模型变更：需 dsh.bundle.patch 声明；profile 级 cordis.patch.yml 不再被
> headless CLI 读取；scope 包名条目被 web 丢弃）。本清单用于每次 DSH 相关升级前。

## 升级前
- [ ] 记录当前 dsh 版本（dsh --version）与插件版本（profiles/*/node_modules/@deepseek-ai/dsh-trinity/package.json）
- [ ] 跑基线: maintenance -Tasks quality-gate,plugin-smoke （留档 PASS 基线）

## 升级后（DSH 升级 / 插件改动 / web 宿主重启后）
- [ ] 1. 插件加载: python scripts/dsh_plugin_smoke.py（bundle 层 + trinity_ping + 水位推进）
- [ ] 2. web 宿主: MCP /mcp 会话调 trinity_ping（web 侧 worker 存活）
- [ ] 3. 结构同步: structure_watermark_check.py 输出 OK（事件流动）
- [ ] 4. 检索: quality-gate R@5 无回归（对比基线 0.916）
- [ ] 5. 存储: reconcile 无新增分叉（pg_only/sq_only/hash_mismatch 与基线一致）
- [ ] 6. 工具清单: 新会话可见 trinity_ping/search/write/update/delete/audit/diagnostics/
      chronicle/tag_search/identity_register/trajectory/sessions/structure_stats/chat

## 已知失效模式速查（直接对照）
| 症状 | 原因 | 处置 |
|---|---|---|
| 会话无 trinity_* 工具 | bundle 未声明 dsh.bundle.patch | 插件 package.json 补 "dsh":{"bundle":{"patch":"./cordis.patch.yml"}} |
| headless 不加载 | rc.7 headless 不读 profile 级 cordis.patch.yml | 插件作为 profile 依赖安装（dsh plugin --profile X add <repo>）+ link 依赖固化 package.json |
| 插件 import 报 ERR_MODULE_NOT_FOUND | link 安装后从 repo 解析依赖 | 插件目录 node_modules/@deepseek-ai/{dsh-tools,schemastery,dsh-subprocess} 建 junction 到宿主树 |
| 结构事件冻结（dsh_events 不增长） | 插件未 apply → 无 structure_sync | 跑本清单 1-3；水位告警 supervisor 兜底 |
| 脚本连接 PG 报 password auth | localhost→::1 | 显式 --host 127.0.0.1（已知坑#1） |

## 自动兜底
- supervisor: 结构水位 24h 无推进 → WARN + webhook（设 TRINITY_ALERT_WEBHOOK 后生效）
- 周计划: 周一 quality-gate + plugin-smoke 自动回归
