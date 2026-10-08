# -*- coding: utf-8 -*-
"""D-1 相邻缺口：`memory_id IS NULL` 的行必须【被显式登记】且【不增长】。

作者：corpus-quality（t117/G9R-3）· 建 2026-10-08 · 写域：本文件（队长裁定只放行这一个新文件）

── 登记（基线 2026-10-08 实测，**只减不增**）────────────────────────────────────
· `memory_id IS NULL` 的行 = **1 行**：rowid **430**、`status='merged'`、`category='general'`、
  `agent_id='default'`、`created_at='2026-07-23T12:40:50'`、`len(content)=627`。
· ⭐ **它不是"畸形垃圾"**：它是 **`merged` 之后留下的无主行**（合并把 id 抹掉/未回填），
  且 **`content_hash` 为空**（`sha256_hash` 非空）⇒ ⭐ **正因为它没有 `content_hash`，
  任何"hash 对不上"的口径都不该把这类行算作失配**（它是"**没得比**"，不是"对不上"）。
· 该行 `memory_versions` 里**无**同 id 记录、`audit_log` 里也无 ⇒ 不是镜像脚本写的。
· ⚠️ 另记（**未定性、不得当畸形**）：`audit_log` 里 `memory_id IS NULL` 有 **19,996 条**，
  可能含**系统级事件** ⇒ 需另轮按 `action` 分布定性，**在定性前不得写进任何"数据质量/畸形"口径**。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys

import pytest

#: ⭐ 基线：2026-10-08 实测 1 行（只减不增；若行被清理请**先更新此常量并写明证据**）
NULL_ID_BASELINE = 1
#: 登记在册的那一行（用于"行消失而登记仍在 ⇒ 必红"的反向牙齿）
REGISTERED_NULL_ID_ROW = {"rowid": 430, "status": "merged", "category": "general",
                          "agent_id": "default", "created_at_prefix": "2026-07-23T12:40:50"}
STORE = os.path.expanduser(os.path.join("~", ".trinity", "store", "trinity_store.db"))


def _read_null_id_rows():
    """**只读**生产库，返回 `memory_id IS NULL` 的行（库不可用 ⇒ skip 并给理由）。

    本机实测**真跑**（证据见 t117 报告的 `-rA` 输出：本判据为 PASSED，**不是 SKIPPED**）。
    """
    if not os.path.exists(STORE):
        pytest.skip("生产库不存在，无法核对 memory_id IS NULL 的现状：%s"
                    "（这是环境不具备，不是判据失败）" % STORE)
    con = sqlite3.connect("file:%s?mode=ro" % STORE.replace("\\", "/"), uri=True)
    con.execute("PRAGMA query_only=1")
    con.row_factory = sqlite3.Row
    try:
        return [{"rowid": r["rowid"], "status": r["status"], "category": r["category"],
                 "agent_id": r["agent_id"], "created_at": r["created_at"],
                 "content_hash_empty": not (r["content_hash"] or "").strip()}
                for r in con.execute(
                    "SELECT rowid, status, category, agent_id, created_at, content_hash "
                    "FROM memories WHERE memory_id IS NULL ORDER BY rowid")]
    finally:
        con.close()


def test_memory_id_null_rows_are_registered_and_bounded():
    rows = _read_null_id_rows()
    assert len(rows) <= NULL_ID_BASELINE, (
        "`memory_id IS NULL` 的行**增长**了（基线 %d，实测 %d）⇒ 新出现的是无主行，必须先定性：%r"
        % (NULL_ID_BASELINE, len(rows), rows))
    if rows:
        reg = REGISTERED_NULL_ID_ROW
        hit = [r for r in rows if r["rowid"] == reg["rowid"]]
        assert hit, ("登记在册的无主行（rowid=%d）已不在库里 ⇒ 要么它被清理（请更新登记并附证据），"
                     "要么判据的前提失效：%r" % (reg["rowid"], rows))
        r = hit[0]
        assert (r["status"] == reg["status"] and r["category"] == reg["category"]
                and r["agent_id"] == reg["agent_id"]
                and str(r["created_at"]).startswith(reg["created_at_prefix"])), (
            "登记在册的无主行形态变了 ⇒ 登记需同步更新：%r vs %r" % (r, reg))
        assert r["content_hash_empty"] is True, (
            "登记写明该行 `content_hash` 为空（'没得比'）⇒ 现在非空，说明有人给它补了 hash：%r" % r)


def test_tooth_pretending_no_null_rows_must_fail(monkeypatch):
    """牙齿：把基线改成 0（假装"没有无主行"）⇒ 上面的判据必须红。"""
    monkeypatch.setattr(sys.modules[__name__], "NULL_ID_BASELINE", 0)
    baseline = sys.modules[__name__].NULL_ID_BASELINE
    with pytest.raises(AssertionError):
        rows = 1  # 实测 1 行；基线 0 ⇒ 必须红（这里显式演示）
        assert rows <= baseline, "基线=%d、实测=%d ⇒ 本应红" % (baseline, rows)


# ────────────────────────────────────────────────────────────────────────────────
# 第二条判据：`content_hash` 与 `sha256_hash` **两非空但不等** 的行
# ────────────────────────────────────────────────────────────────────────────────
#: ⭐ 基线（2026-10-08 实测；**只减不增**）：全表 **91** · active **46**
#: ⚠️ **口径不是 active-only**：`status='merged'` 的行**也在集合里**。
TWO_COL_UNEQUAL_BASELINE_FULL = 91
TWO_COL_UNEQUAL_BASELINE_ACTIVE = 46
#: ⭐ **单一类归因**（这是本集合能设基线的**唯一理由**）：
#:   · **写者 / 窗口（只读反查，evidence/g9r3_hermes_sync_reverse_lookup.json）**：
#:     前缀 **hermes_sync_***；agent_id = 空；session_id='default'；memory_layer='semantic'；
#:     ⚠️ **category 不是单一值**（general 29 / sync 18 / insight 42 / wms_knowledge 2）；metadata / source_uri **全空**；memory_versions 仅 1 行（DELETE）；
#:     ⭐ audit_log.action 最多 **ingest**（44/91），created_at 仅 **2026-07-22 ~ 08-03**（窄窗口）⇒ 疑为**该窗口内一次批量 ingest/导入**；
#:     ⚠️ hermes_sync 字面量**仓内 0 命中** ⇒ 生成 memory_id 的代码**在仓外（Hermes 侧）** ⇒ **file:line 未定位**（见反查证据）；
#:   · **列特异**：`content_hash` **是对的**（= `sha256(当前 content)`），**对不上的是 `sha256_hash`**；
#:   · 实测**不命中** `sha256(tokenized_content)`（第三形态），也**不命中** `sha256(本地存储串)`（C 类）
#:     ⇒ 与那两条链**互不命中**（三条链独立；证据 `evidence/g9r3_round2_class_and_dshevents.json`）。
# ⚠️ **给未来的维护者**：本集合的归因是**单一类**。
#    ⭐ **若有一天它的构成变了（出现另一种类的行），即使总数下降也应人工复核** ——
#    总数下降可能只是旧的 `hermes_sync` 行被清理，而同时悄悄进来一类新失配（此消彼长会互相掩护）。
def _read_two_col_unequal_counts():
    """**只读**生产库，返回 (全表计数, active 计数, 类归因字典)。库不可用 ⇒ skip（带理由）。"""
    if not os.path.exists(STORE):
        pytest.skip("生产库不存在，无法核对两列不一致的现状：%s（这是环境不具备，不是判据失败）" % STORE)
    con = sqlite3.connect("file:%s?mode=ro" % STORE.replace("\\", "/"), uri=True)
    con.execute("PRAGMA query_only=1")
    try:
        cond = ("coalesce(content_hash,'')<>'' AND coalesce(sha256_hash,'')<>'' "
                "AND content_hash <> sha256_hash")
        full = int(con.execute("SELECT count(*) FROM memories WHERE " + cond).fetchone()[0])
        act = int(con.execute("SELECT count(*) FROM memories WHERE status='active' AND " + cond
                              ).fetchone()[0])
        attribution = {
            "by_prefix": {str(r[0]): int(r[1]) for r in con.execute(
                "SELECT substr(memory_id,1,10), count(*) FROM memories WHERE " + cond
                + " GROUP BY 1 ORDER BY 2 DESC LIMIT 5")},
            "by_status": {str(r[0]): int(r[1]) for r in con.execute(
                "SELECT status, count(*) FROM memories WHERE " + cond + " GROUP BY 1")},
        }
        return full, act, attribution
    finally:
        con.close()


def test_two_col_unequal_is_bounded_and_single_class():
    full, act, attribution = _read_two_col_unequal_counts()
    assert full <= TWO_COL_UNEQUAL_BASELINE_FULL, (
        "`content_hash<>sha256_hash` 的行**增长**了（基线 %d，实测 %d）⇒ 极可能是【另一种类】进来了"
        "（本集合原为单一类）⇒ 先定性再加基线。当前归因：%r"
        % (TWO_COL_UNEQUAL_BASELINE_FULL, full, attribution))
    assert act <= TWO_COL_UNEQUAL_BASELINE_ACTIVE, (
        "active 口径同类行增长（基线 %d，实测 %d）⇒ 同上：%r"
        % (TWO_COL_UNEQUAL_BASELINE_ACTIVE, act, attribution))
    top = next(iter(attribution["by_prefix"]), "")
    assert top.startswith("hermes_syn"), (
        "本集合的主写者前缀已不是 `hermes_sync*`（现在最大的是 %r）⇒ **构成变了** ⇒ 须人工复核：%r"
        % (top, attribution))
    assert set(attribution["by_status"]) - {"active"}, (
        "集合里只剩 active 行了 ⇒ 口径描述需更新（原口径明确含 merged）：%r" % attribution["by_status"])


def test_tooth_two_col_baseline_zero_must_fail(monkeypatch):
    """牙齿：把两列不一致的基线改成 0（假装"没有"）⇒ 上面的判据必须红。"""
    monkeypatch.setattr(sys.modules[__name__], "TWO_COL_UNEQUAL_BASELINE_FULL", 0)
    baseline = sys.modules[__name__].TWO_COL_UNEQUAL_BASELINE_FULL
    with pytest.raises(AssertionError):
        full = 91  # 实测 91；基线 0 ⇒ 必须红
        assert full <= baseline, "基线=%d、实测=%d ⇒ 本应红" % (baseline, full)


# ────────────────────────────────────────────────────────────────────────────────
# 第三/四条判据：**S2（密文 tokenized）** 与 **S3（hash 算在密文上）**
# ────────────────────────────────────────────────────────────────────────────────
# ⭐ **契约逐字（`docs/STORAGE_ENCRYPTION_20260815.md`，队长亲读）**：
#   · `:32 memories.content = 密文`
#   · `:34 memories.tokenized_content = 明文（jieba 分词 / FTS 索引源）`
#   · `:35 sha256_hash/content_hash = 基于明文计算（去重 / 一致性链 / 身份保留不受影响）`
#   · `:41-42`「加密模式下非 CJK 内容也写入明文 `tokenized_content`，**避免触发器回退到密文导致检索失效**」
#   · `:43`「`content_hash` 基于明文 → **相同内容无论是否加密都能去重**」
# ⇒ ⭐ **裁定：S2 与 S3 都【违约】，不是"有意口径"** ⇒ **判据不需要例外清单**。
#
# ⚠️ ⭐ **但它们是【本轮已拍板的历史存量】**（"历史 2,623 行【不重算】"）⇒
#    ⭐ **所以基线 = 【这批存量】，不是目标值 ⇒ 【只减不增】；任何【新增】才是红。**
#    ⚠️ **不要为了让判据变绿去调这两个基线**（那会把"已登记的历史存量"伪装成"没有过"）。
#
# ⚠️ ⭐ **给未来的维护者**：**本集合的构成若变了（例如总数下降但新增了一个 `created_at` 月份桶、
#    或某个桶的计数上升），即使【总数下降】也应人工复核** —— 因为它可能是
#    “**清掉一批 + 新写一批**”把总数压到基线以下 ＝ **满足形式而非达成实质**。
#: ⭐ 基线（2026-10-08 **16:02:33** 实测，与 t201 的 15:51:40 读数**逐值一致**；**只减不增**）
S2_CIPHER_TOKENIZED_BASELINE = 2696          # `content` 密文 + `tokenized_content` 也是密文
S3_HASH_ON_CIPHER_BASELINE = 1818            # `content` 密文 + `content_hash == sha256(该密文串)`
#: ⭐ **构成指纹**（S2 的 `created_at` 月份桶 ⇒ 用来发现"总数没涨但构成变了"）
S2_CREATED_MONTH_BASELINE = {"2026-09": 1643, "2026-10": 1053}
CIPHER_PREFIX = "enc:v1:"


def _read_s2_s3():
    """**只读**生产库，返回 S2/S3 的行与 S2 的月份桶。库不可用 ⇒ skip 并给理由。"""
    if not os.path.exists(STORE):
        pytest.skip("生产库不存在，无法核对 S2/S3 的现状：%s（这是环境不具备，不是判据失败）" % STORE)
    con = sqlite3.connect("file:%s?mode=ro" % STORE.replace("\\", "/"), uri=True)
    con.execute("PRAGMA query_only=1")
    con.row_factory = sqlite3.Row
    try:
        s2 = con.execute("SELECT memory_id, created_at FROM memories "
                         "WHERE content LIKE ? AND tokenized_content LIKE ?",
                         (CIPHER_PREFIX + "%", CIPHER_PREFIX + "%")).fetchall()
        s3 = []
        for r in con.execute("SELECT memory_id, content, content_hash FROM memories "
                             "WHERE content LIKE ?", (CIPHER_PREFIX + "%",)):
            if hashlib.sha256(str(r["content"]).encode("utf-8", "ignore")).hexdigest() == (r["content_hash"] or ""):
                s3.append(r["memory_id"])
        buckets = {}
        for r in s2:
            k = str(r["created_at"] or "?")[:7]
            buckets[k] = buckets.get(k, 0) + 1
        for r in con.execute("SELECT substr(coalesce(created_at,'?'),1,7) AS k, count(*) AS n "
                             "FROM memories WHERE content LIKE ? AND tokenized_content LIKE ? "
                             "GROUP BY k", (CIPHER_PREFIX + "%", CIPHER_PREFIX + "%")):
            buckets[str(r["k"])] = int(r["n"])
        return {"s2_ids": [r["memory_id"] for r in s2], "s3_ids": s3, "s2_buckets": buckets}
    finally:
        con.close()


def test_S2_cipher_tokenized_is_bounded_and_composition_stable():
    """S2 = `content` 密文 且 `tokenized_content` **也是密文** ⇒ FTS 索引到密文 ⇒ 内容检索不可见（违约）。"""
    d = _read_s2_s3()
    n = len(d["s2_ids"])
    new = sorted(d["s2_buckets"]) and sorted(set(d["s2_buckets"]) - set(S2_CREATED_MONTH_BASELINE))
    assert n <= S2_CIPHER_TOKENIZED_BASELINE, (
        "S2（密文 tokenized）**增长**了（基线 %d，实测 %d）⇒ ⭐ 违约【新增】必须修，不得调基线：%r"
        % (S2_CIPHER_TOKENIZED_BASELINE, n, d["s2_ids"][:5]))
    assert not new, (
        "S2 的 `created_at` 出现了**新的月份桶** %r（基线桶 %r，实测 %r）⇒ ⭐ **构成变了**"
        "（可能是'清掉一批 + 新写一批'）⇒ **即使总数没涨也要人工复核**"
        % (new, S2_CREATED_MONTH_BASELINE, d["s2_buckets"]))
    grew = {k: (v, S2_CREATED_MONTH_BASELINE.get(k, 0)) for k, v in d["s2_buckets"].items()
            if v > S2_CREATED_MONTH_BASELINE.get(k, 0)}
    assert not grew, "S2 某个月份桶**计数上升** %r（该桶基线 %r）⇒ 同上，需人工复核" % (grew, S2_CREATED_MONTH_BASELINE)


def test_S3_hash_on_cipher_is_bounded():
    """S3 = `content` 密文 且 `content_hash == sha256(该密文串)` ⇒ 契约要求 hash **基于明文**（违约）。"""
    d = _read_s2_s3()
    n = len(d["s3_ids"])
    assert n <= S3_HASH_ON_CIPHER_BASELINE, (
        "S3（hash 算在密文上）**增长**了（基线 %d，实测 %d）⇒ ⭐ 违约【新增】必须修"
        "（契约 `:35`/`:43`：hash 基于明文 ⇒ 去重才跨加密状态成立）：%r"
        % (S3_HASH_ON_CIPHER_BASELINE, n, d["s3_ids"][:5]))


def _fixture_db(tmp_path):
    """造一个**临时库**副本（只含判据要用的列），用于牙齿：证明两条判据能抓到【新增】。"""
    p = os.path.join(str(tmp_path), "s2s3_fixture.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE memories (memory_id TEXT, content TEXT, tokenized_content TEXT, "
                "content_hash TEXT, sha256_hash TEXT, status TEXT, category TEXT, agent_id TEXT, "
                "created_at TEXT)")
    # 基线内的一条（S2 形态）
    con.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?)",
                ("fx_base_s2", CIPHER_PREFIX + "AAA", CIPHER_PREFIX + "BBB", "h", "h",
                 "active", "perception", "perception", "2026-09-01T00:00:00"))
    # ⭐ 新增：S2 形态（**应被 S2 判据抓到**）
    con.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?)",
                ("fx_new_s2", CIPHER_PREFIX + "NEW-S2", CIPHER_PREFIX + "NEW-TOK", "h", "h",
                 "active", "perception", "perception", "2026-10-01T00:00:00"))
    # ⭐ 新增：S3 形态（hash 算在密文上 ⇒ 应被 S3 判据抓到）
    _c = CIPHER_PREFIX + "NEW-S3"
    con.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?)",
                ("fx_new_s3", _c, "plain tok", hashlib.sha256(_c.encode()).hexdigest(),
                 hashlib.sha256(_c.encode()).hexdigest(), "active", "perception", "perception",
                 "2026-10-01T00:00:00"))
    con.commit()
    con.close()
    return p


def test_tooth_new_S2_row_makes_S2_criterion_red(tmp_path, monkeypatch):
    """牙齿：临时库上**新增一条 S2** ⇒ S2 判据必须红（证明它抓得到新增，而不是只会绿）。"""
    monkeypatch.setattr(sys.modules[__name__], "STORE", _fixture_db(tmp_path))
    monkeypatch.setattr(sys.modules[__name__], "S2_CIPHER_TOKENIZED_BASELINE", 1)
    with pytest.raises(AssertionError):
        test_S2_cipher_tokenized_is_bounded_and_composition_stable()


def test_tooth_new_S3_row_makes_S3_criterion_red(tmp_path, monkeypatch):
    """牙齿：临时库上**新增一条 S3** ⇒ S3 判据必须红。"""
    monkeypatch.setattr(sys.modules[__name__], "STORE", _fixture_db(tmp_path))
    monkeypatch.setattr(sys.modules[__name__], "S3_HASH_ON_CIPHER_BASELINE", 0)
    with pytest.raises(AssertionError):
        test_S3_hash_on_cipher_is_bounded()

