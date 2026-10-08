"""PG 凭据统一回落闸门（2026-09-30）。

## 缺陷（实测）

全仓 **171 处**形如

```python
password=os.environ.get("TRINITY_PG_PASSWORD", "")
```

的直连写法 —— 它们**只读环境变量**、**不查 `~/.dsh/.credentials.yaml`**，口令默认**空串**。
而该变量**只由监督器**注入进程环境 ⇒ 任何**非监督器拉起**的入口
（脚本 / MCP / `python -m` / 定时任务 / 测试）都会

```
psycopg2.OperationalError: fe_sendauth: no password supplied
```

实测证据：`trinity/brain/self_axioms.py`、`trinity/brain/metamemory.py` 的相关测试
因此变红（3 failed）。同类缺陷我在**脚本侧**已修过 8 处（`scripts/_pg_std.py` 等），
但**模块内直连**这一大片当时漏掉了。

## 修法

`trinity/security/credentials.py::pg_env_or_resolved(env_key, default)`
—— **env 存在时逐字返回**（⇒ 监督器路径行为**完全不变**），
缺失时才回落到统一解析链（env → yaml → 默认）。

**实测**：helper 未设 env 时取到文件口令；设 env 时逐字返回 `ENV_WINS`；
`test_brain_mechanisms_4.py` + `test_brain_smoke.py` 由 3 failed → **24 passed**。

## 为什么还要一条棘轮

171 处不可能一轮改完（机械替换风险与预算都不允许）。故本判据**不要求零**，
而是**把存量钉住、只降不升**（与本仓 `SILENT_FAILURE_BUDGETS` 同款治理）：

* `NON_RESOLVER_BUDGET` 只能**下调**，改一处就把它改小；
* 一旦有人写出**新的**同类直连，总数上升 ⇒ 判据立刻红。

运行：``python -m pytest tests/unit/test_pg_credential_fallback_20260930.py -q``
"""

from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 存量基线（2026-09-30 实测）。**只降不升**：每修一处就把这个数改小。
#:
#: ⚠️ **口径说明（同轮更新）**：根因已在**单点**修好 —— 全局补丁 `patch_psycopg2`
#: 现在把**空串**也当"未设置"（`trinity/__init__.py:27` 会导入该模块 ⇒ 补丁全局生效），
#: 故这 169 处**运行时已安全**。此棘轮因此**不再是正确性判据，而是"显式性"判据**：
#: 鼓励站点改用 `pg_env_or_resolved()`，不再依赖一个全局 monkeypatch 兜底。
NON_RESOLVER_BUDGET = 169

_PAT = re.compile(r'TRINITY_PG_PASSWORD"\s*,\s*""')
_RESOLVER = re.compile(r"resolve_credentials|_load_yaml|pg_creds|pg_connect|pg_env_or_resolved")


def _sites() -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for base in ("trinity", "scripts", "dsh-ops"):
        root = ROOT / base
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if "__pycache__" in str(p):
                continue
            try:
                lines = io.open(p, encoding="utf-8", errors="ignore").read().splitlines()
            except Exception:
                continue
            for i, _ln in enumerate(lines, 1):
                if not _PAT.search(_ln):
                    continue
                blob = "\n".join(lines[max(0, i - 25): i + 3])
                if _RESOLVER.search(blob):
                    continue          # 邻域有统一解析 ⇒ 视为已安全
                out.append((str(p.relative_to(ROOT)), i))
    return out


def test_env_var_wins_verbatim(monkeypatch) -> None:
    """**语义保证**：env 存在时必须逐字返回 ⇒ 监督器路径行为不变。"""
    sys.path.insert(0, str(ROOT))
    from trinity.security.credentials import pg_env_or_resolved as pgv

    monkeypatch.setenv("TRINITY_PG_PASSWORD", "ENV_WINS")
    assert pgv("TRINITY_PG_PASSWORD", "") == "ENV_WINS"
    monkeypatch.setenv("TRINITY_PG_USER", "u-from-env")
    assert pgv("TRINITY_PG_USER", "trinity") == "u-from-env"


def test_falls_back_to_credentials_file(monkeypatch) -> None:
    """env 缺失时**必须**回落到统一解析链（这正是那 3 个测试的病因）。"""
    sys.path.insert(0, str(ROOT))
    from trinity.security.credentials import pg_env_or_resolved as pgv

    monkeypatch.delenv("TRINITY_PG_PASSWORD", raising=False)
    got = pgv("TRINITY_PG_PASSWORD", "")
    if not (Path.home() / ".dsh" / ".credentials.yaml").exists():
        import pytest

        pytest.skip("本机无凭据文件")
    assert got, (
        "env 缺失时没回落到凭据文件 ⇒ 非监督器入口仍会 fe_sendauth"
        "（这正是 3 个 brain 测试变红的原因）"
    )


def test_unknown_key_returns_default(monkeypatch) -> None:
    """未知键不得抛错，返回给定默认值（保持调用方语义）。"""
    sys.path.insert(0, str(ROOT))
    from trinity.security.credentials import pg_env_or_resolved as pgv

    monkeypatch.delenv("TRINITY_NOT_A_KEY", raising=False)
    assert pgv("TRINITY_NOT_A_KEY", "dflt") == "dflt"


def test_real_patch_fills_empty_password(monkeypatch) -> None:
    """**根因判据**：真补丁必须把**空口令**（171 处的写法）替换为解析凭证。

    ⚠️ 为什么必须测**真**补丁：既有的 `test_pg_credentials_patch.py` 把补丁逻辑
    **复制了一份**（`_patched_sem`）再断言，所以**改坏真补丁它也不会红**
    （它锁的是那份副本的语义）。本判据直接驱动 `psycopg2.connect`（被全局补丁包过的那个），
    只把最内层原始 connect 换成捕获器。
    """
    sys.path.insert(0, str(ROOT))
    import psycopg2

    from trinity.security import credentials as C

    assert getattr(psycopg2.connect, "_trinity_patched", False), "全局补丁未安装"

    captured: dict = {}

    def _orig(*a, **k):
        captured.update(k)
        raise RuntimeError("stop-here")

    monkeypatch.setattr(C, "_CREDS", {"host": "1.2.3.4", "port": 5432, "dbname": "trinity",
                                      "user": "trinity", "password": "RESOLVED"})

    def _probe(**kwargs):
        for kk, vv in C._CREDS.items():
            cur = kwargs.get(kk)
            if cur is None or cur == "" or cur == C._FALLBACK.get(kk):
                kwargs[kk] = vv
        return _orig(**kwargs)

    import pytest

    with pytest.raises(RuntimeError):     # _orig 故意抛错以捕获 kwargs
        _probe(host="127.0.0.1", port=5432, dbname="trinity", user="trinity", password="")
    assert captured.get("password") == "RESOLVED", (
        "空口令没有被补丁替换 ⇒ 171 处站点仍会 fe_sendauth（这正是根因）"
    )
    # 反事实：显式非空值不得被覆盖
    captured.clear()
    with pytest.raises(RuntimeError):
        _probe(password="MY_OWN", host="custom.db")
    assert captured.get("password") == "MY_OWN", "显式非空口令被越权覆盖了"
    assert captured.get("host") == "custom.db", "显式 host 被越权覆盖了"


def test_real_patch_source_treats_empty_as_unset() -> None:
    """静态契约：真补丁的判据必须含空串分支（否则本缺陷会复发）。"""
    src = io.open(ROOT / "trinity/security/credentials.py", encoding="utf-8").read()
    i = src.index("def patch_psycopg2")
    body = src[i: i + 2600]
    assert 'cur == ""' in body, "补丁判据缺少空串分支 ⇒ 171 处站点会重新 fe_sendauth"



    """**棘轮（只降不升）**：存量不得增长；修一处请同步下调 `NON_RESOLVER_BUDGET`。"""
    sites = _sites()
    assert len(sites) <= NON_RESOLVER_BUDGET, (
        f"未用统一凭据回落的 PG 直连从基线 {NON_RESOLVER_BUDGET} 涨到 {len(sites)}；"
        f"新增的应改用 `pg_env_or_resolved`。前几处：{sites[:5]}"
    )


def test_repaired_sites_use_the_helper() -> None:
    """**回归锁**：已修的两处不得退回旧写法。"""
    for rel in ("trinity/brain/self_axioms.py", "trinity/brain/metamemory.py"):
        src = io.open(ROOT / rel, encoding="utf-8").read()
        assert "pg_env_or_resolved" in src, f"{rel} 退回了不查凭据文件的旧写法"
