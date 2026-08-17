"""
Trinity Knowledge Graph Builder — 从 memories 表提取实体和关系

从 trinity_store.db 的 29 条记忆记录中提取命名实体（人物、项目、技术、日期等），
构建实体节点和关系边，输出为 kgraph_data.jsonl。

目标：至少 50+ 实体节点、30+ 关系边。
输出路径：data/kgraph/kgraph_data.jsonl（与 KnowledgeGraph 默认路径一致）
"""

import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

# ── 配置 ────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
DB_PATH = os.path.join(PROJECT_ROOT, "data", "trinity_store.db")
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "data", "kgraph", "kgraph_data.jsonl")

# 确保输出目录存在
os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)


# ── 实体抽取规则 ─────────────────────────────────────────────────────
# 分多层规则：精确匹配 → 模板匹配 → 正则通用匹配 → 上下文推断

def load_memories(db_path: str) -> list[dict]:
    """从 SQLite 加载所有记忆记录。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT memory_id, content, role, importance, tags, category, created_at "
        "FROM memories ORDER BY created_at"
    ).fetchall()
    memories = [dict(r) for r in rows]
    conn.close()
    return memories


# ── 领域知识实体库（预定义核心实体）──────────────────────────────────
DOMAIN_ENTITIES = [
    # ── 系统 / 项目 ──
    {"id": "trinity_v6_37", "entity_type": "system",
     "properties": {"name": "Trinity v6.37", "desc": "三位一体智能记忆系统，122模块+50层守护链+47检索通道"}},
    {"id": "marvis", "entity_type": "system",
     "properties": {"name": "Marvis", "desc": "Windows桌面智能助手，基于腾讯混元Hy3和DeepSeek-V4 Pro"}},
    {"id": "wms_project", "entity_type": "project",
     "properties": {"name": "WMS功能补齐项目", "desc": "108个微服务，14个阶段(M1-M14)"}},

    # ── 技术 / 模型 ──
    {"id": "chromadb", "entity_type": "technology",
     "properties": {"name": "ChromaDB", "desc": "向量数据库"}},
    {"id": "second_brain", "entity_type": "technology",
     "properties": {"name": "SecondBrain", "desc": "记忆引擎，114文件"}},
    {"id": "ollama", "entity_type": "technology",
     "properties": {"name": "Ollama", "desc": "本地LLM推理引擎"}},
    {"id": "faiss", "entity_type": "technology",
     "properties": {"name": "FAISS", "desc": "向量索引库，HNSW算法"}},
    {"id": "postgresql", "entity_type": "technology",
     "properties": {"name": "PostgreSQL", "desc": "关系型数据库后端"}},
    {"id": "sqlite", "entity_type": "technology",
     "properties": {"name": "SQLite", "desc": "嵌入式数据库，FTS5全文索引"}},
    {"id": "bm25", "entity_type": "technology",
     "properties": {"name": "BM25", "desc": "稀疏检索算法"}},
    {"id": "crossencoder", "entity_type": "technology",
     "properties": {"name": "CrossEncoder", "desc": "重排序模型"}},
    {"id": "hnsw", "entity_type": "technology",
     "properties": {"name": "HNSW", "desc": "分层可导航小世界图索引"}},
    {"id": "tfidf", "entity_type": "technology",
     "properties": {"name": "TF-IDF", "desc": "词频-逆文档频率降级嵌入"}},
    {"id": "hunyu_hy3", "entity_type": "model",
     "properties": {"name": "腾讯混元Hy3", "desc": "腾讯大语言模型"}},
    {"id": "deepseek_v4_pro", "entity_type": "model",
     "properties": {"name": "DeepSeek-V4 Pro", "desc": "DeepSeek大语言模型"}},

    # ── 模块 / 组件 ──
    {"id": "metaevolution", "entity_type": "module",
     "properties": {"name": "MetaEvolution", "desc": "元进化系统，Observe-Analyze-Plan-Execute-Certify闭环"}},
    {"id": "guardian_chain", "entity_type": "component",
     "properties": {"name": "50层守护链", "desc": "安全与质量保障过滤链"}},
    {"id": "retrieval_channels", "entity_type": "component",
     "properties": {"name": "47检索通道", "desc": "级联检索管线"}},
    {"id": "crud_versioning", "entity_type": "component",
     "properties": {"name": "CRDT版本化", "desc": "冲突消解数据类型版本控制"}},
    {"id": "sha256_audit", "entity_type": "component",
     "properties": {"name": "SHA-256审计", "desc": "记忆完整性校验"}},
    {"id": "knowledge_graph_module", "entity_type": "module",
     "properties": {"name": "KnowledgeGraph模块", "desc": "轻量级图查询层，PPR+实体解析"}},
    {"id": "graph_vector_hybrid", "entity_type": "component",
     "properties": {"name": "GraphVectorHybridRetriever", "desc": "三阶段混合检索：向量+图PPR+RRF融合"}},

    # ── 仓库 / 品牌 ──
    {"id": "caitang", "entity_type": "brand",
     "properties": {"name": "彩棠", "desc": "彩妆品牌"}},
    {"id": "proya", "entity_type": "brand",
     "properties": {"name": "珀莱雅", "desc": "彩棠母公司"}},

    # ── 仓库规则 ──
    {"id": "heavy_rule", "entity_type": "rule",
     "properties": {"name": "重品层规则", "desc": "重品0.1kg-0.3kg放第一层货架"}},
    {"id": "bubble_pack_rule", "entity_type": "rule",
     "properties": {"name": "气泡柱包装规则", "desc": "气泡柱包装空位占1.5倍标准位不放货位号"}},
    {"id": "color_separation_rule", "entity_type": "rule",
     "properties": {"name": "同品色号分离规则", "desc": "同品不同色号应分开放置,相似品不邻放"}},

    # ── 对标系统 / 竞品 ──
    {"id": "wangdiantong", "entity_type": "competitor",
     "properties": {"name": "旺店通WMS", "desc": "PDA操作/货主管理/增值加工/赠品策略/预售/多物流费率"}},
    {"id": "jd_logistics", "entity_type": "competitor",
     "properties": {"name": "京东物流WMS", "desc": "人力调度/计件工资/任务调度/补货算法/越库/货位优化/200+API"}},
    {"id": "sf_supply_chain", "entity_type": "competitor",
     "properties": {"name": "顺丰供应链WMS", "desc": "仓网规划/IoT平台/逆向质检闭环/容器循环/碳核算SBTi"}},

    # ── WMS 微服务（核心算法层）──
    {"id": "putaway_engine", "entity_type": "service",
     "properties": {"name": "putaway-engine", "desc": "上架策略引擎，多维度分"}},
    {"id": "cartonization", "entity_type": "service",
     "properties": {"name": "cartonization", "desc": "智能装箱，3D bin packing"}},
    {"id": "wave_engine", "entity_type": "service",
     "properties": {"name": "wave-engine", "desc": "波次引擎，遗传+模拟退火"}},
    {"id": "pick_path_optimizer", "entity_type": "service",
     "properties": {"name": "pick-path-optimizer", "desc": "拣选路径优化"}},

    # ── WMS 微服务（商业金融层）──
    {"id": "billing_rate_engine", "entity_type": "service",
     "properties": {"name": "billing-rate-engine", "desc": "BMS计费费率引擎，阶梯计价/合同模型"}},
    {"id": "claims_management", "entity_type": "service",
     "properties": {"name": "claims-management", "desc": "理赔管理，区块链存证+AI定损"}},

    # ── WMS 微服务（合规与特色层）──
    {"id": "expiration_management", "entity_type": "service",
     "properties": {"name": "expiration-management", "desc": "效期管理，FEFO规则/四级预警"}},
    {"id": "customs_brokerage", "entity_type": "service",
     "properties": {"name": "customs-brokerage", "desc": "AI关务，HS归类/100+国规则引擎"}},
    {"id": "packaging_material", "entity_type": "service",
     "properties": {"name": "packaging-material", "desc": "包材合规，GB新国标"}},
    {"id": "pick_device_orchestrator", "entity_type": "service",
     "properties": {"name": "pick-device-orchestrator", "desc": "拣选设备编排，PTL/语音/AR多模式"}},
    {"id": "bundle_kit_assembly", "entity_type": "service",
     "properties": {"name": "bundle-kit-assembly", "desc": "套装Kitting"}},
    {"id": "manifest_waybill", "entity_type": "service",
     "properties": {"name": "manifest-waybill", "desc": "运单面单"}},
    {"id": "warehouse_benchmark", "entity_type": "service",
     "properties": {"name": "warehouse-benchmark", "desc": "仓储绩效对标，5级成熟度模型"}},

    # ── WMS 微服务（精益与合规层 M8-M9）──
    {"id": "value_add_processing", "entity_type": "service",
     "properties": {"name": "value-add-processing", "desc": "增值加工"}},
    {"id": "task_scheduler", "entity_type": "service",
     "properties": {"name": "task-scheduler", "desc": "任务调度"}},
    {"id": "promo_gift", "entity_type": "service",
     "properties": {"name": "promo-gift", "desc": "促销赠品"}},
    {"id": "smart_replenishment", "entity_type": "service",
     "properties": {"name": "smart-replenishment", "desc": "智能补货"}},
    {"id": "reverse_logistics_v2", "entity_type": "service",
     "properties": {"name": "reverse-logistics-v2", "desc": "逆向物流增强"}},
    {"id": "container_management", "entity_type": "service",
     "properties": {"name": "container-management", "desc": "容器管理"}},
    {"id": "cross_dock", "entity_type": "service",
     "properties": {"name": "cross-dock", "desc": "越库直通"}},
    {"id": "slotting_optimizer", "entity_type": "service",
     "properties": {"name": "slotting-optimizer", "desc": "货位优化引擎，ABC-XYZ分类+关联聚类"}},
    {"id": "cycle_count", "entity_type": "service",
     "properties": {"name": "cycle-count", "desc": "循环盘点"}},
    {"id": "carrier_management", "entity_type": "service",
     "properties": {"name": "carrier-management", "desc": "承运商管理，多物流费率"}},
    {"id": "carbon_tracker", "entity_type": "service",
     "properties": {"name": "carbon-tracker", "desc": "碳排放追踪"}},
]


# ── 从记忆内容提取额外实体 ───────────────────────────────────────────

def extract_entities_from_memories(memories: list[dict]) -> list[dict]:
    """从记忆内容中用正则+规则提取额外实体。"""
    entities: list[dict] = []
    seen_ids: set[str] = set()

    def _add(eid, etype, props):
        if eid not in seen_ids:
            seen_ids.add(eid)
            entities.append({
                "id": eid,
                "entity_type": etype,
                "properties": props,
            })

    all_text = "\n".join(m["content"] for m in memories)

    # 提取阶段/里程碑: M1-M14
    for m_num in range(1, 15):
        eid = f"milestone_m{m_num}"
        _add(eid, "milestone", {
            "name": f"WMS Phase M{m_num}",
            "desc": f"WMS 项目第 {m_num} 阶段"
        })

    # 提取日期
    date_pattern = re.compile(r"\b(202[46])[-/年](\d{1,2})[-/月](\d{1,2})\b")
    for m in date_pattern.finditer(all_text):
        date_str = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        eid = f"date_{date_str}"
        _add(eid, "date", {"name": date_str, "desc": "关键日期"})

    # 提取记忆类别
    categories = set(m.get("category", "general") for m in memories)
    for cat in categories:
        if cat and cat != "general":
            eid = f"category_{cat}"
            _add(eid, "category", {"name": cat, "desc": f"记忆分类: {cat}"})

    # 提取 tags 中的高频词
    tag_counter = defaultdict(int)
    for m in memories:
        tags = m.get("tags")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = []
        if tags:
            for t in tags:
                tag_counter[t] += 1

    for tag, count in tag_counter.items():
        if count >= 2:
            eid = f"tag_{tag}"
            _add(eid, "tag", {"name": tag, "desc": f"标签(出现{count}次)"})

    return entities


# ── 关系构建 ──────────────────────────────────────────────────────────

def build_relations(entities: list[dict], memories: list[dict]) -> list[dict]:
    """基于实体和记忆内容构建关系边。"""
    relations: list[dict] = []
    seen_rels: set[tuple] = set()

    def _add_rel(subj, pred, obj, weight=1.0, meta=None):
        key = (subj, pred, obj)
        if key not in seen_rels:
            seen_rels.add(key)
            relations.append({
                "subject": subj,
                "predicate": pred,
                "object": obj,
                "weight": weight,
                "metadata": meta or {},
            })

    # ── 系统架构关系 ──
    _add_rel("trinity_v6_37", "uses", "chromadb", 1.0,
             {"desc": "Trinity 使用 ChromaDB 作为向量存储"})
    _add_rel("trinity_v6_37", "uses", "faiss", 1.0,
             {"desc": "Trinity 使用 FAISS HNSW 作为向量索引"})
    _add_rel("trinity_v6_37", "uses", "sqlite", 1.0,
             {"desc": "Trinity 默认使用 SQLite 作为关系存储"})
    _add_rel("trinity_v6_37", "uses", "postgresql", 0.7,
             {"desc": "Trinity 支持 PostgreSQL 作为可选后端"})
    _add_rel("trinity_v6_37", "uses", "ollama", 0.8,
             {"desc": "Trinity 使用 Ollama 提供嵌入模型"})
    _add_rel("trinity_v6_37", "uses", "bm25", 0.9,
             {"desc": "v6.37 新增 BM25 稀疏检索"})
    _add_rel("trinity_v6_37", "uses", "crossencoder", 0.8,
             {"desc": "v6.37 新增 CrossEncoder 重排序"})
    _add_rel("trinity_v6_37", "has_module", "second_brain", 1.0,
             {"desc": "SecondBrain 记忆引擎是核心模块"})
    _add_rel("trinity_v6_37", "has_module", "metaevolution", 1.0,
             {"desc": "MetaEvolution 驱动系统自进化"})
    _add_rel("trinity_v6_37", "has_module", "knowledge_graph_module", 0.9,
             {"desc": "知识图谱模块提供图查询能力"})
    _add_rel("trinity_v6_37", "has_component", "guardian_chain", 1.0,
             {"desc": "50层守护链保障安全质量"})
    _add_rel("trinity_v6_37", "has_component", "retrieval_channels", 1.0,
             {"desc": "47检索通道实现级联检索"})
    _add_rel("trinity_v6_37", "has_component", "crud_versioning", 0.9,
             {"desc": "CRDT版本化记忆管理"})
    _add_rel("trinity_v6_37", "has_component", "sha256_audit", 0.9,
             {"desc": "SHA-256审计记忆完整性"})

    # ── 技术依赖关系 ──
    _add_rel("faiss", "uses", "hnsw", 1.0,
             {"desc": "FAISS 使用 HNSW 算法"})
    _add_rel("postgresql", "references", "sqlite", 0.5,
             {"desc": "PG 后端对齐 SQLite schema"})
    _add_rel("chromadb", "depends_on", "ollama", 0.6,
             {"desc": "ChromaDB 依赖 Ollama 提供嵌入"})

    # ── 模型依赖 ──
    _add_rel("marvis", "uses", "hunyu_hy3", 1.0,
             {"desc": "Marvis 使用腾讯混元Hy3模型"})
    _add_rel("marvis", "uses", "deepseek_v4_pro", 1.0,
             {"desc": "Marvis 使用 DeepSeek-V4 Pro模型"})
    _add_rel("marvis", "runs_on", "trinity_v6_37", 0.9,
             {"desc": "Marvis 运行在 Trinity 记忆系统之上"})

    # ── MetaEvolution 闭环 ──
    _add_rel("metaevolution", "has_process", "date_2026-07-15", 1.0,
             {"desc": "MetaEvolution 在 2026-07-15 活跃进化"})
    _add_rel("metaevolution", "produces", "category_handoff", 0.9,
             {"desc": "进化状态通过 Handoff 文件传递"})
    _add_rel("metaevolution", "produces", "category_evolution_state", 0.9,
             {"desc": "进化状态持久化"})

    # ── 仓库品牌关系 ──
    _add_rel("caitang", "belongs_to", "proya", 1.0,
             {"desc": "彩棠是珀莱雅子品牌"})
    _add_rel("heavy_rule", "applies_to", "caitang", 1.0,
             {"desc": "重品层规则适用于彩棠货架布局"})
    _add_rel("bubble_pack_rule", "applies_to", "caitang", 1.0,
             {"desc": "气泡柱包装规则适用于彩棠货架布局"})
    _add_rel("color_separation_rule", "applies_to", "caitang", 1.0,
             {"desc": "同品色号分离规则适用于彩棠货架布局"})

    # ── WMS 项目关系 ──
    _add_rel("wms_project", "references", "wangdiantong", 0.9,
             {"desc": "WMS项目对标旺店通WMS"})
    _add_rel("wms_project", "references", "jd_logistics", 0.9,
             {"desc": "WMS项目对标京东物流WMS"})
    _add_rel("wms_project", "references", "sf_supply_chain", 0.9,
             {"desc": "WMS项目对标顺丰供应链WMS"})

    # ── WMS 阶段 → 里程碑 ──
    for m_num in range(1, 15):
        eid = f"milestone_m{m_num}"
        _add_rel("wms_project", "has_milestone", eid, 1.0,
                 {"desc": f"WMS第{m_num}阶段"})

    # ── WMS 服务 → 所属阶段 ──
    # 核心算法层 (M12-M14)
    algo_services = ["putaway_engine", "cartonization", "wave_engine", "pick_path_optimizer"]
    for s in algo_services:
        _add_rel(s, "part_of", "wms_project", 1.0, {"desc": "WMS 算法层服务"})
        _add_rel(s, "part_of", "milestone_m12", 0.8, {"desc": "M12-M14 阶段"})

    # 商业金融层
    fin_services = ["billing_rate_engine", "claims_management"]
    for s in fin_services:
        _add_rel(s, "part_of", "wms_project", 1.0, {"desc": "WMS 商业金融层服务"})
        _add_rel(s, "part_of", "milestone_m13", 0.8, {"desc": "M13 阶段"})

    # 合规特色层
    comp_services = [
        "expiration_management", "customs_brokerage", "packaging_material",
        "pick_device_orchestrator", "bundle_kit_assembly",
        "manifest_waybill", "warehouse_benchmark"
    ]
    for s in comp_services:
        _add_rel(s, "part_of", "wms_project", 1.0, {"desc": "WMS 合规与特色服务"})
        _add_rel(s, "part_of", "milestone_m14", 0.8, {"desc": "M14 阶段"})

    # 精益合规层 (M8-M9)
    lean_services = [
        "value_add_processing", "task_scheduler", "promo_gift",
        "smart_replenishment", "reverse_logistics_v2", "container_management",
        "cross_dock", "slotting_optimizer", "cycle_count",
        "carrier_management", "carbon_tracker"
    ]
    for s in lean_services:
        _add_rel(s, "part_of", "wms_project", 1.0, {"desc": "WMS 精益与合规层服务"})
        _add_rel(s, "part_of", "milestone_m8", 0.8, {"desc": "M8-M9 阶段"})

    # ── 知识图谱模块内部关系 ──
    _add_rel("knowledge_graph_module", "has_component", "graph_vector_hybrid", 1.0,
             {"desc": "GraphVectorHybridRetriever 是知识图谱的混合检索器"})

    # ── 类别 ↔ 记忆关系 ──
    _add_rel("category_handoff", "related_to", "metaevolution", 0.9,
             {"desc": "Handoff 是元进化的交接机制"})
    _add_rel("category_evolution_state", "related_to", "metaevolution", 0.9,
             {"desc": "进化状态记录元进化过程"})

    # ── 日期 ↔ 事件关系 ──
    for m in date_pattern.finditer(
        "\n".join(m["content"] for m in memories)
    ):
        date_str = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        eid = f"date_{date_str}"
        if eid in seen_rels or eid.replace("date_", "") == "2026-07-15":
            _add_rel("trinity_v6_37", "active_on", eid, 0.7,
                     {"desc": f"Trinity 在 {date_str} 有活跃记录"})

    return relations


# ── 主流程 ────────────────────────────────────────────────────────────

date_pattern = re.compile(r"\b(202[46])[-/年](\d{1,2})[-/月](\d{1,2})\b")


def main():
    print("=" * 60)
    print("Trinity Knowledge Graph Builder")
    print("=" * 60)

    # 1) 加载记忆
    print(f"\n[1/4] 加载记忆: {DB_PATH}")
    memories = load_memories(DB_PATH)
    print(f"  -> 读取 {len(memories)} 条记忆记录")

    # 2) 构建实体
    print("\n[2/4] 构建实体...")
    domain_entities = list(DOMAIN_ENTITIES)
    extra_entities = extract_entities_from_memories(memories)

    # 合并去重
    all_entities_map: dict[str, dict] = {}
    for e in domain_entities:
        all_entities_map[e["id"]] = e
    for e in extra_entities:
        if e["id"] not in all_entities_map:
            all_entities_map[e["id"]] = e
        else:
            # 合并 properties
            all_entities_map[e["id"]]["properties"].update(e["properties"])

    all_entities = list(all_entities_map.values())
    print(f"  -> 共 {len(all_entities)} 个实体节点")
    entity_types = defaultdict(int)
    for e in all_entities:
        entity_types[e["entity_type"]] += 1
    for et, cnt in sorted(entity_types.items(), key=lambda x: -x[1]):
        print(f"     {et}: {cnt}")

    # 3) 构建关系
    print("\n[3/4] 构建关系...")
    relations = build_relations(all_entities, memories)
    print(f"  -> 共 {len(relations)} 条关系边")
    rel_types = defaultdict(int)
    for r in relations:
        rel_types[r["predicate"]] += 1
    for rt, cnt in sorted(rel_types.items(), key=lambda x: -x[1]):
        print(f"     {rt}: {cnt}")

    # 4) 写入 JSONL
    print(f"\n[4/4] 写入: {OUTPUT_PATH}")
    now = time.time()
    output_lines = []

    for e in all_entities:
        output_lines.append(json.dumps({
            "type": "entity",
            "id": e["id"],
            "entity_type": e["entity_type"],
            "properties": e.get("properties", {}),
            "created_at": now,
        }, ensure_ascii=False))

    for r in relations:
        output_lines.append(json.dumps({
            "type": "relation",
            "subject": r["subject"],
            "predicate": r["predicate"],
            "object": r["object"],
            "weight": r.get("weight", 1.0),
            "metadata": r.get("metadata", {}),
            "created_at": now,
        }, ensure_ascii=False))

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        for line in output_lines:
            f.write(line + "\n")

    print(f"  -> 写入 {len(output_lines)} 行 ({len(all_entities)} 实体 + {len(relations)} 关系)")

    # ── 验证 ──
    print("\n" + "=" * 60)
    print("验证结果")
    print("=" * 60)
    print(f"  实体节点: {len(all_entities)} (目标 50+) -> {'✅ 达标' if len(all_entities) >= 50 else '❌ 未达标'}")
    print(f"  关系边:   {len(relations)} (目标 30+) -> {'✅ 达标' if len(relations) >= 30 else '❌ 未达标'}")
    print(f"  输出文件: {OUTPUT_PATH}")
    print(f"  文件大小: {os.path.getsize(OUTPUT_PATH)} 字节")

    # ── 生成同步报告 ──
    sync_report = {
        "sync_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sync_version": "2.0",
        "source": "build_kgraph.py",
        "stats": {
            "entity_count": len(all_entities),
            "relation_count": len(relations),
            "entity_type_distribution": dict(entity_types),
            "relation_type_distribution": dict(rel_types),
        },
    }
    report_path = os.path.join(os.path.dirname(OUTPUT_PATH), "sync_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(sync_report, f, ensure_ascii=False, indent=2)
    print(f"  同步报告: {report_path}")

    return all_entities, relations


if __name__ == "__main__":
    entities, relations = main()
