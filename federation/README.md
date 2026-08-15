# Trinity 联邦记忆 (B4) — 多实例同步协议

> 目标：多台 Trinity 实例之间的记忆同步（离线优先、增量、冲突合并）。

## 协议设计（v0）

```
┌─────────────┐   export(agent_id)   ┌──────────────┐
│ Instance A  │ ───────────────────► │ snapshot.json │
└─────────────┘                      └──────┬───────┘
                                            │ bulk_write(entries)
                                            ▼
                                    ┌──────────────┐
                                    │ Instance B   │
                                    └──────────────┘
```

- **导出**：`GET /agents/memory/export?agent_id=...` → 记忆快照（JSON）
- **导入**：`POST /agents/memory/bulk_write` `{entries: [{content, tags, category, importance, ...}]}`
- **增量**：导出带上时间戳，导入方按 `created_at` 过滤（> last_sync_ts）
- **冲突**：同一内容在两侧都存在时，比较 `created_at`/`importance` 取新者
  （引擎侧 `memory_versions` 版本链提供追溯，`/memories/conflicts/resolve` 提供仲裁 API）

## 使用

```bash
# A 实例导出
python federation/sync_protocol.py export --api http://127.0.0.1:8001 --agent alpha --out snapshot.json
# B 实例导入
python federation/sync_protocol.py import --api http://127.0.0.1:8002 --file snapshot.json --since 2026-08-14T00:00:00
# 双实例对比
python federation/sync_protocol.py diff --file-a snapshot_a.json --file-b snapshot_b.json
```

## 离线优先

- 导出/导入均为纯文件操作，可在无网络环境完成
- 下次在线时重放 `sync.log` 即可补齐

## 已知边界

- v0 为"快照式"同步（全量导出+增量过滤），未实现双向增量日志协议（后续用 memory_versions 增量）
- 跨实例 id 不一致：导入端会生成新 memory_id（以 content hash 幂等去重可改进）
