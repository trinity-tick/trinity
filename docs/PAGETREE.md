# MemoryPageTree — PageIndex 借鉴的页式记忆检索（2026-08-26）

Trinity 借鉴 [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex)（Vectorless,
Reasoning-based RAG）实现的主题页树检索层。**默认关闭，显式启用**（可回滚）。

## 机制

| PageIndex 概念 | Trinity 实现 |
|---|---|
| 树索引（目录树） | category → 簇(cluster) → 记忆 的主题页树，簇轴 = persona（非 default）→ 主标签 → untagged |
| 无 LLM 结构提取（Flash） | 建树纯元数据（category/tags/persona），零 LLM，8,992 条记忆约 75s |
| 节点摘要（便宜模型） | 维护链 `-Tasks pagetree` 增量生成（deepseek-chat，20 簇/轮） |
| LLM 树搜索（chat_model） | Phase 3 `mode="reason"`：LLM 相关重判（候选=关键词+页树，带活跃 goal 上下文） |
| 结果可溯源 | 结果附 page_path / page_title / page_node |

## 入口

- `mem.build_pagetree(exclude_categories=[...])` → `<store>/pagetree.json`
- `mem.search(q, page_tree=True, page_k=3)` — 页优先（先定位页、再读页内，基础召回兜底）
- `TRINITY_PAGETREE_HYBRID=on` + hybrid — 页树作为 RRF 通道，**只贡献基础召回未命中的
  记忆（novel_only）**，只增不减
- `mem.search(q, mode="reason")` — LLM 判相关（需 TRINITY_LLM_API_KEY/DEEPSEEK_API_KEY）
- 维护：`powershell -File dsh-ops/trinity-dsh-maintenance.ps1 -Tasks pagetree`（每日 all 链已含）

## 关键设计（含踩坑记录）

1. **存储加密**：建树脚本不得覆盖 `TRINITY_STORAGE_ENCRYPTION`（默认 on）——覆盖为 off
   会把 enc:v1 密文原样读入页树（untagged 簇摘要全是乱码，已修）。
2. **隔离**：pagetree_search 候选按 persona/agent/session/category 过滤（与 search 契约一致）。
3. **短查询守卫**：≤2 个内容词的查询直接走基础召回（页定位无区分度，实证有害）。
4. **小簇词表**：词频 ≥2 过滤会掏空 2-3 条记忆的小簇词表 → 按样本数自适应（≥6 才过滤）。
5. **IDF 页打分**：页词重叠按跨簇 IDF 加权（人名 df 高权重低），0.75 词重叠 + 0.25 基础命中率。
6. **tokenize 去重**：jieba+正则双通道会产生重复词，须去重。

## 实测（500q mock，deepseek-chat，top_k=10，2026-08-26 全量三臂）

| 臂 | R@5 | AnswerAcc | 说明 |
|---|---|---|---|
| 基线 keyword | 0.992 | 0.726 | 引擎默认路径（FTS） |
| 页优先 page_tree | 0.988 | 0.720 | 接近持平；MS +2 题独有增益 |
| hybrid rrf | 0.980 | - | 5 通道融合基线 |
| hybrid + 页树通道(novel_only) | **0.984** | - | 只增不减，MS +0.025 |
| reason（LLM 判题+goal 上下文） | 0.936 | **0.730** | TR AnswerAcc 0.688→**0.812**（+0.125）；MS R@5 塌陷（judge 过选） |

结论：
- mock 题与事实高度近串（keyword 已 98.4%+），页树纯模式 ≈ 持平；**hybrid+novel_only
  页通道是纯增益**（只增不减）。
- reason 模式实证了 PageIndex 的核心论点"相关需要推理"：TR（时序推理，相似度最失效的
  类目）AnswerAcc +0.125；代价是 MS 类（多事实变更题）judge 过选导致 R@5 塌陷——
  后续迭代方向：judge 提示词针对多事实题 + 候选集注入页内事实。
- **默认路径保持 FTS 不动**；页树/推理作为显式增强通道（默认关闭）。

## 二轮优化（2026-08-26，reason 修复 + holdout 实证）

**reason 判题两处根因修复**：①候选按 score 重排会把 FTS 命中的事实挤出 LLM 窗口
（改"基础召回优先 + 页/向量新增追加"）；②judge 过选（只选 2-4 条）→ **judge 只重排、
不截断**（不足 top_k 按基础序填充，召回 >= 关键词）。候选池注入 search_hybrid 向量命中
（窗口 30），页打分接入节点摘要词表（摘要用词不同，能接住近义改写）。

**全量 500q 终验（ae_500_reason_v3.json）**：R@5 0.994、AnswerAcc **0.752**（基线 0.726）；
MS R@5 0.60→0.963；TR +0.099、SS-P +0.134、SS-U +0.020。

**生产难查询 holdout**（output/hard_holdout.json，95 条近义改写，overlap<=40%）：

| 臂 | R@10 | 说明 |
|---|---|---|
| keyword | 0.432 | 近义改写 FTS 失效（mock 0.98 → 0.43） |
| pagetree | 0.179 | 摘要打分 +3pt（0.137→0.179） |
| **reason** | **0.547** | **+0.115 vs keyword**，8 例独中、0 漏检 |

→ **"相关需要推理"在生产难查询上实证**：reason（LLM 判题 + 语义候选）在近义改写查询
上显著优于 FTS；默认仍全关（显式启用）。

## 回滚

- 代码：`git checkout -- trinity/retrieval/pagetree.py trinity/core/client/_pagetree.py
  trinity/core/client/_search.py trinity/core/client/__init__.py trinity/adapters/sqlite/_crud.py
  trinity/retrieval/hybrid_retriever.py`
- 产物：删除 `~/.trinity/store/pagetree.json`（重新 `build_pagetree` 即重建）
- 维护链：从 `dsh-ops/trinity-dsh-maintenance.ps1` 的 `$allowed`/`all`/switch 移除 pagetree
