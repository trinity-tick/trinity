#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力面台账（T6 能力面收口，2026-10-06）。

## 这个模块解决什么

仓库里"能力清单"（名字、数字、方法名）与"代码现实"长期存在四类脱钩，本模块把
它们写成**机器可核对的对照表**，并由
`tests/unit/test_capability_hygiene_20261006.py` 逐条重算现实后比对 —— 
**任何一格对不上都红**，两个方向都红：

  ① `SINGLE_DEFINITION_CLASSES` —— 必须**恰好一处**定义的能力类；
     多一处定义（幽灵/死副本）即红，少一处（文件被删）也红。
  ② `DECLARED_SHADOW_COPIES` —— **决定不收敛**的同名副本（有风险），
     必须写明理由与风险；未登记的同名副本由 ④ 的闭世界断言抓住。
  ③ `DECLARED_NOOP_METHODS` —— 名字暗示有逻辑、实体却是**单条常量 return**
     的方法。要么实现，要么登记为 declared-noop；**未登记的即红**（闭世界）。
  ④ `DOCUMENTED_DUPLICATES` —— second_brain 包内其余同名定义（经实测为**同形异义**，
     非副本）的完整清点；`{同名定义全部} == ② ∪ ④` 必须成立 ⇒ 不存在"默默多留一份"。
  ⑤ `ROSTER` —— 50 守卫 / 47 通道这类**能力数字**的「声明口径」与「实现口径」，
     与活体对象逐一对照（declared 是名册长度，enforcing/contributing 才是能力）。

## 为什么放在代码里而不是文档里

`CAPABILITY-HYGIENE.md`（产出目录）是人读的叙述；本模块是**判据的单一来源**。
只改注释不算收口 —— 本模块里的每一条都被测试重算，改注释不会让测试变绿。

（本模块自身只做静态分析，不 import 被测对象，避免循环依赖。）
"""
from __future__ import annotations

import ast
import os
import re

# ── 扫描根 ────────────────────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))      # D:\trinity-code
CAPABILITY_ROOT = os.path.join(REPO_ROOT, "trinity", "modules", "second_brain")

_SKIP_DIRS = {"__pycache__"}


def _rel(path: str) -> str:
    return os.path.relpath(path, REPO_ROOT).replace("\\", "/")


def iter_capability_py(root: str = CAPABILITY_ROOT):
    """按**确定性顺序**枚举 second_brain 下的 .py（排除 .bak / __pycache__）。"""
    out = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for f in sorted(files):
            if f.endswith(".py") and ".bak" not in f:
                out.append(os.path.join(cur, f))
    return out


# ── ① 必须恰好一处定义的能力类 ────────────────────────────────────────────

SINGLE_DEFINITION_CLASSES = {
    # 名字: 为什么它必须在能力面上"只此一份"
    "GuardianChainV50": (
        "运行副本在 engine_guardian_retrieval.py（engine_core → engine 门面 → "
        "SecondBrainV636.guardian_chain）。曾另有 guardian.py / guardian_retrieval.py "
        "两份同名副本，缺 enforcing_count() ⇒ loader.py::diagnostics() 必抛 AttributeError。"
        "T6 已把这两处改为显式转口。"
    ),
    "RetrievalSystemV47": (
        "运行副本在 engine_guardian_retrieval.py。曾另有 guardian_retrieval.py 一份，"
        "键名 ch01..ch47、无 CONTRIBUTES / contributing_count() / search()，"
        "且 scripts/channel_census.py 把它当「引擎注册面」的来源读。T6 已改为转口。"
    ),
    "AuditableRecallReceipt": (
        "confidence_scored_retrieval.py 曾在**同一文件内定义两次**：:165 dataclass 记录、"
        ":511 生成器。后定义者胜 ⇒ 生成器的 generate() 按 dataclass 字段名构造自己，"
        "必然 TypeError。T6 把生成器改名为 AuditableReceiptJournal，记录类恢复唯一。"
    ),
    # p1_preamble.py 的 16 个类：与 engine_core_types.py AST 完全相同、全仓 0 引用 ⇒ 转口
    "ContextAction": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "ExecutionGear": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "GovernanceState": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "CertificateStatus": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "MemoryErrorType": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "CacheWriteDecision": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "ConsolidationPhase": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "ContextObject": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "ContextCommit": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "MemoryHead": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "ProvenanceRecord": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "ContinuityState": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "SafetyAlarm": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "ExactKVEntry": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "ConsolidationRecord": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
    "ValueCategoryMapping": "engine_core_types.py 为唯一定义点（p1_preamble.py 已改为转口）。",
}


# ── ② 决定**不**收敛的同名副本（有风险，必须写明）──────────────────────────

DECLARED_SHADOW_COPIES = {
    "RelationalVersioning": {
        "runtime": "trinity/modules/second_brain/engine_data_pipeline.py",
        "shadow": "trinity/modules/second_brain/cb49_52.py",
        "facade_exported": True,
        "reason": (
            "cb49_52.py 是 CB49-CB52 的旧实现副本；运行副本在 engine_data_pipeline.py。"
            "本项与 scripts/consistency_stress_test.py 的依赖不冲突，但同一文件里的另外"
            "三个类与脚本**构造参数不兼容**，整包收敛会一次性打断脚本 ⇒ 按「同一份副本"
            "必须整体处置」处理，本类随整包暂不收敛。"
        ),
        "risk": (
            "两个类体当前接口子集相同（public API 集合一致），但**没有**任何判据保证它们"
            "继续同步；若有人只改运行副本，cb49_52 的消费者会拿到旧行为且无人报警。"
        ),
    },
    "ContextualChunkIngestion": {
        "runtime": "trinity/modules/second_brain/engine_data_pipeline.py",
        "shadow": "trinity/modules/second_brain/cb49_52.py",
        "facade_exported": True,
        "reason": "同上（cb49_52 整包）。",
        "risk": (
            "**构造签名不兼容**：运行副本 `(chunk_similarity_threshold=0.6, "
            "atomic_memories_per_chunk=5)`，影子副本 `(chunk_similarity_threshold=0.6, "
            "chunk_min_tokens=50)`。engine_core.py:360 用的是运行副本的参数名；"
            "任何 import 到影子副本的代码都会以 TypeError 或**语义不同的默认值**跑到两条不同的路径上。"
        ),
    },
    "ObserverReflector": {
        "runtime": "trinity/modules/second_brain/engine_observability.py",
        "shadow": "trinity/modules/second_brain/cb49_52.py",
        "facade_exported": True,
        "reason": "同上（cb49_52 整包）。",
        "risk": (
            "**构造签名完全不兼容**：运行副本 `(observer_token_threshold=800, "
            "reflector_token_threshold=3000)`，影子副本 `(observation_window=10, "
            "reflection_interval=30, token_estimate_factor=4.0)`。"
            "影子副本的 `diagnostics()` 还带 10 个 `... or True` 的**恒真能力自述**"
            "（cb49_52.py:443-448），读起来像「这 10 项都验过了」。"
        ),
    },
    "GroundTruthEpisodes": {
        "runtime": "trinity/modules/second_brain/engine_diagnostics.py",
        "shadow": "trinity/modules/second_brain/cb49_52.py",
        "facade_exported": True,
        "reason": (
            "**这是本项唯一有活体消费者的影子副本**：`scripts/consistency_stress_test.py:69`"
            "`from trinity.modules.second_brain.cb49_52 import GroundTruthEpisodes`，"
            "并在 :106 以**影子专属参数**构造 `GroundTruthEpisodes(short_term_capacity=20, "
            "max_episodes=500)`。scripts/ 不在 T6 写域内 ⇒ 收敛会打断一个我无权修改的脚本。"
        ),
        "risk": (
            "**真实发生的混淆**：脚本测的是影子实现，而引擎跑的是 engine_diagnostics 实现；"
            "构造签名不兼容（`short_term_capacity/max_episodes/max_episode_turns` vs "
            "`short_term_size/context_window_extension/retrieval_depth`）⇒ 脚本的准确率读数"
            "不能代表引擎能力。**未修原因**：修它需要同时改 scripts/（写域外），"
            "留作残留项 R2 交 captain 派工。"
        ),
    },
}


# ── ④ second_brain 内其余同名定义（实测为「同形异义」，非副本）─────────────

DOCUMENTED_DUPLICATES = {
    "CausalEdge": {
        "sites": ["trinity/modules/second_brain/causal_memory.py",
                  "trinity/modules/second_brain/causal_semantic_graph_memory.py"],
        "kind": "homonym",
        "disposition": "保留（两模块各自拥有，实体不同：ast 体长 1118/968，字段不一致）",
        "risk": "同名不同义；同时 import 两处的调用方可能取错类型。无已知消费者同时使用两者。",
    },
    "RepairStatus": {
        "sites": ["trinity/modules/second_brain/cascade_repair_engine.py",
                  "trinity/modules/second_brain/reflective_repair_memory.py"],
        "kind": "homonym",
        "disposition": "保留（一个是 str 常量枚举、一个用 enum.auto，语义与取值都不同）",
        "risk": "两个模块都被 self_healing.py import；同名枚举混用会在比较时静默为 False。",
    },
    "RiskLevel": {
        "sites": ["trinity/modules/second_brain/audit_trail.py",
                  "trinity/modules/second_brain/reflective_repair_memory.py"],
        "kind": "homonym",
        "disposition": "保留（成员集合不同；audit_trail 全仓 0 import，reflective_repair_memory 由 self_healing 使用）",
        "risk": "低。两侧取值域不同，跨模块比较会静默不相等。",
    },
    "SourceType": {
        "sites": ["trinity/modules/second_brain/audit_trail.py",
                  "trinity/modules/second_brain/confidence_scored_retrieval.py"],
        "kind": "homonym",
        "disposition": "保留（audit_trail 是字符串来源类型；confidence_scored_retrieval 是 1..5 权威性权重）",
        "risk": "**中度**：confidence_scored_retrieval.SourceType 是活体接口"
                "（trinity/core/client/_search.py 等 3 处 import），"
                "若有人误 import 到 audit_trail 的同名枚举，权重会变成字符串，",
    },
}


# ── ③a 恒真（`X or True`）能力自述：闭世界 ────────────────────────────────
#
# `len(self.chunks) > 0 or True` 这种写法**恒为 True**，却读起来像"这项检查通过"。
# 它比"恒假"更危险：恒假会让功能失效，恒真会让**读数**失效而功能照旧 ——
# 也就是本仓反复出现的"绿色但空"。
#
# 实测（2026-10-06）：second_brain 内共 7 处，**全部**在 cb49_52.py（影子副本）
# 的 `diagnostics()` 里：`CB50_*` 3 处（:309/:310/:313）、`CB51_*` 2 处（:443/:444）、
# `CB52_*` 2 处（:601/:602）。运行副本（engine_observability / engine_diagnostics）
# 的 `diagnostics()` 里一处也没有 —— 所以这条**不是**在给活体路径记账，
# 而是在披露影子副本的能力自述不可信。
DECLARED_TRUE_CLAIMS = {
    "trinity/modules/second_brain/cb49_52.py": {
        "count": 7,
        "lines": [309, 310, 313, 443, 444, 601, 602],
        "why": (
            "影子副本的 `diagnostics()` 用 `X or True` 上报 7 项能力，恒为 True ⇒ "
            "这些读数没有判别力。该文件零运行时引用（见 DECLARED_SHADOW_COPIES），"
            "故不构成活体假绿；登记在此是为了：① 披露；② 防止这套写法扩散到运行副本。"
        ),
    },
}

# ── ⑤a 名字说有、实质没有：**declared-lexical** 通道 ──────────────────────
#
# 这一类与 ③（恒假）**不同**：实现是真的，只是**名字指的东西和实际服务它的东西不是一回事**。
# 判据不是"方法返回常量"，而是"这个名字在默认配置下指向哪条实现"（可由披露字段派生）。
DECLARED_LEXICAL_CHANNELS = {
    "breakdown.vector": {
        "declared_name": "vector（向量通道）",
        "actual_impl_default": "lexical:SQLiteAdapter.search_memories（ts_rank + ILIKE 词法检索）",
        "switch": "TRINITY_VECTOR_CHANNEL",
        "default": "lexical",
        "evidence_file_line": [
            {"file": "trinity/core/client/_hybrid_index.py",
             "anchor": 'os.environ.get("TRINITY_VECTOR_CHANNEL")'},
            {"file": "trinity/core/client/_hybrid_index.py",
             "anchor": "return False"},
            {"file": "trinity/core/client/_hybrid_index.py",
             "anchor": 'lexical:%s.search_memories'},
        ],
        "disclosure_field": (
            "breakdown.vector_channel —— 由 `vector_channel_impl()` **派生**"
            "（_hybrid_index.py:97-127），接线点 _hybrid_search.py:908 / :1008 / :1328。"
            "读 `vector` 这个计数时**必须**同读该字段。"
        ),
        "repro": "D:\\\\DSH官网\\\\trinity-optimize-20261006\\\\vector_channel_repro.py → "
                 "evidence/vector_channel_repro.json（默认 env 未设时 "
                 "`_use_embedding_channel(sqlite, use_ann=True) is False`、"
                 "`vector_channel_impl(...) == 'lexical:SQLiteAdapter.search_memories'`）",
        "disposition": "declared-lexical（**登记命名口径**，不在此处改检索代码）",
        "why": (
            "**判定 = 登记 declared-lexical，不改名、不退役、本轮不修**，理由三条："
            "① 披露出口已经存在且是派生的（`breakdown.vector_channel`），"
            "缺的是**能力清单里的措辞**，不是机制；"
            "② 同文件 docstring :80-84 记录 §1388 端到端 A/B —— **接线改对之后 top-10 一点没变**"
            "（向量通道候选被下游融合完全压掉），按预注册纪律『任一判据不满足即记负结果、保持默认』"
            "⇒ 单独打开这个开关不构成修复，真正的约束在融合/排序阶段；"
            "③ 退役会丢掉已实现且可切换的嵌入路径（`auto`+PG、`pgvector` 两档实测落 embedding 分支）。"
        ),
        "owner": (
            "检索代码写权在 t3（trinity/core/client/**）。本项只负责"
            "『能力清单该怎么写』：凡引用 `vector` 计数的地方必须按 declared-lexical 措辞，"
            "并同读 `breakdown.vector_channel`。"
        ),
    },
}

# ── ⑤b 配额为 0 的能力：declared-off（有实测依据）vs 沉默的 no-op ────────
#
# 「配置里声明了一个能力、实际配额为 0」是这一类。**不能一律当缺陷**：
# 有实测依据的默认关闭是**决定**，没有依据的才是缺陷。区别在于能不能拿出证据。
DECLARED_ZERO_QUOTA = {
    "TRINITY_ATLAS_COLD_SLOTS": {
        # ── 登记结构：**文本锚**（2026-10-06 t65 改造）──────────────────────────
        # 旧结构是 `{file, line, contains}`，其中 `line` 被当成权威 ⇒ 每次有人在同一文件
        # 里插行（本轮 t9/t12/t23/t51 各插过）都会把登记推走，产生一条"应更新登记"的告警
        # （实测：1007→[1045]、1010→[1048]、1005→[1045,1054,1056]）。
        # **`line` 已删除**：现在只认 **`anchor`**（要在该文件的**代码行**里找得到的那段文本）。
        # 文本锚**不会因别人插行而漂移** ⇒ 登记不需要维护，告警也不会再刷屏。
        # 若某条确实想给人一个行号提示，可加 `line_hint`（**非权威**，只打印提示、不要求更新）。
        "file_line": [
            {"file": "trinity/engine_worker.py",
             "anchor": "_COLD_SLOTS_DEFAULT = \"0\""},
            {"file": "trinity/engine_worker.py",
             "anchor": "def _cold_slots_setting()"},
            {"file": "trinity/bridges/opening_surface.py",
             "anchor": "COLD_SLOTS_DEFAULT = 2"},
            {"file": "trinity/bridges/delivery_policy.py",
             "anchor": "def cold_slots_v2("},
        ],
        "declared": "冷槽位通道（cold_candidates / cold_slots）",
        "production_quota": 0,
        "not_a_noop": (
            "能力**已完整实现**：`opening_surface.py` 有槽位预留逻辑，"
            "`tests/unit/test_cold_slot_channel.py` 有 11 条判据；"
            "被归零的只是**默认配额**。"
        ),
        "evidence_of_intent": (
            "`_COLD_SLOTS_DEFAULT` 上方 :973-981 记录 §818 三档配对 A/B"
            "（LongMemEval oracle，n=96/96/60，同题集同 seed）：证据充分时冷条 **−7.3pp**"
            "（翻转 2 好 / 9 差，**有害**）、证据不足时 −2.1pp（无显著差异，白花 token）、"
            "零召回时无正收益证据 ⇒ 三种场景都没有被证明有用，其中一种还有害 ⇒ 默认关闭。"
            "证据文件 dsh-ops/evidence/{p818_injection_attribution.txt, p818_weak_recall.txt}。"
        ),
        "v2_lever": (
            "T9 已加回杠杆：`TRINITY_DELIVERY_V2=on` 时 `cold_slots_v2()` 把配额抬到"
            "`TRINITY_DELIVERY_COLD_SLOTS`（默认 1），接线在 engine_worker.py:1550-1551。"
        ),
        "disposition": "declared-off-by-measurement（**不是** declared-noop，也**不是**待修缺陷）",
        "why_not_fix": (
            "把默认值改回 2 等于**推翻有实测支撑的结论**，且 §818 已给出回滚开关；"
            "本轮不需要、也不应该改。"
        ),
        "correct_ledger_wording": (
            "「冷槽位通道：已实现；**生产默认 0 格**（§818 实测默认关闭）；"
            "`TRINITY_ATLAS_COLD_SLOTS=2` 可复现开启；`TRINITY_DELIVERY_V2=on` 时 ≥1 格。」"
            "**不得**写成「冷通道：开」。"
        ),
        "risk": (
            "能力清单若只写「冷通道：开」（`opening_surface.COLD_SLOTS_DEFAULT = 2` 就是这样一个"
            "会误导的函数级默认值 —— 生产永远取不到它），读者会把 2 当成产能。"
        ),
        "owner": "trinity/engine_worker.py 现归 corpus-quality（t9 刚改过）；本项不写它。",
    },
}

#: 判据扫描用：`X or True` 的形态
TRUE_CLAIM_RE = re.compile(r"\bor\s+True\b")

def true_claim_counts(sources: dict) -> dict:
    """{rel: 次数} —— **代码里**出现 `X or True`（恒真）的文件与次数。

    ⚠️ 必须走 AST 而不是正则：本模块自己的注释/字符串里就写着 `X or True`
    （留痕需要），正则版会把它数进来（首跑实测 6 次误报）——
    这正是本仓「判据只看文本会被自己的留痕骗」的老毛病。
    走 AST 只认真正的 `ast.BoolOp(Or, ..., Constant(True))`。
    """
    out = {}
    for rel, tree in parse_sources(sources):
        n = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                if any(isinstance(v, ast.Constant) and v.value is True
                       for v in node.values):
                    n += 1
        if n:
            out[rel] = n
    return out


# ── ③ 恒假 / 恒真：declared-noop 名单（闭世界）─────────────────────────────
DECLARED_NOOP_METHODS = {
    "trinity/modules/second_brain/engine_guardian_retrieval.py::RetrievalSystemV47.search": {
        "const": "[]",
        "why": (
            "V47 是**通道注册表**，不持有记忆数据源（同文件 CONTRIBUTES=False / "
            "DATA_SOURCE=None）。这里返回空列表是**与聚合器的契约**："
            "让 aggregator 调用 .search() 时不抛 AttributeError 而触发通道降级"
            "（trinity/agents/degradation.py 专门为此登记了 registry-only 原因）。"
        ),
        "why_not_implemented": (
            "本类没有任何数据源可查，'实现'只能等于**编造结果** —— 那正是本仓禁止的假绿。"
        ),
        "why_not_renamed_to_noop": (
            "**改名会改变真实行为**：degradation 的通道探针按 `.search` 这个名字探测，"
            "改名为 `noop_search` 会让探针落到「无此成员」分支 ⇒ V47 变成"
            "错误通道（而不是登记在案的 registry-only）。"
            "故保留名字，改用**登记 + 判据**（本表 + 测试）承担「这是 declared-noop」的披露义务。"
        ),
    },
    "trinity/modules/second_brain/cb49_52.py::ObserverReflector._extract_referenced_date": {
        "const": "None",
        "why": "影子副本（非运行时，见 DECLARED_SHADOW_COPIES）的私有辅助方法，未实现。",
        "why_not_renamed_to_noop": (
            "该文件是**零运行时引用**的影子副本，改名只会制造无谓 diff；"
            "真正的风险（同名不同体）已由 ② 登记承担。"
        ),
    },
    "trinity/modules/second_brain/cb49_52.py::ObserverReflector._extract_details": {
        "const": "[]",
        "why": "同上（影子副本私有辅助方法，未实现）。",
        "why_not_renamed_to_noop": "同上。",
    },
    "trinity/modules/second_brain/cb49_52.py::ObserverReflector._detect_preferences": {
        "const": "[]",
        "why": (
            "同上；额外注意：同文件的 `diagnostics()` 用 `\"CB51_preference_detection\": True` "
            "把这条恒假能力**写成 True 上报**（cb49_52.py:446），"
            "属于「恒假实现冒充能力」的直接样本，已在本表与报告中点名。"
        ),
        "why_not_renamed_to_noop": "同上。",
    },
}


# ── ⑤ 能力数字：声明口径 vs 实现口径 ───────────────────────────────────────

ROSTER = {
    "guardian_declared": {
        "value": 50,
        "live": "trinity/modules/second_brain/engine_guardian_retrieval.py::GuardianChainV50.total",
        "meaning": "名册长度（L1..L50 字符串名字，含论文出处）",
    },
    "guardian_enforcing": {
        "value": 0,
        "live": "trinity/modules/second_brain/engine_guardian_retrieval.py::GuardianChainV50.enforcing_count()",
        "meaning": "**真正带执行体**的守护层数（由 shields 的值类型推导）",
    },
    "channels_registered": {
        "value": 47,
        "live": "trinity/modules/second_brain/engine_guardian_retrieval.py::RetrievalSystemV47.total",
        "meaning": "注册名册长度（channel_1..channel_47）",
    },
    "channels_contributing": {
        "value": 0,
        "live": "trinity/modules/second_brain/engine_guardian_retrieval.py::RetrievalSystemV47.contributing_count()",
        "meaning": "**持有数据源**、能真正供料的通道数（由 channels 的值推导）",
    },
    "second_brain_modules": {
        "value": 22,
        "live": "trinity/modules/second_brain/engine_core.py::SecondBrainV636.total_modules",
        "meaning": "引擎真实注册模块数（**不是** 122 —— 那 100 个是 range() 占位名，已于 2026-09-30 移除）",
    },
    "second_brain_exports": {
        "value": 91,
        "live": "trinity/modules/second_brain/__init__.py::EXPORTED_COUNT（== len(__all__)）",
        "meaning": "包对外导出名数（**不是**旧文案里的 29）",
    },
}

#: 对外/对内材料里出现这些数字时，**必须**同时给出实现口径（本表就是那个"必须"的依据）
ROSTER_CAVEAT = (
    "50 / 47 是**申报名册数**，不是能力数：实测 guard_50 enforcing=0、channels_47 "
    "contributing=0。引用时必须注明口径（见 engine_core._collect_system_module_metrics "
    "的 *_declared / *_registered 与 *_enforcing / *_contributing 两组键）。"
)


# ── 静态分析（纯函数，便于用合成输入做负向实测）─────────────────────────────

#: 名字里出现这些词 ⇒ 作者**声称**这里有逻辑。
LOGIC_HINT = re.compile(
    r"(detect|verif|validat|check|audit|enforc|guard|scan|ensure|resolve|"
    r"merge|search|retriev|rank|score|filter|select|compute|calc|infer|"
    r"decide|route|apply|execute|run|process|build|load|ingest|consolidat|"
    r"repair|heal|summar|extract|evaluat|recommend|predict|generate|"
    r"expand|classif|normaliz|aggregat|fuse|dedup)",
    re.IGNORECASE,
)

#: 名字里出现这些词 ⇒ 返回常量是**合理的**（显式占位/默认值）。
NOOP_HINT = re.compile(
    r"(noop|no_op|placeholder|todo|stub|declared|unsupported|not_implemented|"
    r"empty|dummy|sentinel|default|fallback)",
    re.IGNORECASE,
)


def parse_sources(sources: dict) -> "list[ast.Module]":
    """把 {相对路径: 源码} 解析成 AST 列表（解析失败者跳过，不静默假装成功）。"""
    out = []
    for rel, src in sorted(sources.items()):
        try:
            out.append((rel, ast.parse(src)))
        except SyntaxError:
            continue
    return out


def class_definition_sites(sources: dict) -> dict:
    """{类名: [{file, line}, ...]} —— 同名类的**所有**定义点。"""
    out: dict = {}
    for rel, tree in parse_sources(sources):
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                out.setdefault(node.name, []).append({"file": rel, "line": node.lineno})
    return out


def duplicate_class_names(sources: dict) -> dict:
    return {k: v for k, v in class_definition_sites(sources).items() if len(v) > 1}


def _const_return(node: ast.AST):
    if isinstance(node, ast.Constant):
        return True, repr(node.value)
    if isinstance(node, ast.List) and not node.elts:
        return True, "[]"
    if isinstance(node, ast.Dict) and not node.keys:
        return True, "{}"
    if isinstance(node, ast.Set) and not node.elts:
        return True, "set()"
    if isinstance(node, ast.Tuple) and not node.elts:
        return True, "()"
    return False, ""


def _strip_docstring(body):
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def constant_return_methods(sources: dict) -> dict:
    """{`rel::Class.method`: 常量文本} —— 实体退化为单条常量 return 且**名字暗示有逻辑**。

    只认"函数体（去 docstring 后）恰好一条 `return <常量>`"，避免把正常分支误判。
    """
    out: dict = {}
    for rel, tree in parse_sources(sources):
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                body = _strip_docstring(fn.body)
                if len(body) != 1 or not isinstance(body[0], ast.Return):
                    continue
                is_const, text = _const_return(body[0].value)
                if not is_const:
                    continue
                if not LOGIC_HINT.search(fn.name) or NOOP_HINT.search(fn.name):
                    continue
                out["%s::%s.%s" % (rel, cls.name, fn.name)] = text
    return out


def load_capability_sources() -> dict:
    """读取 second_brain 下全部 .py 的源码（供上述纯函数使用）。"""
    out = {}
    for path in iter_capability_py():
        with open(path, encoding="utf-8", errors="replace") as fh:
            out[_rel(path)] = fh.read()
    return out


# ══════════════════════════════════════════════════════════════════════════════
# ⑥ `EFFECTIVENESS_FACTS` —— 「存在但不生效」要作为**能力事实**登记，不能只写"已收敛"
#
# 2026-10-06（t22）：verifier 的对抗性复核（`ADVERSARIAL-VERIFICATION.md` F5 + §0.3）指出，
# 我（t6）的"收敛"只解决了**身份**（同名类只剩 1 处定义、`same_object=true`），
# **没有**解决**效力**：`GuardianChainV50` 的 50 级守卫里 **0** 级在 enforcing、
# `RetrievalSystemV47` 的 47 个通道里 **0** 个在 contributing。
#
# 我在外部独立复算过（`t22_effectiveness_probe.py`，实例化真对象后数出来）：
#   · guardian ：`total=50`，`shields` 的**值类型直方图 = {'str': 50}` ⇒ enforcing_count()=0
#   · retrieval：`total=47`，`channels` 的值类型直方图 = {'str': 47} ⇒ contributing_count()=0
#   · 两个 `validate()` 都返回 True（因为 `DECLARED_NOOP` / `CONTRIBUTES is False` 已声明）
#   · `GuardianChainV50.get_new_shields()` 有 **17 条 dict** 值 —— 这是**唯一**一处带
#     "非字符串"的值，若被并进 `self.shields` 会让 enforcing 变成 17。
#     静态检索：**没有任何非注释引用**（见 `dead_registry_call_sites()`）⇒ 它是**死名册**。
#
# ⇒ **核心命题：数量声明与生效数量是两件事。** 触发这条登记的现实风险是：
#    有人把 `get_new_shields()` 并进名册，`enforcing_count()` 从 0 变 17，
#    **看起来"修好了"，但一个真执行体都没加** —— 因为 `enforcing_count()` 的定义只是
#    `not isinstance(v, str)`，一个带 name/paper/purpose 的**元数据 dict** 也会被算成执行体。
#    所以本表同时登记 `registry_value_types`（**钉住"为什么是 0"，而不只是"是 0"**）。
EFFECTIVENESS_FACTS = {
    "guardian": {
        "declared_key": "guardian_declared",
        "declared": 50,
        "effective_key": "guardian_enforcing",
        "effective": 0,
        "declared_live": "engine_guardian_retrieval.py::GuardianChainV50.total",
        "effective_live": "engine_guardian_retrieval.py::GuardianChainV50.enforcing_count()",
        "registry_attr": "shields",
        "registry_value_types": {"str": 50},
        "counting_method": (
            "`enforcing_count()` = `sum(1 for v in self.shields.values() if not isinstance(v, str))`；"
            "我另在外部独立重算同式并对照 self-reported 值，两者相等。"
        ),
        "why_zero": (
            "`self.shields` 是 `__init__` 里一次性写入的 50 个**字符串名字**（L1..L50，含论文出处），"
            "全仓**没有第二处赋值**（`self.shields` 只有 1 个赋值点）⇒ 0 是**构造性**结果，不是运行期状态。"
        ),
        "can_be_positive_when": (
            "仅当 `self.shields` 里出现**非 str** 的值。唯一现成来源是 `get_new_shields()`（17 条 dict）——"
            "但它当前**零调用点**；若把它并进去，`enforcing_count()` 会变 17 而**一个真执行体都没加**"
            "（dict 只是 name/paper/purpose 元数据）⇒ 那是**改指标不是改能力**。"
        ),
        "declared_noop_flag": "DECLARED_NOOP = True（类属性，已显式声明本类为 no-op）",
        "dead_richer_registry": "GuardianChainV50.get_new_shields()（17 条 dict，零调用点）",
    },
    "retrieval": {
        "declared_key": "channels_registered",
        "declared": 47,
        "effective_key": "channels_contributing",
        "effective": 0,
        "declared_live": "engine_guardian_retrieval.py::RetrievalSystemV47.total",
        "effective_live": "engine_guardian_retrieval.py::RetrievalSystemV47.contributing_count()",
        "registry_attr": "channels",
        "registry_value_types": {"str": 47},
        "counting_method": (
            "`contributing_count()` 只把「值不是 str 且有 `data_source`」的通道计入；"
            "我另在外部独立重算并对照，两者相等。"
        ),
        "why_zero": (
            "`self.channels` 是 `__init__` 里一次性写入的 47 个**字符串名字**（channel_1..channel_47），"
            "无第二处赋值、无 `data_source` ⇒ 0 是**构造性**结果。"
        ),
        "can_be_positive_when": (
            "仅当某个通道的值变成「带 `data_source` 的 dict/对象」。当前没有任何这样的通道。"
        ),
        "declared_noop_flag": "CONTRIBUTES = False / DATA_SOURCE = None（类属性，已显式声明仅注册）",
        "dead_richer_registry": None,
    },
}

#: 本表的对外说明（与 `ROSTER_CAVEAT` 配套：ROSTER 给数字，这里给**效力**与**不成效的原因**）
EFFECTIVENESS_CAVEAT = (
    "**数量声明与生效数量是两件事**：`50 / 47` 是申报名册长度，"
    "`enforcing / contributing` 才是能力。当前实测 **0/50** 与 **0/47**，"
    "且 0 是**构造性**的（名册全是字符串名字，零执行体、零数据源）。"
    "引用这两个数字时必须同时给出「声明」与「生效」两个口径；"
    "**不得**把「同名类已收敛成 1 处定义」读成「能力已恢复」。"
)


def effectiveness_registry_facts(sources: dict) -> dict:
    """**静态**量出名册的值类型（不 import 被测对象，避免循环依赖）。

    对 `self.<attr> = {...}` 这种一次性字面量赋值，返回其**值类型直方图**。
    这样"为什么是 0"（全是 str）也被钉住，而不只是"是 0"。
    """
    out: dict = {}
    wanted = {f["registry_attr"]: key for key, f in EFFECTIVENESS_FACTS.items()}
    for rel, tree in parse_sources(sources):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if not (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "self" and tgt.attr in wanted):
                    continue
                if not isinstance(node.value, ast.Dict):
                    out.setdefault(tgt.attr, {"non_literal": True})
                    continue
                hist: dict = {}
                for v in node.value.values:
                    hist[_runtimeish_type(v)] = hist.get(_runtimeish_type(v), 0) + 1
                out[tgt.attr] = {"file": rel, "line": node.lineno,
                                 "value_type_histogram": hist,
                                 "n": len(node.value.values),
                                 "capability": wanted[tgt.attr]}
    return out


def _runtimeish_type(node: ast.AST) -> str:
    """把 AST 节点名映射成**运行期类型名**，好与 `type(v).__name__` 直接对照。

    为什么需要：静态直方图若报 `Constant`，运行期报 `str`，两处就对不上了 ——
    而本表存在的意义正是"让声明与实测**可对照**"。
    """
    if isinstance(node, ast.Constant):
        return type(node.value).__name__
    if isinstance(node, ast.Dict):
        return "dict"
    if isinstance(node, ast.List):
        return "list"
    if isinstance(node, ast.Tuple):
        return "tuple"
    if isinstance(node, ast.Set):
        return "set"
    if isinstance(node, ast.Call):
        return "object"      # 构造出来的对象：**这才是真执行体的形态**
    if isinstance(node, ast.Name):
        return "name"
    return type(node).__name__


def dead_registry_call_sites(sources: dict) -> dict:
    """**"存在但不生效"的机械判据**：登记了却零调用点的名册/方法。

    只统计**真实引用**（AST 的 `ast.Name` / `ast.Attribute` 取值，或 `Call`），
    **不**统计注释与 docstring 里的字面提及 —— 否则判据会被自己的留痕骗
    （本仓已有前科：整份文本正则把讲解文字也数进去）。

    当前登记对象：`get_new_shields`（`GuardianChainV50` 的 17 条 dict 名册）。
    返回 {名字: {"definition_sites": int, "reference_sites": [{file, line}], "dead": bool}}。
    """
    targets = ("get_new_shields",)
    out = {name: {"definition_sites": 0, "reference_sites": [], "dead": True} for name in targets}
    for rel, tree in parse_sources(sources):
        # docstring 节点集合：AST 里字符串常量就是"文字提及"，**不算引用**
        doc_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                doc_nodes.add(id(node.value))
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    doc_nodes.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in targets:
                out[node.name]["definition_sites"] += 1
            if isinstance(node, ast.Attribute) and node.attr in targets:
                out[node.attr]["reference_sites"].append({"file": rel, "line": node.lineno})
            elif isinstance(node, ast.Name) and node.id in targets:
                out[node.id]["reference_sites"].append({"file": rel, "line": node.lineno})
    for name, rec in out.items():
        # "零引用"才算死；定义点本身不是引用
        rec["dead"] = len(rec["reference_sites"]) == 0
    return out
