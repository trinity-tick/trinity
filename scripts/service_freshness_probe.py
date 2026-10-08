#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""service_freshness_probe.py —— **在线服务的语料新鲜度**判据（§792 的可执行化）。

## 为什么需要它（§792 实测暴露）

2026-09-17 实测：**常驻 api(:8001) 检索不到它启动之后写入的任何记忆**（11/11 单向）。
后果比"仪器分歧"严重得多 —— 一个记忆系统的在线服务，
**看不到自己启动之后被记住的东西**；而所有 REST 口径的检索评测量的是
**冻结在启动时刻的语料视图**。

**最值得记的一点**：本仓闸门 `retrieval_wiring_audit` **两周前就登记过**
`ann_index.add_vector` / `startup_prewarm` 在生产路径**零引用** ——
但那是**离后果很远的信号**（"这个函数没人调用"），没有人把它与
"服务看不到新记忆"连起来。本判据直接测**后果**：

    取启动之后写入的记忆，用其**正文**当 query，问服务能不能查到。

## 判据

    fresh_visible / n  == n  ⇒  FRESH_OK     （服务能看到启动后的写入）
    fresh_visible / n  <  n  ⇒  STALE_VIEW   （**服务视图冻结**，exit 1）
    取不到样本 / 服务不可达  ⇒  UNTESTABLE   （exit **2**，**fail-closed**）
    **探针读的库 ≠ 服务服务的库 ⇒ CROSS_STORE_MISSING（exit 3，口径不适用）**

### ⚠️ 归因前提（T77/I17 加，**这条以前缺**）

`STALE_VIEW` 的含义是"**服务自己看不见自己应该有的写入**"。它只在
**探针读的库 == 被测服务服务的库**时才成立。t75（I15）实测过反例：
本机常驻 API 的 `/diagnostics` 报 `adapter=sqlite`，而本探针默认读 **PG**
（第 53 行 `setdefault("TRINITY_STORAGE_BACKEND","postgresql")`）⇒ 那时给出的 `STALE_VIEW`
是**跨库缺失**，会把人引向"重启服务"，而重启与病因无关。

⇒ 现在探针启动时会**读一次服务侧 `/diagnostics` 的 adapter** 并与**自身 backend**（按适配器实例判）比对：
不一致 ⇒ `CROSS_STORE_MISSING`（**绝不再叫 STALE_VIEW**）；一致 ⇒ 原语义不变；
取不到 ⇒ **不猜**：保留原 verdict，但把"归因前提未验证"写进 `why` 与 `adapter_check` 字段。

### 退出码（T77 明确化）

    0 = FRESH_OK          服务能看到启动后写入
    1 = STALE_VIEW / NOT_RANKED   **服务侧**问题（冻结 / 排序）
    2 = UNTESTABLE        测不了（取不到样本或服务不可达）
    3 = CROSS_STORE_MISSING   **口径不适用**（探针的库 ≠ 服务的库）——
        ⚠️ **不得**把 rc=3 读成"服务有问题"；闸门应据此**跳过**该条，而不是报红

⚠️ **取样必须排除 `perception` / `test` 类**：§792 的**首跑对照缺陷**就是栽在这 ——
那两类可能被"图册来源白名单/证据门控"按**类别**剔除，与"时间冻结"混淆。
排除之后才能把"时间"这一个变量隔出来。

## 为什么**暂不进标准闸门集**（诚实说明，本仓纪律）

它现在**必然是红的**（缺陷还在）。把一个恒红的判据放进闸门集 =
本仓 G10 前科「**恒红的闸门会被忽略**」。
故本轮作 **REPORT-ONLY**：出数、给 verdict、写证据，但**不改 `docs/GATE_SET.json`**；
**待 §792 的缺陷修复后**再把它升为 required（与 P1-0 处理 `U1-c` 的方式一致：
"降级不是删除"，升级要走同一条路）。

用法：
    python scripts/service_freshness_probe.py [--port 8001] [--n 6] [--json]
退出码：0=FRESH_OK；1=STALE_VIEW/NOT_RANKED；2=UNTESTABLE（fail-closed）；
        3=CROSS_STORE_MISSING（顺口径不适用，**不是**"服务有问题"）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")

#: 取样必须排除的类别前缀（**判据的一部分**，不是实现细节）：
#: 它们可能被图册白名单/证据门控**按类别**剔除 ⇒ 与"时间冻结"混淆（§792.1 的对照缺陷）。
EXCLUDE_CATEGORY_PREFIXES = ("perception", "test")


def evaluate(start_epoch, rows, ask, limit=None, by_id=None) -> dict:
    """纯函数：给定「启动后写入的行」与「问服务」的可调用对象，判定新鲜度。

    `rows`: [(memory_id, content, category, created_at)]
    `ask`: content -> set(memory_ids)（由服务返回）
    `limit`: 最多判定多少条**可用**样本（排除类别之后计；None=全部）
    `by_id`: memory_id -> bool（**对照探针**：绕开检索、按 id 直取）

    2026-09-18（判据修正，**纠正 §792 的结论**）：原判据只看"是否出现在 top-k 里"，
    于是把**排序问题**误判成**视图冻结**。实测反例（同一进程、同一时刻）：
      · 近重复长 query（探针自己攒下的近重复语料）⇒ 新记忆排不进 top-5，判 STALE_VIEW；
      · 唯一标记词 query             ⇒ 同一条记忆**被检索到**，且按 id 直取 200。
    ⇒ 服务并没有冻结，是"**新记忆在近重复语料里排不进 top-k**"。
    故补 `by_id` 对照：top-k 未命中但按 id 可见 ⇒ **NOT_RANKED**（排序/去重问题），
    只有"按 id 也取不到"才算 **STALE_VIEW**（真冻结）。两者混为一谈会误导修复方向
    （本轮就曾据此去重启服务——重启并不解决问题）。
    """
    ok_rows, skip = [], []
    for mid, content, cat, created in rows:
        c = str(cat or "")
        if any(c.startswith(p) for p in EXCLUDE_CATEGORY_PREFIXES):
            skip.append({"id": mid, "category": c, "why": "排除类别（白名单混淆）"})
            continue
        ok_rows.append((mid, content, c, created))
    if limit:
        ok_rows = ok_rows[:limit]
    if not ok_rows:
        return {"verdict": "UNTESTABLE", "n": 0, "visible": 0, "skipped": skip,
                "why": "启动后写入的行全部落在排除类别里（没有可用于判定的样本）"}
    detail, visible = [], 0
    for mid, content, c, created in ok_rows:
        try:
            got = set(ask(content))
        except Exception as exc:  # noqa: BLE001
            return {"verdict": "UNTESTABLE", "n": len(ok_rows), "visible": visible,
                    "skipped": skip, "why": "服务不可达：%s" % str(exc)[:80], "detail": detail}
        hit = mid in got
        row = {"id": mid, "category": c, "created": created, "visible": hit}
        if not hit and by_id is not None:
            # 对照：绕开检索按 id 直取 —— 区分"排序没排上"与"视图真的看不到"
            try:
                row["by_id"] = bool(by_id(mid))
            except Exception as _e:  # noqa: BLE001
                row["by_id"] = None
        visible += 1 if hit else 0
        detail.append(row)
    if visible == len(ok_rows):
        verdict, why = "FRESH_OK", "服务能看到启动之后写入的全部记忆"
    else:
        missing = [d for d in detail if not d["visible"]]
        # 只要有一条"按 id 也取不到"，才算真冻结（保守：宁可报 STALE_VIEW）
        frozen = [d for d in missing if d.get("by_id") is False]
        if by_id is not None and not frozen:
            verdict = "NOT_RANKED"
            why = ("服务**能看到**这些记忆（按 id 直取成功），但它们没进 top-k ⇒ "
                   "**排序/近重复问题，不是视图冻结**（勿据此重启服务）")
        else:
            verdict = "STALE_VIEW"
            why = "服务**看不到**启动之后写入的记忆 ⇒ 视图冻结在启动时刻"
    return {"verdict": verdict, "n": len(ok_rows), "visible": visible,
            "skipped": skip, "detail": detail, "why": why}


def _dialect(adapter) -> str:
    """按**实例自身的类/模块名**判定方言（不看环境变量）。

    为什么按实例：PG 不可用时引擎会**降级到 SQLite 只读模式**（`trinity/_tags.py::_conn_ctx`
    的注释就记着这条）⇒ 若按 `TRINITY_STORAGE_BACKEND` 判定，降级时就会选错占位符风格。
    """
    name = ("%s.%s" % (type(adapter).__module__, type(adapter).__name__)).lower()
    if "sqlite" in name:
        return "sqlite"
    if "postgres" in name or "psycopg" in name:
        return "postgresql"
    return "unknown"


def _fresh_rows(after_epoch: float, n: int, adapter=None):
    """取启动之后写入的行（**两种后端都可用**，不依赖适配器私有属性）。

    ⚠️ T73/I13 修的两个真缺陷都在这里：

    * **缺陷 2（私有接口）**：原实现 `with ad._get_conn() as conn:` + PG 专有 SQL
      （`memory_id::text` / `to_timestamp(%s)` / `%s`）⇒ SQLite 后端下
      `'SQLiteAdapter' object has no attribute '_get_conn'`。
      现改为：连接上下文走**仓库既有的共享助手** `trinity._tags._conn_ctx(adapter)`
      （它按适配器实际能力在 PG `_get_conn()` 与 SQLite 裸 `_conn` 之间切换，
      并明写"两者占位符风格不同"这条纪律），SQL 按 `_dialect()` 选方言。
    * 本函数**不新增生产侧公开方法**（`trinity/adapters/**` 不在本任务写域）——
      复用共享助手等价于"用公开读法"，且不把私有属性名再抄进本脚本。

    `adapter` 传入时直接用它（判据/单测用），否则自行 `Trinity()`。
    """
    from trinity._tags import _conn_ctx
    if adapter is None:
        from trinity.core.client import Trinity
        adapter = Trinity()._adapter
    dia = _dialect(adapter)
    limit = max(60, n * 10)
    if dia == "sqlite":
        sql = ("select memory_id, substr(content,1,160), category, created_at "
               "from memories where status='active' and length(content) > 60 "
               "and datetime(created_at) > datetime(?, 'unixepoch') "
               "order by created_at desc limit ?")
        params = (after_epoch, limit)
    elif dia == "postgresql":
        sql = ("select memory_id::text, left(content, 160), category, created_at::text "
               "from memories where status='active' and length(content) > 60 "
               "and created_at > to_timestamp(%s) order by created_at desc limit %s")
        params = (after_epoch, limit)
    else:
        raise RuntimeError("未知存储后端（%s）：本探针只实现了 sqlite/postgresql 两种方言"
                           % type(adapter).__name__)
    ctx = _conn_ctx(adapter)
    if ctx is None:
        raise RuntimeError("适配器没有可用的连接接口（%s）：既非 _get_conn 也无 _conn"
                           % type(adapter).__name__)
    with ctx as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()


#: 兼容别名（旧名带 `_pg_` 前缀，会让人以为"只能 PG"；保留以免外部引用断裂）
def _pg_rows(after_epoch: float, n: int):  # pragma: no cover - 兼容壳
    return _fresh_rows(after_epoch, n)


def _service_adapter(base: str, timeout: float = 20.0):
    """读**服务侧**的适配器标识（`GET /diagnostics` 的 `adapter.adapter`）。

    T77/I17：这是"归因正确性"的前提 —— 探针读的库与被测服务的库**必须同源**，
    否则 `STALE_VIEW`（"服务视图冻结在启动时刻"）这个归因就是错的。
    取不到就返回 `(None, 原因)`，**不猜**。
    """
    try:
        import urllib.request as _u
        with _u.urlopen(base.rstrip("/") + "/diagnostics", timeout=timeout) as r:
            d = json.load(r)
        ad = d.get("adapter")
        if isinstance(ad, dict):
            return (str(ad.get("adapter") or "") or None), None
        if isinstance(ad, str) and ad:
            return ad, None
        return (str(d.get("backend") or "") or None), None
    except Exception as exc:  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, str(exc)[:120])


def _probe_backend():
    """探针**自己**读的是哪种库：优先按**适配器实例**判（`_dialect`，不看环境变量），
    实例判不出来才退回环境变量。返回 `(backend, source, error)`，`source ∈ {instance, env}`。
    """
    try:
        from trinity.core.client import Trinity
        ad = Trinity()._adapter
        d = _dialect(ad)
        if d != "unknown":
            return d, "instance", None
        return (str(os.environ.get("TRINITY_STORAGE_BACKEND") or "") or None), "env", None
    except Exception as exc:  # noqa: BLE001
        env = str(os.environ.get("TRINITY_STORAGE_BACKEND") or "") or None
        return env, "env", "%s: %s" % (type(exc).__name__, str(exc)[:120])


def _fail(args, why: str, rc: int = 2, **extra) -> int:
    """**失败也要说人话 + 机器可解析**（T73/I13 缺陷 1 的修法）。

    契约（`--json` 时）：**永远**在 stdout 上打印**恰好一个**合法 JSON 对象（失败路径也是），
    字段至少含 `ok=false` / `verdict=UNTESTABLE` / `reason` / `rc`；
    人类可读的 `[UNTESTABLE] …` 行**改走 stderr**（保留"同时打印"，但**不污染 stdout** ——
    与成功路径一致：`--json` 时 stdout 只有 JSON，见 main 末尾的 `if args.json:` 分支）。
    退出码**不变**（仍 fail-closed：默认 2）——修的是"失败时不说人话"，**不是**把失败放行。
    """
    payload = {"ok": False, "verdict": "UNTESTABLE", "reason": why, "rc": rc,
               "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    payload.update(extra)
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        print("[UNTESTABLE] %s" % why, file=sys.stderr)
    else:
        print("[UNTESTABLE] %s" % why)
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="在线服务语料新鲜度判据（REPORT-ONLY）")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--base", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    base = args.base or "http://127.0.0.1:%d" % args.port
    try:
        import pending_activation as pa                     # noqa: PLC0415
        start = pa._proc_start_epoch(args.port)
    except Exception as exc:  # noqa: BLE001
        #: T73/I13：失败路径**也必须**输出 JSON（`--json` 是契约，不是修饰）
        return _fail(args, "取服务启动时刻失败：%s" % exc, port=args.port, stage="proc_start")
    if not start:
        return _fail(args, "端口 %d 上没有可识别的服务进程" % args.port,
                     port=args.port, stage="proc_start")

    import datetime
    started = datetime.datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = _fresh_rows(start, args.n)
    except Exception as exc:  # noqa: BLE001
        return _fail(args, "读库失败：%s" % str(exc)[:200], port=args.port,
                     stage="read_rows", started_at=started,
                     error_type=type(exc).__name__)

    def ask(content: str):
        body = json.dumps({"query": str(content)[:120], "top_k": args.top_k}).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request(
            base + "/memory/search/hybrid", data=body,
            headers={"Content-Type": "application/json"}), timeout=60))
        return {str(h.get("memory_id")) for h in (r.get("results") or [])}

    def check_by_id(mid: str) -> bool:
        """对照探针：按 id 直取（绕开全部检索通道与缓存）。"""
        try:
            with urllib.request.urlopen(base + "/memories/" + str(mid), timeout=30) as resp:
                return int(getattr(resp, "status", resp.getcode())) == 200
        except Exception:  # noqa: BLE001
            return False

    rep = evaluate(start, rows, ask, limit=args.n, by_id=check_by_id)
    rep.update({"service": base, "started_at": started, "start_epoch": start})
    #: ── T77/I17：**归因前提检查**（探针读的库 vs 服务服务的库）────────────────────────────
    #: 不同源 ⇒ `STALE_VIEW`（"服务视图冻结在启动时刻"）是**错误归因**，会把人引向"重启服务"
    #: （t75/I15 已实测反驳：重启动与病因无关）。故换新 verdict 名，**绝不再叫 STALE_VIEW**。
    svc_adapter, svc_err = _service_adapter(base)
    probe_backend, be_source, be_err = _probe_backend()
    cross_store = bool(svc_adapter and probe_backend and svc_adapter != probe_backend)
    rep.update({
        "service_adapter": svc_adapter,
        "probe_backend": probe_backend,
        "probe_backend_source": be_source,
        "cross_store": cross_store,
        "adapter_check": {"service_adapter": svc_adapter, "service_error": svc_err,
                          "probe_backend": probe_backend, "probe_backend_source": be_source,
                          "probe_error": be_err,
                          "conclusive": bool(svc_adapter and probe_backend)},
    })
    if cross_store and rep.get("verdict") == "STALE_VIEW":
        rep["verdict_before_adapter_check"] = "STALE_VIEW"
        rep["verdict"] = "CROSS_STORE_MISSING"
        rep["why"] = ("**跨库缺失，不是视图冻结**：探针读的是 **%s** 库（判定来源=%s），"
                      "而被测服务跑的是 **%s** 库 ⇒ 这些行本来就不在服务服务的那个库里。"
                      "**重启服务与病因无关**（t75/I15 实测）。"
                      % (probe_backend, be_source, svc_adapter))
    elif (not rep["adapter_check"]["conclusive"]) and rep.get("verdict") == "STALE_VIEW":
        #: 判定不了 ⇒ **不静默**：保留原 verdict，但把"前提未验证"写进 why 与字段
        rep["why"] = ("%s（⚠️ 归因前提**未验证**：服务侧 adapter=%r err=%r；探针 backend=%r err=%r）"
                      % (rep.get("why", ""), svc_adapter, svc_err, probe_backend, be_err))
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print("在线服务语料新鲜度 —— %s（启动于 %s）" % (base, started))
        print("=" * 78)
        print("  探针读的库=%s（%s）｜服务跑的库=%s（跨库=%s）"
              % (probe_backend, be_source, svc_adapter, cross_store))
        for d in rep.get("detail", []):
            _extra = ""
            if not d["visible"] and d.get("by_id") is not None:
                _extra = "  按id直取=%s" % ("✓" if d["by_id"] else "✗")
            print("  %s cat=%-22s created=%s  服务可见=%s%s"
                  % (d["id"][:8], d["category"], str(d["created"])[:19],
                     "✓" if d["visible"] else "✗", _extra))
        if rep.get("skipped"):
            print("  排除 %d 条（%s）" % (len(rep["skipped"]), EXCLUDE_CATEGORY_PREFIXES))
        print("-" * 78)
        print("  visible %s/%s  ⇒  **%s**" % (rep["visible"], rep["n"], rep["verdict"]))
        print("  %s" % rep.get("why", ""))
        print()
        print("  [REPORT-ONLY] 本判据**暂不进标准闸门集**：它现在必然为红，")
        print("               恒红闸门会被忽略（本仓 G10）。待 §792 缺陷修复后升为 required。")
    # rc 映射（T77 明确化）：
    #   0 = FRESH_OK（服务能看到启动后写入）
    #   1 = STALE_VIEW / NOT_RANKED（**服务侧**问题：冻结 / 排序）
    #   2 = UNTESTABLE（测不了：取不到样本或服务不可达）
    #   3 = CROSS_STORE_MISSING（**口径不适用**：探针读的库 ≠ 服务服务的库）
    #       ⚠️ **不得**用 rc 表达"服务有问题" ⇒ 单列 3，与 1 区分（闸门可据此不看这条，而不是报红）
    return {"FRESH_OK": 0, "STALE_VIEW": 1, "NOT_RANKED": 1,
            "CROSS_STORE_MISSING": 3}.get(rep["verdict"], 2)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
