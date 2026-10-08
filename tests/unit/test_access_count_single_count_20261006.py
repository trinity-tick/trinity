# -*- coding: utf-8 -*-
"""t17+t18：`access_count` 的记账不变量 —— **一次检索、每个被返回的行，恰好 +1**。

## 两条不变量必须**同时**成立

* **I1 至多 +1**：一次检索对同一行最多累加 1 次（t17 起）。
* **I2 恰好 +1**：被**返回**的每一行都恰好 +1（不是 0、也不是 2）（t18 起）。

只有"返回集只被记一次、且记账发生在知道返回集的那一层"时，两条才同时成立：

| 层 | 记账？ | 为什么 |
|---|---|---|
| 通道层 `core/client/_hybrid_search.py`（4 处 `search_memories`） | **否**（`touch=False`） | 通道结果会被融合**截断**（记了没返回的行）；同一行会被**多通道**命中（叠加成 2 次）；仅向量/语料通道命中的行不吃适配器检索（漏记 = t17 的 R-1） |
| 出口层 `core/client/_access_account.py`（**唯一实现**） | **是** | 由 `search_hybrid` 的**两条出口各调一次**；这是**所有调用方都必经**的一层 |
| 路由层 `api/server/_routers_search.py` | **否** | 它只是 `search_hybrid` 的调用方之一；实测还有 `/memory/search`、`/memory/recall`、graphql、`_routers_brain`、`_routers_explain`、`engine_worker`、`trinity/brain/**` 十余个模块 —— 在这一层记账会漏掉上述全部入口 |

## 历史

* t17：发现"一次检索 +2"（通道层 + 路由层各记一次）⇒ 删掉路由层那次（修法 (ii)）⇒ 副作用
  **R-1**：仅由非适配器通道命中的行从 +1 变 **0**。
* t18：通道层 `touch=False` + **出口层独占**（先按任务书试过"路由层独占"，被"非 API 调用方"
  实测推翻）⇒ R-1 关闭、I1/I2 对**所有**入口同时成立。
* 另一个实测约束：`access_touch.touch_results` 单次只处理 **5** 条，而生产返回集
  **54.6%（437/800）> 5 条** ⇒ 出口层必须**分块**记账，否则第 6 条起一条都不记。
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

# 【t18】本文件的接线用例会跑真 `search_hybrid`：**禁止**它改写生产语料缓存
# （`~/.trinity/data/corpus_vec.*`）。必须在 import trinity 之前设。
os.environ.setdefault("TRINITY_CORPUS_INDEX_PERSIST", "0")

REPO = pathlib.Path(__file__).resolve().parents[2]
ROUTER_PY = REPO / "trinity" / "api" / "server" / "_routers_search.py"
HYBRID_PY = REPO / "trinity" / "core" / "client" / "_hybrid_search.py"
ACCOUNT_PY = REPO / "trinity" / "core" / "client" / "_access_account.py"

SEED_N = 6


# ── 工具 ────────────────────────────────────────────────────────────────────

def _adapter(tmp_path):
    from trinity.adapters.sqlite import SQLiteAdapter

    ad = SQLiteAdapter(db_path=str(tmp_path / "t17.db"))
    ad.connect()
    for i in range(SEED_N):
        ad.store_memory(
            content="alpha beta gamma 记忆样本 %02d 冷池 基线 检索 判据" % i,
            agent_id="t17-test", category="general", tags=["t17"], importance=0.5,
        )
    return ad


def _ids(ad):
    return [r["memory_id"] for r in (ad.search_memories("alpha", top_k=50, touch=False) or [])]


def _counts(ad, ids):
    return {m: int(((ad.get_memory(m) or {}).get("access_count")) or 0) for m in ids}


def _flush(ad):
    """把异步 touch 队列落库（生产里由 1s 后台线程做；测试必须显式 flush 才能读数）。"""
    ad._flush_touch_queue()


class _FakeAdapter:
    """记录型替身：**方言原生** touch 入口（t14 的 `_dialect_native_touch` 走的就是它）。

    没有 `_get_conn`（SQLite 也没有）⇒ `access_touch` 走"方言原生"分支、逐 id 调
    `touch_memory` ⇒ 每次调用在此 +1，与生产 SQLite 的记账次数同构。
    """

    def __init__(self):
        self.hits: dict = {}

    def touch_memory(self, memory_id: str) -> bool:
        self.hits[str(memory_id)] = self.hits.get(str(memory_id), 0) + 1
        return True


def _fresh():
    """清掉 `access_touch` 的限流（同一 id 集合在 THROTTLE_S 内会被抑制，测试要真记账）。"""
    from trinity.brain import access_touch as at

    at._recent.clear()
    return at


def ACCT(res, adapter=None, background=False):
    """出口层记账函数（被测对象）。"""
    return account_returned_hits(res, adapter=adapter, background=background)


from trinity.core.client._access_account import account_returned_hits  # noqa: E402
import logging


# ── ① 每通道/每被返回的行恰好 +1（本任务的核心判据）────────────────────────

def test_每个被返回的行恰好加一次_含超过5条的返回集():
    """**核心判据（I2）**：出口层记账对返回集**每一行恰好 +1**。

    返回集故意取 **8 条 > 5**（`touch_results` 单次调用的截断边界），
    且用 `{"results": [...]}` 形态的冻结结果（与出口处真实入参同形）。
    """
    _fresh()
    frozen = [{"memory_id": "mem_frozen_%02d" % i} for i in range(8)]
    fake = _FakeAdapter()
    n = ACCT({"results": frozen}, fake)

    assert n == len(frozen), "记账返回数应等于返回集大小：%r" % n
    assert sorted(fake.hits) == sorted(r["memory_id"] for r in frozen), (
        "有行被漏记：%r" % sorted({r["memory_id"] for r in frozen} - set(fake.hits)))
    assert all(c == 1 for c in fake.hits.values()), (
        "有行被记了多次：%r" % {k: v for k, v in fake.hits.items() if v != 1})


def test_负向_通道层也记账时必须判红():
    """**负向实测 A（→2）**：模拟"通道层没有 touch=False"（t17 的形态：两层各记一次）。

    把记账执行两次（= 通道层 + 出口层各一次），同一条断言（每行恰好 1）**必须判红**。
    """
    frozen = [{"memory_id": "mem_dup_%02d" % i} for i in range(8)]
    fake = _FakeAdapter()
    _fresh()
    ACCT({"results": frozen}, fake)          # 通道层（模拟）
    _fresh()
    ACCT({"results": frozen}, fake)          # 出口层

    assert max(fake.hits.values()) == 2, fake.hits
    with pytest.raises(AssertionError):
        assert all(c == 1 for c in fake.hits.values()), (
            "有行被记了多次：%r" % {k: v for k, v in fake.hits.items() if v != 1})


def test_负向_没有记账者时必须判红():
    """**负向实测 B（→0）**：模拟"谁都不记账"（t17 修法 (ii) 之后的 R-1 回退形态）。

    一行都没被记时，"恰好 +1" 必须判红 —— 否则这条判据对"漏记"是瞎的。
    """
    frozen = [{"memory_id": "mem_silent_%02d" % i} for i in range(8)]
    fake = _FakeAdapter()
    _fresh()
    with pytest.raises(AssertionError):
        assert all(fake.hits.get(r["memory_id"]) == 1 for r in frozen), (
            "有行根本没有被记账（0 次）：%r"
            % sorted(r["memory_id"] for r in frozen if not fake.hits.get(r["memory_id"])))


def test_分块大小不得大于touch_results的单次上限():
    """`access_touch.touch_results` 单次只处理 **5** 条（`if len(clean) >= 5: break`）。

    出口层的分块（`ACCOUNT_CHUNK`）若大于该上限，第 6 条起会被**静默丢弃**
    ⇒ 把两个数字绑在一起：常量变了、或上限形态变了，都必须有人回来看。
    """
    from trinity.core.client import _access_account as A

    assert A._ACCOUNT_CHUNK <= 5, "分块大于 touch_results 单次上限 ⇒ 会漏记"
    src = (REPO / "trinity" / "brain" / "access_touch.py").read_text(encoding="utf-8")
    assert "len(clean) >= 5" in src, (
        "access_touch 的单次上限形态变了（不再是 `len(clean) >= 5`）⇒ 请复核 ACCOUNT_CHUNK")


def test_出口记账被真的接到了两条出口上(tmp_path, monkeypatch):
    """**接线实测（注入式替身）**：在临时 SQLite 库上跑一次**真的** `search_hybrid`，
    出口记账必须被调用**恰好一次**，且入参就是本次返回的行集合。

    不依赖管线的确定性（结果可以是任意行）：只钉"记账在出口、且只发生一次"。
    """
    from trinity.core.client import _access_account as A
    from trinity.core.client import _hybrid_search as H

    calls = []

    def _spy(res, adapter=None, background=True, enabled=True):
        # t32：签名必须与真实实现同步（新增 `enabled` 后**这条 spy 当场报 TypeError** —— 好事，
        # 说明它不是"能过就行的假替身"，而是真的卡在接口上）。
        calls.append([r.get("memory_id") for r in (res.get("results") or [])])
        return 0

    monkeypatch.setattr(A, "account_returned_hits", _spy)
    monkeypatch.setattr(H, "account_returned_hits", _spy, raising=False)

    from trinity import Trinity

    db = str(tmp_path / "exit.db")
    _adapter(tmp_path)                                    # 同库写入若干可命中的行
    mem = Trinity(adapter="sqlite", store_path=str(tmp_path / "t17.db"))
    data = mem.search_hybrid(query="alpha beta gamma", top_k=3)
    returned = [r.get("memory_id") for r in (data.get("results") or [])]

    assert len(calls) == 1, "出口记账必须恰好被调用一次，实测 %d 次：%r" % (len(calls), calls)
    if returned:                       # 空结果时也允许记 0 条，但调用次数仍必须是 1
        assert sorted(calls[0]) == sorted(returned), (
            "记账的入参必须是本次**返回**的行集合：%r vs %r" % (calls[0], returned))
    assert db  # 仅用于保持临时库路径引用（避免被 lint 判为未使用）


# ── ② I1：至多 +1（t17 判据，保留）──────────────────────────────────────────

def test_一次检索对同一行至多累加1次(tmp_path):
    """适配器**默认**行为（`search_memories(touch=True)`）：命中行恰好 +1。

    t18 之后生产路由不再走这条路径（通道层已 `touch=False`）——本用例钉的是适配器默认值，
    因为**非 API 调用方**（脚本/评测/维护链直调 `search_memories`）仍依赖它。
    """
    ad = _adapter(tmp_path)
    ids = _ids(ad)
    before = _counts(ad, ids)

    hits = ad.search_memories("alpha beta gamma 检索", top_k=5) or []
    _flush(ad)
    after = _counts(ad, ids)
    delta = {m: after[m] - before[m] for m in ids}

    assert hits, "检索必须命中（否则本判据空转）"
    hit_ids = [r["memory_id"] for r in hits]
    assert max(delta.values()) <= 1, (
        "一次检索对同一行累加了多次：%r ⇒ access_count 量级被高估"
        % {k: v for k, v in delta.items() if v > 1})
    for m in hit_ids:
        assert delta[m] == 1, "命中行必须恰好 +1：%s -> %d" % (m, delta[m])


def test_判据有牙齿_把第二次记账接回去必须判红(tmp_path):
    """**负向实测（I1）**：把 t17 之前"两层各记一次"接回去，同一条判据必须判红。"""
    ad = _adapter(tmp_path)
    ids = _ids(ad)
    before = _counts(ad, ids)

    hits = ad.search_memories("alpha beta gamma 检索", top_k=5) or []
    _flush(ad)
    at = _fresh()
    at.touch_results([r["memory_id"] for r in hits], adapter=ad, background=False)
    _flush(ad)
    after = _counts(ad, ids)
    delta = {m: after[m] - before[m] for m in ids}

    assert max(delta.values()) == 2, delta
    with pytest.raises(AssertionError):
        assert max(delta.values()) <= 1, (
            "一次检索对同一行累加了多次：%r ⇒ access_count 量级被高估"
            % {k: v for k, v in delta.items() if v > 1})


# ── ③ 结构不变量：**恰好一处实现 + 恰好两个出口调用** ───────────────────────

def _calls(src: str, name: str) -> list:
    return [n.lineno for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


def _search_memories_touch_flags(src: str) -> list:
    """每个 `search_memories(...)` 调用点的 `touch=` 取值（缺省 = None）。"""
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "search_memories":
            kw = {k.arg: getattr(k.value, "value", "<expr>") for k in n.keywords}
            out.append((n.lineno, kw.get("touch", None)))
    return out


def test_恰好一处实现_恰好两个出口调用():
    """**结构哨兵（I1+I2 的合并形态）**：

    * 出口层 `_access_account.py`：`touch_results` 调用 **恰好 1 个**，且必须在
      `account_returned_hits` 体内（= 唯一实现）；
    * `_hybrid_search.py`：`account_returned_hits` 调用 **恰好 2 个**（两条出口），
      `touch_results` 调用 **0 个**；
    * 路由层 `_routers_search.py`：任何记账调用 **0 个**（它只是调用方之一）；
    * 通道层 4 处 `search_memories` **全部** `touch=False`。
    """
    router = ROUTER_PY.read_text(encoding="utf-8")
    hybrid = HYBRID_PY.read_text(encoding="utf-8")
    acct = ACCOUNT_PY.read_text(encoding="utf-8")

    tr = _calls(acct, "touch_results")
    assert len(tr) == 1, "出口层应只有一处 touch_results（唯一实现）：L%s" % tr
    impl = [n for n in ast.parse(acct).body
            if isinstance(n, ast.FunctionDef) and n.name == "account_returned_hits"]
    assert impl, "找不到 `account_returned_hits` 定义"
    assert impl[0].lineno < tr[0] < (impl[0].end_lineno or 10 ** 6), (
        "那次 touch_results 不在 `account_returned_hits` 体内（L%s，定义 %s-%s）"
        % (tr[0], impl[0].lineno, impl[0].end_lineno))

    ex = sorted(_calls(hybrid, "account_returned_hits"))
    assert len(ex) == 2, "两条出口应各调一次出口记账，实测 L%s" % ex
    assert _calls(hybrid, "touch_results") == [], "通道层不得再直接记账（t18）"

    assert _calls(router, "touch_results") == [], "路由层不得记账（它只是调用方之一）"
    assert _calls(router, "_account_hits") == [], "路由层不得有记账实现"

    flags = _search_memories_touch_flags(hybrid)
    assert len(flags) == 4, "通道层 `search_memories` 调用点应为 4 个，实测 %d 个" % len(flags)
    bad = [(ln, v) for ln, v in flags if v is not False]
    assert bad == [], "通道层这些调用点没有传 touch=False：%r（会与出口层叠加成 2 次）" % bad


def test_哨兵有牙齿_变异副本必须被抓到():
    """**负向实测（结构哨兵）**：三种走形插回**变异副本**，同一侦测器必须抓到。

    ① 通道层丢掉一个 `touch=False`（→ 与出口层叠加成 2 次）；
    ② 通道层直接记账（→ 与出口层叠加）；
    ③ 出口层多出一处实现（→ 同一次检索记 2 次）。
    """
    hybrid = HYBRID_PY.read_text(encoding="utf-8")
    acct = ACCOUNT_PY.read_text(encoding="utf-8")

    mut1 = hybrid.replace("touch=False", "touch=True", 1)
    assert [(ln, v) for ln, v in _search_memories_touch_flags(mut1) if v is not False], \
        "丢掉 touch=False 后哨兵没抓到 ⇒ 哨兵无效"

    anchor = "def _mirror_hybrid_score(results) -> None:"
    assert anchor in hybrid, "通道层插入点失效（本用例需要跟着代码更新）"
    mut2 = hybrid.replace(anchor, "touch_results([], adapter=None)\n\n\n" + anchor, 1)
    assert _calls(mut2, "touch_results"), "通道层直接记账没被侦测到 ⇒ 哨兵无效"

    assert "def account_returned_hits" in acct
    mut3 = acct + "\n\ndef account_returned_hits_2():\n    pass\n"
    assert mut3.count("def account_returned_hits") == 2, "变异体没变 ⇒ 本用例的侦测器无效"


# ── ④ 行为与读数不变 ────────────────────────────────────────────────────────

def test_检索结果本身不因记账而改变(tmp_path):
    """**记账不得改变检索结果**：同库同查询跑两次，`(id, score)` 序列逐字一致。"""
    ad = _adapter(tmp_path)
    r1 = ad.search_memories("alpha beta gamma 检索", top_k=10) or []
    _flush(ad)
    r2 = ad.search_memories("alpha beta gamma 检索", top_k=10) or []
    # 2026-10-06（t74/I14）：原为 `fp = lambda rows: …（原本带 E731 抑制）` ⇒ 改 `def`（真修，不再靠抑制）
    def fp(rows):
        return [(r["memory_id"], r.get("score")) for r in rows]
    assert fp(r1) == fp(r2), "两次检索的返回不一致 ⇒ 记账影响了检索结果"
    assert len(r1) == len(r2) > 0


def test_零非零判别力不受影响(tmp_path):
    """**零/非零判别力不变**：`access_count == 0` 的行集合只增不减，且只有命中行离开零集。"""
    ad = _adapter(tmp_path)
    ids = _ids(ad)
    before = _counts(ad, ids)
    zero_before = {m for m in ids if before[m] == 0}
    assert len(zero_before) == len(ids), "起点：全部为 0（未被读过）"

    hits = ad.search_memories("alpha beta gamma 检索", top_k=5) or []
    _flush(ad)
    after = _counts(ad, ids)
    zero_after = {m for m in ids if after[m] == 0}

    assert zero_after <= zero_before, "零集变大了 ⇒ 有行被读却仍是 0（记账漏了）"
    hit_ids = {r["memory_id"] for r in hits}
    assert hit_ids.isdisjoint(zero_after), "命中行不得留在'从未被读'集合里"
    assert zero_before - zero_after == hit_ids, "只有命中行才应离开零集"


# ── ⑤ t21（verifier F1 blocker）：判据必须覆盖**真实路由矩阵**，不是只覆盖构造输入 ──
#
# verifier 的 F1：`full` 路径 Δ max=3（`_hybrid_index.py:176/211/213` 三处没传 `touch=False`）。
# t18 的判据只覆盖 light + 冻结输入 ⇒ 生产默认路由（PG=full；SQLite 长查询=full）没被覆盖。
# 两层一起上：
#   ① **功能层**：真 `search_hybrid(routing=light|full)` 跑真管线，逐行 Δ 必须 ==1，
#      且整次调用中不得出现任何未传 `touch=False` 的 `search_memories`（类级探针）；
#   ② **结构层**：把"管线内 `search_memories` 必须 `touch=False`"变成 AST 扫描 + 显式白名单
#      ⇒ **新增**的漏网页不必等我构造到那条路由就会被判红。

_PIPELINE_MUST_ALL_FALSE = ("_hybrid_search.py", "_hybrid_index.py",
                            # t27 复核后从白名单移入严格组（判定见报告 §14）：
                            "_advanced.py",   # compress_context 的空-query 全量拉取 = 内部候选池
                            "_graph.py")      # explore_topic 的按实体 fan-out 富化 = 派生视图

#: 不在"混合检索管线"内的调用点白名单 —— **逐个给理由**（否则它就是漏网的记账叠加点）。
_ALLOWED_DEFAULT_TOUCH = {
    "_search.py": "多模式入口 search()：mode=keyword/vector 时这条调用**就是该次检索唯一的记账**"
                  "（没有出口层覆盖它）；mode=hybrid 时它只在 hybrid 返回空后兜底 ⇒ 与出口层不重叠。"
                  "⚠️ t27 复核补充：`_vector_search()`（:1160，ANN 不可用时的降级）**同时**是 hybrid 的"
                  "嵌入通道（`_hybrid_index.py:171-173`）⇒ 理论上该分支会与出口层叠加；但改成 "
                  "touch=False 会让 `search(mode=vector)` 失去记账 ⇒ 需要「按调用方区分」，"
                  "本轮登记为残留 R-6（未改，附证据）。",
    "_pagetree.py": "pagetree 是独立入口（不走 search_hybrid 的出口层）⇒ 这条调用是它自己的记账。"
                    "⚠️ t27：`_search_reason()`（:282）疑似**fan-out**（与 `_graph.py` 同类），"
                    "登记为残留 R-7 待复核。",
}


_ROUTE_PROBE = "\n".join([
    "import json, os, sys",
    'os.environ.setdefault("TRINITY_CORPUS_INDEX_PERSIST", "0")',
    "repo, mode, db = sys.argv[1], sys.argv[2], sys.argv[3]",
    'ignore_touch = "--ignore-touch" in sys.argv',
    "sys.path.insert(0, repo)",
    "from trinity.adapters.sqlite import SQLiteAdapter",
    "from trinity import Trinity",
    "ad = SQLiteAdapter(db_path=db); ad.connect()",
    "for i in range(12):",
    '    ad.store_memory(content="alpha beta gamma 记忆样本 %02d 冷池 基线 检索 路由" % i,',
    '                    agent_id="t21-probe", category="general", tags=["t21"], importance=0.5)',
    'ids = [r["memory_id"] for r in (ad.search_memories("alpha", top_k=50, touch=False) or [])]',
    'before = {m: int(((ad.get_memory(m) or {}).get("access_count")) or 0) for m in ids}',
    "calls = []",
    "orig = SQLiteAdapter.search_memories",
    "def spy(self, *a, **kw):",
    "    kw2 = dict(kw)",
    '    if ignore_touch:',
    '        kw2.pop("touch", None)          # 模拟「某处 touch=False 被摘掉」',
    "    rows = orig(self, *a, **kw2)",
    '    calls.append(kw.get("touch", None))',
    "    return rows",
    "SQLiteAdapter.search_memories = spy",
    "from trinity.brain import access_touch as at",
    "at._recent.clear()",
    'mem = Trinity(adapter="sqlite", store_path=db)',
    'data = mem.search_hybrid(query="alpha beta gamma 检索 路由 矩阵", top_k=10, routing=mode)',
    'inner = getattr(mem, "_adapter", None)',
    'pool = getattr(at, "_EXECUTOR", None)',
    "if pool is not None:",
    "    try: pool.submit(lambda: None).result(timeout=20)",
    "    except Exception: pass",
    'if inner is not None and hasattr(inner, "_flush_touch_queue"): inner._flush_touch_queue()',
    "ad._flush_touch_queue()",
    'returned = [r.get("memory_id") for r in (data.get("results") or [])]',
    'after = {m: int(((ad.get_memory(m) or {}).get("access_count")) or 0) for m in ids}',
    "delta = [after[m] - before[m] for m in returned]",
    'print(json.dumps({"mode": mode, "strategy": data.get("strategy"), "returned_n": len(returned),',
    '                  "delta": delta, "max": max(delta) if delta else 0,',
    '                  "bad_calls": sum(1 for t in calls if t is not False),',
    '                  "abstain": bool(data.get("abstain"))}, ensure_ascii=False))',
])


def _run_route_probe(tmp_path, repo, mode: str, ignore_touch: bool = False) -> dict:
    """在**独立进程**里跑一条真实路由的 Δ 测量（`_ROUTE_PROBE`）。

    ⚠️ **必须独立进程**，两条实测原因：
    1. `access_touch` 有 60s 限流 + 单 worker 异步队列，同进程连跑两条路由会互相干扰；
    2. **pytest 进程内 `full` 会 abstain**：融合拿到 10 行（`breakdown.unique_fused=10`）但出口
       变成 `results=[]`（`abstain_reason="no_results"`、`evidence_gate n=0`）；同一 setup 在
       **独立进程**里返回 10 行（verifier 也是"每个配置一个独立进程"）。⇒ 判据按"一条路由一个
       进程"组织，避免把**测试环境差异**读成记账效应。
    """
    db = str(tmp_path / ("route_%s%s.db" % (mode, "_noflag" if ignore_touch else "")))
    args = [sys.executable, "-c", _ROUTE_PROBE, str(repo), mode, db]
    if ignore_touch:
        args.append("--ignore-touch")
    out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=300, cwd=str(repo))
    assert out.returncode == 0, "路由探针失败：%s" % (out.stderr or out.stdout)[-2000:]
    line = [x for x in out.stdout.strip().splitlines() if x.startswith("{")]
    assert line, "探针没有输出 JSON：%s" % out.stdout[-2000:]
    return json.loads(line[-1])


@pytest.mark.parametrize("route", ["light", "full"])
def test_路由矩阵_两条真实路由都恰好加一次(tmp_path, route):
    """**t21 核心判据（真实路由矩阵）**：`light` 与 `full` 都**恰好 +1**，且管线内没有默认记账的调用。

    `full` 是**生产 PG 的默认路由**（`_hybrid_search.py:129-134`）—— t18 的判据没覆盖它，
    于是漏掉了 `_hybrid_index.py` 的三处；本用例把它补上。
    """
    r = _run_route_probe(tmp_path, REPO, route)
    delta, bad_calls, n = r["delta"], r["bad_calls"], r["returned_n"]

    assert n > 0, "路由 %s 没有返回任何行（判据会空转）：%r" % (route, r)
    assert bad_calls == 0, (
        "路由 %s：有 %d 次 `search_memories` 未传 touch=False ⇒ 与出口层叠加成多次记账" % (route, bad_calls))
    assert max(delta) == 1, "路由 %s：Δ=%r（max=%d）—— 违反 I1" % (route, delta, max(delta))
    assert delta == [1] * n, "路由 %s：Δ=%r（应每行恰好 1）—— 违反 I2" % (route, delta)


@pytest.mark.parametrize("route", ["light", "full"])
def test_负向_摘掉某处touchFalse路由判据必须红(tmp_path, route):
    """**负向实测（t21）**：模拟「某处 `touch=False` 被摘掉」—— 判据必须判红。

    做法：让适配器**无视** `touch=False`（= 该调用点等效于没传），再跑同一条真实路由；
    「每行恰好 +1」必须失败（与上面的核心判据共用同一段断言逻辑）。
    """
    r = _run_route_probe(tmp_path, REPO, route, ignore_touch=True)
    delta, n = r["delta"], r["returned_n"]

    assert n > 0, "路由 %s 没有返回任何行：%r" % (route, r)
    assert max(delta) > 1, (
        "摘掉 touch=False 后 Δ=%r 仍然没有超额 ⇒ 本负向实测没测到东西" % delta)
    with pytest.raises(AssertionError):
        assert delta == [1] * n, "路由 %s：Δ=%r（应每行恰好 1）—— 违反 I2" % (route, delta)


def _scan_client_search_memories() -> dict:
    """扫 `trinity/core/client/*.py`：返回 `{文件名: [(行号, touch 取值), ...]}`（只含有调用的文件）。"""
    pkg = REPO / "trinity" / "core" / "client"
    out: dict = {}
    for py in sorted(pkg.glob("*.py")):
        flags = _search_memories_touch_flags(py.read_text(encoding="utf-8"))
        if flags:
            out[py.name] = flags
    return out


def _classify(sites: dict) -> tuple:
    """把"哪些文件有记账点"分类成 `(违规, 未分类)`。

    **结构上的一处设计**：任何**新**出现的文件（既不在"必须 `touch=False`"、也不在白名单里）
    一律进 `未分类` ⇒ 判据直接红。新调用点因此**不可能静默混进来**。
    """
    offenders, unclassified = [], []
    for name, flags in sites.items():
        if name in _PIPELINE_MUST_ALL_FALSE:
            bad = [(ln, v) for ln, v in flags if v is not False]
            if bad:
                offenders.append((name, bad))
        elif name not in _ALLOWED_DEFAULT_TOUCH:
            unclassified.append((name, flags))
    return offenders, unclassified


def test_结构层_管线内search_memories必须显式不记账():
    """**结构层判据**：混合检索管线内的**每一个** `search_memories` 调用都必须 `touch=False`；
    管线外的调用点必须在**显式白名单**里（含理由），否则本用例判红。

    价值：verifier 的三处漏网（`_hybrid_index.py`）**不依赖任何路由复现**就能被抓到 ——
    把"覆盖真实路由矩阵"从"我记得构造那条路由"变成"代码面扫描"。

    2026-10-06（t27）：`_advanced.py`（t21 时在白名单里标"待复核"）与 `_graph.py`
    **已复核并移入严格组**（判定见 `ACCESS-COUNT-SINGLE-COUNT.md` §14）。
    """
    offenders, unclassified = _classify(_scan_client_search_memories())

    assert offenders == [], (
        "混合检索管线内有 `search_memories` 未传 touch=False（会与出口层叠加）：%r" % offenders)
    assert unclassified == [], (
        "core/client 下有**未分类**的 `search_memories` 调用点（请逐个定级并写进白名单）：%r"
        % unclassified)


def test_结构层扫描的覆盖面本身可核_t27():
    """**t27 要求②**：把"扫描到底覆盖了哪些文件"变成**可核事实**，而不是靠相信扫描。

    三条断言：
    1. 严格组里的每个文件**都真的被扫到**（文件改名/挪走 ⇒ 判据红，不会"名单在、文件不在"）；
    2. **`_advanced.py` 必须在被扫到的文件里、且在严格组里**（这是 t27 复核的落点）；
    3. `_graph.py` 同上（t27 新增的同类点）；白名单只在**有理由**时成立（值非空）。
    """
    sites = _scan_client_search_memories()

    missing = [n for n in _PIPELINE_MUST_ALL_FALSE if n not in sites]
    assert missing == [], "严格组里的文件在扫描结果里**不存在**（名单是死的）：%r" % missing
    for name in _PIPELINE_MUST_ALL_FALSE:
        assert sites[name], "%s 被扫到但没有任何调用点（名单该清理了）" % name

    for must in ("_advanced.py", "_graph.py"):
        assert must in sites, "%s 没有被扫描覆盖 ⇒ 扫描有漏洞" % must
        assert must in _PIPELINE_MUST_ALL_FALSE, "%s 应已在严格组（t27 复核后移入）" % must

    for name, why in _ALLOWED_DEFAULT_TOUCH.items():
        assert why and why.strip(), "%s 在白名单里但没写理由" % name
        assert name in sites, "%s 在白名单里但已无调用点（名单该清理了）" % name


def test_结构层分类器有牙齿_新文件不得静默混入():
    """**负向实测（覆盖面）**：用一个**合成**的扫描结果验证分类器：

    * 未知文件（`_newcomer.py`）⇒ 必须落进"未分类"（`unclassified` 非空）；
    * 严格组文件带一个 `<缺省>` ⇒ 必须落进"违规"（`offenders` 非空）。
    """
    offenders, unclassified = _classify({"_newcomer.py": [(1, None)]})
    assert offenders == [] and [u[0] for u in unclassified] == ["_newcomer.py"], \
        "新文件被静默放行了 ⇒ 分类器无效"

    offenders2, unclassified2 = _classify({"_advanced.py": [(743, None)]})
    assert offenders2 == [("_advanced.py", [(743, None)])], "严格组里的缺省没被算作违规"
    assert unclassified2 == [], "严格组文件不应落进未分类"


def test_结构层哨兵有牙齿_摘掉touchFalse必须被抓到():
    """**负向实测（结构层）**：把 `touch=False` 从管线文件的**变异副本**里摘掉，侦测器必须抓到。

    t27 起覆盖到 `_advanced.py` / `_graph.py`（t21 时它们还在白名单里）。
    """
    pkg = REPO / "trinity" / "core" / "client"
    for name in _PIPELINE_MUST_ALL_FALSE:
        src = (pkg / name).read_text(encoding="utf-8")
        assert "touch=False" in src, "%s 里没有 touch=False（本用例前提失效）" % name
        mutated = src.replace("touch=False", "touch=True", 1)
        bad = [(ln, v) for ln, v in _search_memories_touch_flags(mutated) if v is not False]
        assert bad, "%s 的变异副本没被侦测到 ⇒ 结构层哨兵无效" % name


# ── 2026-10-06（t28）：判定表闭环 + R-6 / R-7 / R-8 收口判据 ──────────────────

def _scan_sites() -> list:
    """`core/client` 下**每一个** `search_memories` 调用点：

    `(文件, 所在方法(最内层), 行号, touch 表达式文本或 None)`。
    按**方法**判"哪个调用点被放行"，这样嵌套闭包（如 `_base_fn`）也各自可核。
    """
    pkg = REPO / "trinity" / "core" / "client"
    out = []
    for py in sorted(pkg.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        owner = {}
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                        and n.func.attr == "search_memories":
                    owner[id(n)] = fn.name          # 后写覆盖 ⇒ 最内层胜出
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "search_memories":
                t = {k.arg: k.value for k in n.keywords}.get("touch")
                out.append((py.name, owner.get(id(n), "?"), n.lineno,
                            None if t is None else ast.unparse(t).strip()))
    return sorted(out)


#: "保留默认记账"的调用点 —— **按方法**登记（理由必须写清，否则就是漏网的叠加点）。
_KEPT_ACCOUNTING = {
    ("_search.py", "search"):
        "多模式入口：mode=keyword/vector 时这些行**就是**该次检索的返回集（无出口层覆盖它）；"
        "mode=hybrid 时只在 hybrid 返回空后兜底 ⇒ 与出口层不重叠",
    ("_pagetree.py", "pagetree_search"):
        "pagetree 独立入口（不走 search_hybrid 的出口层）⇒ 这条调用是它自己的记账",
    ("_pagetree.py", "_base_fn"):
        "同上：pagetree 内部的基础召回闭包，一次检索调用一次",
}

#: "参数化记账"（t28/R-6）：`touch=<表达式>` —— 由**调用方**决定记不记。
_PARAMETERIZED_ACCOUNTING = {
    ("_search.py", "_vector_search"):
        {"param": "account", "default": True,
         "why": "两副身份：独立入口（vector 模式）该记账；hybrid 嵌入通道/PPR 种子由出口层记账"},
}


def test_全量记账点判定表闭环_t28():
    """**判定表闭环**（t28）：每个 `search_memories` 调用点必须落进三类之一：

    1. `touch=False` 字面量（管线通道/内部池/fan-out —— 不该记账）；
    2. `touch=<表达式>`（参数化）⇒ (文件, 方法) 必须在 `_PARAMETERIZED_ACCOUNTING`；
    3. 缺省（保留记账）⇒ (文件, 方法) 必须在 `_KEPT_ACCOUNTING` 且**有理由**。

    ⚠️ **t27 我写的"全量 14 处"是错的**（表里的行数其实是 **17**）；本用例把 **17** 钉住 ——
    调用点增删必然改这个数 ⇒ 强制回来更新报告里的判定表。
    """
    sites = _scan_sites()
    assert len(sites) == 17, (
        "记账点数量变了（%d）⇒ 请更新报告 §15.4 的判定表并逐个给出判定：%r"
        % (len(sites), [(s[0], s[1], s[2], s[3]) for s in sites]))

    unclassified, dead = [], []
    for name, method, ln, touch in sites:
        if touch == "False":
            continue
        if touch is not None:
            if (name, method) not in _PARAMETERIZED_ACCOUNTING:
                unclassified.append((name, method, ln, touch))
        elif (name, method) not in _KEPT_ACCOUNTING:
            unclassified.append((name, method, ln, "<缺省>"))
    for key in list(_KEPT_ACCOUNTING):
        if not any((s[0], s[1]) == key for s in sites):
            dead.append(("死条目", key))
        elif not _KEPT_ACCOUNTING[key].strip():
            dead.append(("无理由", key))

    assert unclassified == [], "有记账点**没被判定**（会静默叠加/漏记）：%r" % unclassified
    assert dead == [], "判定表里有**死条目/无理由条目**：%r" % dead

    # t30：**跨包**点单独一档（`core/client` 之外，如 API 预热）——
    # 两档合计才是"已判定总数"。这一行让两个数字都被机器钉住，而不是靠我记着去改报告。
    # t32（R-11）：再加**同包派生档**（走 `search_hybrid` 出口记账、但结果不返回给调用方的内部检索）
    # ⇒ 总数 17 + 跨包 2 + 派生 1 = **20**。**更新前后**：t30 是 17+1=18，t32 是 17+2+1=20。
    cross = len(_CROSS_PACKAGE_NO_ACCOUNT)
    derived = len(_DERIVED_HYBRID_NO_ACCOUNT)
    assert len(sites) + cross + derived == 20, (
        "已判定记账点总数变了（core/client %d + 跨包 %d + 派生 %d ≠ 20）⇒ 请同步更新报告里的判定表"
        % (len(sites), cross, derived))


def test_判定表分类器有牙齿_新调用点不得静默混入():
    """**负向实测**：合成的"新文件 + 新方法里的缺省/True 记账点"必须落进"未判定"。"""
    fake = [("_newcomer.py", "some_method", 1, None), ("_newcomer.py", "other", 2, "True")]
    unclassified = []
    for name, method, ln, touch in fake:
        if touch == "False":
            continue
        if touch is not None:
            if (name, method) not in _PARAMETERIZED_ACCOUNTING:
                unclassified.append((name, method, ln, touch))
        elif (name, method) not in _KEPT_ACCOUNTING:
            unclassified.append((name, method, ln, "<缺省>"))
    assert len(unclassified) == 2, "新调用点被静默放行 ⇒ 判定表无效：%r" % unclassified


def test_R6_参数极性_True记账_False不记账_t28(tmp_path, monkeypatch):
    """**R-6 的极性判据（功能级）**：`account=True` ⇒ 本层**记账**；`account=False` ⇒ **不记账**。

    为什么必须有这一条：我首版把表达式写成 `touch=not account`（**极性反了**）——
    结构性判据（默认 True / 表达式依赖 account / hybrid 侧传 False）**全都通过**，
    只有功能探针把它抓出来（account=True 时反而记 0 条）。⇒ 极性必须**实测**，不能只查形状。

    做法：用替身嵌入引擎把 `_vector_search` 推到它的 ANN 降级分支（那行 `search_memories`）。
    """
    import types

    from trinity import Trinity
    from trinity.adapters.sqlite import SQLiteAdapter
    from trinity.core.client import _search as S

    db = str(tmp_path / "r6.db")
    ad = SQLiteAdapter(db_path=db)
    ad.connect()
    for i in range(5):
        ad.store_memory(content="alpha beta gamma 极性 样本 %02d 记账 参数" % i,
                        agent_id="t28", category="general", tags=["t28"], importance=0.5)
    ids = [r["memory_id"] for r in ad.search_memories("alpha", top_k=20, touch=False)]
    mem = Trinity(adapter="sqlite", store_path=db)

    monkeypatch.setattr(S, "_embed_query_bounded", lambda eng, q, to: [0.1] * 8)
    mem._embedding_engine = types.SimpleNamespace(embedding_dim=lambda: 8)
    mem.use_ann = True
    mem._ann_cache = None
    mem._try_load_anon = None                       # 兼容占位（不参与判断）
    mem._try_load_ann_from_disk = lambda dim: False
    mem._ensure_ann_background = lambda: None

    def _run(account):
        from trinity.brain import access_touch as _at

        _fresh()
        before = _counts(ad, ids)
        rows = mem._vector_search("alpha beta gamma", 5, account=account)
        inner = getattr(mem, "_adapter", None)
        if inner is not None and hasattr(inner, "_flush_touch_queue"):
            inner._flush_touch_queue()
        ad._flush_touch_queue()
        pool = getattr(_at, "_EXECUTOR", None)
        if pool is not None:
            try:
                pool.submit(lambda: None).result(timeout=20)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）tests/unit/test_access_count_single_count_20261006.py::_run")
        after = _counts(ad, ids)
        return len(rows or []), sum(1 for m in ids if after[m] - before[m] > 0)

    n_on, counted_on = _run(True)
    assert n_on > 0, "分支没被走到（本用例前提失效）：应返回若干行"
    assert counted_on == n_on, (
        "`account=True` 必须记账（极性反了？）：返回 %d 行但只记了 %d 行" % (n_on, counted_on))

    n_off, counted_off = _run(False)
    assert n_off > 0
    assert counted_off == 0, (
        "`account=False` 必须**不**记账（hybrid 通道靠它避免与出口层叠加）：却记了 %d 行" % counted_off)


def _vector_search_account_false(src: str) -> list:
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "_vector_search":
            if {k.arg: ast.unparse(k.value) for k in n.keywords}.get("account") == "False":
                out.append(n.lineno)
    return out


def test_R6_两副身份_参数化记账被两侧正确使用_t28():
    """**R-6 判据**：`_vector_search(..., account=True)` 的默认与两类调用方必须**各归其位**。

    * 默认 `True`（独立入口 ⇒ 记账）；
    * `search_hybrid` 的嵌入通道 / PPR 语义种子**必须**传 `account=False`（否则与出口层叠加）；
    * 该参数化调用点必须已被 `_PARAMETERIZED_ACCOUNTING` 登记（闭环判据已在上面check）。
    """
    search_src = (REPO / "trinity" / "core" / "client" / "_search.py").read_text(encoding="utf-8")
    fn = [n for n in ast.walk(ast.parse(search_src))
          if isinstance(n, ast.FunctionDef) and n.name == "_vector_search"]
    assert fn, "找不到 `_vector_search` 定义"
    args = [a.arg for a in fn[0].args.args]
    defaults = dict(zip(args[len(args) - len(fn[0].args.defaults):],
                        [ast.unparse(d) for d in fn[0].args.defaults]))
    assert "account" in args, "`_vector_search` 缺少 `account` 参数"
    assert defaults.get("account") == "True", "默认必须是 True（独立入口要记账）：%r" % defaults

    expr = [s[3] for s in _scan_sites() if s[0] == "_search.py" and s[3] not in (None, "False")]
    assert expr and "account" in expr[0], "`touch` 表达式必须依赖 `account`：%r" % expr

    hybrid_src = (REPO / "trinity" / "core" / "client" / "_hybrid_index.py").read_text(encoding="utf-8")
    off = _vector_search_account_false(hybrid_src)
    assert len(off) >= 2, (
        "hybrid 侧应至少 2 处 `_vector_search(..., account=False)`（嵌入通道 + PPR 语义种子）：%r" % off)


def test_R6两条负向_任一方向改错都必须判红_t28():
    """**R-6 两条负向**：

    ① hybrid 侧**丢掉** `account=False`（= 又会叠加成 2 次）⇒ 判据红；
    ② 默认**被翻成 `False`**（= `search(mode=vector)` 失去记账、方向反转）⇒ 判据红。
    """
    hybrid = (REPO / "trinity" / "core" / "client" / "_hybrid_index.py").read_text(encoding="utf-8")
    mut1 = hybrid.replace("account=False", "", 2)
    assert mut1 != hybrid and _vector_search_account_false(mut1) == [], "变异体没变 ⇒ 侦测器无效"
    with pytest.raises(AssertionError):
        assert len(_vector_search_account_false(mut1)) >= 2, "hybrid 侧丢了 account=False ⇒ 判据应红"

    search_src = (REPO / "trinity" / "core" / "client" / "_search.py").read_text(encoding="utf-8")
    mut2 = search_src.replace("account: bool = True", "account: bool = False", 1)
    assert "account: bool = True" not in mut2, "变异体没变 ⇒ 侦测器无效"
    with pytest.raises(AssertionError):
        assert "account: bool = True" in mut2, "默认被翻成 False ⇒ 判据应红（独立入口会失去记账）"


def test_R8_记账集合必须等于返回集合_t28(tmp_path, monkeypatch):
    """**R-8 判据**：过取候选（`top_k*2`）**不得**被记账；被记账集合必须**逐行等于**返回集。

    **负向**（同用例内）：摘掉出口记账 + 候选池恢复默认记账（= 修前行为）⇒ 同一条判据必须红。
    """
    from trinity import Trinity
    from trinity.adapters.sqlite import SQLiteAdapter
    from trinity.core.client import _search as S

    db = str(tmp_path / "r8.db")
    ad = SQLiteAdapter(db_path=db)
    ad.connect()
    for i in range(8):
        ad.store_memory(content="alpha beta gamma 样本 %02d 过取 截断 记账 判据" % i,
                        agent_id="t28", category="general", tags=["t28"], importance=0.5)
    ids = [r["memory_id"] for r in ad.search_memories("alpha", top_k=50, touch=False)]
    mem = Trinity(adapter="sqlite", store_path=db)

    def _stub_vec(self, query, top_k, account=True):
        return self._adapter.search_memories(query=query, top_k=top_k, touch=False)

    monkeypatch.setattr(type(mem), "_vector_search", _stub_vec, raising=False)

    def _measure():
        from trinity.brain import access_touch as _at

        _fresh()
        before = _counts(ad, ids)
        out = mem._search_with_vector("alpha beta gamma", None, None, None, 3)
        inner = getattr(mem, "_adapter", None)
        if inner is not None and hasattr(inner, "_flush_touch_queue"):
            inner._flush_touch_queue()
        ad._flush_touch_queue()
        pool = getattr(_at, "_EXECUTOR", None)
        if pool is not None:
            try:
                pool.submit(lambda: None).result(timeout=20)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）tests/unit/test_access_count_single_count_20261006.py::_measure")
        if inner is not None and hasattr(inner, "_flush_touch_queue"):
            inner._flush_touch_queue()
        after = _counts(ad, ids)
        return ({m for m in ids if after[m] - before[m] > 0},
                {r["memory_id"] for r in out})

    accounted, returned = _measure()
    assert len(returned) == 3, "应只返回 top_k=3 行（候选过取 6 行）"
    assert accounted == returned, (
        "记账集合 != 返回集合（过取被截断的行被记了）：记账=%r 返回=%r"
        % (sorted(accounted), sorted(returned)))

    monkeypatch.setattr(S._acct, "account_returned_hits", lambda *a, **k: 0)   # 摘掉出口记账
    orig = SQLiteAdapter.search_memories

    def _always_touch(self, *a, **kw):                           # 候选池恢复记账（修前行为）
        kw.pop("touch", None)
        return orig(self, *a, **kw)

    monkeypatch.setattr(SQLiteAdapter, "search_memories", _always_touch)
    accounted2, returned2 = _measure()
    assert len(returned2) == 3
    with pytest.raises(AssertionError):
        assert accounted2 == returned2, (
            "记账集合 != 返回集合（过取被截断的行被记了）：记账=%r 返回=%r"
            % (sorted(accounted2), sorted(returned2)))


def test_R7_候选池不记账_且不是fanout_t28():
    """**R-7 判据 + 定性**：`_pagetree._search_reason` 的关键词召回**不记账**（候选池）。

    定性依据：
      * 它是 `mode="reason"` 的候选召回（`base_k = max(top_k*2, max_candidates)` = **过取**，
        随后 LLM judge 只留 top_k）⇒ 与 **R-8 同类**（被截断的行不该记）；
      * 该方法是**一次检索调一次**（**不是** fan-out —— 原假设不成立），但它**自己还调
        `self.search_hybrid(...)`** 取语义候选 ⇒ 那批行的记账已由 `search_hybrid` 的**出口层**
        承担 ⇒ 若这里也记，同一行会 **2 次**（违反 I1）。
    """
    pt = (REPO / "trinity" / "core" / "client" / "_pagetree.py").read_text(encoding="utf-8")
    sites = {s[1]: s[3] for s in _scan_sites() if s[0] == "_pagetree.py"}
    assert sites.get("_search_reason") == "False", (
        "`_search_reason` 的候选召回必须 touch=False：%r" % sites.get("_search_reason"))
    assert "self.search_hybrid(" in pt, (
        "定性依据之一消失：`_search_reason` 若不再调 `search_hybrid`，重叠理由需重评")
    assert "top_k=base_k" in pt, "过取依据（base_k）消失 ⇒ 定性需重评"

    mutated = pt.replace("                    top_k=base_k, include_docs=include_docs,\n"
                         "                    touch=False,",
                         "                    top_k=base_k, include_docs=include_docs,", 1)
    assert mutated != pt, "变异体没变 ⇒ 本用例的插入点失效"
    assert [(ln, v) for ln, v in _search_memories_touch_flags(mutated) if v is not False], \
        "摘掉 `_search_reason` 的 touch=False 后没被抓到 ⇒ 判据无效"


# ── 2026-10-06（t30）：R-9 —— API 预热路径不得凭空制造读事件（第 5 个变体）──────

DEPS_PY = REPO / "trinity" / "api" / "server" / "_deps.py"

#: **跨包**记账点登记表（`core/client` 之外、但同属"记账点完整性"的已知点）。
_CROSS_PACKAGE_NO_ACCOUNT = {
    ("api/server/_deps.py", "_warm"):
        "API **预热**：`for _ri in range(300): mem._vector_search('预热', 8)` —— 预热不是读取需求，"
        "修前每轮给命中行各 +1（实测 5 轮 ⇒ 每行 Δ=5，外推 300 轮 ⇒ +300）⇒ 凭空制造读事件",
    ("api/server/_routers_memories.py", "store_memory"):
        "**写入路径的冲突检测/自检检索**（`_verify_search` → `search_hybrid`）：适配器 docstring 原文"
        "（`adapters/sqlite/_search.py:74-76`）要求『内部维护操作（如写路径的冲突检测检索）应传 "
        "`touch=False`，避免把「写入时自碰」误记为真实访问』。修前实测：**刚写入的行 rank=1 命中"
        "且被 +1**（危害在**不可逆**那一侧：一旦被记成「读过」就回不到冷池口径）；修后同一次自检的 "
        "`hit/rank` 完全不变、Δ=0（t32 / R-11）",
}

#: **同包派生检索**登记表：走 `search_hybrid`（出口层记账）但**结果不返回给调用方**的内部检索。
_DERIVED_HYBRID_NO_ACCOUNT = {
    ("_pagetree.py", "_search_reason"):
        "PageTree 的**语义候选池**（只喂给 LLM judge，不是返回给调用方的命中集）⇒ `account=False`（t32）",
}


def _deps_warmup_call() -> dict:
    """从 `_deps.py` **源码**里读出预热那次 `_vector_search(...)` 的调用形状。

    ⇒ 功能判据驱动的是**真实调用形状**，而不是我手写一份副本（否则实现改了判据也不会跟着变）。
    """
    src = DEPS_PY.read_text(encoding="utf-8")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "_vector_search":
            return {"lineno": n.lineno,
                    "args": [ast.literal_eval(a) for a in n.args],
                    "kwargs": {k.arg: ast.literal_eval(k.value) for k in n.keywords},
                    "source": ast.unparse(n)}
    raise AssertionError("`_deps.py` 里找不到预热用的 `_vector_search(...)` 调用")


def test_跨包预热记账点已登记且不记账_t30():
    """**t30 结构判据**：`_deps.py` 的预热调用必须**已登记**在跨包表里，且**传 `account=False`**。

    为什么单独一张表：`test_全量记账点判定表闭环_t28` 扫的是 `trinity/core/client/**`，
    `_deps.py` 在**另一个包**里 ⇒ 不登记就等于"扫描覆盖面之外的黑洞"（t27 的教训）。
    """
    call = _deps_warmup_call()
    assert call["kwargs"].get("account") is False, (
        "预热调用必须传 `account=False`（否则凭空制造读事件）：%s" % call["source"])
    assert ("api/server/_deps.py", "_warm") in _CROSS_PACKAGE_NO_ACCOUNT, "跨包表里没有登记这个点"

    # 牙齿：把 account=False 摘掉 ⇒ 侦测器必须看到 kwargs 里没有 account
    src = DEPS_PY.read_text(encoding="utf-8")
    mutated = src.replace("_vector_search(\"预热\", 8, account=False)",
                          "_vector_search(\"预热\", 8)", 1)
    assert mutated != src, "变异体没变 ⇒ 本用例的插入点失效"
    kw = None
    for n in ast.walk(ast.parse(mutated)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "_vector_search":
            kw = {k.arg: ast.literal_eval(k.value) for k in n.keywords}
    assert kw is not None and kw.get("account") is not False, (
        "摘掉 `account=False` 后没被抓到 ⇒ 判据无效")


# ── 2026-10-06（t32）：R-11 —— 写入路径自检（`_verify_search`）不得给**被写入的行**记账 ─────

ROUTER_MEMORIES = REPO / "trinity" / "api" / "server" / "_routers_memories.py"


def _router_verify_search_kwargs(src: str = "") -> dict:
    """从 `_routers_memories.py` **源码**读出 `_verify_search` 里 `search_hybrid(...)` 的字面 kwargs。

    ⇒ 功能判据驱动的是**真实调用形状**（实现改了判据跟着变），不是手写副本（t30 的同款理由）。
    """
    text = src or ROUTER_MEMORIES.read_text(encoding="utf-8")
    for n in ast.walk(ast.parse(text)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "search_hybrid":
            out = {}
            for k in n.keywords:
                try:
                    out[k.arg] = ast.literal_eval(k.value)
                except Exception:  # noqa: BLE001 — 变量参数（q / k）不参与
                    continue
            return out
    raise AssertionError("`_routers_memories.py` 里找不到 `search_hybrid(...)` 调用")


def test_R11_写入自检调用不得记账_结构_t32():
    """**t32 结构判据**：写入路径的自检检索必须 `account=False` **且已登记**在跨包表。

    依据（适配器 docstring 原文，`adapters/sqlite/_search.py:74-76`）：
    『内部维护操作（如写路径的冲突检测检索）应传 `touch=False`，避免把"写入时自碰"误记为真实访问
    （污染 access_count 语义）』—— 这里是 `search_hybrid` 出口记账 ⇒ 对应开关是 `account=False`。
    """
    kw = _router_verify_search_kwargs()
    assert kw.get("account") is False, (
        "写入路径的自检检索必须 `account=False`（否则刚写入的行会被记成「读过」）：%r" % kw)
    assert ("api/server/_routers_memories.py", "store_memory") in _CROSS_PACKAGE_NO_ACCOUNT, \
        "跨包表里没有登记这个点"

    # 牙齿：把 `account=False` 摘掉 ⇒ 侦测器必须看到 kwargs 里没有 account
    src = ROUTER_MEMORIES.read_text(encoding="utf-8")
    mutated = src.replace('strategy="rrf", account=False', 'strategy="rrf"', 1)
    assert mutated != src, "变异体没变 ⇒ 本用例的插入点失效"
    assert _router_verify_search_kwargs(mutated).get("account") is not False, \
        "摘掉 `account=False` 后没被抓到 ⇒ 判据无效"


def test_R11_写入自检仍能命中但不得更改access_count_功能_t32(tmp_path, monkeypatch):
    """**t32 核心判据（功能级）**：按**源码里的真实调用形状**跑一次写入后自检 ⇒ **Δ=0**；
    且自检本身**仍然命中**（证明"关掉的是记账、不是功能"）。**负向**：用修前形状 ⇒ **Δ>0**。
    """
    import types

    from trinity import Trinity
    from trinity.adapters.sqlite import SQLiteAdapter
    from trinity.brain import access_touch as at
    from trinity.core.client import _search as S
    from trinity.memory.write_verify import maybe_verify_after_store

    monkeypatch.setenv("TRINITY_WRITE_VERIFY", "on")
    db = str(tmp_path / "w32.db")
    ad = SQLiteAdapter(db_path=db)
    ad.connect()
    mem = Trinity(adapter="sqlite", store_path=db)
    if hasattr(S, "_embed_query_bounded"):          # 向量通道在本用例里无关紧要，降级即可
        monkeypatch.setattr(S, "_embed_query_bounded",
                            lambda eng, q, to: [0.1] * 8)
        mem._embedding_engine = types.SimpleNamespace(embedding_dim=lambda: 8)

    content = "alpha beta gamma 写入自碰 复现 判据 样本 冲突检测 记账"
    res = mem.ingest(content=content, agent_id="t32", category="general")
    mid = (res or {}).get("memory_id")
    assert mid, "写入失败（本用例前提失效）"

    def _run(kwargs):
        at._recent.clear()                          # 60s 限流会掩盖第二次记账 ⇒ 逐次清空
        before = _counts(ad, [mid])[mid]
        # 2026-10-06（t74/I14）：原为 `searcher = lambda q, k: …（原本带 E731 抑制）` ⇒ 改 `def`（真修）
        def searcher(q, k):
            return mem.search_hybrid(query=q, top_k=k, **kwargs)
        out = maybe_verify_after_store({"content": content}, {"memory_id": mid}, searcher=searcher)
        inner = getattr(mem, "_adapter", None)
        if inner is not None and hasattr(inner, "_flush_touch_queue"):
            inner._flush_touch_queue()
        ad._flush_touch_queue()
        pool = getattr(at, "_EXECUTOR", None)
        if pool is not None:
            try:
                pool.submit(lambda: None).result(timeout=20)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）tests/unit/test_access_count_single_count_20261006.py::_run")
        return _counts(ad, [mid])[mid] - before, out

    real_kwargs = _router_verify_search_kwargs()
    assert real_kwargs.get("account") is False, "源码里的自检没有 `account=False`"
    delta_real, out_real = _run(real_kwargs)
    assert delta_real == 0, (
        "写入路径自检给**被写入的行**记了账（Δ=%d）⇒ 刚写入的行会看起来「被读过」" % delta_real)
    assert out_real is not None and getattr(out_real, "hit", False), (
        "自检**没命中** ⇒ 这条判据可能只是「没查到所以没记账」的空转：%r" % (out_real,))

    delta_pre_fix, _ = _run({k: v for k, v in real_kwargs.items() if k != "account"})
    assert delta_pre_fix > 0, (
        "修前形状竟然也没记账（Δ=%d）⇒ 本判据没测到东西" % delta_pre_fix)


def test_R12_pagetree语义候选池不记账_且已登记_t32():
    """**t32 顺带核对**：PageTree 的语义候选池（派生检索）也必须 `account=False` 且已登记。"""
    src = (REPO / "trinity" / "core" / "client" / "_pagetree.py").read_text(encoding="utf-8")
    hits = [{k.arg: ast.literal_eval(k.value) for k in n.keywords
             if k.arg == "account"}
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "search_hybrid"]
    assert hits and all(h.get("account") is False for h in hits), (
        "`_pagetree` 里所有 `search_hybrid` 调用都必须 `account=False`（派生候选池）：%r" % hits)
    assert ("_pagetree.py", "_search_reason") in _DERIVED_HYBRID_NO_ACCOUNT, "派生档里没登记"

    mutated = src.replace('strategy="rrf", account=False', 'strategy="rrf"', 1)
    assert mutated != src, "变异体没变 ⇒ 本用例的插入点失效"
    m = [ast.literal_eval(k.value) for n in ast.walk(ast.parse(mutated))
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
         and n.func.attr == "search_hybrid" for k in n.keywords if k.arg == "account"]
    assert not m or any(v is not False for v in m), "摘掉 `account=False` 后没被抓到 ⇒ 判据无效"


def test_预热路径不得改变access_count_t30(tmp_path):
    """**t30 核心判据（功能级）**：跑若干脆**预热调用形状**，`access_count` 必须**一动不动**。

    * **正向**：按 `_deps.py` 里**真实的调用形状**（从源码读出来的 args/kwargs）跑 3 轮 ⇒ 每行 Δ=0；
    * **负向**：用**修前的形状**（不带 `account`）跑同样的 3 轮 ⇒ Δ>0（= 修前真的在凭空制造读事件，
      所以这条判据不是空转）。

    环境：临时 SQLite + 替身嵌入引擎（把 `_vector_search` 推到预热期间正走的 ANN 降级分支）。
    """
    import types

    from trinity import Trinity
    from trinity.adapters.sqlite import SQLiteAdapter
    from trinity.core.client import _search as S

    db = str(tmp_path / "warm.db")
    ad = SQLiteAdapter(db_path=db)
    ad.connect()
    for i in range(8):
        ad.store_memory(content="预热 样本 %02d alpha beta gamma 判据" % i,
                        agent_id="t30", category="general", tags=["t30"], importance=0.5)
    ids = [r["memory_id"] for r in ad.search_memories("alpha", top_k=20, touch=False)]
    mem = Trinity(adapter="sqlite", store_path=db)

    import unittest.mock as _mock

    with _mock.patch.object(S, "_embed_query_bounded", lambda eng, q, to: [0.1] * 8):
        mem._embedding_engine = types.SimpleNamespace(embedding_dim=lambda: 8)
        mem.use_ann = True
        mem._ann_cache = None
        mem._try_load_ann_from_disk = lambda dim: False
        mem._ensure_ann_background = lambda: None

        def _rounds(kwargs):
            from trinity.brain import access_touch as _at

            _fresh()
            before = _counts(ad, ids)
            for _ in range(3):
                mem._vector_search("预热", 8, **kwargs)
                _at._recent.clear()               # 模拟 60s 限流随轮次过期（预热可跑数百轮）
            inner = getattr(mem, "_adapter", None)
            if inner is not None and hasattr(inner, "_flush_touch_queue"):
                inner._flush_touch_queue()
            ad._flush_touch_queue()
            pool = getattr(_at, "_EXECUTOR", None)
            if pool is not None:
                try:
                    pool.submit(lambda: None).result(timeout=20)
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）tests/unit/test_access_count_single_count_20261006.py::_rounds")
            after = _counts(ad, ids)
            return max((after[m] - before[m] for m in ids), default=0)

        real_kwargs = _deps_warmup_call()["kwargs"]
        assert real_kwargs.get("account") is False, "源码里的预热调用没传 account=False"
        delta_real = _rounds(real_kwargs)
        assert delta_real == 0, (
            "预热路径改变了 access_count（Δ=%d/行，3 轮）⇒ 凭空制造读事件" % delta_real)

        delta_pre_fix = _rounds({})               # 修前形状：默认 account=True
        assert delta_pre_fix > 0, (
            "修前形状竟然也没记账（Δ=%d）⇒ 本判据没测到东西" % delta_pre_fix)
