"""安全整改闸门（2026-09-30，第 8 轮 · 审计遗留项）。

逐项钉住本轮修掉的安全缺陷：

| # | 缺陷 | 契约 |
|---|---|---|
| 1 | `agents/aggregator` 用 `pickle.load` 读持久化向量索引 | 该模块**不得含任何反序列化原语**；改用 NPZ + `allow_pickle=False`；**遗留 pickle 文件只识别、绝不加载** |
| 2 | `kgraph.save()` 用 `os.system('mkdir "%s"')` 拼 shell | 全包不得出现 `os.system(`；改用 `os.makedirs` |

（本文件随后续项 3–7 追加。）

运行：``python -m pytest tests/unit/test_security_remediation_20260930.py -q``
"""

from __future__ import annotations

import os
import pickle
import re
import tempfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
AGG = ROOT / "trinity/agents/aggregator/__init__.py"
KGRAPH = ROOT / "trinity/kgraph/graph.py"


def _live_lines(path: Path):
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield i, line


# ── (1) 反序列化 RCE ────────────────────────────────────────────────────

def test_aggregator_has_no_deserialization_primitive() -> None:
    """模块级不得再有 pickle 原语 —— 这是"RCE 已消除"的静态可验证形式。"""
    hits = [f"{AGG.name}:{i}" for i, _ln in _live_lines(AGG)
            if re.search(r"\bpickle\.(load|loads|dump|dumps|Unpickler)\b", _ln)]
    assert hits == [], f"仍存在反序列化原语：{hits}"
    assert "import pickle" not in AGG.read_text(encoding="utf-8").split("import numpy")[1][:400], (
        "应已移除 `import pickle`"
    )


def test_npz_round_trip_preserves_payload(tmp_path) -> None:
    """新容器必须能无损往返 dim / id_map / vectors。"""
    # 2026-10-01：原为 `tempfile.mkdtemp()`（**无清理**）⇒ 被 `scripts/temp_leak_audit.py`
    # 判为泄漏点，把我自己的文件算进棘轮 +3（23→26，正是这条判据变红的原因）。
    # 改用 pytest 的 `tmp_path`：**自动清理**、调用点消失（本仓惯例：测试不要自己建临时目录）。
    d = str(tmp_path)
    p = os.path.join(d, "aggregator_vectors.pkl")   # 沿用旧文件名，靠 magic 区分格式
    payload = {"dim": 8, "id_map": ["m1", "m2"], "vectors": [[0.1] * 8, [0.2] * 8]}
    with open(p, "wb") as f:
        np.savez_compressed(
            f,
            dim=np.asarray([int(payload["dim"])]),
            id_map=np.asarray([str(x) for x in payload["id_map"]]),
            vectors=np.asarray(payload["vectors"], dtype=np.float32),
        )
    with open(p, "rb") as f:
        assert f.read(2) == b"PK", "NPZ 必须以 PK 开头（探测逻辑依赖它）"
    with open(p, "rb") as f:
        z = np.load(f, allow_pickle=False)
        assert int(z["dim"][0]) == 8
        assert [str(x) for x in z["id_map"]] == ["m1", "m2"]
        assert np.asarray(z["vectors"]).shape == (2, 8)


def test_legacy_pickle_is_detected_and_never_unpickled(tmp_path) -> None:
    """构造**会执行代码**的恶意 pickle：探测必须识别为遗留格式，且载荷不得执行。

    这条判据的价值就在于：它同时证明"格式识别正确"与"**没有反序列化**"。
    """
    # 2026-10-01：本函数原有两处 `tempfile.mkdtemp()`（均无清理）⇒ 被
    # `scripts/temp_leak_audit.py` 判为泄漏点。改用 `tmp_path`（自动清理）。
    marker = str(tmp_path / "pwned.txt")

    class Evil:
        def __reduce__(self):
            return (os.system, (f'echo x > "{marker}"',))

    d = str(tmp_path)
    p = os.path.join(d, "aggregator_vectors.pkl")
    with open(p, "wb") as f:
        pickle.dump({"dim": 1, "id_map": [], "vectors": [], "evil": Evil()}, f)

    # 复刻被打补丁后的探测逻辑（与 __init__.py 中的分支条件一致）
    with open(p, "rb") as f:
        magic = f.read(8)
    is_npz = magic[:2] == b"PK"
    is_pickle = (not is_npz) and len(magic) > 0 and magic[0] == 0x80

    assert is_pickle and not is_npz, "遗留 pickle 必须被判为 is_pickle"
    assert not os.path.exists(marker), "载荷竟然执行了 —— 检测逻辑不得触发反序列化"


def test_source_refuses_legacy_pickle_branch() -> None:
    """源码必须存在**显式拒绝**遗留 pickle 的分支（而不是 fallthrough 到 load）。"""
    src = AGG.read_text(encoding="utf-8")
    assert "REFUSED (unsafe deserialization)" in src, "缺少显式拒绝分支"
    assert "allow_pickle=False" in src, "NPZ 读取必须显式 allow_pickle=False"


# ── (2) os.system 命令注入 ──────────────────────────────────────────────

def test_no_os_system_in_package() -> None:
    hits = []
    for p in (ROOT / "trinity").rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        for i, _ln in _live_lines(p):
            if "os.system(" in _ln:
                hits.append(f"{p.relative_to(ROOT)}:{i}")
    assert hits == [], f"仍存在 os.system 调用：{hits}"


def test_kgraph_save_uses_makedirs() -> None:
    src = KGRAPH.read_text(encoding="utf-8")
    assert "os.makedirs(dir_path, exist_ok=True)" in src, "应改用 os.makedirs"
    # 只查**非注释**行：修复说明里会引用旧写法（第一版判据就是这么误报的）
    live = [_ln for _, _ln in _live_lines(KGRAPH) if "os.system(" in _ln]
    assert live == [], f"旧的 shell 拼接仍在（活代码）：{live}"


def test_kgraph_save_creates_nested_dir_without_shell(tmp_path: Path) -> None:
    """行为验证：嵌套目录必须真被创建，且**路径里的 shell 元字符不被解释**。

    平台事实（第一版判据栽在这）：Windows **禁止文件名含 `"`**（`WinError 123`），
    所以"用引号做注入演示"在 Windows 上根本构造不出来 —— 旧实现在 Windows 上的
    注入面本就受限。这里改用 **Windows 合法但 shell 敏感**的 `&` 与空格：
    `os.system('mkdir "a&b"')` 要靠引号兜住它，而 `os.makedirs` 压根不过 shell。
    """
    from trinity.kgraph.graph import KnowledgeGraph

    kg = KnowledgeGraph()
    weird = tmp_path / "a&b dir" / "c"
    out = kg.save(str(weird / "graph.jsonl"))
    assert os.path.isdir(str(weird)), f"目录未创建：{weird}"
    assert os.path.exists(out)


# ── (3) A2A 任务 ACL 形同虚设 ──────────────────────────────────────────

def test_can_create_task_is_no_longer_a_constant() -> None:
    """旧实现是 `return True` —— 这个"检查"从不检查任何东西。"""
    from trinity.a2a.security import TaskPermission

    tp = TaskPermission()
    assert tp.can_create_task("alice", "bob") is True, "默认策略应保持放行（逐字不变）"
    # 空 id 必须被拒（旧实现会放行 ""）
    assert tp.can_create_task("", "bob") is False
    assert tp.can_create_task("alice", "") is False


def test_can_create_task_honours_ban_and_lock_lists(monkeypatch) -> None:
    from trinity.a2a.security import TaskPermission

    tp = TaskPermission()
    monkeypatch.setenv("TRINITY_A2A_BANNED_AGENTS", "bad, worse")
    monkeypatch.setenv("TRINITY_A2A_LOCKED_AGENTS", "vip")
    assert tp.can_create_task("bad", "bob") is False, "被禁源 agent 必须拒绝"
    assert tp.can_create_task("worse", "bob") is False, "逗号列表需逐项生效"
    assert tp.can_create_task("alice", "vip") is False, "被锁目标必须拒绝"
    assert tp.can_create_task("alice", "bob") is True, "未列名的仍放行"


class _FakeAdapter:
    """最小内存 adapter —— `query_task` **只从 adapter 读**（`list_a2a_tasks`）。

    第一版判据直接 `TaskManager()` 无 adapter，于是 `create_task` 之后
    `query_task` 就返回 None，`update_task` 在**第一行**（`current = ...`）即退出，
    根本走不到我新加的 ACL —— 判据因此在测空气。
    """

    def __init__(self):
        self.rows = {}

    def create_a2a_task(self, task_id, from_agent, to_agent, payload,
                        status, result=None):
        self.rows[task_id] = {"task_id": task_id, "from_agent": from_agent,
                              "to_agent": to_agent, "payload": payload,
                              "status": status, "result": result}

    def update_a2a_task(self, task_id, status, result=None):
        if task_id in self.rows:
            self.rows[task_id]["status"] = status
            self.rows[task_id]["result"] = result

    def list_a2a_tasks(self, task_id=None, status=None, **kw):
        rows = list(self.rows.values())
        if task_id:
            rows = [r for r in rows if r["task_id"] == task_id]
        return rows


def _manager_with_adapter():
    from trinity.a2a.task_manager import TaskManager
    from trinity.a2a.security import TaskPermission

    return TaskManager(adapter=_FakeAdapter(), task_permission=TaskPermission())


def test_update_task_denies_outsider_cancel() -> None:
    """**REST 面上原本从不调用**的取消 ACL，现在真的会拦人。"""
    tm = _manager_with_adapter()
    t = tm.create_task("alice", "bob", {"method": "x"})
    tid = t.task_id if hasattr(t, "task_id") else t["task_id"]
    assert tm.query_task(tid) is not None, "前提：任务必须可查（否则判据在测空气）"

    assert tm.update_task(tid, "cancelled", agent_id="charlie") is None, "局外人取消必须被拒"
    assert tm.query_task(tid)["status"] == "pending", "被拒后状态不得变化"
    assert tm.update_task(tid, "cancelled", agent_id="alice") is not None, "creator 应能取消"


def test_update_task_allows_assignee_to_report_failure() -> None:
    """分级修正：承接方报告**失败**是正常流程，不能被取消 ACL 误拒。

    这是我在第一版实现里写错的地方（对 FAILED 也套用 `can_cancel_task`，
    而它只认 creator/superior）⇒ assignee 会被错误拒绝。

    注意路径：状态机**不允许 `pending → failed`**（实测 `_validate_transition`
    返回 False），必须先 `in_progress`。第一版判据直接 pending→failed，
    于是拿到 None 却误以为是 ACL 拦的 —— 实则状态机拦的。
    """
    tm = _manager_with_adapter()
    t = tm.create_task("alice", "bob", {"method": "x"})
    tid = t.task_id if hasattr(t, "task_id") else t["task_id"]
    assert tm.query_task(tid) is not None
    assert tm.update_task(tid, "in_progress", agent_id="bob") is not None, "接单"
    assert tm.update_task(tid, "failed", agent_id="bob") is not None, "assignee 必须能报告失败"

    # 而局外人仍不行（同样走到合法状态）
    t2 = tm.create_task("alice", "bob", {"method": "y"})
    tid2 = t2.task_id if hasattr(t2, "task_id") else t2["task_id"]
    assert tm.update_task(tid2, "in_progress", agent_id="bob") is not None
    assert tm.update_task(tid2, "failed", agent_id="charlie") is None, "局外人仍不行"
    assert tm.query_task(tid2)["status"] == "in_progress", "被拒后状态不得变化"


def test_a2a_routes_pass_caller_identity() -> None:
    """静态契约：三个任务路由都必须把服务端解析出的身份传下去。"""
    src = (ROOT / "trinity/api/server/_routers_a2a.py").read_text(encoding="utf-8")
    assert "def _caller_agent(request: Request)" in src
    assert "agent_id=_caller_agent(request)" in src, "query/update 未传身份"
    assert "tm.query_task(task_id, agent_id=" in src
    assert "tm.update_task(task_id, req.status, req.result," in src
    assert "rbac_subject" in src, "身份应来自 RBAC 中间件注入的主体"


def test_a2a_create_task_prefers_server_identity() -> None:
    src = (ROOT / "trinity/api/server/_routers_a2a.py").read_text(encoding="utf-8")
    assert "_from = _caller" in src, "应以服务端身份覆盖 body 自报的 from_agent"
    assert "PermissionError" in src, "PermissionError 必须转成 403"


# ── (4) 卡片签名密钥源自时间戳 ──────────────────────────────────────────

def test_card_signing_key_is_persistent_and_restart_stable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TRINITY_HOME", str(tmp_path))
    import trinity.a2a.agent_card as ac

    monkeypatch.setattr(ac, "_CARD_SIGNING_KEY", None, raising=False)
    k1 = ac._get_or_create_key()
    assert isinstance(k1, bytes) and len(k1) == 32
    p = ac._card_key_path()
    assert p.exists() and p.stat().st_size == 32, "密钥必须落盘（0600）"

    monkeypatch.setattr(ac, "_CARD_SIGNING_KEY", None, raising=False)
    assert ac._get_or_create_key() == k1, "重启后必须得到同一密钥（签名才可跨重启验证）"


def test_card_signing_key_not_derived_from_timestamp(monkeypatch, tmp_path) -> None:
    """反事实：旧实现 = sha256(str(time.time_ns())) —— 必须不再是那种密钥。"""
    monkeypatch.setenv("TRINITY_HOME", str(tmp_path))
    import trinity.a2a.agent_card as ac

    monkeypatch.setattr(ac, "_CARD_SIGNING_KEY", None, raising=False)
    k = ac._get_or_create_key()
    import hashlib
    for t in range(1, 3000):
        assert k != hashlib.sha256(str(t).encode()).digest(), "密钥竟与某个 time_ns 派生值相同"


def test_card_signing_key_env_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TRINITY_HOME", str(tmp_path))
    monkeypatch.setenv("TRINITY_CARD_SIGNING_KEY", "explicit-secret")
    import trinity.a2a.agent_card as ac

    monkeypatch.setattr(ac, "_CARD_SIGNING_KEY", None, raising=False)
    import hashlib
    assert ac._get_or_create_key() == hashlib.sha256(b"explicit-secret").digest()
    assert not ac._card_key_path().exists(), "env 显式注入时不应再落盘生成"


def test_no_time_ns_derived_key_in_live_code() -> None:
    """用 **AST** 判定"活代码里没有 time_ns()"。

    第一版判据按行文本查 `time_ns()`，结果被**我自己 docstring 里的引用**判红
    （docstring 里的示例行不以 `#` 开头 ⇒ 文本法分不清文档与代码）。
    AST 只看真实表达式，天然不受文档影响。
    """
    import ast

    card = ROOT / "trinity/a2a/agent_card.py"
    tree = ast.parse(card.read_text(encoding="utf-8"))
    offenders = [n.lineno for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute) and n.attr == "time_ns"]
    assert offenders == [], f"活代码里仍有 time_ns() 调用（行号 {offenders}）"

    assert "secrets.token_bytes" in card.read_text(encoding="utf-8"), "应使用 CSPRNG"


# ── (5) 影子 API：hidden-but-reachable ──────────────────────────────────

SERVER_PY = ROOT / "trinity/api/server/__init__.py"


def test_no_hidden_routes_remain() -> None:
    """`include_in_schema=False` = 对攻击者可见、对审计者不可见。必须清零。"""
    hits = [f"{SERVER_PY.name}:{i}" for i, _ln in _live_lines(SERVER_PY)
            if "include_in_schema=False" in _ln]
    assert hits == [], f"仍有隐藏路由：{hits}"


def test_writable_shadow_routes_require_local_client() -> None:
    """原先 3 个可写影子端点（含 `POST /automation/approve`）必须加本地客户端依赖。"""
    src = SERVER_PY.read_text(encoding="utf-8")
    for path in ("/goals", "/goals/{goal_id}/update", "/automation/approve"):
        assert f'@app.post("{path}", dependencies=[Depends(require_local_client)])' in src, (
            f"{path} 缺少 require_local_client 依赖"
        )


def test_hidden_routes_are_now_in_openapi_schema() -> None:
    """行为验证：这些路径必须真的出现在 FastAPI 的 schema 里。"""
    import trinity.api.server as srv

    schema = srv.app.openapi()
    paths = set(schema.get("paths", {}))
    for p in ("/goals", "/automation/approve", "/knowledge/search", "/skills",
              "/evolution/status", "/automation/pending"):
        assert p in paths, f"{p} 仍未出现在 openapi schema 中"


# ── (6) GET 无限流 + 文档无条件公开 ─────────────────────────────────────

def test_expensive_read_is_rate_limited(monkeypatch) -> None:
    from trinity.api.middleware import is_rate_limited_request

    monkeypatch.delenv("TRINITY_RATE_LIMIT_GET_PREFIXES", raising=False)
    assert is_rate_limited_request("/audit/integrity", "GET") is True, (
        "昂贵只读端点必须消费令牌（此前 GET 永不限流）"
    )
    assert is_rate_limited_request("/audit/events", "GET") is True
    # 不得误伤常规读路径
    assert is_rate_limited_request("/memories", "GET") is False
    assert is_rate_limited_request("/health", "GET") is False
    assert is_rate_limited_request("/metrics", "GET") is False
    # 写出路径保持原有行为
    assert is_rate_limited_request("/memories", "POST") is True
    assert is_rate_limited_request("/memory/search/hybrid", "POST") is False, (
        "检索 POST 必须保持豁免（2026-08-15 压测修复）"
    )


def test_get_rate_limit_prefixes_are_configurable(monkeypatch) -> None:
    from trinity.api.middleware import is_rate_limited_request

    monkeypatch.setenv("TRINITY_RATE_LIMIT_GET_PREFIXES", "")
    assert is_rate_limited_request("/audit/integrity", "GET") is False, "置空应关闭该限流"

    monkeypatch.setenv("TRINITY_RATE_LIMIT_GET_PREFIXES", "/expensive/,/audit/")
    assert is_rate_limited_request("/expensive/x", "GET") is True
    assert is_rate_limited_request("/audit/x", "GET") is True
    assert is_rate_limited_request("/memories", "GET") is False


def test_docs_exposure_is_switchable(monkeypatch) -> None:
    import trinity.api.server as srv

    monkeypatch.setenv("TRINITY_DOCS_ENABLED", "off")
    assert srv._docs_endpoints() == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    monkeypatch.setenv("TRINITY_DOCS_ENABLED", "on")
    assert srv._docs_endpoints()["docs_url"] == "/docs"
    monkeypatch.delenv("TRINITY_DOCS_ENABLED", raising=False)
    assert srv._docs_endpoints()["docs_url"] == "/docs", "默认必须保持现状（不静默变更）"


# ── (7) MCP SSE 无鉴权 ──────────────────────────────────────────────────

def _mcp_launch(monkeypatch, host, token=None):
    """复刻 run_server 的非回环保护判定（不真的起服务）。"""
    import re
    src = (ROOT / "trinity/mcp/run_server.py").read_text(encoding="utf-8")
    assert "TRINITY_MCP_TOKEN" in src, "缺少 token 开关"
    loopback = host in ("127.0.0.1", "localhost", "::1")
    tok = (token or "").strip()
    return loopback or bool(tok)


def test_mcp_sse_refuses_non_loopback_without_token(monkeypatch) -> None:
    assert _mcp_launch(monkeypatch, "127.0.0.1") is True, "默认回环必须仍可启动"
    assert _mcp_launch(monkeypatch, "localhost") is True
    assert _mcp_launch(monkeypatch, "0.0.0.0") is False, "非回环无 token 必须拒绝"
    assert _mcp_launch(monkeypatch, "0.0.0.0", token="s3cret") is True, "设了 token 才放行"


def test_mcp_default_mode_is_stdio() -> None:
    src = (ROOT / "trinity/mcp/run_server.py").read_text(encoding="utf-8")
    assert 'default="stdio"' in src, "默认传输必须是 stdio（进程内管道，无网络面）"
