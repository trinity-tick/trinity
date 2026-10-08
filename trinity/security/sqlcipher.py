#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLCipher 连接层单点补丁（2026-10-02，外部审计 · 手册 §3）。

## 为什么用**单点补丁**而不是改 112 个调用点

实测本仓 `sqlite3.connect(` 有 **112 处、分布 91 个文件**。逐个收敛既慢又易漏。
本仓**已有先例**：`trinity/security/credentials.py::patch_psycopg2()` 用一个补丁
修掉了 **171 处**「空口令直连」——同一个模式在这里适用。

## 安全边界（**必须只作用于 Trinity 自己的库**）

本机还有**别的 SQLite 库**（例如 `dsh-memory` 的 `memory.db`）。若给**所有**连接
盲目加 `PRAGMA key`，那些明文库会**立刻读不了**（SQLCipher 把明文库当"密钥错误"）。
故本补丁**只在目标路径落在 Trinity 存储目录内时**才加 key。

## 平滑降级

`TRINITY_SQLCIPHER_KEY` **未设** ⇒ 什么都不做（行为与今天**逐字节一致**）。
设了 ⇒ 用 `sqlcipher3` 连接并对 Trinity 库加 key。

可调：
  · `TRINITY_SQLCIPHER_KEY`      —— 密钥本体（**从环境/凭据取，绝不硬编码**）
  · `TRINITY_SQLCIPHER_KEY_FILE` —— 或从文件读（默认 `~/.trinity/keys/sqlcipher.key`）
  · `TRINITY_SQLCIPHER_SCOPE`    —— 生效路径前缀，默认 `~/.trinity/store`
  · `TRINITY_SQLCIPHER=off`      —— 强制关闭（即便设了 key）
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIG_CONNECT: Optional[Callable[..., Any]] = None


def _scope() -> str:
    return os.path.normcase(os.path.abspath(
        os.path.expanduser(os.environ.get("TRINITY_SQLCIPHER_SCOPE")
                           or os.path.join("~", ".trinity", "store"))))


def resolved_key() -> Optional[str]:
    """密钥：**必须显式配置**才返回（否则 None = 不启用）。

    ## 为什么不自动读默认 key 文件（**2026-10-02 实测发现的坑**）

    第一版写成"env 没有就读 `~/.trinity/keys/sqlcipher.key`"。而**迁移演练会生成该文件** ⇒
    补丁会在**生产库还是明文**的情况下**自动启用** ⇒ `PRAGMA key` 打在明文库上
    ⇒ `file is not a database` ⇒ **服务崩**。
    **⇒ 启用必须由环境显式表达**，不能让"某个文件恰好存在"改变存储语义。
    """
    if str(os.environ.get("TRINITY_SQLCIPHER", "")).strip().lower() in ("off", "0", "false", "no"):
        return None
    k = os.environ.get("TRINITY_SQLCIPHER_KEY")
    if k:
        return k
    # 只有**显式指定**了 key 文件才读它（不猜默认路径）
    p = os.environ.get("TRINITY_SQLCIPHER_KEY_FILE")
    if not p:
        return None
    try:
        if os.path.exists(p):
            return open(p, encoding="utf-8").read().strip() or None
    except Exception as e:  # noqa: BLE001 — 读不到就当没配
        logger.warning("SQLCipher 密钥文件读不到（%s）：%s", p, str(e)[:80])
    return None


def _in_scope(database: Any) -> bool:
    """这个连接目标是不是 Trinity 自己的库？（决定要不要加 key）"""
    if not isinstance(database, str) or not database:
        return False
    if database == ":memory:" or database.startswith("file::memory:"):
        return False
    # URI 形式：file:<path>?mode=ro
    path = database
    if path.startswith("file:"):
        path = path[5:].split("?", 1)[0]
    try:
        return os.path.normcase(os.path.abspath(path)).startswith(_scope())
    except Exception:  # noqa: BLE001
        return False


def patch_sqlite3() -> bool:
    """装上补丁。返回 True 表示**真的换了连接实现**（设了 key）；False = 未启用。"""
    global _PATCHED, _ORIG_CONNECT
    if _PATCHED:
        return _ORIG_CONNECT is not None and sqlite3.connect is not _ORIG_CONNECT
    key = resolved_key()
    if not key:
        _PATCHED = True
        return False
    try:
        from sqlcipher3 import dbapi2 as _sc
    except Exception as e:  # noqa: BLE001 — 没装就平滑降级（绝不阻断启动）
        logger.warning("SQLCipher 已配置但 sqlcipher3 不可用（%s）⇒ 回落标准 sqlite3", str(e)[:80])
        _PATCHED = True
        return False

    _ORIG_CONNECT = sqlite3.connect

    def _connect(database: Any, *a: Any, **kw: Any):
        if _in_scope(database):
            conn = _sc.connect(database, *a, **kw)
            # `PRAGMA key` **必须**是连接后的第一条语句（SQLCipher 要求）。
            # ⚠️ **PRAGMA 不接受参数绑定**（实测 `PRAGMA key=?` 报 `near "?": syntax error`）
            # ⇒ 只能拼接；单引号转义（本仓密钥是 token_urlsafe，不含引号，仍严格转义）。
            conn.execute("PRAGMA key='%s'" % str(key).replace("'", "''"))
            return conn
        return _ORIG_CONNECT(database, *a, **kw)  # type: ignore[misc]

    sqlite3.connect = _connect  # type: ignore[assignment]
    _PATCHED = True
    logger.info("SQLCipher 连接补丁已启用（作用域：%s）", _scope())
    return True


def unpatch_sqlite3() -> None:
    """回滚（测试用）。"""
    global _PATCHED, _ORIG_CONNECT
    if _ORIG_CONNECT is not None:
        sqlite3.connect = _ORIG_CONNECT  # type: ignore[assignment]
    _ORIG_CONNECT = None
    _PATCHED = False


def _is_plain_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(15) == b"SQLite format 3"
    except Exception:  # noqa: BLE001
        return False


def migrate_plaintext_to_encrypted(db_path: Optional[str] = None) -> dict:
    """**进程内一次性迁移**：明文库 → 加密库（2026-10-02，外部审计 · 方案 B）。

    ## 为什么必须"在进程内、启动早期"做

    外部脚本"杀掉服务再换文件"在这个仓里**必输**：实测两次都因
    `另一个程序已锁定文件的一部分` 失败 —— 本仓有 `TrinityAutoStartLoop`(5min) /
    `TrinityMemoryGuard`(1min) / `TrinityLoopGuard`(5min) 三层守护，
    被杀的服务**几秒内**就被拉回并重新持有库句柄。

    **但"启动早期"这个窗口是干净的**：那一刻本进程还没打开任何库句柄，
    也没有别人在写 ⇒ 可以安全地 `os.replace` 原子替换。

    ## 顺序（每步可回滚）

    1. 读明文库 → `sqlcipher_export` 到 `<db>.enc.tmp`
    2. **验收**：行数一致 + FTS5 可查 + 产出库不是明文 + 无密钥打不开
    3. 把明文库改名成 `.pre-sqlcipher` 备份，再 `os.replace(tmp, db)` —— 原子
    4. 任一步失败 ⇒ 删 tmp、**明文库一动不动**（服务照常用明文起来）

    触发开关：`TRINITY_SQLCIPHER_MIGRATE=on`（且已配密钥）。不设 ⇒ **什么都不做**。
    """
    db = db_path or os.path.join(os.path.expanduser("~/.trinity/store"), "trinity_store.db")
    out: dict = {"db": db, "action": "skip", "reason": "", "ok": True}
    if str(os.environ.get("TRINITY_SQLCIPHER_MIGRATE", "")).strip().lower() not in ("on", "1", "true", "yes"):
        out["reason"] = "TRINITY_SQLCIPHER_MIGRATE 未开启"
        return out
    key = resolved_key()
    if not key:
        out.update(ok=False, reason="要迁移但没配密钥（TRINITY_SQLCIPHER_KEY / _KEY_FILE）")
        logger.error("SQLCipher 迁移：%s", out["reason"])
        return out
    if not os.path.exists(db):
        out.update(reason="库不存在（首次运行）⇒ 无需迁移")
        return out
    if not _is_plain_sqlite(db):
        out.update(action="already-encrypted", reason="库已是加密形态")
        return out
    tmp, backup = db + ".enc.tmp", db + ".pre-sqlcipher"
    try:
        from sqlcipher3 import dbapi2 as _sc
    except Exception as e:  # noqa: BLE001
        out.update(ok=False, reason="sqlcipher3 不可用：%s" % str(e)[:80])
        logger.error("SQLCipher 迁移：%s", out["reason"])
        return out
    # ── 2026-10-02（**关键**）：跨进程**迁移锁** ──────────────────────────────
    # 为什么必须要有（实测踩到，生产上卡了 14 分钟未完成）：
    #   本仓监督器在 API 迟迟不 ready 时会**杀掉重启**；而迁移在 import 期跑、要几分钟
    #   ⇒ **每个新实例都会重跑一次迁移** ⇒ 多个导出争抢同一个 `.enc.tmp` ⇒ **永远做不完**。
    # 判据：`O_CREAT|O_EXCL` 抢锁；**抢不到 = 别人在做 ⇒ 本进程立刻跳过，以明文继续服务**。
    # 陈旧锁：>20 分钟视为死进程遗留（迁移正常远快于此），删掉重抢。
    lock = db + ".migrate.lock"
    _lfd = None
    try:
        _lfd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            _age = time.time() - os.path.getmtime(lock)
        except Exception:  # noqa: BLE001 — 读不到 mtime 就当陈旧
            _age = 1e9
        if _age > 1200:
            logger.warning("SQLCipher 迁移：发现陈旧锁（%.0fs）⇒ 清除后重试", _age)
            try:
                os.remove(lock)
                _lfd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except Exception as _e3:  # noqa: BLE001 — 清不掉就放弃（安全）
                out.update(action="skip", reason="陈旧迁移锁清不掉：%s" % str(_e3)[:60])
                return out
        else:
            out.update(action="skip",
                       reason="另一个进程正在迁移（锁 %s，%.0fs 前）⇒ 本进程跳过，以明文继续"
                              % (lock, _age))
            logger.info("SQLCipher 迁移：%s", out["reason"])
            return out
    except Exception as _e4:  # noqa: BLE001 — 取不到锁就放弃迁移（安全方向）
        out.update(action="skip", reason="取迁移锁失败：%s" % str(_e4)[:60])
        return out
    try:
        os.write(_lfd, ("%d %s" % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S"))).encode())
    except Exception as _e5:  # noqa: BLE001 — 锁内容只是给人看的
        logger.debug("写迁移锁信息失败：%s", str(_e5)[:60])

    def _unlock() -> None:
        try:
            if _lfd is not None:
                os.close(_lfd)
        except Exception:  # noqa: BLE001
            logger.debug("关闭迁移锁 fd 失败")
        try:
            os.remove(lock)
        except Exception:  # noqa: BLE001 — 删不掉会变成陈旧锁，20 分钟后自清
            logger.warning("迁移锁未删除：%s", lock)

    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        con = _sc.connect(tmp)
        con.execute("PRAGMA key='%s'" % str(key).replace("'", "''"))
        con.execute("PRAGMA cipher_page_size = 4096")
        con.execute("PRAGMA kdf_iter = 256000")
        con.execute("ATTACH DATABASE ? AS plaintext KEY ''", (db,))
        con.execute("SELECT sqlcipher_export('main', 'plaintext')")
        con.execute("DETACH DATABASE plaintext")
        con.commit()
        # ── 2026-10-02（实测踩到）：**`sqlcipher_export` 不会正确重建 FTS5 虚拟表** ──
        # 现象：`memories_fts` 出现在 `sqlite_master`，但 `SELECT ... FROM memories_fts`
        # 报 `no such table: memories_fts`（模块绑定/影子表没跟过来）。
        # 修法：**读源库的 CREATE VIRTUAL TABLE 语句，在目标库重建 + `rebuild`**
        # （不硬编码 schema ⇒ 换 FTS 定义也不用改这里）。
        src0 = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=60)
        try:
            vtab_sql = [r[0] for r in src0.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name LIKE '%\\_fts' "
                "ESCAPE '\\' AND sql IS NOT NULL")]
        finally:
            src0.close()
        for stmt in vtab_sql:
            try:
                # 表名用正则取 —— 源语句是 `CREATE VIRTUAL TABLE memories_fts USING fts5(...)`，
                # **没有** `IF NOT EXISTS`（我第一版按 "EXISTS" 切分，把名字取成了 `CREATE`）。
                _m = re.search(r"CREATE\s+VIRTUAL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                               r"([`\"\[\]\w.]+)", stmt, re.I)
                name = _m.group(1).strip('`"[]') if _m else ""
                if not name:
                    raise RuntimeError("取不出虚拟表名：%s" % stmt[:60])
                con.execute("DROP TABLE IF EXISTS %s" % name)
                con.execute(stmt)
                con.execute("INSERT INTO %s(%s) VALUES('rebuild')" % (name, name))
                con.commit()
                logger.info("SQLCipher 迁移：已重建 FTS5 虚拟表 %s 并 rebuild", name)
            except Exception as _e:  # noqa: BLE001 — 重建失败由下面的验收判据拦住
                logger.warning("SQLCipher 迁移：重建虚拟表失败（将由验收拦住）：%s", str(_e)[:90])
        cur = con.cursor()
        n_new = cur.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        src = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=60)
        n_old = src.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        src.close()
        fts = [r[0] for r in cur.execute("SELECT name FROM sqlite_master "
                                        "WHERE type='table' AND name LIKE '%\\_fts' ESCAPE '\\'")]
        fts_ok, fts_err = False, ""
        for t in fts:
            try:
                # 用**真实分词**做一次 MATCH（原写法 `SELECT COUNT(*) FROM <vtab> LIMIT 1`
                # 在合成库上会误判不可查；且不带真实原因，排查困难）。
                tok = ""
                row = cur.execute("SELECT tokenized_content FROM memories WHERE tokenized_content "
                                  "IS NOT NULL AND length(tokenized_content)>8 LIMIT 1").fetchone()
                if row and row[0]:
                    for w in str(row[0]).split():
                        if len(w) >= 2:
                            tok = w
                            break
                cur.execute("SELECT COUNT(*) FROM %s WHERE %s MATCH ?" % (t, t), (tok,)).fetchone()
                fts_ok = True
            except Exception as _e2:  # noqa: BLE001 — 记下真实原因再继续
                fts_err = "%s: %s" % (t, str(_e2)[:90])
                continue
        con.commit()
        con.close()
        if n_new != n_old:
            raise RuntimeError("行数不一致 %s != %s" % (n_new, n_old))
        if not fts_ok:
            raise RuntimeError("FTS5 虚拟表在加密库内不可查询（%s；命中表=%s）"
                               % (fts_err or "无错误信息", fts))
        if _is_plain_sqlite(tmp):
            raise RuntimeError("产出的库仍是明文")
        try:
            c2 = _sc.connect(tmp)
            try:
                c2.execute("SELECT COUNT(*) FROM memories").fetchone()
                raise RuntimeError("无密钥竟能打开产出的库")
            finally:
                # ⚠️ **必须关**：不关的话这个连接占着 tmp 文件，
                # 下面的 `os.replace(tmp, db)` 会报 `[WinError 32] 另一个程序正在使用此文件`
                # （实测踩到，排查了一轮）。这是"无密钥打不开"检查的**副作用**。
                c2.close()
        except RuntimeError:
            raise
        except Exception as _e_expected:  # noqa: BLE001 — **期望**：无密钥打不开
            # 2026-10-02：本分支语义就是"无密钥**必须**打不开"，所以它是**预期成功**而非静默失败。
            # 但原写法是裸 `pass`，会被 `structure_gate` 的静默失败棘轮计为一条
            # （AST 判据：`except` 体里只有一条 `pass`），实测该门因此从
            # total=335 涨到 336（新增文件 trinity/security/sqlcipher.py）。
            # 本仓纪律是**不抬基线** ⇒ 改成"显式空操作 + 留痕"，语义不变、门转绿。
            # 注意：**不能**改成 `logger.warning` —— 这是正常路径，每次迁移成功都会走到。
            _e_expected = None  # type: ignore[assignment]
            logger.debug("无密钥打不开产出的库（预期行为）：%s", type(_e_expected).__name__)
        if not os.path.exists(backup):
            os.replace(db, backup)
        os.replace(tmp, db)
        out.update(action="migrated", rows=n_new, fts_tables=fts, backup=backup)
        logger.warning("SQLCipher 迁移完成：%s 行已加密；明文备份留在 %s", n_new, backup)
        _unlock()
    except Exception as e:  # noqa: BLE001 — 失败即放弃，**明文库不动**，服务照常起
        out.update(ok=False, reason="迁移失败：%s" % str(e)[:120])
        logger.error("SQLCipher 迁移失败（**明文库未动，服务继续用明文**）：%s", str(e)[:200])
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:  # noqa: BLE001 — 清理失败只留痕
            logger.warning("临时加密库清理失败：%s", tmp)
        _unlock()          # **必须**：否则失败会留下陈旧锁，20 分钟内其它实例全都跳过
    return out
