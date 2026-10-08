# -*- coding: utf-8 -*-
"""判据：**索引重建不得在检索请求路径上做**（2026-09-29，用户授权 ①）。

## 实测来由（本判据要钉住的那次事故）

| 读数 | 值 |
|---|---|
| `data/aggregator_vectors.pkl` | 只覆盖 **1/19,347** 条 ⇒ 索引恒空 |
| 一次查询（并发窗口） | **221,159 ms** 返回 |
| 同查询、**单客户端** | **302,002 ms 后连接被服务器重置** |
| 监督器处置 | `UNHEALTHY beyond grace` ⇒ kill + restart（19:39:23 / 19:47:41） |
| 重建本身 | 19k 条 × 0.15–0.30 s/行 ⇒ **25–97 分钟** |

根因是 `_vector.py::vector_search` 在请求路径里同步调用 `_rebuild_index()`：
索引一旦为空，**每个查询**都赌一次 25–97 分钟的重活。

## 本文件断言四件事

1. **行为**：池非空 + 索引空 ⇒ 返回 `[]`、`_rebuild_index` **一次都没被调用**，
   且状态必须是 `index_rebuilding`（不是 `no_index`：这是可恢复降级，不是"没内容"）。
2. **反事实**：`TRINITY_AGG_INLINE_REBUILD=on` ⇒ 旧行为**真的**回来（证明开关不是装饰）。
3. **硬上限**：查询嵌入必须**有界** —— 慢嵌入器 ⇒ 返回 `[]` + `query_embed_timeout`，
   而不是无限等（超时线程仍在后台跑完，这点在实现里已如实记录）。
4. **不误伤**：池为空 / 就绪但无内容时，既有 A2 三态语义不变
   （`test_degradation_honesty.py` 覆盖，本文件只做交叉确认）。

判据用**行为断言**（调用计数 + 状态），不用源码字符串 —— 本仓已被"文本窗口骗"
三次（§24.2/§32.6），其中一次就是命中了注释里逐字写的旧代码。
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import numpy as np
import pytest

from trinity.agents.aggregator import _vector as _vector_mod
from trinity.agents.aggregator._vector import _VectorMixin

#: 进程级事实（装没装 faiss 决定 `_search_locked` 走哪条互斥分支）——
#: 判据必须按它喂对应的替身，否则"同一份测试在两个解释器上结论不同"（见下）。
_HAS_FAISS = bool(getattr(_vector_mod, "_HAS_FAISS", False))

_DIM = 8


class _Stub(_VectorMixin):
    """最小聚合器替身：只装配 vector_search 用到的字段。

    `rebuild_calls` 是**成本代理**：真身这一次调用 = 19k 条 embedding。
    """

    def __init__(self, *, ready: bool = True, pool=None, index=None, id_map=None,
                 embed_delay_s: float = 0.0):
        self._lock = threading.RLock()
        self._faiss_index = index
        self._index_id_map = id_map if id_map is not None else []
        self._pool = pool if pool is not None else {}
        self._tracer = None
        self._embedding_ready = threading.Event()
        if ready:
            self._embedding_ready.set()
        self._dim = _DIM
        self.rebuild_calls = 0
        self._embed_delay_s = embed_delay_s

    def _start_warmup(self):
        return None

    def _get_embedding_fn(self):
        def _fn(_t):
            if self._embed_delay_s:
                time.sleep(self._embed_delay_s)
            return np.zeros(_DIM, dtype=np.float32)
        return _fn

    def _rebuild_index(self):
        self.rebuild_calls += 1


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例从"生产默认"开始，避免用例间 env 泄漏（本仓已踩过进程级标记的坑）。"""
    for _k in ("TRINITY_AGG_INLINE_REBUILD", "TRINITY_AGG_QUERY_EMBED_TIMEOUT_S",
               "TRINITY_AGG_PREWARM_REBUILD"):
        monkeypatch.delenv(_k, raising=False)
    yield


def _pool_of(n: int):
    """池非空即可 —— 真身只看 `bool(self._pool)`。"""
    return {f"m{i}": object() for i in range(n)}


class Test请求路径不得重建:
    def test_索引空时不重建_且如实降级为_index_rebuilding(self):
        a = _Stub(pool=_pool_of(3))
        out = a.vector_search("任意查询", top_k=5)

        assert out == [], "索引为空时返回空是既有行为（由其它通道兜底）"
        assert a.rebuild_calls == 0, (
            "**请求路径调用了 _rebuild_index** —— 这正是 221–302 s 挂死 + 被健康守卫 kill 的根因；"
            "生产池是 19,347 条 ⇒ 这一次调用等于 25–97 分钟的重活"
        )
        st = a.vector_search_status()
        assert st.get("reason") == "index_rebuilding", (
            "必须把'索引重建中（可恢复）'与'确实没有可检索内容'区分开，"
            "实际 reason=%r" % (st.get("reason"),)
        )

    def test_反事实_开关打开时旧行为真的回来(self, monkeypatch):
        monkeypatch.setenv("TRINITY_AGG_INLINE_REBUILD", "on")
        a = _Stub(pool=_pool_of(3))
        a.vector_search("任意查询", top_k=5)
        assert a.rebuild_calls >= 1, (
            "TRINITY_AGG_INLINE_REBUILD=on 必须恢复'请求路径按需重建' —— "
            "开关不能是装饰（本仓纪律：开关 + 可回滚，且回滚要实测）。"
            "（真身的重试循环最多调 2 次，故断言 >=1）"
        )

    def test_索引非空时照常返回_不受本改动影响(self):
        """索引就绪 ⇒ 正常返回候选（本改动只动"索引为空"那一路）。

        2026-10-06（测试归因轮 T1）：原实现只喂一个**仿 faiss** 的替身（只有
        `.search()`）。而 `_search_locked` 有**两条**互斥分支（`_HAS_FAISS` 决定）：

            faiss 可用 ⇒ `self._faiss_index.search(...)`；
            无 faiss   ⇒ 把 `self._faiss_index` 当 (N, dim) **ndarray** 走 numpy 余弦。

        于是同一个替身"装 faiss 的解释器上绿、不装的（`.venv`）红"，且失败现场是
        `TypeError: loop of ufunc does not support argument 0 of type _FakeIdx` ——
        看起来像生产缺陷，实际是**判据只覆盖了一条分支**。
        现按进程级事实 `_HAS_FAISS` 分别喂**该分支真正会拿到的东西**：
        两条分支都被钉住，且在两种解释器上结论一致（不依赖装没装 faiss）。
        """
        if _HAS_FAISS:
            class _FakeIdx:
                """faiss 可用时 `_search_locked` 走 `.search()`；用最小替身喂确定结果。"""

                def search(self, _qv, k):
                    return (np.array([[0.9, 0.8]], dtype=np.float32),
                            np.array([[0, 1]], dtype=np.int64))

            index = _FakeIdx()
        else:
            # 无 faiss ⇒ 生产路径把索引对象当 (N, dim) 数组用（`_vector.py:1017-1021`）
            index = np.array([[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                              [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]], dtype=np.float32)

        a = _Stub(pool=_pool_of(3), index=index, id_map=["a", "b"])

        out = a.vector_search("任意查询", top_k=2)
        assert len(out) == 2, "索引就绪时必须正常返回候选（本改动只动'索引为空'那一路）"
        assert a.rebuild_calls == 0

    def test_池为空时不重建_且reason仍是no_index(self):
        """不误伤：池为空 ⇒ 没有任何可重建的东西，语义仍是 A2 的 no_index。"""
        a = _Stub(pool={})
        a.vector_search("任意查询", top_k=5)
        assert a.rebuild_calls == 0
        assert a.vector_search_status().get("reason") == "no_index"


class Test进程内后台重建默认关闭:
    """第二半：即使不在请求栈上，进程内全量重建也会占满串行嵌入器（三项症状见实现注释）。"""

    def test_默认不后台重建(self, monkeypatch):
        a = _Stub(pool=_pool_of(3))
        a._prewarm_ann_index()
        assert a.rebuild_calls == 0, (
            "默认（TRINITY_AGG_PREWARM_REBUILD 未设）不得在进程内跑全量重建 —— "
            "它会占满串行嵌入器，使请求侧 fn(query) 排队（实测 221–302 s），"
            "并让重启后的 /health 60 s 无响应而被健康守卫 kill"
        )

    def test_反事实_开启后真的重建(self, monkeypatch):
        monkeypatch.setenv("TRINITY_AGG_PREWARM_REBUILD", "on")
        a = _Stub(pool=_pool_of(3))
        a._prewarm_ann_index()
        assert a.rebuild_calls >= 1, (
            "TRINITY_AGG_PREWARM_REBUILD=on 必须恢复进程内预热重建 —— 开关不能是装饰"
        )


class Test查询嵌入必须有硬上限:
    def test_慢嵌入器不得把检索拖死(self, monkeypatch):
        monkeypatch.setenv("TRINITY_AGG_QUERY_EMBED_TIMEOUT_S", "0.2")
        a = _Stub(pool=_pool_of(3), embed_delay_s=5.0)

        t0 = time.time()
        out = a.vector_search("任意查询", top_k=5)
        elapsed = time.time() - t0

        assert out == []
        assert elapsed < 2.0, (
            "查询嵌入超时后必须**立刻**返回；实际耗时 %.2fs —— "
            "墙钟上限没生效（这正是 221–302 s 那条路径）" % elapsed
        )
        assert a.vector_search_status().get("reason") == "query_embed_timeout", (
            "超时必须有名有姓，否则又是静默降级：%r" % (a.vector_search_status(),)
        )

    def test_快嵌入器不受影响(self, monkeypatch):
        """防"一律报警"式假阳性：正常嵌入不得被误判成超时。"""
        monkeypatch.setenv("TRINITY_AGG_QUERY_EMBED_TIMEOUT_S", "5")
        a = _Stub(pool=_pool_of(3))
        a.vector_search("任意查询", top_k=5)
        assert a.vector_search_status().get("reason") != "query_embed_timeout"

    def test_上限为0时回到改动前行为(self, monkeypatch):
        """可回滚：0 = 不限（本仓纪律：每个新默认都要有一行回滚）。"""
        monkeypatch.setenv("TRINITY_AGG_QUERY_EMBED_TIMEOUT_S", "0")
        a = _Stub(pool=_pool_of(3), embed_delay_s=0.3)
        t0 = time.time()
        a.vector_search("任意查询", top_k=5)
        assert time.time() - t0 >= 0.3, "上限=0 时必须真的等待（证明开关生效）"
