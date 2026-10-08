"""Unit test fixtures for Trinity v8.0 identity / audit / a2a packages."""

import os
import sys
import tempfile
import logging

import pytest

# 2026-10-06（t18）：**测试进程一律不得改写生产语料缓存**
# `~/.trinity/data/corpus_vec.*` —— 它由 `trinity.core.client._corpus_persist` 在
# 检索触发到脏计数阈值时落盘，而全仓有多个测试会跑真 `search_hybrid`。
# 只在自己的测试文件里设这个开关**不够**（实测：一次 228 用例的批量跑仍会把它写掉，
# 见 ACCESS-COUNT-SINGLE-COUNT.md §12.6），必须在这里设一次覆盖整个 tests/unit。
#
# ⚠️ 2026-10-06（t21，verifier 抓到的**可绕过口**）：原来写的是 `setdefault` ——
#   而 `dsh-ops/trinity-supervisor.ps1:247` 会设 `TRINITY_CORPUS_INDEX_PERSIST=1`
#   ⇒ **supervisor 环境里跑测试，守卫被静默绕过**。我实测复现：
#   外部预设 =1 + 跑一批跑真 hybrid 的测试 ⇒ 缓存 manifest mtime 18:52:57 → **19:32:42**
#   （size 135 → 136）。⇒ 改为**硬赋值**（外部值一律覆盖）。
# 若某个测试**确实**要测"落盘发生"，必须在**它自己**的用例里显式 opt-in
#   （`monkeypatch.setenv(...)` 在 conftest 之后执行、仍然生效），
#   判据见 `tests/unit/test_corpus_index_persist_switch_20261006.py`。
os.environ["TRINITY_CORPUS_INDEX_PERSIST"] = "0"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

logging.disable(logging.CRITICAL)


@pytest.fixture
def adapter():
    """Isolated SQLite adapter on a temporary database file."""
    from trinity.adapters.sqlite import SQLiteAdapter

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    a = SQLiteAdapter(db_path=db_path)
    a.connect()
    yield a
    a.disconnect()
    if os.path.exists(db_path):
        os.unlink(db_path)
    wal = db_path + "-wal"
    shm = db_path + "-shm"
    for f in (wal, shm):
        if os.path.exists(f):
            os.unlink(f)


@pytest.fixture
def identity_manager(adapter):
    """IdentityManager backed by an isolated SQLite DB."""
    from trinity.identity.identity_manager import IdentityManager

    return IdentityManager(storage_adapter=adapter)


@pytest.fixture
def auditor():
    """Auditor instance with fresh metrics."""
    from trinity.audit.auditor import Auditor

    return Auditor(adapter=None)


@pytest.fixture
def task_manager(adapter):
    """TaskManager backed by an isolated SQLite DB."""
    from trinity.a2a.task_manager import TaskManager

    return TaskManager(adapter=adapter)


@pytest.fixture
def capability_registry():
    """CapabilityRegistry with in-memory cache, no persistence adapter."""
    from trinity.a2a.capability_registry import CapabilityRegistry

    return CapabilityRegistry(adapter=None)


@pytest.fixture(autouse=True)
def _isolate_trinity_home(tmp_path_factory, monkeypatch):
    """整目录单测的 `TRINITY_HOME` 隔离（2026-09-22 §1247）。

    **实测来由**：`test_pool_status_sync.py` 在**真实环境**上构造 `MemoryAggregator`
    ⇒ 加载生产池（71MB 级 / 万条以上）并在**请求路径**里同步做全量 ANN 重建
    （分块 64 条 × 每次 HTTP 上限 30s ⇒ 上百次 Ollama 往返）。全线程快照（§1246）显示
    事件循环线程与 3 个 `agg-ann-prewarm` 全堵在 `requests.post`；
    **同一命令、只把 `TRINITY_HOME` 换成临时目录** ⇒ **7 passed in 4.19s**（此前 >300s 超时）。

    为什么提升到整目录：`tests/unit` 里有 **9 个文件**会构造 `MemoryAggregator`
    （`test_ann_prewarm` / `test_r5_reserves` / `test_rl_feedback_loop` / `test_graph_channel` …），
    每一个都可能把生产池与模型服务拖进单测。

    边界：这是**默认隔离**，不是强制 —— 确实需要真实环境的用例设
    `TRINITY_TEST_REAL_HOME=1` 即可退出（本仓纪律：开关 + 可回滚）。
    """
    if os.environ.get("TRINITY_TEST_REAL_HOME") == "1":
        yield
        return
    monkeypatch.setenv("TRINITY_HOME", str(tmp_path_factory.mktemp("trinity_home")))
    yield


@pytest.fixture(autouse=True)
def _stop_aggregator_background_threads():
    """测试收尾停掉 `MemoryAggregator` 起的**常驻**后台线程（2026-09-22 §1247）。

    实测（§1246 全线程快照，同一次卡死里数出来的）：一个测试进程里同时有
    **4+ 个 `agg-cleanup`**、3 个 `agg-ann-prewarm`。前者是
    `while not self._stop_cleanup.wait(N)` 的**常驻循环** —— 每构造一个聚合器就多一个，
    从不退出；后者是一次性线程，但会在 Ollama 上排队（那正是 §1246 卡死的现场）。

    聚合器目前**没有** close()/shutdown() API，所以这里只做两件**安全**的事：
      · 置 `_stop_cleanup` ⇒ cleanup 循环退出（重复置位幂等）；
      · `cancel()` 掉 `_persist_timer`（否则测试结束后定时器还会继续落盘）。
    用 `gc` 找实例是**不得已**：加一个实例注册表属于运行时改动，留待有人拍板再做
    （本仓纪律：动运行时先快照 + `verify_noninvasive.py` 证明非侵入）。
    """
    yield
    try:
        import gc

        from trinity.agents.aggregator import MemoryAggregator
    except Exception:  # noqa: BLE001 —— 收集失败不许影响测试结论
        return
    for obj in gc.get_objects():
        try:
            if not isinstance(obj, MemoryAggregator):
                continue
            ev = getattr(obj, "_stop_cleanup", None)
            if ev is not None:
                ev.set()
            timer = getattr(obj, "_persist_timer", None)
            if timer is not None:
                timer.cancel()
        except Exception:  # noqa: BLE001
            continue
