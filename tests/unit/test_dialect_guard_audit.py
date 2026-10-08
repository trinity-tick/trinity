# -*- coding: utf-8 -*-
"""方言盲能力守卫：检测判据的判据（T14，2026-10-06）。

## 治什么

```python
if hasattr(adapter, "_get_conn"):     # 只在上 PGAdapter 为真
    ...
else:
    return []                          # SQLite 上静默走这里：什么都没做
```

`_get_conn()` **只存在於 `PostgreSQLAdapter`**；`SQLiteAdapter` 只有 `_conn` /
`_get_read_conn` ⇒ 按**后端能力名**做的守卫在另一个后端上恒假 ⇒ 分支根本不进。
它不是"异常被吞"（驱动层没吞异常，裸 sqlite3 跑同一句会抛）——是**分支根本没进**。

## 本文件为什么存在

只修那 10 处而不加判据，第 11 处还会被写出来。故：
① 检测逻辑在 `scripts/dialect_guard_audit.py`（可单独跑）；
② 本文件把"**真实仓必须为 0 违规**"钉住 ⇒ 任何人新增一个方言盲守卫，这里立刻红；
③ 每个方向的**负向实测**都在下面，包含**改动前的真实源码**（`git show HEAD:`）。

运行：``python -m pytest tests/unit/test_dialect_guard_audit.py -q``
"""
from __future__ import annotations

import importlib.util
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "scripts", "dialect_guard_audit.py")


def _module():
    spec = importlib.util.spec_from_file_location("_dialect_guard_audit", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _scan(src: str, name: str = "synth.py"):
    return _module().scan_source(name, src)


# ── ① 真实仓必须 0 违规 ────────────────────────────────────────────────

def test_真实仓不得有方言盲守卫():
    m = _module()
    rep = m.audit()
    assert rep["count"] == 0, (
        "发现方言盲能力守卫（按后端能力名 hasattr + 能力缺失分支静默早退 / 直连硬编码后端）：\n  "
        + "\n  ".join("%s:%d [%s] %s" % (h["file"], h["line"], h["kind"], h["why"][:80])
                      for h in rep["findings"])
        + "\n⇒ 三选一：①改用 `trinity._tags._conn_ctx`；②保持 no-op 但响亮"
          "（logger/raise/计数）；③登记 declared-unsupported 并给出理由")


def test_扫描面不能是空的():
    """防"因为什么都没扫所以 0 违规"这种假绿。"""
    m = _module()
    files = list(m._iter_py(os.path.join(ROOT, "trinity")))
    assert len(files) > 300, "只扫到 %d 个文件 —— 判据已失去覆盖面" % len(files)
    assert m.BACKEND_SPECIFIC_CAPS, "能力名白名单空了"


# ── ② 负向实测：方言盲形态必须被判红 ──────────────────────────────────

_BAD_SILENT_GUARD = '''def f(adapter, doc, limit=6):
    try:
        if not hasattr(adapter, "_get_conn"):
            return []
        with adapter._get_conn() as conn:
            cur = conn.cursor()
    except Exception:
        return []
    return rows
'''

_BAD_ELSE_HARDCODED = '''def g(adapter, ids):
    if adapter is not None and hasattr(adapter, "_get_conn"):
        conn = adapter._get_conn()
    else:
        conn = _shared_conn()
    return conn
'''

_BAD_OR_NOT_HASATTR = '''def w(adapter):
    if adapter is None or not hasattr(adapter, "_get_conn"):
        return []
    return [1]
'''

_BAD_PSYCOPG2 = '''def k(adapter):
    if not hasattr(adapter, "_get_conn"):
        conn = psycopg2.connect(host="127.0.0.1", port=5432)
    return conn
'''

_GOOD_NATIVE = '''def s(adapter, ids):
    if adapter is None:
        conn = _shared_conn()
    elif hasattr(adapter, "_get_conn"):
        conn = adapter._get_conn()
    else:
        native = _native(adapter, ids)
        if native is not None:
            return native
        _UNSUPPORTED[type(adapter).__name__] = 1
        logger.error("unsupported")
        return 0
    return conn
'''

_GOOD_LOUD = '''def p(adapter):
    if not hasattr(adapter, "_get_conn"):
        logger.warning("adapter %s 无 _get_conn", type(adapter).__name__)
        return []
    return [1]
'''

_GOOD_ERROR_DICT = '''def r(adapter):
    if not hasattr(adapter, "_get_conn"):
        return {"error": "needs PG adapter"}
    return {}
'''


@pytest.mark.parametrize("name,src,kind", [
    ("silent_early_exit", _BAD_SILENT_GUARD, "silent-early-exit"),
    ("else_hardcoded_conn", _BAD_ELSE_HARDCODED, "hardcoded-conn"),
    ("or_not_hasattr_silent", _BAD_OR_NOT_HASATTR, "silent-early-exit"),
    ("psycopg2_direct", _BAD_PSYCOPG2, "hardcoded-conn"),
])
def test_负向实测_方言盲形态必须判红(name, src, kind):
    found = _scan(src, "bad_%s.py" % name)
    assert found, "方言盲形态 %s 没有被抓到 ⇒ 判据没有牙齿" % name
    assert kind in {f["kind"] for f in found}, (name, found)


@pytest.mark.parametrize("name,src", [
    ("native_then_loud", _GOOD_NATIVE),
    ("loud_logger", _GOOD_LOUD),
    ("loud_error_dict", _GOOD_ERROR_DICT),
])
def test_反向_已正确处置的写法不得被误报(name, src):
    found = _scan(src, "good_%s.py" % name)
    assert not found, "正确写法 %s 被误报：%r" % (name, found)


# ── ③ 拿**改动前的真实源码**再压一次（最硬的一次负向实测）───────────────

_PREFIX_FILES = [
    ("trinity/brain/access_touch.py", "hardcoded-conn"),
    ("trinity/brain/hebbian_links.py", "hardcoded-conn"),
    ("trinity/retrieval/doc_router.py", "silent-early-exit"),
]


@pytest.mark.parametrize("rel,kind", _PREFIX_FILES)
def test_负向实测_改动前的真实源码必须被判红(rel, kind):
    """`git show HEAD:<path>` 取回 T14 改动前的真实源码 ⇒ 必须报出对应形态。

    这条证明两件事：① 判据抓得住**真实代码**（不只是合成样本）；
    ② T14 的修复确实把这一类从仓里清掉了（因为当前文件是 0 违规）。
    """
    try:
        src = subprocess.run(["git", "show", "HEAD:%s" % rel], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", check=True).stdout
    except Exception as exc:  # noqa: BLE001
        pytest.skip("取不到 HEAD 版本（非 git 工作区）：%s" % exc)
    found = _scan(src, "HEAD_%s" % rel.replace("/", "_"))
    assert found, "改动前的 %s 没有被判红 ⇒ 判据抓不住真实代码" % rel
    assert kind in {f["kind"] for f in found}, (rel, [f["kind"] for f in found])
