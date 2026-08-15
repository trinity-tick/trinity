# Trinity 存储加密（B5，2026-08-15）

> 可选 AES-256-GCM 落盘加密：保护 SQLite 中的敏感记忆正文，兼容 FTS5 检索、
> 去重哈希链与版本链。

## 1. 开关与密钥

```powershell
# 启用（写入路径生效）
$env:TRINITY_STORAGE_ENCRYPTION = "on"
# 密钥：二选一
$env:TRINITY_STORAGE_KEY = "<64 hex 字符 = 32 字节>"   # 显式密钥（推荐 CI）
# 或省略 —— 首次运行自动生成到 ~/.trinity/secrets/storage.key（权限 0600）
```

- 未设置开关 → 明文模式（默认，行为与历史版本完全一致）。
- 密钥缺失/非法 → 记录错误并**回退明文**（不阻断服务）。
- **密钥即数据**：更换/丢失密钥后历史密文无法解密（GCM 认证失败）。密钥轮换
  需全量 re-encrypt（当前未内置，见 §5 取舍）。

## 2. 加密范围

| 列/表 | 处理 |
|---|---|
| `memories.content` | **密文**（`enc:v1:<base64(nonce‖ct‖tag)>`） |
| `memory_versions.content` | **密文**（版本链同密） |
| `memories.tokenized_content` | **明文**（jieba 分词/FTS 索引源） |
| `sha256_hash` / `content_hash` | **基于明文计算**（去重/一致性链/身份保留不受影响） |
| `memories_fts`（FTS5 虚拟表） | 存 tokenized 明文 → 全文检索可用 |
| tags/category/metadata 等元数据 | 不加密（检索/过滤依赖） |

## 3. 检索与哈希兼容性

- **FTS5**：索引用 `COALESCE(tokenized_content, content)`。加密模式下非 CJK
  内容也写入明文 tokenized_content，避免触发器回退到密文导致检索失效。
- **去重**：`content_hash` 基于明文 → 相同内容无论是否加密都能去重。
- **一致性链**：`sha256_hash` 明文语义保持 → 身份保留/漂移检测不受影响。

## 4. 配套修复（本轮）

1. **FTS5 独立表迁移**：旧库 `memories_fts` 是 external content 表
   （`content='memories'`），SQLite 会忽略触发器写入值、直接索引
   `memories.content` —— 加密后索引密文导致检索整体失效。连接时自动检测并
   DROP 重建为独立表 + 回填（幂等，见 `SQLiteAdapter._create_fts5`）。
2. **CJK 查询分词修复**：`_tokenize_fts_query` 不再对 CJK 词"字间加空格"
   （unicode61 把连续 CJK 当单个 token，`"机 密 记 忆"` 永远匹配不到索引里的
   整词 token）。改用 jieba 词直接查询。**此修复同时改善了明文模式的中文检索**
   （此前 CJK 多字查询实际依赖 LIKE 兜底）。

## 5. 取舍与限制

- **tokenized_content 明文**：为保 FTS 可搜，分词结果不加密。极高敏部署可选：
  - 关闭 FTS（`search_memories` 自动回退 LIKE，但 LIKE 在密文上失效）→ 不可取；
  - 仅索引非敏感字段，或对 tokenized 再做同态/可搜索加密（超出当前范围，列入
    roadmap）。
- **密钥轮换**：当前需停机 + 全量解密重写；建议密钥固化（env/文件），
  运维流程见 docs/OPS_NOTES.md。
- **性能**：GCM 加解密开销 ~µs 级，批量写入无感知（benchmark 见 PERF_NOTES）。

## 6. 验证

```powershell
# 明文对照组（默认）
python scripts/storage_encryption_demo.py
# 加密组
$env:TRINITY_STORAGE_ENCRYPTION = "on"
python scripts/storage_encryption_demo.py
# 单元测试（29 例：加解密/开关/集成/迁移）
python -m pytest tests/unit/test_storage_encryption.py -q
```

两组均须 `RESULT: PASS ✅`：明文组验证内容原样落盘；加密组验证密文落盘 +
API 解密一致 + FTS 中英文检索命中 + 版本链/更新/批量读取路径解密。
