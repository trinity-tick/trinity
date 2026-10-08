"""项 16 闸门：冷启动残余（持久化/预热 BM25 与向量索引）2026-09-30。

## 现场

进程内 cProfile（含文档语料 24,863 篇）：

| 指标 | 实测 |
|---|---|
| 冷 full 查询 | **5.52 s** |
| 热 full 查询 | **0.08 s（69×）** |
| 其中 `threading.wait`（**在等后台 BM25 构建**） | **3.275 s** |
| `bm25._tokenize` findall×24,864 / `_term_counts` / `_rebuild_idf` | 0.640 / 0.490 / 0.291 s |
| `decrypt`×22,599（为取正文）/ `sqlite3.execute`×38 | 0.315 / 0.469 s |
| `jieba.initialize` | 0.582 s |

服务侧仍撞 20 s 硬上限并降级，而同一查询进程内只要 0.2 s（预热 BM25 后）。

## 修了什么

### ① BM25 索引在**监听端口之前**就绪（`api/server/__init__.py`）
`_deps._startup_prewarm` 确实会构建 BM25，但也在**后台守护线程**里轮询
⇒ 与首个请求**竞速**，而输家恰好是"用户看到的第一个查询"。

**先量了持久化，再选的同步等待**（`probe_bm25_persist.py`）：
    fetch 1.95s + build 2.18s = 4.13s；落盘 **11.3 MB** / 保存 6.26s / **载入 1.90s**（只省 2.2s）
    载入后 top-10 与在线构建**逐字一致**
⇒ 落盘只值 2.2 s 却要背 11.3 MB 缓存 + 失效判定；**同步等待**代价相同、零缓存。
实测：未等 BM25 的首查询 **7.61 s** → 等就绪后 **0.20 s（38×）**。
附带收益：`_wait_bm25_ready` 的 docstring 记载并发构建+首查会概率性触发
**Windows access violation（0xC0000005）** 崩溃，join 序列化同时消除了它。

### ② 向量索引的 `id_map` 旁车 + **按位置的前缀校验**（`agents/aggregator/__init__.py`）
每次启动都打印：

    vector index row count mismatch (idx=19355 pool=19356) — discard, will rebuild

**差 1 行**就让 ~84 MB 的索引被**整个丢弃、全量重建**，而重建正是与查询争用嵌入器的来源。
根因：faiss 分支**只写索引本身、没写 id_map**（id_map 仅存在于非 faiss 的 NPZ 分支）
⇒ 加载时无法核对"索引第 p 行 == 池第 p 条"，只能全有或全无。

修法：faiss 分支**同批次**落盘 `*.idmap.json`（JSON，**绝不用 pickle** —— 与第 8 轮
反序列化 RCE 的纪律一致），加载时按位置核对：
* 计数相等且逐位相同 ⇒ 采用（旧行为）；
* 索引是池的**严格前缀** ⇒ **采用前缀**，尾部走增量补齐；
* 其余（重排/删条/对不上）⇒ 仍丢弃重建（**绝不猜**）。

运行：``python -m pytest tests/unit/test_cold_start_persistence_20260930.py -q``
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "trinity/api/server/__init__.py"
AGG = ROOT / "trinity/agents/aggregator/__init__.py"
BM25 = ROOT / "trinity/retrieval/bm25_index.py"


# ── ① BM25 必须在监听前就绪 ─────────────────────────────────────────────

def test_bm25_prewarm_before_listen() -> None:
    src = SERVER.read_text(encoding="utf-8")
    assert "TRINITY_PREWARM_BM25" in src, "缺少 BM25 预热开关"
    assert "_ensure_bm25_index()" in src and "_wait_bm25_ready(" in src, (
        "必须在监听前同步等待 BM25 就绪（否则与首个请求竞速）"
    )
    i_bm = src.index("TRINITY_PREWARM_BM25")
    # 注意：`uvicorn.run` 在文件更早处（注释/文档串）也出现过 ⇒ 必须取**最后一次**出现，
    # 否则判据会拿一个注释里的位置去比大小（我的第一版就是这么误报的）。
    i_run = src.rindex("uvicorn.run")
    assert i_bm < i_run, "BM25 预热必须在 uvicorn.run **之前**（否则竞速依旧）"


def test_bm25_prewarm_has_rollback_switch() -> None:
    src = SERVER.read_text(encoding="utf-8")
    assert re.search(r'TRINITY_PREWARM_BM25",\s*"1"', src), "默认应为开，且可 env 关闭"


# ── ② BM25 持久化容器（安全格式 + 校验）─────────────────────────────────

def test_bm25_persistence_is_not_pickle() -> None:
    """索引缓存是**被反复读取**的文件，绝不能用 pickle（读时执行代码）。"""
    src = BM25.read_text(encoding="utf-8")
    assert "def save(" in src and "def load(" in src
    live = [_ln for _ln in src.splitlines()
            if not _ln.lstrip().startswith("#") and re.search(r"\bpickle\.", _ln)]
    assert live == [], f"BM25 持久化里出现 pickle：{live}"
    assert "gzip" in src and "json" in src, "应使用 gzip+JSON 安全容器"


def test_bm25_fingerprint_rejects_stale_and_corrupt(tmp_path: Path) -> None:
    """指纹不符 / 文件损坏都必须返回 None（调用方走重建，绝不静默用错索引）。"""
    from trinity.retrieval.bm25_index import BM25Index

    idx = BM25Index()
    idx.add_documents([("d1", "hello world"), ("d2", "hello trinity"), ("d3", "其他")])
    p = str(tmp_path / "bm25.json.gz")
    size = idx.save(p)
    assert size > 0

    fp = idx.corpus_fingerprint(idx._doc_lengths.keys())
    ok = BM25Index.load(p, expect_fingerprint=fp)
    assert ok is not None and ok.doc_count == 3
    assert [(d, round(s, 6)) for d, s in ok.search("hello", top_k=5)] ==\
           [(d, round(s, 6)) for d, s in idx.search("hello", top_k=5)], "载入后检索结果必须一致"

    assert BM25Index.load(p, expect_fingerprint="0" * 32) is None, "指纹不符必须拒绝"
    bad = tmp_path / "bad.json.gz"
    bad.write_bytes(b"definitely not gzip")
    assert BM25Index.load(str(bad)) is None, "损坏文件必须拒绝"
    assert BM25Index.load(str(tmp_path / "missing.json.gz")) is None


# ── ③ 向量索引 id_map 旁车 + 前缀校验 ───────────────────────────────────

def test_faiss_branch_persists_idmap_sidecar() -> None:
    src = AGG.read_text(encoding="utf-8")
    assert ".idmap.json" in src, "faiss 分支必须落盘 id_map 旁车"
    # 旁车必须与主文件同批次替换
    assert "_replace_with_retry(_im_tmp" in src, "旁车必须随主文件一起替换（防新旧错配）"


def test_prefix_acceptance_semantics() -> None:
    """复刻判定逻辑：只有**逐位相同的前缀**才可采用，重排/超长/无旁车不等一律丢弃。"""

    def decide(pool_ids, ntotal, saved_ids):
        accepted = False
        id_map = None
        if saved_ids is not None and ntotal <= len(pool_ids):
            if saved_ids == pool_ids[:ntotal]:
                id_map = pool_ids[:ntotal]
                accepted = True
        if accepted:
            return id_map
        if ntotal != len(pool_ids):
            return None
        return list(pool_ids)

    pool = [f"m{i}" for i in range(10)]
    # 计数相等 + 旁车一致 ⇒ 采用（前缀校验通过即等价于整体一致）
    assert decide(pool, 10, pool) == pool
    # 严格前缀（实测现场是差 1）⇒ 采用前缀
    assert decide(pool, 9, pool[:9]) == pool[:9]
    assert decide(pool, 7, pool[:7]) == pool[:7]
    # 重排 ⇒ 丢弃（绝不猜）
    assert decide(pool, 9, pool[1:10]) is None
    # 索引比池长 ⇒ 丢弃
    assert decide(pool, 11, pool) is None
    # 无旁车且计数不等（旧行为）⇒ 丢弃
    assert decide(pool, 9, None) is None
    # 无旁车且计数相等（旧行为）⇒ 采用（按池顺序）
    assert decide(pool, 10, None) == pool


def test_discard_path_is_still_conservative() -> None:
    """判据必须确认"对不上时仍然丢弃" —— 前缀校验不能把保守性弄丢。"""
    src = AGG.read_text(encoding="utf-8")
    assert "does not match prefix" in src, "缺少「对不上就丢弃」的显式留痕"
    assert "self._faiss_index = None" in src


def test_idmap_sidecar_is_json_not_pickle() -> None:
    """旁车必须是 JSON，且**活代码里**不得出现 pickle。

    只查非注释行 —— 第一版把**注释里**那句"绝不用 pickle"也当成了证据
    （解释文字里复述被判据盯住的词，是本轮我已犯过一次的同类错误）。
    """
    src = AGG.read_text(encoding="utf-8")
    i = src.index(".idmap.json")
    window_lines = src[max(0, i - 1200): i + 400].splitlines()
    live = [_ln for _ln in window_lines
            if not _ln.lstrip().startswith("#") and re.search(r"\bpickle\.", _ln)]
    assert live == [], f"旁车活代码里出现 pickle：{live}"
    assert "json.dump" in src or "json.load" in src, "旁车应使用 JSON"


# ── ⑷ 回归：既有的向量索引进退位判据不得被破坏 ──────────────────────────

def test_row_count_mismatch_logging_still_present() -> None:
    src = AGG.read_text(encoding="utf-8")
    assert "row count mismatch" in src, "原有告警必须保留（可观测性）"
