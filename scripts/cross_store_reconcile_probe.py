#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cross_store_reconcile_probe.py —— B3/A5-03：**两库一致性/滞后的可失败判据**（t87）。

## 这个脚本存在的原因（t75 的教训）

t75 实测过：本机常驻 API 跑 **SQLite**（`GET :8001/diagnostics` 的 `adapter=sqlite`），
而插件的 engine worker 写 **PG(5432)**；两边**各自新鲜、跨库滞后**（当时量级：SQLite→PG ≈18.8h、
PG→SQLite ≈3.24h，口径见下）。那时**没有任何判据**会让这件事自己报警 —— 本脚本就是那条判据。

## 口径（**先立口径，再谈数字**；照 t75 的纪律）

| 量 | 口径 | 关键限制 |
|---|---|---|
| **同库读数** `self_exists_ratio` | 在**同一个库**里按 `memory_id` 回查自己最近 N 条 | 这是**对照组**（应≈1.0），**不得**与跨库读数并列成"一致性" |
| **跨库读数** `cross_exists_ratio` | 同 `memory_id` 在**对侧库**可查的比例（取样规则两库**完全一致**） | 才是"跨库差异" |
| **滞后** `lag_seconds` | **采样时刻 − 该行 `created_at`**，取"最新且在对侧存在"的那一行 | ⚠️ **存在性非单调** ⇒ 它只作**量级**，**不得**读成"从某刻起停止同步" |

⛔ **本脚本不实现 outbox / CDC**（那是 D-10 的三选一，属架构决策）—— 它只**测量与报警**。

## ⭐⭐ `DIVERGENT` 在这台机器上是【**D28-A 的既定后果**】，不是待修缺陷（G6/t109，2026-10-08）

`dsh-ops/DECISIONS_PENDING.md` 的 **D28** 逐字：**「状态：已拍板（A：保持 SQLite 为常驻 API 的库 +
修正 `trinity.yaml` 的陈旧描述；2026-09-27 用户授权）」**，并**明确否决过 B（正式上 PG）**，
理由是"**B 改变全局写入目标**……同一数据在不同端读到的不同部分 ⇒ 事故"。

⇒ 因此本脚本量到的 **A=SQLite（常驻 API 的库） vs B=PG（插件/工具面的库）** 之间的
`DIVERGENT` / `rc=1` **是那条已拍板架构的预期表现**：
**两个面分别读两个库是【已决定的设计】**（G6/t109 曾提"让 API 读 PG"，**因 D28-A 被撤回**）。
⚠️ **请勿把 `DIVERGENT` 直接当成故障、更不要据此再提一次"上 PG"** —— 那会推翻一条**用户授权**的决定；
要改口径，先改 D28（那需要用户授权，不是本判据的职责）。
⭐ 本脚本仍然有价值：它把"**分歧到了多大**（`cross_exists_ratio` / `lag_seconds`）"变成**可报警的数字**，
而"这个分歧是否可接受"是**决策**，不是本脚本能回答的。


## 四个结果（"不可比"与"不一致"必须是**不同的结果**；t77 的 rc=3 思路）

| verdict | rc | 含义 |
|---|---|---|
| `CONSISTENT` | 0 | 跨库存在性 ≥ `--min-exists-ratio` 且滞后 ≤ `--max-lag-seconds` |
| `DIVERGENT` | 1 | 真有差异（`reason` 细分为 `missing_rows` / `lag_exceeded` / `both`） |
| `INCOMPARABLE` | **3** | **口径不适用**（两侧同一个库 / 表不存在 / 命名空间不一致 / 取样规则对不上）⇒ **不得报"不一致"** |
| `UNTESTABLE` | 2 | 读不了（连不上、无样本） |

用法（**只读**）：
    python scripts/cross_store_reconcile_probe.py --a sqlite:<path> --b pg:<creds-name> [--json]
    python scripts/cross_store_reconcile_probe.py --selftest          # 用内存假库自证三态
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: 取样规则（**两库必须完全一致**，否则"不可比"）：最新的 N 个 active 行
DEFAULT_K = 40#: 判定阈值（显式，可被 CLI 覆盖 —— 判据必须可失败）
DEFAULT_MIN_EXISTS_RATIO = 0.95
DEFAULT_MAX_LAG_SECONDS = 3600.0
#: **同库自检**阈值：一侧连"自己的最新行"都查不到 ⇒ 是**读数/配置**坏了，不是"跨库不一致"
#: （留 0.9 容忍采样与回查之间的并发删除；低于它 ⇒ `INCOMPARABLE/self_check_failed`）
DEFAULT_MIN_SELF_RATIO = 0.9

#: G6/t109（2026-10-08 修）：**默认值必须是"真的那两个库"**。
#: 原版 `--a/--b` 默认空串 ⇒ 无参跑只打印 usage + rc=2，**且不吐 JSON**（与其它失败路径不一致）；
#: G2/t109 与队长都撞上过这条（"rc=2 且只打印 usage、未给出错误行"）。
#: 判据要能被"照抄一行"地用，就必须有可用默认：SQLite = 常驻 API 服务的库；PG = 本机在用端口。
DEFAULT_PG_PORT = 5432


def _default_sqlite() -> str:
    env = (os.environ.get("TRINITY_STORE") or "").strip()
    cands = ([env] if env else []) + [
        os.path.join(os.path.expanduser("~"), ".trinity", "store-restored", "trinity_store.db"),
        os.path.join(os.path.expanduser("~"), ".trinity", "store", "trinity_store.db"),
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return "sqlite:%s" % p
    return ""


LAG_NOTE = ("存在性**非单调**（同一库内新旧行是否在对侧出现并非单调）⇒ "
            "lag_seconds 只作**量级**参考，**不得**读成「从某刻起停止同步」")


class Side:
    """一侧库的**只读**句柄。`kind` ∈ {sqlite, postgresql, stub}。

    统一接口（判据只依赖这三个）：
      · `newest(k)` → [(memory_id, created_at_str), ...] 按 created_at 倒序，最多 k 条
      · `has(memory_id)` → bool
      · `identity()` → 稳定的身份串（用于"两侧是不是同一个库"的**不可比**判定）
    """

    def __init__(self, kind: str, identity: str, newest_fn: Callable, has_fn: Callable,
                 count_fn: Optional[Callable] = None, note: str = "") -> None:
        self.kind = kind
        self._identity = identity
        self._newest = newest_fn
        self._has = has_fn
        self._count = count_fn
        self.note = note

    def identity(self) -> str:
        return self._identity

    def newest(self, k: int) -> List[Tuple[str, str]]:
        return list(self._newest(k))

    def has(self, memory_id: str) -> bool:
        return bool(self._has(memory_id))

    def count(self) -> Optional[int]:
        return self._count() if self._count else None

    #: 写入一律禁止（本脚本只读）—— 给判据一个可断言的牙齿
    def write(self, *_a, **_k):
        raise PermissionError("cross_store_reconcile_probe 是只读探针：不得写入任何库")


def _parse_sqlite_dt(s: str) -> Optional[float]:
    """把多种时间写法归一成 epoch（SQLite 侧 ISO-Z、PG 侧 `+08` 缺分钟位都见过）。"""
    if not s:
        return None
    c = str(s).replace("Z", "+0000")
    import re as _re
    c = _re.sub(r"([+-]\d{2})$", r"\1:00", c)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(c, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp()
        except Exception:  # noqa: BLE001
            continue
    return None


def sqlite_side(path: str) -> Side:
    """**只读**打开 SQLite（`mode=ro` URI ⇒ 任何写都会报错）。"""
    uri = "file:%s?mode=ro" % str(path).replace("\\", "/")
    con = sqlite3.connect(uri, uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("select 1 from memories limit 1")           # 表不存在 ⇒ 抛错 ⇒ 上层判 UNTESTABLE

    def newest(k: int):
        return [(str(r["memory_id"]), str(r["created_at"])) for r in con.execute(
            "select memory_id, created_at from memories where status='active' "
            "order by created_at desc limit ?", (k,))]

    def has(mid: str) -> bool:
        return con.execute("select 1 from memories where memory_id=? limit 1", (mid,)).fetchone() is not None

    def count() -> int:
        return con.execute("select count(*) from memories where status='active'").fetchone()[0]

    return Side("sqlite", "sqlite:%s" % os.path.realpath(str(path)), newest, has, count,
                note="read-only URI mode=ro")


def _pg_creds() -> Dict[str, Any]:
    """PG 凭证：**走仓内统一入口**（优先 `scripts/_pg_std.pg_creds()`，退回 `trinity.security.credentials`）。

    T87b（门禁驱动的修正）：原先本文件自己读 `~/.dsh/.credentials.yaml` ⇒ 判据
    `tests/unit/test_credentials_readers_20261006.py` 把它归入「**提到凭证路径但没有 I/O 调用点**」
    （它的 I/O 探测只认 `open(<字面路径或模块级常量>)`，看不到「上一行 `p = …credentials.yaml`、
    下一行 `open(p)`」这种**局部变量**写法）⇒ 本文件被当成盲区。
    ⇒ 处置不是改那张清单，而是**改用统一入口**：既符合仓内纪律（t31 已把 11 个 scripts/ 站点改成
    `_pg_std.pg_creds`），也让本文件**不再提及凭证路径**（不再进"提到就算"的预筛）。
    """
    try:
        import _pg_std                                     # noqa: PLC0415 —— scripts/ 内的统一入口
        return dict(_pg_std.pg_creds())
    except Exception:  # noqa: BLE001
        from trinity.security.credentials import resolve_credentials   # noqa: PLC0415
        return dict(resolve_credentials())


def postgres_side(port: int = 5432, dbname: str = "trinity", user: str = "trinity") -> Side:
    """**只读**打开 PG（`set_session(readonly=True)`）。凭证走 `_pg_creds()`（统一入口）。"""
    creds = _pg_creds()
    port = int(port or creds.get("port") or 5432)
    dbname = dbname or str(creds.get("dbname") or "trinity")
    user = user or str(creds.get("user") or "trinity")
    import psycopg2
    con = psycopg2.connect(host=creds.get("host") or "127.0.0.1", port=port, dbname=dbname,
                           user=user, password=creds.get("password") or "", connect_timeout=8)
    con.set_session(readonly=True, autocommit=True)
    cur = con.cursor()
    cur.execute("select 1 from memories limit 1")

    def newest(k: int):
        cur.execute("select memory_id::text, created_at::text from memories "
                    "where status='active' order by created_at desc limit %s", (k,))
        return [(str(r[0]), str(r[1])) for r in cur.fetchall()]

    def has(mid: str) -> bool:
        cur.execute("select 1 from memories where memory_id::text = %s limit 1", (mid,))
        return cur.fetchone() is not None

    def count() -> int:
        cur.execute("select count(*) from memories where status='active'")
        return int(cur.fetchone()[0])

    return Side("postgresql", "postgresql:127.0.0.1:%d/%s" % (port, dbname), newest, has, count,
                note="readonly session")


def stub_side(name: str, rows: Sequence[Tuple[str, str]]) -> Side:
    """内存假库（**判据专用**）：`rows` = [(memory_id, created_at), ...]。"""
    rows = [(str(a), str(b)) for a, b in rows]
    ids = {a for a, _ in rows}

    def newest(k: int):
        return sorted(rows, key=lambda r: r[1], reverse=True)[:k]

    def has(mid: str) -> bool:
        return str(mid) in ids

    def count() -> int:
        return len(rows)

    return Side("stub", "stub:%s" % name, newest, has, count, note="in-memory")


def reconcile(a: Side, b: Side, *, k: int = DEFAULT_K,
              min_exists_ratio: float = DEFAULT_MIN_EXISTS_RATIO,
              max_lag_seconds: float = DEFAULT_MAX_LAG_SECONDS,
              min_self_ratio: float = DEFAULT_MIN_SELF_RATIO,
              now: Optional[float] = None) -> Dict[str, Any]:
    """核心口径：**同库**与**跨库**分开算；"不可比"与"不一致"给不同结果。"""
    now = float(now if now is not None else datetime.datetime.now().timestamp())
    rep: Dict[str, Any] = {
        "ts": datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "caliber": {"sample": "每侧按 created_at 倒序取最新 %d 条 active 行（两库同一规则）" % k,
                    "cross_exists_ratio": "同 memory_id 在对侧可查的比例",
                    "self_exists_ratio": "**对照组**：同库回查（不得与跨库并列）",
                    "lag_seconds": "采样时刻 − 「最新且在对侧存在」那行的 created_at",
                    "lag_note": LAG_NOTE,
                    "thresholds": {"min_exists_ratio": min_exists_ratio,
                                   "max_lag_seconds": max_lag_seconds}},
        "sides": {"a": {"kind": a.kind, "identity": a.identity(), "count": a.count(),
                        "note": a.note},
                  "b": {"kind": b.kind, "identity": b.identity(), "count": b.count(),
                        "note": b.note}},
        "self": {}, "cross": {}, "verdict": None, "rc": None, "reason": None,
    }
    #: ① 不可比的前置检查：同一个库 / 一侧无样本 ⇒ **不得**报"不一致"
    if a.identity() == b.identity():
        rep.update({"verdict": "INCOMPARABLE", "rc": 3,
                    "reason": "两侧身份相同（同一库）⇒ 跨库口径不适用",
                    "note": "按 t77 的 rc=3 思路：'不可比'与'不一致'必须是不同结果"})
        return rep
    ra, rb = a.newest(k), b.newest(k)
    if not ra or not rb:
        rep.update({"verdict": "INCOMPARABLE", "rc": 3,
                    "reason": "一侧在取样窗口内没有样本（a=%d, b=%d）" % (len(ra), len(rb))})
        return rep
    #: ② 同库对照（应≈1.0；**只作对照**）
    for tag, side, rows in (("a", a, ra), ("b", b, rb)):
        hit = sum(1 for mid, _ in rows if side.has(mid))
        rep["self"][tag] = {"n": len(rows), "exists": hit,
                            "ratio": round(hit / len(rows), 4)}
    #: ②.5 **配置 vs 数据**的判别器：一侧连自己都查不到 ⇒ 读数/配置坏了（**不是**"跨库不一致"）
    bad_self = {t: v["ratio"] for t, v in rep["self"].items() if v["ratio"] < min_self_ratio}
    if bad_self:
        rep.update({"verdict": "INCOMPARABLE", "rc": 3, "reason": "self_check_failed",
                    "note": ("同库自检不达标（%s）⇒ 属于**读数/配置**问题，"
                             "**不得**报成跨库不一致（口径先立：同库是对照组）" % bad_self)})
        return rep
    #: ③ 跨库存在性（两个方向都给，**不合并成一个数**）
    for tag, src, dst, rows in (("a_to_b", a, b, ra), ("b_to_a", b, a, rb)):
        hit = sum(1 for mid, _ in rows if dst.has(mid))
        rep["cross"][tag] = {"n": len(rows), "exists": hit, "ratio": round(hit / len(rows), 4),
                             "missing": [mid for mid, _ in rows if not dst.has(mid)][:10]}
    #: ④ 滞后（只取"最新且在对侧存在"的那行；口径已声明非单调）
    lags: Dict[str, Any] = {}
    for tag, src_rows, dst in (("a_to_b", ra, b), ("b_to_a", rb, a)):
        newest_present = None
        for mid, created in src_rows:            # src_rows 已按 created_at 倒序
            if dst.has(mid):
                newest_present = (mid, created)
                break
        ep = _parse_sqlite_dt(newest_present[1]) if newest_present else None
        lags[tag] = {"newest_present_id": newest_present[0] if newest_present else None,
                     "newest_present_created_at": newest_present[1] if newest_present else None,
                     "lag_seconds": (round(now - ep, 1) if ep else None),
                     "note": LAG_NOTE}
    rep["lag"] = lags
    #: ⑤ 判定：任一方向的跨库比例不达标 或 任一方向滞后超阈值 ⇒ DIVERGENT（reason 细分）
    worst_ratio = min(v["ratio"] for v in rep["cross"].values())
    worst_lag = max((v["lag_seconds"] or 0.0) for v in lags.values())
    missing = worst_ratio < min_exists_ratio
    lagged = worst_lag > max_lag_seconds
    if missing or lagged:
        rep.update({"verdict": "DIVERGENT", "rc": 1,
                    "reason": ("both" if (missing and lagged) else
                               ("missing_rows" if missing else "lag_exceeded")),
                    "detail": {"worst_cross_ratio": worst_ratio,
                               "worst_lag_seconds": worst_lag}})
    else:
        rep.update({"verdict": "CONSISTENT", "rc": 0, "reason": "跨库存在性与滞后均在阈值内"})
    return rep


def _side_from_spec(spec: str) -> Side:
    """`sqlite:<path>` / `pg:<port>` / `stub:<name>`。"""
    if spec.startswith("sqlite:"):
        return sqlite_side(spec.split(":", 1)[1])
    if spec.startswith("pg:"):
        return postgres_side(port=int(spec.split(":", 1)[1] or 5432))
    raise SystemExit("不认识的 side 规格：%r（预期 sqlite:<path> 或 pg:<port>）" % spec)


def _selftest() -> int:
    """用内存假库自证三态（**不碰任何真实库**）。"""
    base = [("m%d" % i, "2026-10-07T1%d:00:00+00:00" % i) for i in range(5)]
    cases = {
        "CONSISTENT": (stub_side("a", base), stub_side("b", base[::-1])),
        "DIVERGENT": (stub_side("a", base), stub_side("b", base[:1])),
        "INCOMPARABLE": (stub_side("a", base), stub_side("a", base)),
    }
    ok = True
    for want, (a, b) in cases.items():
        r = reconcile(a, b, k=5, min_exists_ratio=0.95, max_lag_seconds=10 ** 9)
        got = r["verdict"]
        ok &= (got == want)
        print("%-14s verdict=%-13s rc=%s reason=%s" % (want, got, r["rc"], r["reason"]))
    return 0 if ok else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="两库一致性/滞后判据（只读）")
    ap.add_argument("--a", default="", help="sqlite:<path>")
    ap.add_argument("--b", default="", help="pg:<port>")
    ap.add_argument("--k", type=int, default=DEFAULT_K)
    ap.add_argument("--min-exists-ratio", type=float, default=DEFAULT_MIN_EXISTS_RATIO)
    ap.add_argument("--max-lag-seconds", type=float, default=DEFAULT_MAX_LAG_SECONDS)
    ap.add_argument("--min-self-ratio", type=float, default=DEFAULT_MIN_SELF_RATIO)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args(list(argv) if argv is not None else None)
    if a.selftest:
        return _selftest()
    #: ⭐ G6/t109：无参跑**不再只打印 usage** —— 走"真默认库"，且缺规格时**吐 JSON**（与其它路径一致）。
    spec_a = a.a or _default_sqlite()
    spec_b = a.b or "pg:%d" % DEFAULT_PG_PORT
    if not (spec_a and spec_b):
        print(json.dumps({"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          "verdict": "UNTESTABLE", "rc": 2,
                          "reason": "缺 A/B 规格且找不到默认：a=%r b=%r（--a sqlite:<path> / "
                                    "--b pg:<port>）" % (a.a, a.b)}, ensure_ascii=False, indent=1))
        return 2
    try:
        sa, sb = _side_from_spec(spec_a), _side_from_spec(spec_b)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"verdict": "UNTESTABLE", "rc": 2,
                          "reason": "%s: %s" % (type(exc).__name__, str(exc)[:140])},
                         ensure_ascii=False))
        return 2
    rep = reconcile(sa, sb, k=a.k, min_exists_ratio=a.min_exists_ratio,
                    max_lag_seconds=a.max_lag_seconds, min_self_ratio=a.min_self_ratio)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=1)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print("两库一致性判据 —— %s" % rep["ts"])
        print("  A=%s  B=%s" % (rep["sides"]["a"]["identity"], rep["sides"]["b"]["identity"]))
        for tag, v in rep["cross"].items():
            print("  跨库 %-6s exists=%s/%s ratio=%s" % (tag, v["exists"], v["n"], v["ratio"]))
        for tag, v in rep.get("lag", {}).items():
            print("  滞后 %-6s lag_seconds=%s" % (tag, v["lag_seconds"]))
        print("  ⇒ **%s** (rc=%s, reason=%s)" % (rep["verdict"], rep["rc"], rep["reason"]))
    return int(rep["rc"] or 0)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception as _exc:  # noqa: BLE001
        #: T94/C2：**静默必须是显式选择** —— 流重配置失败（罕见）不阻断，但**留痕**（stderr 一行）。
        print("[warn] stdout 重配置为 utf-8 失败（继续，可能影响中文输出）：%r" % (_exc,),
              file=sys.stderr)
    raise SystemExit(main())
