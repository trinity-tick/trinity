# Trinity 最佳实践（C4）

> 基于 2026-08-14 全量执行（A1-A5/B1-B5/C1-C4）沉淀的工程经验。

## 1. 写入

- **按会话聚合写入**：整段多轮对话用 `POST /memories/session` 聚合为一条记忆。
  LoCoMo 实测：逐 turn 写入 Recall@5=0.14，聚合写入后 0.88。
- **带结构化字段**：`tags`/`category`/`importance` 三件套必填，检索与治理都依赖它们。
- **RBAC 头**：写路径必须带 `X-Agent-ID`（缺失 → 401）；gateway 已内置。

## 2. 检索

- **默认用聚合池通道**（`/agents/memory/search?mode=hybrid`）：结果自带 content；
  引擎 hybrid 只返回 id+score（A1 已修复 content_preview，但全文仍需按 id 回填或走池）。
- **策略选择**（A2 实测，10 查询 × 5 策略）：
  - 默认 `rrf`（233ms / 100% 命中，最快且稳）
  - 需要召回多样性用 `fusion`（与 rrf Jaccard 0.88）
  - **避免单独用 cascade**（命中率仅 30%）
  - `pool/keyword` 模式慢且命中差（2301ms / 0%），勿用
- **图谱多跳**：概念层（仓库→店铺→物流）适合 `/graph/traverse`，订单层适合按谓词查询。

## 3. 生命周期

- **衰减/分层**：每天跑 `maintenance.ps1 -Tasks decay,tiers`；decay 用 mock LLM 是安全的
  （摘要 + 归档 status 变更，可恢复）。
- **压缩**：`/memory/compress` 实测预算 2048 下原 1729→1369 token（约 -21%），
  适合长期 agent 的上下文治理；压缩前导出备份。
- **冲突**：同主题新旧事实并存时，用 `/memories/conflicts/resolve` 仲裁（建议 importance 优先）。

## 4. 运维

- **聚合池文件**：`aggregator_pool.json` 缺失会以空池启动 → 监控文件存在性
  （2026-08-14 曾因误判损坏被轮转为 .corrupt，实际是完整数据，已恢复）。
- **supervisor 自愈**：api/mcp/collector 每 5 分钟检查拉起；改 API 代码后
  `Stop-Process` 掉 8001 的进程再跑一遍 supervisor 即可热更新。
- **API 已知坑（2026-08-14 修复）**：
  - `GET /memories/{id}` 曾因 embedding BLOB 序列化 500 → 已剔除 BLOB
  - `/agents/memory/export` 曾因 `vars(dv)` 含 set 500 → 已改用 `to_dict(full=True)`
  - cross-modal 检索会加载视觉模型，模型缺失/大模型时可能长时间阻塞甚至拖垮进程 → 慎用

## 5. 多智能体（B3）

- 私有 agent 检索必须带自身 `agent_id` 过滤；viewer 角色禁写；
  共享类目（general/task/insight）单独配置，策略文件 `governance/policy.yaml`。

## 6. 合规（B5）

- 导出/删除/审计全链路可用（实测 8/8）；
- 生产部署补 TLS 与存储加密（本地仅内网）。

## 7. 部署

- 单机：`python gateway/server.py` 即得 OpenAI 兼容记忆服务（:8002）
- 全栈：`docker compose -f gateway/docker-compose.yml up -d`
- 迁移：见 `docs/MIGRATION_GUIDE.md`
