# -*- coding: utf-8 -*-
r"""④/⑤ 属**两条不同流水线** ⇒ 不得相减（B6/t90，队长裁定 ②(i)(ii)(iii)）

## 为什么要有这个文件（**防"假背离"**）

t90 只读实测（探针 `four_face_alignment_probe.py --limit 300`）：

```
④ fusion_roster (aggregator 的 ranked_lists 融合名册) = [graph_ppr, keyword, serendipity, vector]
⑤ supplying     (客户端 search_hybrid 的 breakdown)   = [bm25, graph, vector]
                                                        逐通道 {vector:300, bm25:189, graph:184}
roster ⊆ supplying = False
```

⚠️ **这个 False 不是缺陷，是"两条流水线"的正常结果**：
- `graph_ppr`（名册）↔ `graph`（运行期）= **同物不同名**（`CROSS_LABEL_ALIASES` 已登记，**需单独放行**才能统一）；
- `bm25` 在运行期供料但**不在** aggregator 名册里（它是**客户端** `_hybrid_index` 侧通道）；
- `keyword` 是 aggregator 的 seed（`ranked_lists = [kw_results]`），**本来就不会**出现在客户端的 breakdown 里；
- `serendipity` 最近 300 条 **0 次**（它的门控要求 `vec_ids` 非空 + `TRINITY_SERENDIPITY != off`）。

⇒ **把它们相减会得出"假背离"，并制造假红。** 本文件把这条路**封死**：
`cross_pipeline_difference()` **拒绝对两条不同流水线的面做差**，只允许同流水线内相减。
（与 B4 把"混算字段"封死是同一手法。）

## 边界

- 本文件**只读**、**不连库**：⑤ 的运行期读数留在探针里（需要 PG，属环境依赖），
  ⛔ **不把环境依赖写进 CI 判据** —— 否则会重演本轮一路在抓的"取数失败就静默"。
  这里只对**流水线归属**这一层做断言（纯逻辑，确定性）。
- `NON_CHANNEL_KEYS` **引用** `scripts/channel_census.py:53-57` 的口径（队长裁定 ③：不要两处各维护一份）。
"""
from __future__ import annotations

import ast
import io
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 两条流水线：**名字与成员都不是一回事**，禁止互相相减。
PIPELINES = {
    "aggregator_fusion": {
        "face_field": "capability_roster",
        "source": "DegradationManager().statistics()['capability_roster']",
        "criterion": "向 `aggregator/_search.py::_SearchMixin.query` 的 `ranked_lists` "
                     "供一个非空元素（seed 或 append）",
    },
    "client_hybrid": {
        "face_field": "breakdown",
        "source": "audit_log(action='search_hybrid').details['breakdown']（**只读**）",
        "criterion": "客户端 `search_hybrid` 响应里 breakdown 的非零、非元字段键",
    },
}

#: ⭐ 同物不同名：**已登记为一条真实的小不一致**（选手册裁定 ②(iii)）。
#: ⚠️ 统一名字会动 `FUSION_CHANNELS_WIRED` 的键及其引用与判据 ⇒ **需单独放行**；
#: 本文件只在**登记层面**钉住它，使其不再隐形。
CROSS_LABEL_ALIASES = {
    "graph_ppr": {
        "runtime_label": "graph",
        "pipelines": ("aggregator_fusion", "client_hybrid"),
        "status": "registered_pending_separate_release",
        "why": "aggregator 名册叫 graph_ppr，客户端 breakdown 叫 graph；同物不同名",
    },
}

#: ⭐ **不同流水线的正常现象** —— **不得**被当成背离报（选手册裁定 ②⚠️ 行的要求）。
#: 若不显式登记，将来会有人（或判据）把它们当缺口而制造假红。
NORMAL_CROSS_PIPELINE_PHENOMENA = {
    "serendipity": "名册成员；最近 300 条 search_hybrid 里 0 次（门控需 vec_ids 非空 + "
                   "TRINITY_SERENDIPITY != 'off'）⇒ 属正常，不是背离",
    "keyword": "aggregator 的 seed（ranked_lists 初值）；客户端的 breakdown 里**本来就不该**出现",
    "bm25": "客户端侧通道；**不在** aggregator 名册里 ⇒ 不是「名册漏登记」",
    "graph_ppr": "名册名；运行期叫 graph（见 CROSS_LABEL_ALIASES）⇒ 同一个东西的两个标签",
}


def cross_pipeline_difference(face_a: dict, face_b: dict) -> list:
    """**拒绝假背离**：只有当两个面**属同一流水线**时才允许相减；否则报违规。

    `face_a` / `face_b` 形如 `{"pipeline": "<key>", "members": [...]}`。
    返回违规说明列表（空 = 允许相减 / 本次没有"跨流水线相减"的行为）。
    """
    pa = (face_a or {}).get("pipeline")
    pb = (face_b or {}).get("pipeline")
    if pa not in PIPELINES or pb not in PIPELINES:
        return ["未登记的流水线：%r / %r ⇒ 先登记再比较（否则口径不可核）" % (pa, pb)]
    if pa != pb:
        return ["**跨流水线相减**（%s vs %s）⇒ 会得出假背离：两条流水线的名字与成员都不是一回事"
                % (pa, pb)]
    return []


def same_pipeline_difference(face_a: dict, face_b: dict) -> list:
    """同流水线内相减：给出差集（这是**合法**的操作）。"""
    bad = cross_pipeline_difference(face_a, face_b)
    if bad:
        raise ValueError("；".join(bad))
    a, b = set(face_a.get("members") or []), set(face_b.get("members") or [])
    return sorted(a - b)


def channel_census_non_channel_keys() -> set:
    """**引用**既有口径（队长裁定 ③）：从 `scripts/channel_census.py` 读取 `NON_CHANNEL_KEYS`。

    ⚠️ 用 AST 读字面量，**不 import 那个脚本**（它有 argparse/main 副作用），也不抄一份常量。
    """
    src = io.open(ROOT / "scripts" / "channel_census.py", encoding="utf-8-sig",
                  errors="replace").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "NON_CHANNEL_KEYS":
                    return set(ast.literal_eval(node.value))
    return set()


# ── 判据 ──────────────────────────────────────────────────────────────────

def test_两条流水线都已登记且互不相同() -> None:
    """④/⑤ 必须各带**定义与来源**地登记，且**不是同一条**流水线。"""
    assert set(PIPELINES) == {"aggregator_fusion", "client_hybrid"}
    for name, p in PIPELINES.items():
        assert p.get("source"), "流水线 %s 缺来源" % name
        assert p.get("criterion"), "流水线 %s 缺成员资格判据" % name
    assert (PIPELINES["aggregator_fusion"]["face_field"]
            != PIPELINES["client_hybrid"]["face_field"]), "两条流水线不得指向同一个字段"


def test_假背离被封死_跨流水线相减必须报违规() -> None:
    """⭐ 队长裁定 ②(ii)：把"假背离"这条路封死。"""
    agg = {"pipeline": "aggregator_fusion", "members": ["graph_ppr", "keyword", "serendipity", "vector"]}
    cli = {"pipeline": "client_hybrid", "members": ["bm25", "graph", "vector"]}

    bad = cross_pipeline_difference(agg, cli)
    assert bad and "跨流水线相减" in bad[0], "跨流水线相减没有被拒 ⇒ 假背离仍可被算出来"

    # 同流水线内相减是**合法**的（防"过宽 ⇒ 什么都禁"）
    assert cross_pipeline_difference(agg, dict(agg)) == []
    assert same_pipeline_difference(agg, {"pipeline": "aggregator_fusion",
                                         "members": ["vector"]}) == ["graph_ppr", "keyword", "serendipity"]
    # 同流水线相减**不得**被误禁
    assert sorted(set(cli["members"]) - set(agg["members"])) == ["bm25", "graph"]  # 仅示范差集本身


def test_负向实测_未登记的流水线不得参与比较() -> None:
    """未登记的流水线 ⇒ 报违规（不能靠"不认识的键"蒙混过去）。"""
    bad = cross_pipeline_difference({"pipeline": "who_is_this", "members": []},
                                    {"pipeline": "client_hybrid", "members": []})
    assert bad and "未登记的流水线" in bad[0]


def test_同物不同名已单独登记_pending单独放行() -> None:
    """⭐ 队长裁定 ②(iii)：`graph_ppr` ↔ `graph` 登记为一条**真实的小不一致**。"""
    assert "graph_ppr" in CROSS_LABEL_ALIASES
    e = CROSS_LABEL_ALIASES["graph_ppr"]
    assert e["runtime_label"] == "graph"
    assert e["status"] == "registered_pending_separate_release", \
        "该项未标注「需单独放行」⇒ 有人可能顺手改掉键名"
    assert "同物不同名" in e["why"]


def test_跨流水线的正常现象不得被当成背离报() -> None:
    """⭐ 队长裁定 ②⚠️：`serendipity` 0 次 / `keyword` 不出现在 ⑤ / `bm25` 不在名册
    —— 全部属**正常**；若判据把它们算成缺口 ⇒ 假红。"""
    for name in ("serendipity", "keyword", "bm25", "graph_ppr"):
        assert name in NORMAL_CROSS_PIPELINE_PHENOMENA, "%s 未登记为正常现象" % name
        assert NORMAL_CROSS_PIPELINE_PHENOMENA[name], "%s 的登记缺理由" % name

    # 反事实：如果把这些名字当成"缺口"，就应当被判为**不允许**（用本文件的封死函数表达）
    agg = {"pipeline": "aggregator_fusion", "members": ["keyword", "serendipity"]}
    cli = {"pipeline": "client_hybrid", "members": ["bm25"]}
    assert cross_pipeline_difference(agg, cli), \
        "把 keyword/serendipity 与 bm25 放在一起比较竟然被允许 ⇒ 假红入口没封住"


def test_NON_CHANNEL_KEYS_引用既有口径而非抄一份() -> None:
    """⭐ 队长裁定 ③：引用 `scripts/channel_census.py:53-57`，避免双份口径漂移。"""
    keys = channel_census_non_channel_keys()
    assert keys, "未能从 scripts/channel_census.py 读到 NON_CHANNEL_KEYS（口径源变了？）"
    assert {"unique_fused", "routing", "routing_requested"} <= keys, \
        "既有口径的关键元字段缺失：%s" % sorted(keys)
    # 元字段**不得**出现在能力名册里（同一类"把非通道当通道"的错）
    from trinity.agents.degradation import DegradationManager
    roster = set(DegradationManager().statistics().get("capability_roster") or [])
    assert not (roster & keys), "元字段被当成通道：%s" % sorted(roster & keys)


# ══════════════════════════════════════════════════════════════════════════
# ② 【t90 落地】`health_roster_alignment` 的登记必须与集合运算**逐字一致**
#
# 2026-10-07（t90）已在 `degradation.py::statistics()` 加了这个**纯报告**字段；
# ⛔ `_health` / 门控 / tier **一个字没动**（那是 t89 测出来的硬约束，队长已采纳）。
# ══════════════════════════════════════════════════════════════════════════

def alignment_violations(align: dict, avail, roster, noncap) -> list:
    """**登记面校验器**（纯函数 ⇒ 可注入篡改）：登记的两个方向必须与集合运算逐字一致。

      A1 `availability_face` **必须等于** `sorted(avail)`（登记不许自说自话）；
      A2 `wired_but_not_in_availability` **必须等于** `sorted(roster − avail)`
         ⇒ ⭐ **反向**：人为把一条真供料从登记里摘掉 ⇒ 必红；
      A3 `in_availability_but_not_wired` **必须等于** `sorted(avail ∩ noncap)`；
      A4 ⭐ **牙齿**：`capability_roster == availability_face`（把可用面抄进名册）⇒ 必红。
    """
    bad = []
    av, ro, nc = set(avail or []), set(roster or []), set(noncap or [])
    got_face = sorted(align.get("availability_face") or [])
    if got_face != sorted(av):
        bad.append("A1：availability_face=%s，而 active_channels=%s" % (got_face, sorted(av)))
    got_missing = sorted(align.get("wired_but_not_in_availability") or [])
    if got_missing != sorted(ro - av):
        bad.append("A2：wired_but_not_in_availability=%s，而 roster−avail=%s ⇒ 漏报/多报"
                   % (got_missing, sorted(ro - av)))
    got_extra = sorted(align.get("in_availability_but_not_wired") or [])
    if got_extra != sorted(av & nc):
        bad.append("A3：in_availability_but_not_wired=%s，而 avail∩noncap=%s"
                   % (got_extra, sorted(av & nc)))
    if ro and ro == av:
        bad.append("A4：capability_roster == availability_face ⇒ 把可用面当成了能力名册")
    return bad


def _live_alignment() -> tuple:
    from trinity.agents.degradation import DegradationManager
    st = DegradationManager().statistics()
    return (st.get("health_roster_alignment") or {}, st.get("active_channels") or [],
            st.get("capability_roster") or [], sorted((st.get("non_capability_names") or {})))


def test_health_roster_alignment_登记存在且与集合运算逐字一致() -> None:
    """② 落地：新字段存在，且两个方向**逐字等于**集合运算（不是手写死的字面量）。"""
    align, avail, roster, noncap = _live_alignment()
    assert align, "`statistics()` 里没有 health_roster_alignment ⇒ t90 的登记没落地"
    for k in ("availability_face", "wired_but_not_in_availability",
              "in_availability_but_not_wired", "scope", "criterion"):
        assert k in align, "登记缺字段 %s" % k
    bad = alignment_violations(align, avail, roster, noncap)
    assert bad == [], "登记与集合运算不一致：%s" % bad
    # 实测的两个方向（防有人把方向反过来理解）
    assert align["wired_but_not_in_availability"] == ["graph_ppr", "serendipity"]
    assert align["in_availability_but_not_wired"] == ["beamlight", "exabase", "second_brain"]


def test_负向实测_摘掉一条真供料必须红() -> None:
    """⭐ 队长裁定 ③ 的**反向**：从登记里摘掉一条真供料（`graph_ppr`）⇒ 必红。"""
    align, avail, roster, noncap = _live_alignment()
    tampered = dict(align)
    tampered["wired_but_not_in_availability"] = [
        ch for ch in align["wired_but_not_in_availability"] if ch != "graph_ppr"]
    bad = alignment_violations(tampered, avail, roster, noncap)
    assert any("A2" in b for b in bad), "摘掉一条真供料却没红：%s" % bad

    # 另一个方向：凭空**多加**一条也必红（防"只查少不查多"）
    tampered2 = dict(align)
    tampered2["in_availability_but_not_wired"] = sorted(
        list(align["in_availability_but_not_wired"]) + ["vector"])
    assert any("A3" in b for b in alignment_violations(tampered2, avail, roster, noncap)), \
        "凭空多加一条却没红"


def test_牙齿_把可用面抄进名册必须红() -> None:
    """⭐ ② 的牙齿（登记面口径）：`capability_roster := availability_face` ⇒ 必红。"""
    align, avail, roster, noncap = _live_alignment()
    bad = alignment_violations(align, avail, avail, noncap)   # 名册被抄成可用面
    assert any("A4" in b for b in bad), "把可用面抄进名册却没红：%s" % bad
    # availability_face 被改错也必红（防登记自说自话）
    wrong = dict(align, availability_face=["keyword"])
    assert any("A1" in b for b in alignment_violations(wrong, avail, roster, noncap))


def test_roster_scope必须写明两条流水线且禁止相减() -> None:
    """⭐ 队长裁定 ②(i)：④ 的 scope 文案必须写明 ④/⑤ 属**两条不同流水线**。

    ⚠️ 函数名里**不得**出现圈码数字（如 U+2463 的 ④）—— 它不是合法的 Python 标识符字符，
    会让整个文件 SyntaxError（本文件的作者在这里踩过一次：collect 报 1 error、
    棘轮从 109 变 116 并多出 `invalid-syntax×7`）。用「四个面」这类汉字代替。
    """
    from trinity.agents.degradation import DegradationManager
    st = DegradationManager().statistics()
    scope = st["capability_roster_scope"]
    for token in ("FUSION", "search_hybrid", "NOT be differenced", "FALSE divergence"):
        assert token in scope, "④ 的 scope 未写明 %r（两条流水线口径）" % token
    align = st["health_roster_alignment"]
    assert "禁用" in align["scope"], "登记面未写明『相减成单一数字属禁用』"
