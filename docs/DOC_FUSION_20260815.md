# 方案/文档融合手册（2026-08-15）

> Trinity 能把**方案规划文档与实际文档**融合进记忆库，实现"文档即记忆、可检索、可溯源"。
> 脚本：`scripts/fuse_docs.py`（章节级切分 + 溯源 + 幂等 + 可选 LLM 图谱抽取）。

## 一、能融合什么

| 类型 | 示例文件 | 归类 category |
|---|---|---|
| 方案/规划 | PLANNING_REVIEW、FUTURE_ROADMAP_V3、OPTIMIZATION_DIRECTIONS | `doc:plan` |
| 汇总/概览 | TRINITY_SUMMARY、FEATURE_OVERVIEW、CAPABILITY_MAP | `doc:summary` |
| 运维/合规 | OPS_NOTES、PERF_NOTES、COMPLIANCE_GDPR、STORAGE_ENCRYPTION | `doc:ops` |
| 基准/对比 | BENCHMARKS、COMPARISON_VS_2026_SOTA | `doc:benchmark` |
| 协议/集成 | MEMORY_MARKET_PROTOCOL、MCP_STATUS、DSH_INTEGRATION | `doc:protocol` |
| 其他文档 | 任意 .md/.markdown | `doc:general` |

## 二、怎么融合

```powershell
# 默认融合 docs/*.md → 生产库（persona=trinity-docs, agent=doc-fusion）
python scripts/fuse_docs.py

# 预览（不写入）
python scripts/fuse_docs.py --dry-run

# 指定目录 / persona / 强制重写
python scripts/fuse_docs.py --dir D:/my-docs --persona my-team --force

# 开启 LLM 图谱抽取（写入时对每条建实体/关系，需 TRINITY_LLM_API_KEY）
$env:TRINITY_LLM_EXTRACT = "on"
python scripts/fuse_docs.py
```

## 三、融合后的效果（2026-08-15 实测）

- **已融合 382 章节**（42 个文件），分布：plan 42 / summary 95 / ops 34 / benchmark 42 / protocol 18 / general 151。
- **章节级切分**：按 `##`/`###` 标题切块，每条记忆带 source_uri（原文件路径）、section（标题）、line（行号）、doc_fingerprint（幂等指纹）。
- **幂等**：重复运行跳过已导入（指纹 `sha256(path|mtime|title)`），实测重跑 382 全跳过。
- **语义检索**：跨文档查询命中正确来源，例如：
  - "多智能体治理 B3 策略" → PLANNING_REVIEW 的 B3 段
  - "存储加密 AES" → STORAGE_ENCRYPTION 文档
  - "MCP v2 streamable" → MCP_STATUS 的 MCP v2 段
- **隔离**：独立 persona（trinity-docs）+ agent（doc-fusion），不污染默认记忆池；检索时 `persona_id="trinity-docs"` 即只搜文档。

## 四、配套：文档记忆的完整生命周期

1. **写入**：fuse_docs.py（批导入）或 file_harvester（增量采集）或 REST `POST /memories`（带 source_uri）。
2. **图谱**：`TRINITY_LLM_EXTRACT=on` 写路径 LLM 事实抽取（实体+关系谓词，对齐 Mem0/Zep）；
   或由 maintenance 的 MemoryAgent 后台统一提取。
3. **治理**：衰减/归档/去重自动适用（文档记忆参与 daily 链）。
4. **检索**：hybrid（BM25+向量+图谱）、跨模态、时点查询全可用。
5. **审计**：每条文档记忆带 SHA-256 + 版本链 + 审计日志（可溯源、可证明）。

## 五、已知注意事项

- **批量导入性能**：默认 `postprocess=False`（写入即时返回，避免逐条语义关联慢）；
  需要立即建图时加 `TRINITY_LLM_EXTRACT=on` 或由后台 MemoryAgent 统一处理。
- **控制台编码**：Windows GBK 控制台打印 ✅ 会崩，脚本已用 ASCII PASS/FAIL。
- **生产库并发**：融合是批量写，避开运行中 API 的写窗口（SQLite WAL 下短事务无碍；
  若遇 database is locked 等待重试即可，或停 API 后导入）。
