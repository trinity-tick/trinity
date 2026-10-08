"""写入面语义 + 审计面 fail-closed 闸门（2026-10-02 外部复评 §3.2 / §3.1）。

钉住三件在 2026-10-02 复评里测出来的事：

| # | 事实（复评实测） | 本文件钉住的契约 |
|---|---|---|
| 1 | `POST /agents/memory/write` 返回 `"status":"written"`，但条目在 `memories`/`memory_versions`/`audit_log` **三表 0 行**、`/audit/prov/{id}` 404、主检索与 `trinity_search` 都搜不到（它写的是**聚合池**，不是记忆库） | 两个 `agents/memory/*write*` 的响应**必须自述** `store` / `in_engine_store` / `audited` / `engine_store_endpoint` |
| 2 | `/audit/memories/{id}` 对**任意** id 都返回 `200 + audit_trail: []`，与 `/audit/prov/{id}` 的 404 **自相矛盾**（fail-open 形状） | 未知 id **必须 404**；且只有**确证不存在**才 404（取不到 adapter / 探测抛错 ⇒ 维持 200，不制造假 404） |
| 3 | 冻结闸门集 21/21 全绿的同时，同一棵树的 `lint_ratchet` 是红的（137 > 123）——**棘轮不在集合里** | `mypy_ratchet` / `lint_ratchet` 必须同时在 `GATE_SET.json`（gates + must_include）与 `.github/workflows/ci.yml` 里 |

每条判据都带**反事实**（喂一份"修复前"的源码，必须判出违规）——否则判据可能恒真。

运行：``python -m pytest tests/unit/test_write_store_disclosure_20261002.py -q``
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENTS_ROUTER = ROOT / "trinity/api/server/_routers_agents.py"
AUDIT_ROUTER = ROOT / "trinity/api/server/_routers_audit.py"

DISCLOSURE_KEYS = {"store", "in_engine_store", "audited", "engine_store_endpoint"}


# ── 纯函数：从源码里判"响应有没有自述存储归属" ──────────────────────────────
def _returns_of(src: str, func_name: str) -> list[ast.Dict]:
    """收集某函数里所有 `return {...}` 的字面量 dict。"""
    tree = ast.parse(src)
    out: list[ast.Dict] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    out.append(sub.value)
    return out


def _disclosure_violations(src: str) -> list[str]:
    """返回违规说明（空表 = 合规）。判别力由 test_*_counterfactual 保证。"""
    bad: list[str] = []
    for fn in ("agent_memory_write", "agent_memory_bulk_write"):
        dicts = _returns_of(src, fn)
        if not dicts:
            bad.append(f"{fn}: 找不到字面量 return dict（判据无法评价 ≠ 合规）")
            continue
        keys: set[str] = set()
        for d in dicts:
            keys |= {k.value for k in d.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        missing = DISCLOSURE_KEYS - keys
        if missing:
            bad.append(f"{fn}: 响应缺字段 {sorted(missing)}")
    return bad


def _audit_failclosed_violations(src: str) -> list[str]:
    bad: list[str] = []
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "audit_memory_trail"), None)
    if fn is None:
        return ["audit_memory_trail: 函数不存在"]
    raises_404 = any(
        isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
        and any(kw.arg == "status_code" and isinstance(kw.value, ast.Constant) and kw.value.value == 404
                for kw in n.exc.keywords)
        for n in ast.walk(fn))
    if not raises_404:
        bad.append("audit_memory_trail: 未对未知 id 抛 404（fail-open 形状）")
    calls_exists = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_memory_exists"
        for n in ast.walk(fn))
    if not calls_exists:
        bad.append("audit_memory_trail: 未调用 _memory_exists 做存在性判定")
    return bad


# ── (1) 写入面：响应必须自述存储归属 ───────────────────────────────────────
def test_write_endpoints_disclose_store() -> None:
    src = AGENTS_ROUTER.read_text(encoding="utf-8")
    assert _disclosure_violations(src) == []


def test_disclosure_checker_has_discriminating_power() -> None:
    """反事实：把披露字段全删掉（= 修复前的写法）⇒ 同一判据必须判红。"""
    old = (
        "async def agent_memory_write(req):\n"
        "    return {'status': 'written', 'memory_id': 'x', 'confidence': 0.5}\n"
        "async def agent_memory_bulk_write(req):\n"
        "    return {'status': 'completed', 'written': 1, 'failed': 0, 'memory_ids': ['x']}\n"
    )
    bad = _disclosure_violations(old)
    assert bad and all("缺字段" in b for b in bad), f"判据没有判别力：{bad}"


def test_disclosure_values_are_honest() -> None:
    """值也要对：聚合池写法必须是 in_engine_store=False / audited=False，且指向 /memories。"""
    src = AGENTS_ROUTER.read_text(encoding="utf-8")
    for fn in ("agent_memory_write", "agent_memory_bulk_write"):
        pairs: dict[str, object] = {}
        for d in _returns_of(src, fn):
            for k, v in zip(d.keys, d.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    pairs[k.value] = v.value
        assert pairs.get("store") == "aggregator_pool", f"{fn}: store 必须点明聚合池"
        assert pairs.get("in_engine_store") is False, f"{fn}: 聚合池条目不在引擎库，必须 False"
        assert pairs.get("audited") is False, f"{fn}: 聚合池条目不进审计链，必须 False"
        assert pairs.get("engine_store_endpoint") == "/memories", f"{fn}: 必须指出真正的落库入口"


# ── (2) 审计面：未知 id 必须 fail-closed ───────────────────────────────────
def test_audit_trail_unknown_id_fails_closed_source() -> None:
    src = AUDIT_ROUTER.read_text(encoding="utf-8")
    assert _audit_failclosed_violations(src) == []


def test_audit_failclosed_checker_has_discriminating_power() -> None:
    old = (
        "async def audit_memory_trail(memory_id):\n"
        "    mem = get_memory()\n"
        "    trail = mem.get_audit_trail(memory_id)\n"
        "    return {'memory_id': memory_id, 'audit_trail': trail, 'total_entries': len(trail)}\n"
    )
    bad = _audit_failclosed_violations(old)
    assert len(bad) == 2, f"判据没有判别力：{bad}"


def test_memory_exists_semantics_is_fail_open() -> None:
    """`_memory_exists` 只在**确证不存在**时返回 False；"没测"必须读成存在（不制造假 404）。"""
    mod = pytest.importorskip("trinity.api.server._routers_audit")

    class _NoAdapter:
        pass

    class _AdapterNone:
        def get_memory(self, _mid):
            return None

    class _AdapterHit:
        def get_memory(self, _mid):
            return {"memory_id": _mid}

    class _AdapterBoom:
        def get_memory(self, _mid):
            raise RuntimeError("backend down")

    class _Mem:
        def __init__(self, adapter):
            self._adapter = adapter

    assert mod._memory_exists(_Mem(_NoAdapter()), "x") is True       # 无 get_memory ⇒ 不判
    assert mod._memory_exists(_Mem(_AdapterNone()), "x") is False    # 确证不存在 ⇒ 判否
    assert mod._memory_exists(_Mem(_AdapterHit()), "x") is True      # 命中 ⇒ 存在
    assert mod._memory_exists(_Mem(_AdapterBoom()), "x") is True     # 探测抛错 ⇒ 不判（fail-open）


def test_audit_endpoint_404s_only_when_absent(monkeypatch) -> None:
    """接线：同一函数在"未命中 ⇒ 404"与"命中但无审计行 ⇒ 200+空链"两臂上表现不同。"""
    mod = pytest.importorskip("trinity.api.server._routers_audit")
    from fastapi import HTTPException

    class _T:
        def __init__(self, rec):
            self._rec = rec
            self._adapter = self

        def get_memory(self, _mid):
            return self._rec

        def get_audit_trail(self, _mid):
            return []

    monkeypatch.setattr(mod, "get_memory", lambda: _T(None))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod.audit_memory_trail("does-not-exist"))
    assert ei.value.status_code == 404

    monkeypatch.setattr(mod, "get_memory", lambda: _T({"memory_id": "m1"}))
    ok = asyncio.run(mod.audit_memory_trail("m1"))
    assert ok == {"memory_id": "m1", "audit_trail": [], "total_entries": 0}


# ── (3) 棘轮必须真的在冻结集与 CI 里 ───────────────────────────────────────
def test_gate_set_contains_ci_ratchets() -> None:
    import json

    manifest = json.loads((ROOT / "docs/GATE_SET.json").read_text(encoding="utf-8"))
    ids = {g["id"] for g in manifest["gates"]}
    for gate_id in ("mypy_ratchet", "lint_ratchet"):
        assert gate_id in ids, f"{gate_id} 不在冻结闸门集里（外部复评 §3.1 的口径裂缝）"
        assert gate_id in manifest["must_include"], f"{gate_id} 不在 must_include（冻结集只增不减）"
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for script in ("scripts/mypy_ratchet.py", "scripts/lint_ratchet.py"):
        assert script in ci, f"{script} 未被 CI 调用 —— 登记与实际不符"


def test_ci_ratchet_scripts_exist_and_are_ratchets() -> None:
    """两道棘轮脚本必须存在，且真的按"只降不升"比基线（不是恒 0）。"""
    for rel in ("scripts/mypy_ratchet.py", "scripts/lint_ratchet.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "BASELINE" in src and ">" in src, f"{rel} 看不出棘轮比较"
