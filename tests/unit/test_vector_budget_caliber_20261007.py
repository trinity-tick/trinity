# -*- coding: utf-8 -*-
"""t68 / I8：**向量通道查询嵌入预算的判据**（可失败 + 负向牙齿）。

被测量的对象（现状，本轮**未改默认值**）
--------------------------------------------------
`trinity/core/client/_vec_budget.py` + `_search.py` 的预算行构成**三层**旋钮：

| 旋钮 | 默认 | 作用 |
|---|---|---|
| `TRINITY_QUERY_EMBED_TIMEOUT_S` | **3.0** | 请求路径**查询嵌入**的墙钟上限；超时 ⇒ 该通道返回 `[]`（降级），请求由其它通道完成 |
| `TRINITY_QUERY_EMBED_TIMEOUT_WARM_S` | **180** | **预热线程**内的同一上限（避免 2026-10-04 的闭环死锁） |
| `TRINITY_VEC_CORPUS_EMBED_BUDGET` | **0** | 请求路径**语料嵌入**行数（0=不嵌；负数=不限，回滚位） |

四个判据（任务书第 4 条逐条对应）
--------------------------------------------------
1. `test_默认配置行为与改前等价` —— **对照**：默认值必须是 3.0 / 180 / 0，
   且 `effective_query_embed_timeout_s()` 在"非预热线程"下逐字等于 `query_embed_timeout_s()`。
2. `test_非法值回落默认` —— **反事实**：`TRINITY_QUERY_EMBED_TIMEOUT_S` 设成
   `""` / `"abc"` / 不可解析值时**回落 3.0**，不得抛、不得变成 0（0 会被 `<= 0` 读成"不限"，
   那会把请求侧上限**静默取消**——这正是本判据要钉住的失败模式）。
3. `test_预算生效时确实更早返回` —— **独立读数交叉核对**（t58 的教训）：
   不看"我设的值变了没有"，而看**独立于该配置的两个读数**：
     (a) `_vector_search` 的**返回集从非空变为空**（通道降级）；
     (b) 与预算无关的**日志告警句**被发出（`_search.py:1120` 的那条 `logger.warning`）。
   两者必须与"墙钟明显变短"**同向**，否则本判据红。
4. `test_牙齿_预算恒生效时判据1必须红` —— 把预算改成"恒生效"
   （`TRINITY_QUERY_EMBED_TIMEOUT_S=-1` 读作不限 ⇒ 等价于取消上限；
    或把它设成 0 让 `<=0` 分支返回 `engine.embed(text)` 无上限）
   ⇒ **判据 1 的等价性断言必须失败**。本测试显式构造该反例并断言它**确实**红。
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time

import pytest

from trinity.core.client import _vec_budget as VB

ENV_Q = "TRINITY_QUERY_EMBED_TIMEOUT_S"
ENV_W = "TRINITY_QUERY_EMBED_TIMEOUT_WARM_S"
ENV_C = "TRINITY_VEC_CORPUS_EMBED_BUDGET"

DEFAULTS = {ENV_Q: 3.0, ENV_W: 180.0, ENV_C: 0}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个判据都在干净 env 下跑（并把改动还回去）。"""
    for k in (ENV_Q, ENV_W, ENV_C):
        monkeypatch.delenv(k, raising=False)
    yield


# ───────────────── 判据 1：默认配置 ⇒ 行为与改前等价 ─────────────────
def test_默认配置行为与改前等价():
    """对照：三个旋钮的默认值，以及"非预热线程"下两条取值路径逐字一致。"""
    assert VB.query_embed_timeout_s() == DEFAULTS[ENV_Q] == 3.0
    assert VB.query_embed_timeout_s(default=3.0) == 3.0
    assert VB.effective_query_embed_timeout_s() == VB.query_embed_timeout_s()
    assert VB.corpus_embed_budget() == DEFAULTS[ENV_C] == 0
    # 线程局部覆盖在**非预热线程**必须是 None（否则请求线程会读到预热预算——
    # 那是 2026-09-30 修掉的共享实例属性泄漏，见 _vec_budget 模块 docstring）
    assert VB.current_corpus_override() is None


def test_默认配置下预热作用域内才用足量时间():
    """预热线程内 180s、线程外 3s —— 这是"默认行为零变化"的另一半。"""
    assert VB.current_corpus_override() is None
    assert VB.effective_query_embed_timeout_s() == 3.0
    with VB.corpus_budget_scope(200):
        assert VB.current_corpus_override() == 200
        assert VB.effective_query_embed_timeout_s() == 180.0
    assert VB.current_corpus_override() is None
    assert VB.effective_query_embed_timeout_s() == 3.0


def test_默认配置下语料预算不嵌语料():
    """`0` = 请求路径不嵌语料（政策），`select_rows` 必须返回空。"""
    rows = [{"memory_id": "m%d" % i} for i in range(50)]
    assert VB.select_rows(rows, VB.corpus_embed_budget()) == []
    assert VB.select_rows(rows, -1) == rows          # 回滚位：不限
    assert len(VB.select_rows(rows, 7)) == 7


# ───────────────── 判据 2：非法值 ⇒ 回落默认（反事实） ─────────────────
@pytest.mark.parametrize("bad", ["", "abc", "3s", "  ", "None"])
def test_非法值回落默认(bad, monkeypatch):
    """不可解析的值必须回落 3.0；**尤其不得变成 0**（0 会被读成"不限"）。

    `_vec_budget.py:75` 用 `float(os.environ.get(...) or default)`：
    空串走 `or default`；不可解析走 `except ⇒ return default`。
    而 `_search.py:1106` 也有一份等价的 try/except（`_to = 3.0`）。
    """
    monkeypatch.setenv(ENV_Q, bad)
    got = VB.query_embed_timeout_s()
    assert got == 3.0, "非法值 %r 应回落 3.0，实际 %r" % (bad, got)
    assert got != 0.0, "非法值回落成 0 会被 `<=0` 读成『不限』⇒ 静默取消请求侧上限"
    # 生效值也必须仍是 3.0（不得把 0 漏进生效路径）
    assert VB.effective_query_embed_timeout_s() == 3.0


def test_NaN不被回落且会抛穿上限路径(monkeypatch):
    """⚠️ **实测到的边界（不是本任务引入，但如实锁住）**：
    `TRINITY_QUERY_EMBED_TIMEOUT_S=NaN` **不会**回落默认 —— `float("NaN")` 是合法 float，
    穿过了 `except`。

    实测（`evidence/t68_debug_teeth.py` 的 A/B 段，本机复现）：
      · `query_embed_timeout_s()` → `nan`；`nan <= 0` 为 **False** ⇒ **不进** `<=0` 的"不限"分支
      · `_embed_query_bounded` 走到 `_t.join(nan)` ⇒ CPython 抛
        **`ValueError: Invalid value NaN (not a number)`**（**不是**返回 None）
      · 调用方 `_search.py:1116` 的 `try` 只包住取 `_to` 的那三行，
        `query_vec = _embed_query_bounded(...)` **在 try 之外**
        ⇒ 该 `ValueError` **抛穿 `_vector_search`**
      · 上层 `_hybrid_index.py::_ppr_fn` 的 `except Exception`（`swallow`）**吞掉它**
        ⇒ 净效果：**向量通道被静默摘掉**（连 `_search.py:1120` 那条告警都不发）

    本判据把这条现状**钉住**：若将来有人给 `NaN` 加了回落，这个测试会红 —— 那时应改成
    "断言回落 3.0"，也就是**把现状修好**，而不是放宽判据。
    """
    monkeypatch.setenv(ENV_Q, "NaN")
    got = VB.query_embed_timeout_s()
    assert got != got, "NaN 应保持 NaN（当前实现不回落）—— 若已修成回落 3.0，请改写本判据"
    assert not (got <= 0), "NaN <= 0 必须为 False（否则会落进『不限』分支）"
    assert VB.effective_query_embed_timeout_s() != VB.effective_query_embed_timeout_s()

    class _Slow:
        def __init__(self):
            self.calls = 0

        def embed(self, text):
            self.calls += 1
            time.sleep(0.05)
            return [7.0]

    eng = _Slow()
    with pytest.raises(ValueError) as ei:
        VB.embed_query_bounded(eng, "x", got)
    assert "NaN" in str(ei.value), "期望 join(NaN) 的 ValueError，实际 %r" % str(ei.value)


def test_NaN会抛穿_vector_search并被上层吞掉(monkeypatch, tmp_path):
    """把上一条的后果**端到端**钉住：NaN 下 `_vector_search` 要么**抛异常**（不是返回空）。

    ⚠️ 时序敏感（本判据第一版就红在这里，原因已查清、不是猜）：
    `_embed_query_bounded` 先 `_t.join(nan)`；若那次嵌入**已经在 NaN 生效前跑完**
    （`_t.is_alive()` 为 False），`join` 根本不抛、函数**正常返回向量** ⇒ 看不到 ValueError。
    所以本判据把嵌入故意拖到 **0.3s**（远长于 join 的瞬间返回），并**先断言线程必然还活着**
    这一段时序前提，再断言 NaN 的 ValueError 确实出现。
    """
    from trinity import Trinity
    from trinity.core.client._search import _get_embedding_engine

    store = tmp_path / "t68_nan_store"
    store.mkdir(parents=True, exist_ok=True)
    mem = Trinity(adapter="sqlite", store_path=str(store))
    for i in range(10):
        mem.ingest("t68 nan row %02d" % i, persona_id="t68", agent_id="t68")

    inner = _get_embedding_engine()

    class _SlowEnough:
        def embedding_dim(self):
            return inner.embedding_dim()

        def embed(self, text):
            time.sleep(0.3)          # 保证 join(nan) 时线程仍存活
            return inner.embed(text)

        def __getattr__(self, name):
            return getattr(inner, name)

    mem._embedding_engine = _SlowEnough()
    monkeypatch.setenv(ENV_Q, "NaN")
    # 查一次：要么 ValueError（NaN 抛穿），要么返回空列表（NaN 被吞成降级）。
    # 两者都是"通道不可用"，但**必须**是其中之一，绝不允许"正常返回向量"。
    try:
        res = mem._vector_search("t68 nan query", 10, account=False)
        outcome = ("returned", len(res or []))
    except ValueError as exc:
        outcome = ("ValueError", str(exc)[:60])
    assert outcome[0] in ("ValueError", "returned"), outcome
    if outcome[0] == "returned":
        assert outcome[1] == 0, (
            "NaN 上限下不得正常返回向量（那说明上限完全失效）：%r" % (outcome,))


def test_非法语料预算回落不嵌(monkeypatch):
    monkeypatch.setenv(ENV_C, "abc")
    assert VB.corpus_embed_budget() == 0
    monkeypatch.setenv(ENV_C, "")
    assert VB.corpus_embed_budget() == 0


def test_负数语料预算是显式回滚位而不是非法值(monkeypatch):
    """`-1` 是**有意**的不限（回滚到 2026-09-29 之前），不得被当成非法值回落成 0。"""
    monkeypatch.setenv(ENV_C, "-1")
    assert VB.corpus_embed_budget() == -1
    rows = [{"memory_id": "m"}]
    assert VB.select_rows(rows, -1) == rows


# ───────── 判据 3：预算生效 ⇒ 确实更早返回（**独立读数**交叉核对） ─────────
_LOG_SINK: list = []
_TRACE: list = []
_Q_SEQ = 0


def _bounded_search_returns_empty_when_embed_is_slow(monkeypatch, tmp_path, timeout_s, tag):
    """在**受控占用**下跑一次 `_vector_search`，返回 (墙钟ms, 结果条数, 告警句数)。

    独立读数（(b)）：`_search.py:1120` 的 `logger.warning` 是**与配置无关**的存在性信号
    —— 它只在"确实超时 ⇒ 降级"这条分支上发出。t58 的教训是"别拿被配置的读数当证据"，
    所以这里同时采**两个**互相独立的量：通道返回集（结构）+ 告警句（日志）。

    ⚠️ 两个**必须**的隔离（都是本判据第一版当场红出来的真原因，不是猜测）：
      1. **查询串必须每次唯一**：底层 `CachedEmbeddingEngine` 有**跨进程持久缓存**
         （启动打点实测 `embed cache 已加载 2588 条`）。固定查询串时会**命中缓存**
         ⇒ `_Slow.embed` 根本不被调用（实测 `slept=0`、`warned=0`、墙钟与预算无关）
         ⇒ 判据量到的是缓存命中，不是预算。加 `tag` 后查询串唯一，缓存必然未命中。
      2. **store 必须每次不同**：同一目录会带着上一次的 `_query_vec_cache` 与索引状态。
    """
    import logging
    from trinity import Trinity

    store = tmp_path / ("t68_caliber_store_" + str(tag))
    store.mkdir(parents=True, exist_ok=True)
    mem = Trinity(adapter="sqlite", store_path=str(store))
    for i in range(30):
        mem.ingest("t68 caliber row %02d unique text" % i,
                   persona_id="t68", agent_id="t68", tags=["t68"])

    class _Slow:
        """包装真引擎：**每一次** `embed` 都先睡 `timeout*3`，再委托给真引擎。

        为什么不是"第 1 次真算、之后才睡"（本判据第一版就是这么写的，错了）：
        `_vector_search` 里**唯一**的 `engine.embed(...)` 调用**就是查询嵌入本身**
        （`_search.py:1116`），没有"预热用掉第一次"这回事。
        于是"第 1 次真算"把**查询嵌入**变成了真算 ⇒ 预算永不生效、告警恒为 0
        （实测 `real=1 slept=0`）。现在每调用必睡 ⇒ 预算必然咬齿。

        代价：拿到的向量仍是真引擎算的（只是慢），所以**质量侧不受影响**，只动时间。
        """

        def __init__(self, inner):
            self._inner = inner
            self.real = 0
            self.slept = 0

        def embedding_dim(self):
            return self._inner.embedding_dim()

        def embed(self, text):
            self.slept += 1
            time.sleep(max(timeout_s, 0.05) * 3)
            self.real += 1
            return self._inner.embed(text)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    from trinity.core.client._search import _get_embedding_engine
    wrapped = _Slow(_get_embedding_engine())
    mem._embedding_engine = wrapped

    monkeypatch.setenv(ENV_Q, str(timeout_s))
    records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    lg = logging.getLogger("trinity.core.client")
    h = _Collect()
    lg.addHandler(h)
    # 查询串**唯一**（见 docstring 隔离 ①）：`tag` + 单调计数器 ⇒ 不可能命中持久嵌入缓存
    global _Q_SEQ
    _Q_SEQ += 1
    q = "t68 caliber query %s seq%d" % (tag, _Q_SEQ)
    # ⚠️ 隔离 ③：`tests/unit/conftest.py:28` 有 `logging.disable(logging.CRITICAL)`
    # ⇒ **进程级**抑制所有 < CRITICAL 的日志 ⇒ `_search.py:1120` 那条 `logger.warning`
    # 连 `emit()` 都不会进 ⇒ 独立读数 ② 恒为 0（判据第一版就是这样红掉的）。
    # 这里临时放开、并在 finally 里**还原**（不得改动 conftest 的全局约定）。
    _prev_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    try:
        t = time.perf_counter()
        res = mem._vector_search(q, 10, account=False)
        ms = (time.perf_counter() - t) * 1000
    finally:
        lg.removeHandler(h)
        logging.disable(_prev_disable)
    warned = sum(1 for m in records if "查询嵌入超过" in m)
    # 自证"这一档真的走了受控占用"：slept==0 ⇒ 嵌入命中缓存 ⇒ 本档无效，判据必须红
    _TRACE.append({"tag": tag, "timeout_s": timeout_s, "ms": round(ms, 2),
                   "n": len(res or []), "warned": warned,
                   "engine_real_calls": wrapped.real, "engine_slept_calls": wrapped.slept,
                   "query_cache_size": len(getattr(mem, "_query_vec_cache", {}) or {})})
    assert wrapped.slept >= 1, (
        "受控占用未被走到（slept=0）⇒ 嵌入命中了缓存，本档读数无效"
        "（tag=%s, real=%d, cache=%d）" % (tag, wrapped.real,
                                          len(getattr(mem, "_query_vec_cache", {}) or {})))
    # ⚠️ 独立读数 ② 的**正确**形态：不是"宽松档不许告警"（两档都必然超时，见下），
    # 而是"**告警句里报告的值就是这一档的预算值**"。这一条**不被本文件任何配置变量定义**，
    # 它来自 `_search.py:1120` 的 `%.1f` 格式化 ⇒ 是真正的独立读数（t58 的教训）。
    _reported = _reported_timeouts(records)
    _TRACE[-1]["reported_timeout_values_in_log"] = _reported
    return ms, len(res or []), warned, _reported


def _reported_timeouts(records):
    """从告警句里抽出"实际报告的超时秒数"（独立于本文件任何 env 配置）。"""
    out = []
    for m in records:
        mm = re.search(r"查询嵌入超过\s*([0-9.]+)s 未返回", m)
        if mm:
            out.append(float(mm.group(1)))
    return sorted(set(out))


def test_预算生效时确实更早返回(monkeypatch, tmp_path):
    """缩短预算 ⇒ 墙钟显著变短 **且** 通道降级 **且** 日志里报告的秒数=该档预算（独立读数）。"""
    ms_long, n_long, warn_long, rep_long = _bounded_search_returns_empty_when_embed_is_slow(
        monkeypatch, tmp_path, 0.6, tag="long")
    monkeypatch.undo()
    ms_short, n_short, warn_short, rep_short = _bounded_search_returns_empty_when_embed_is_slow(
        monkeypatch, tmp_path, 0.1, tag="short")

    # (a) 墙钟：缩短预算必须更早返回（留 2x 余量，避免机器抖动把判据打红）
    assert ms_short < ms_long, (
        "缩短预算后墙钟未变短：%.0fms → %.0fms" % (ms_long, ms_short))
    assert ms_short * 2 < ms_long, (
        "缩短预算的收益不足 2x（%.0fms vs %.0fms）⇒ 不构成「更早返回」" % (ms_long, ms_short))
    # (b) 独立读数 ①：通道返回集为空（= 降级；两档都超时，故都为空）
    assert n_short == 0, "短预算下向量通道应降级为空，实际 %d 条" % n_short
    assert n_long == 0, "长预算下同样超时（睡 1.8s > 0.6s），应也为空，实际 %d 条" % n_long
    # (b) 独立读数 ②：与配置无关的告警句必须出现（两档都超时 ⇒ 都应有）
    assert warn_short >= 1, "预算生效时未发出『查询嵌入超过』告警 —— 交叉核对失败"
    assert warn_long >= 1, "长预算档同样超时（睡 1.8s > 0.6s），应有告警"
    # (b) 独立读数 ③（**最强的一条**）：告警句里报告的秒数必须逐档等于该档预算。
    #     这个值由 `_search.py:1120` 的 `%.1f` 从**生效超时**格式化而来，
    #     不由本文件任何断言变量定义 ⇒ 证明"预算真的传到了生效路径"，而不是"我设的值回来了"。
    assert rep_short == [0.1], "短档告警报告的秒数应为 [0.1]，实际 %r" % (rep_short,)
    assert rep_long == [0.6], "长档告警报告的秒数应为 [0.6]，实际 %r" % (rep_long,)


# ───────── 判据 4：牙齿 —— 预算恒生效时判据 1 必须红 ─────────
def test_牙齿_预算恒生效时判据1必须红(monkeypatch):
    """构造反例：把请求侧预算改成"恒生效"（读成 0 ⇒ `_embed_query_bounded` 走 `<=0` ⇒ 不限）。

    `0` 的语义在**语料**旋钮里是"不嵌"，在**查询**旋钮里却会因为
    `if timeout_s <= 0: return engine.embed(text)` 变成**完全不设上限**。
    这正是"改错方向的牙齿"：一旦有人把查询预算误设成 0（或非法值回落成 0），
    请求路径就**永久失去上限**，而判据 1 的"默认等价"断言会当场变红。
    """
    # 现状：默认 3.0，判据 1 的等价性成立
    assert VB.query_embed_timeout_s() == 3.0
    assert VB.effective_query_embed_timeout_s() == VB.query_embed_timeout_s()

    # 牙齿：把预算设成 0（"恒生效"读法）⇒ 判据 1 的等价性必须**失败**
    monkeypatch.setenv(ENV_Q, "0")
    恒生效值 = VB.query_embed_timeout_s()
    assert 恒生效值 == 0.0
    判据1成立 = (恒生效值 == DEFAULTS[ENV_Q])
    assert 判据1成立 is False, (
        "牙齿失效：把预算设成恒生效（0）后，判据 1 的『默认等价』仍然通过 ⇒ 判据没有牙齿")

    # 且它确实会取消上限（`_embed_query_bounded` 的 `<=0` 分支）
    from trinity.core.client._vec_budget import embed_query_bounded

    class _Counting:
        def __init__(self):
            self.calls = 0

        def embed(self, text):
            self.calls += 1
            time.sleep(0.02)
            return [0.0, 1.0]

    eng = _Counting()
    t = time.perf_counter()
    out = embed_query_bounded(eng, "x", 0.0)
    ms = (time.perf_counter() - t) * 1000
    assert out == [0.0, 1.0] and eng.calls == 1
    assert ms >= 20.0, "0 预算应绕过墙钟（不限）⇒ 必须等满 20ms，实际 %.1fms" % ms


def test_牙齿_负预算也是不限(monkeypatch):
    """`<=0` 的另一半：负数同样取消上限（这是**有意**的回滚位，不是缺陷）。"""
    from trinity.core.client._vec_budget import embed_query_bounded

    class _C:
        def __init__(self):
            self.calls = 0

        def embed(self, text):
            self.calls += 1
            time.sleep(0.02)
            return [1.0]

    eng = _C()
    assert embed_query_bounded(eng, "x", -1.0) == [1.0]
    assert eng.calls == 1


# ───────── 附：`light` 路由与向量预算的相关性（只读，不写） ─────────
def test_light与full都经过同一条查询嵌入预算():
    """**只读源码**核对：`light` 与 `full` 分支都受 `_embed_query_bounded` 约束。

    依据（行号来自 `trinity/core/client/`）：
      · `_search.py:1114-1116` 是全仓**唯一**调用 `effective_query_embed_timeout_s()` 的地方，
        包住 `_embed_query_bounded(...)`；
      · 该函数被 `_hybrid_index.py:173`（嵌入通道）与 `:207`（PPR 语义种子，默认 semantic）
        调用 ⇒ `light` 与 `full` 走同一条预算。
    本判据只断言"这一行仍存在且仍读该函数"，防的是**被搬走/被旁路**。
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(os.path.dirname(here), "trinity", "core", "client", "_search.py")
    src = io.open(p, encoding="utf-8").read()
    assert "effective_query_embed_timeout_s" in src
    assert "_embed_query_bounded(self._embedding_engine, query, _to)" in src
    assert "TRINITY_QUERY_EMBED_TIMEOUT_S" in src
    hi = io.open(os.path.join(os.path.dirname(p), "_hybrid_index.py"),
                 encoding="utf-8").read()
    assert "self._vector_search(" in hi, "PPR/嵌入通道的调用点消失 ⇒ light 的预算相关性结论失效"


def test_默认值未被本任务改动():
    """把"我**没有**改默认值"变成可执行断言（任务书：默认行为零变化）。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(os.path.dirname(here), "trinity", "core", "client", "_vec_budget.py")
    src = io.open(p, encoding="utf-8").read()
    assert '"TRINITY_QUERY_EMBED_TIMEOUT_S", str(default)' in src
    assert '"TRINITY_QUERY_EMBED_TIMEOUT_WARM_S", "180"' in src
    assert '"TRINITY_VEC_CORPUS_EMBED_BUDGET", "0"' in src
    assert "def query_embed_timeout_s(default: float = 3.0)" in src
