# -*- coding: utf-8 -*-
"""t50/G9 判据：**6 条裸 SQL 直写路径**都必须过 PII 守卫（"所有 PII 都掩码"的前提）。

每条判据都是**运行时**的：用**假 PG 连接**捕获真正发给 `INSERT INTO memories` 的参数
（`a2a_memory` 用真临时 SQLite）。判据形状统一为"**捕获到的任何 SQL 参数里不得出现原始 PII**"，
并且**每条都带负向**：`TRINITY_ADAPTER_GUARD=0` ⇒ 原始 PII **必须**重新出现（证明它在测守卫）。
另有**顺序**判据：守卫调用必须在 `sha256`/加密/INSERT **之前**（G7 确立的不变量）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SITES = {
    "a2a": "trinity/a2a_memory.py",
    "memory_manager": "trinity/brain/memory_manager.py",
    "memory_transaction": "trinity/brain/memory_transaction.py",
    "actor": "trinity/cognition/actor.py",
    "vms_pg": "trinity/vms/backends/postgres_backend.py",
    "perceive": "trinity/api/server/_routers_brain.py",
}

# ⚠️ 样本必须用**真实域**：`example.com` 是 RFC 2606 占位域 ⇒ **有意不掩**，
# 用它当样本会得出"邮箱不被掩"的错误结论（t50 原版就踩了这个坑，见下面的历史段）。
PII = "联系人 13800138000，邮箱 zhang@huice.com，卡号 4111111111111111。"
NO_PII = "今天读了一本书，讲分布式一致性，收获不小。"

#: **策略层真正会掩的**标识符（G2 的 9 类里手机号/银行卡号走校验门，确定性最高）。
#: ⚠️ **邮箱不在其中** —— 这是 **G2 策略层的既有空洞**（G3-R3 / G8-R2 已登记：本仓
#: `example.com` 这类地址**不会被掩**，客户端路径也一样）。本文件**显式承认**该空洞（见
#: `test_已知空洞_邮箱在策略层不被掩_t50`），而**不是**把断言放宽到看不见它 ——
#: 修它属 `trinity/security/**`（G2 写域），t50 无权改、已在报告里作为阻断项登记。
MASKABLE = ("13800138000", "4111111111111111", "zhang@huice.com")
# ↑ t50 更正：把**真实域邮箱**也纳入"必须被掩"的集合（原版只断言手机/卡号，因为样本误用了占位域）⇒ 六处判据**更强**了，不是放宽。
RAW = MASKABLE + ("zhangsan@example.com",)

def _raw_pii_in(strings, needles=RAW) -> list:
    out = []
    for s in strings:
        for p in needles:
            if isinstance(s, str) and p in s:
                out.append(p)
    return sorted(set(out))


# ── 假 PG：捕获所有 execute 的 SQL 与参数 ────────────────────────────────────
class _FakePG:
    def __init__(self, fetchall_rows=None, fetchone=(0,)):
        self.log = []
        self._rows = fetchall_rows or []
        self._one = fetchone

    def connect(self, *a, **k):
        outer = self

        class _Cur:
            def execute(self, sql, params=None):
                outer.log.append((sql, params))

            def fetchall(self):
                return outer._rows

            def fetchone(self):
                return outer._one

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Conn:
            autocommit = True

            def cursor(self):
                return _Cur()

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        return _Conn()

    def strings(self) -> list:
        out = []
        for _sql, params in self.log:
            if params is None:
                continue
            if isinstance(params, (list, tuple)):
                out.extend(p for p in params if isinstance(p, str))
            elif isinstance(params, str):
                out.append(params)
        return out

    def memory_insert_strings(self) -> list:
        """**只取 `INSERT INTO memories` 的参数** —— 判据必须限定到落记忆表的那一条。

        为什么必须限定（t50 实测到的边界）：`_routers_brain.memory_perceive` 会**先**写
        `memories`（已掩码）、**再**写 `perceptions`。而 `perceptions` 表按**代码里的明确设计**
        保持明文（`:322-327` 注释：situation_stream 等按明文读）⇒ 若把"任何 SQL 参数"都算进来，
        就会把这条**有意为之**的设计当成"守卫没接上"。⇒ 判据限定 memories 表，
        同时对 perceptions 的明文**单独显式断言**（见 `test_perceive的perceptions表按设计保留明文_t50`）。
        """
        out = []
        for sql, params in self.log:
            if "INSERT INTO MEMORIES" not in (sql or "").upper().replace("  ", " "):
                continue
            if params is None:
                continue
            if isinstance(params, (list, tuple)):
                out.extend(p for p in params if isinstance(p, str))
            elif isinstance(params, str):
                out.append(params)
        return out


def _install_fake_pg(monkeypatch, rows=None):
    import psycopg2

    fake = _FakePG(fetchall_rows=rows)
    monkeypatch.setattr(psycopg2, "connect", fake.connect)
    return fake


# ── ① a2a_memory（真临时 SQLite）────────────────────────────────────────────
def _drive_a2a(tmp_path, content):
    from trinity.a2a_memory import AdapterMemoryStore, create_memory_entry
    from trinity.adapters.sqlite import SQLiteAdapter

    db = str(tmp_path / "a2a.db")
    ad = SQLiteAdapter(db_path=db)
    ad.connect()
    store = AdapterMemoryStore(ad)
    entry = create_memory_entry(content=content, persona_id="p1", source_agent="t50")
    store._upsert_adapter(entry)                      # 私有但有代表性：裸 SQL 镜像路径
    got = (ad.get_memory(entry.memory_id) or {}).get("content")
    return store, got


# ── 驱动函数：每个返回"若干被写入的字符串" ─────────────────────────────────
def _drive_memory_manager(monkeypatch, content):
    from trinity.brain import memory_manager as mm

    fake = _install_fake_pg(monkeypatch, rows=[("s1", [{"content": content, "importance": 0.9}])])
    mm.promote_working_memory(min_importance=0.5, max_promote=3)
    return fake.memory_insert_strings()


def _drive_memory_transaction(monkeypatch, tmp_path, content):
    from trinity.brain import memory_transaction as tx

    monkeypatch.setattr(tx, "STATE_FILE", str(tmp_path / "txn.json"))
    fake = _install_fake_pg(monkeypatch)      # begin() 也要连库（`SELECT count(*)`）
    tx.self_rollback()
    tx.begin()                                # `write()` 要求有活跃事务（否则 `_apply` 返回 False）
    tx.write(content, "txn")
    tx.commit()
    return fake.memory_insert_strings()


def _drive_actor(monkeypatch, content):
    from trinity.cognition import actor as ac

    fake = _install_fake_pg(monkeypatch)
    monkeypatch.setattr(ac, "_retrieve", lambda *a, **k: [])
    monkeypatch.setattr(ac, "_skills", lambda *a, **k: [])
    monkeypatch.setattr(ac, "_selfcheck", lambda *a, **k: {"confidence": 0.9, "level": "ok"})
    monkeypatch.setattr(ac, "llm_chat", lambda *a, **k: content)
    ac.execute("t50 探针目标")
    return fake.memory_insert_strings()


def _drive_vms_pg(monkeypatch, content):
    from trinity.vms.backends.postgres_backend import PostgresBackend

    fake = _install_fake_pg(monkeypatch)
    be = object.__new__(PostgresBackend)
    be._conn = fake.connect()
    be.add(content=content, agent_id="t50", category="general")
    return fake.memory_insert_strings()


def _drive_perceive(monkeypatch, content):
    from trinity.api.server import _routers_brain as rb
    from trinity.brain import perception as perc

    fake = _install_fake_pg(monkeypatch)

    class _Eng:
        def evaluate(self, channel, signal, importance):
            # `memory_perceive` 用到 ev 的四个键（habituation/repeat 也是，缺一个就 KeyError）
            return {"salience": 0.9, "importance": 0.8, "habituation": 0.1, "repeat": 1}

        def should_encode(self, salience):
            return True

    monkeypatch.setattr(perc, "get_perception_engine", lambda: _Eng())
    rb.memory_perceive(channel="t50", signal=content, importance=0.8, session_id="s1", image=None)
    return fake.memory_insert_strings()


# ── 判据主体：6 处，每处三件事（掩码 / 反事实 / 关守卫⇒明文）────────────────
@pytest.mark.parametrize("site", ["memory_manager", "memory_transaction", "actor", "vms_pg", "perceive"])
def test_裸SQL五处_含PII必须掩码_无PII逐字不变_关守卫则明文_t50(site, monkeypatch, tmp_path):
    drivers = {
        "memory_manager": lambda mp, c: _drive_memory_manager(mp, c),
        "memory_transaction": lambda mp, c: _drive_memory_transaction(mp, tmp_path, c),
        "actor": lambda mp, c: _drive_actor(mp, c),
        "vms_pg": lambda mp, c: _drive_vms_pg(mp, c),
        "perceive": lambda mp, c: _drive_perceive(mp, c),
    }
    drive = drivers[site]
    leaked_on = _raw_pii_in(drive(monkeypatch, PII), MASKABLE)
    assert drive(monkeypatch, PII) and leaked_on == [], (
        "%s：含 PII 的写入里仍出现**策略层会掩的原始标识符** %r（守卫没接上或接晚了）"
        % (site, leaked_on))    # 反事实：无 PII 不许改写
    fake = _FakePG()
    hits = [s for s in drive(monkeypatch, NO_PII) if NO_PII in s or NO_PII[:12] in s]
    assert hits, "%s：无 PII 的文本没按原样进入 SQL 参数（被改写了？）" % site
    # 负向（牙齿）：关掉守卫 ⇒ 原始 PII 必须重新出现
    monkeypatch.setenv("TRINITY_ADAPTER_GUARD", "0")
    leaked = _raw_pii_in(drive(monkeypatch, PII), MASKABLE)
    assert leaked, "%s：`TRINITY_ADAPTER_GUARD=0` 后原始标识符竟然没出现 ⇒ 本判据没在测守卫" % site
    assert fake is not None


def test_裸SQL一处_含PII必须掩码_无PII逐字不变_关守卫则明文_a2a_t50(tmp_path, monkeypatch):
    _store, stored = _drive_a2a(tmp_path, PII)
    assert stored and _raw_pii_in([stored], MASKABLE) == [], \
        "a2a 镜像写入仍落**策略层会掩的原始标识符**：%r" % stored

    _s2, stored2 = _drive_a2a(tmp_path, NO_PII)
    assert stored2 == NO_PII, "a2a：无 PII 的文本被改写了：%r" % stored2

    monkeypatch.setenv("TRINITY_ADAPTER_GUARD", "0")
    _s3, stored3 = _drive_a2a(tmp_path, PII)
    assert _raw_pii_in([stored3], MASKABLE), (
        "a2a：关掉守卫后原始标识符竟然没出现 ⇒ 判据空转：%r" % stored3)


# ── 📌 历史（原位保留，不要删）：我原来在这里写了一条**错误**判据 ─────────────
# `test_已知空洞_邮箱在策略层不被掩_t50` 断言"策略层对 `example.com` **不掩邮箱**"，并据此在
# 报告 §8 申报了一条**阻断项**、把责任归到 G2 写域。
#
# ❌ **该结论已被队长实测推翻**（我随后独立复现，逐域对照一致）：
#      用例                scan_pii        redact_identifiers
#      真实域 huice.com    ['邮箱']        z***@***.com      ✅ 被掩
#      真实域 qq.com       ['邮箱']        z***@***.com      ✅ 被掩
#      占位域 example.com   []              zhangsan@example.com  **有意不掩**（RFC 2606/6761）
#    ⇒ **策略层确实掩邮箱**；不被掩的**只有占位域**。
#    **我的错法（本轮同一族）**：① 拿**占位域** `example.com` 当样本；
#    ② 用了 `scan_sensitive().flagged`（那是**类别**面，不是 PII 类别）而不是 `scan_pii()` 的 kinds
#    ⇒ **从单一条件推广**成"策略层根本不掩邮箱"，并据此申报阻断项。
#    ⇒ 下面改成**两条互为反事实**的判据（与 G2 的 C9/C10 同构）：**必须两条都在**。


def test_占位域邮箱有意不掩_依据RFC2606_6761_t50():
    """**占位域**（RFC 2606/6761）**有意不掩** —— 它们是文档/测试用域，掩了反而干扰测试与示例。

    依据：`127.0.0.1` 之外的保留域名（`example.com/.net/.org`、`.test/.invalid/.localhost`、
    `local`/`internal`/`corp`）在 G2 的策略里被显式豁免（verifier 独立复现：全量 27,859 条里
    **被拦域只有 `example.com` ×2**、**没有任何真实公共后缀域被拦**）。
    """
    from trinity.security import sensitive as S

    for dom in ("example.com", "example.org", "example.net", "localhost", "x.invalid",
                "x.test", "host.internal", "host.corp", "host.local"):
        txt = "联系我 zhang@%s 谢谢" % dom
        out, labels = S.redact_identifiers(txt, cause="pii")
        assert "zhang@%s" % dom in out, "占位域 %s 竟然被掩了 ⇒ 口径变了：%r" % (dom, out)
        assert labels == [], "占位域 %s 不该产出标签：%r" % (dom, labels)


def test_真实域邮箱必掩_反事实_t50():
    """**真实域必掩**（这条与上一条互为反事实：缺任何一条，就会被 `example.com` 误导）。

    ⚠️ 本条**可失效**：若策略层哪天不再掩真实域邮箱，它立刻变红 ⇒ 强制回来更新报告与残余。
    """
    from trinity.security import sensitive as S

    for dom in ("huice.com", "qq.com", "163.com", "gmail.com", "outlook.com", "wangdian.cn"):
        txt = "联系我 zhang@%s 谢谢" % dom
        rep = S.scan_pii(txt)
        out, labels = S.redact_identifiers(txt, cause="pii")
        assert "zhang@%s" % dom not in out, "真实域 %s 的邮箱**没被掩**：%r" % (dom, out)
        assert "邮箱×1" in labels or any("邮箱" in str(x) for x in labels), (
            "真实域 %s 的标签里应出现邮箱：%r" % (dom, labels))
        assert any("邮箱" in str(k) for k in (rep.get("kinds") or [])), (
            "`scan_pii` 应把真实域邮箱识别为邮箱类：%r" % (rep,))


def test_perceive的perceptions表按设计保留明文_t50(monkeypatch):
    """**把"有意为之"与"缺陷"分开**：`memory_perceive` 会写两张表 ——

    * `memories`：**已掩码**（本文件其余判据覆盖）；
    * `perceptions`：**按代码里的明确设计保留明文**（`_routers_brain.py:322-327` 注释：
      "perceptions 表（另一张表、由 situation_stream 等按明文读）保持明文不动"）。

    ⇒ 本条把后者**显式断言出来**：它是**已知的按设计行为**，不是"守卫漏了"。
    若哪天口径改成"perceptions 也要脱敏"，本条会红 ⇒ 强制回来更新报告与残余清单。
    """
    from trinity.api.server import _routers_brain as rb
    from trinity.brain import perception as perc

    log: list = []

    class _Cur:
        def execute(self, sql, params=None):
            log.append((sql, params))

        def fetchall(self):
            return []

        def fetchone(self):
            return (0,)

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        autocommit = True

        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def close(self):
            pass

    import psycopg2

    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Conn())

    class _Eng:
        def evaluate(self, channel, signal, importance):
            return {"salience": 0.9, "importance": 0.8, "habituation": 0.1, "repeat": 1}

        def should_encode(self, salience):
            return True

    monkeypatch.setattr(perc, "get_perception_engine", lambda: _Eng())
    rb.memory_perceive(channel="t50", signal=PII, importance=0.8, session_id="s1", image=None)

    per = [p for s, p in log if "INSERT INTO PERCEPTIONS" in (s or "").upper()]
    assert per, "没抓到 perceptions 的写入（前提失效）"
    flat = [x for p in per for x in (p if isinstance(p, (list, tuple)) else [p])]
    assert any(isinstance(x, str) and "13800138000" in x for x in flat), (
        "perceptions 表竟然不再保留明文 ⇒ 口径变了：请更新报告（本条是「按设计」的钉子）")


# ── 顺序判据：守卫必须在 sha256/加密/INSERT 之前（逐处）─────────────────────
@pytest.mark.parametrize("site", sorted(SITES))
def test_守卫必须在哈希或加密或INSERT之前_t50(site):
    src = (REPO / SITES[site]).read_text(encoding="utf-8")
    lines = src.replace("\r\n", "\n").split("\n")
    guard = [i for i, line in enumerate(lines, 1)
             if "adapter_pii_guard" in line and "import" not in line]
    assert guard, "%s：找不到守卫调用（下沉没生效）" % site
    first_guard = guard[0]
    marks = []
    for i, line in enumerate(lines, 1):
        if "INSERT INTO memories" in line:
            marks.append(("INSERT", i))
        if "sha256(" in line and "encode(" in line:
            marks.append(("sql_sha256", i))
        if re.search(r"=\s*self\._compute_sha256\(|hashlib\.sha256\(", line):
            marks.append(("py_sha256", i))
        if "_enc_content(" in line and "=" in line:
            marks.append(("encrypt", i))
    assert marks, "%s：没找到哈希/加密/INSERT 标记（判据失效）" % site
    later = [(k, i) for k, i in marks if i < first_guard]
    assert not later, "%s：有 %r 出现在守卫(L%d)**之前** ⇒ 哈希算原文/落库掩码后正文（行自相矛盾）" % (
        site, later, first_guard)


# ── vms 两 backend 一致（t48 已覆盖 sqlite 侧；这里钉住 PG 侧同一行为）──────
#: 2026-10-06（t71/I11）：**显式列出允许的 `SQLiteVMSBackend.add` 签名**。
#: 原实现是"第一个签名 `TypeError` ⇒ 换位置参数 ⇒ 再失败就 `pytest.skip`" ——
#: ⭐ **接口签名一变就静默跳过**（跳过掩盖真实变化）。现在改成：逐一试允许签名，
#: **都不匹配就响亮失败**（签名变了必须有人来看这条判据）。
ALLOWED_VMS_ADD_SIGNATURES = (
    "keyword:content+agent_id+category",     # 实装签名（实测形参：content/agent_id/persona_id/...）
    "positional:content+agent_id",           # 历史兼容形态
)


def _call_vms_add(be, content, agent_id, category):
    """按**允许的签名白名单**调用；一个都不匹配 ⇒ 响亮失败（不得跳过）。"""
    errors = []
    try:
        return be.add(content=content, agent_id=agent_id, category=category), \
            ALLOWED_VMS_ADD_SIGNATURES[0]
    except TypeError as e:
        errors.append(("keyword:content+agent_id+category", repr(e)))
    try:
        return be.add(content, agent_id), ALLOWED_VMS_ADD_SIGNATURES[1]
    except Exception as e:  # noqa: BLE001
        errors.append(("positional:content+agent_id", repr(e)))
    pytest.fail(
        "[t71/I11] `SQLiteVMSBackend.add` 的签名**不在允许清单**里（%s）⇒ 本判据拒绝静默跳过。"
        "若这是有意的签名变更，请把新签名加进 ALLOWED_VMS_ADD_SIGNATURES 并复核本判据。尝试记录：%r"
        % (ALLOWED_VMS_ADD_SIGNATURES, errors))


def test_vms两backend行为一致_都掩码_t50(tmp_path, monkeypatch):
    # ① sqlite backend：走适配器（t48 之后由适配器守卫掩码）
    from trinity.adapters.sqlite import SQLiteAdapter
    from trinity.vms.backends.sqlite_backend import SQLiteVMSBackend

    db = str(tmp_path / "vms.db")
    ad = SQLiteAdapter(db_path=db)
    ad.connect()
    be = object.__new__(SQLiteVMSBackend)
    be._adapter = ad
    try:
        r, _sig = _call_vms_add(be, PII, "t50", "general")
    finally:
        pass
    mid = (r or {}).get("memory_id") or ""
    sqlite_stored = ((ad.get_memory(mid) or {}).get("content")) if mid else None
    # ② PG backend：同一守卫（假 conn 捕获参数）
    pg_strings = _drive_vms_pg(monkeypatch, PII)
    # 2026-10-06（t71/I11）：原来是 `pytest.skip("sqlite backend 未返回可读行（跳过一致性断言…）")`
    # —— **一致性断言被静默放弃**（L-C 逃逸口）⇒ 改**响亮失败**：
    # 本判据的全部内容就是"两 backend 一致"，读不到行就等于没测。
    if not mid or sqlite_stored is None:
        ad.disconnect()
        pytest.fail(
            "[t71/I11] sqlite backend 未返回可读行（memory_id=%r, content=%r）⇒ **一致性断言无法执行**。"
            "这不是'不适用'：本判据存在的理由就是比对两个 backend 的落库形态；"
            "调用记录=%r" % (mid, sqlite_stored, r))
    try:
        assert _raw_pii_in([sqlite_stored], MASKABLE) == [], \
            "sqlite backend 落库仍有**策略层会掩的原始标识符**：%r" % sqlite_stored
        assert _raw_pii_in(pg_strings, MASKABLE) == [], "pg backend 写入仍明文 ⇒ 两 backend 不一致"
    finally:
        ad.disconnect()
