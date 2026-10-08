#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pending_activation.py — 逐条核验「待重启才生效」的改动，让状态**自愈**。

## 为什么（§783.6 遗留①）

此前把"线上未生效"写成一句话。实测（`scripts/activation_scope_probe.py`）表明它**分层**：

| 进程类 | 读代码时机 | 实测（同一修复） |
|---|---|---|
| 新起 worker（DSH 会话走的路） | 每次 spawn | **9.5/10 ⇒ 已生效** |
| 常驻 api(:8001) / mcp(:8003) | 上次启动 | 5.57/10 ⇒ 待重启 |

⇒ 把"待重启"的**残留部分**登记在 `dsh-ops/PENDING_ACTIVATION.json`，本脚本逐条核验：
核验通过即报 ACTIVE（**不改文件**，避免"为了让闸门变绿而改登记"）。

退出码：0 = 无待生效项；1 = 仍有待生效项（**这是状态，不是失败** —— 用于 CI/巡检可读）。

用法：
    python scripts/pending_activation.py
    python scripts/pending_activation.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

#: P0-1/P1-1：`opening` 的**生产来源标签**。刻意本地定义为字面量（本脚本保持可独立运行，
#: 不为一个字符串去 import `trinity.engine_worker` 而拉起整个引擎）。
#: 与 `trinity/engine_worker.py::ORIGIN_PLUGIN` **必须一致** ——
#: 由 `tests/unit/test_opening_origin_dimension.py` 的一致性用例压着。
ORIGIN_PLUGIN = "dsh-plugin"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "dsh-ops", "PENDING_ACTIVATION.json")

#: openapi **就绪阈值**（§1091-§1093）：少于这么多路由视为「API 半可用」⇒ 一律 UNKNOWN，
#: 不判「路由不存在」。取 50 与本仓既有就绪守卫（doc_claims_check 的 MIN_OPENAPI_ROUTES）同值。
MIN_OPENAPI_ROUTES = 50


def _proc_start_epoch(port: int) -> "float | None":
    """监听该端口的进程的启动时间（epoch 秒）。用 CIM 取，不依赖 psutil。"""
    try:
        import service_restart_gated as srg  # 复用端口→pid 解析
        pid = srg.port_pid(port)
        if not pid:
            return None
        # 自纠（留痕）：初版用 `Get-CimInstance ... CreationDate` + `-UFormat %%s` 解析，
        # 在 Python 里拼字符串时 % 转义出错 ⇒ 恒返回 None（判据静默失效）。
        # 改用下面这条已单独验证过的形态（pid 23540 → 1789566413.689）。
        ps = ("(Get-Process -Id %d).StartTime.ToUniversalTime()"
              ".Subtract([datetime]'1970-01-01').TotalSeconds" % pid)
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        out = (r.stdout or "").strip().splitlines()
        # 自纠（留痕）：初版用 `out[-1].lstrip("-").isdigit()` 当守卫 —— 只认**整数**，
        # 而 PowerShell 返回的是 `1789566413.689` 这种浮点 ⇒ **恒判失败**，判据静默失效
        # （表现为 st=None ⇒ UNKNOWN）。已改为直接 float() + 异常兜底。
        try:
            return float(out[-1].strip())
        except (IndexError, ValueError):
            return None
    except Exception:  # noqa: BLE001
        return None


def _commit_epoch(rev: str) -> "float | None":
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%ct", rev], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20)
        return float((r.stdout or "").strip())
    except Exception:  # noqa: BLE001
        return None


def check_service_after_commit(port: int, rev: str) -> dict:
    """**通用激活判据**：常驻服务进程的启动时间晚于修复提交 ⇒ 已吃到该改动。

    比"跑一次行为探针"更普适（MCP 没有对应的行为探针），也比"人肉记得重启过"可靠：
    只要服务没重启，它的启动时间就早于提交 ⇒ 判 PENDING，**不可能自欺**。
    """
    st, ct = _proc_start_epoch(port), _commit_epoch(rev)
    if st is None or ct is None:
        return {"state": "UNKNOWN", "detail": "取不到进程启动时间或提交时间（st=%s ct=%s）" % (st, ct)}
    from datetime import datetime
    def fmt(e):  # t76：原为 lambda（曾被一条 E731 抑制指令压掉）；改 def 去掉抑制，语义不变
        return datetime.fromtimestamp(e).strftime("%m-%d %H:%M:%S")
    if st >= ct:
        return {"state": "ACTIVE",
                "detail": "服务启动于 %s，晚于修复提交 %s(%s) ⇒ 已吃到修复" % (fmt(st), rev[:8], fmt(ct))}
    return {"state": "PENDING",
            "detail": "服务启动于 %s，**早于**修复提交 %s(%s) ⇒ 仍是旧代码，待重启" % (fmt(st), rev[:8], fmt(ct))}


def check_topup() -> dict:
    """作用域补齐是否已在常驻服务生效：复用 verify_scoped_topup_live.py 的判定。"""
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                     "verify_scoped_topup_live.py"),
                        "--limit", "30", "--json"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    line = [x for x in (r.stdout or "").splitlines() if x.strip().startswith("{")]
    if not line:
        return {"state": "UNKNOWN", "detail": (r.stderr or r.stdout or "")[-160:]}
    d = json.loads(line[-1])
    return {"state": d.get("verdict") or "UNKNOWN",
            "detail": "当前平均返回 %s（基线 %s）空结果 %s"
                      % (d.get("avg_returned"), (d.get("baseline") or {}).get("avg_returned"),
                         d.get("empty"))}


def check_evidence_gate() -> dict:
    """评测语料判据：**代码层** + **api 常驻进程是否已重启到该提交之后**（两条都要）。"""
    code = ("import sys; sys.path.insert(0, %r);"
            "from trinity.retrieval.evidence_gate import is_eval_row;"
            "print('YES' if is_eval_row({'persona_id':'ckpt-test'}) else 'NO')" % ROOT)
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    code_ok = "YES" in (r.stdout or "")
    svc = check_service_after_commit(8001, "538ccd1")     # 该改动的提交之一
    if code_ok and svc.get("state") == "ACTIVE":
        return {"state": "ACTIVE", "detail": "代码层生效；且 %s" % svc["detail"]}
    return {"state": "CODE_OK" if code_ok else "UNKNOWN",
            "detail": "代码层判 persona=ckpt-test 为评测语料 = %s；常驻侧：%s"
                      % (code_ok, svc.get("detail"))}


def check_opening_recall() -> dict:
    """开场浮现（P0-1/P1-1）是否已在**真实会话**上生效。

    判据：`~/.trinity/data/opening_surface_counters.json` 的
    **`by_origin['dsh-plugin'].calls > 0`**。

    为什么用这个而不是"门控有没有打开"：插件在门控打开时自报 `origin=dsh-plugin`，
    而所有探针一律自报 `probe:*`（见 `engine_worker.ORIGIN_PLUGIN`）⇒ 只有这个桶
    才代表**真实 DSH 会话真的调过**。**探针把桶填满不算生效**
    —— 那正是本项要治的病（"被测量代替了被使用"）。

    附带旁证（不参与判定）：门控是否已持久化到环境变量，用来区分
    "没打开" 与 "打开了但还没有真实会话跑过"。
    """
    p = os.path.expanduser("~/.trinity/data/opening_surface_counters.json")
    try:
        with open(p, encoding="utf-8") as fh:
            oc = json.load(fh) or {}
    except Exception as exc:  # noqa: BLE001
        return {"state": "UNKNOWN", "detail": "读不到 %s：%s" % (p, exc)}
    bo = oc.get("by_origin") or {}
    prod = int((bo.get(ORIGIN_PLUGIN) or {}).get("calls") or 0)
    probe = sum(int((v or {}).get("calls") or 0) for k, v in bo.items()
                if str(k).startswith("probe:"))
    gate = os.environ.get("TRINITY_AUTO_RECALL")
    gate_note = ("TRINITY_AUTO_RECALL=%s（本进程）" % gate) if gate else \
        "TRINITY_AUTO_RECALL 未在本进程环境中（插件侧默认 off ⇒ 需下一次 DSH 启动才吃到）"
    if prod > 0:
        return {"state": "ACTIVE",
                "detail": "生产来源调用 %d 次（探针 %d 次）；%s" % (prod, probe, gate_note)}
    return {"state": "PENDING",
            "detail": "生产来源调用 **0** 次、探针 %d 次 ⇒ 注入通路在真实会话上尚未执行；%s"
                      % (probe, gate_note)}


def check_api_strategy_default() -> dict:
    """REST /memory/search/hybrid 的默认融合策略是否已变成引擎默认的 rrf（§806）。

    判据是**行为**的，不是结构性的：不传 strategy 打一次真接口，读回响应体里的
    strategy 字段。

      · 响应 strategy == "rrf"    ⇒ ACTIVE（常驻服务已在用与引擎一致的默认策略）
      · 响应 strategy == "fusion" ⇒ PENDING（还是旧代码；本轮『不发布』未重启常驻服务）
      · 服务不可达 / 无该字段     ⇒ UNKNOWN

    为什么还带一条代码层读数：把"代码没改"与"改了但服务陈旧"分开 —— 两者的处置
    完全不同（前者是没做，后者只是没重启）。按本仓纪律**行为优先**：
    结构性判据（进程启动时间 vs 提交时间）在这里能判，但行为读数更直接，故以行为为准。
    """
    code = ("import sys; sys.path.insert(0, %r);"
            "from trinity.api.server._models import HybridSearchRequest as R;"
            "print(R(query='x').strategy)" % ROOT)
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    lines = [x for x in (r.stdout or "").splitlines() if x.strip()]
    code_default = lines[-1] if lines else "?"
    try:
        import urllib.request
        body = json.dumps({"query": "default strategy activation probe",
                           "top_k": 1}).encode()
        req = urllib.request.Request("http://127.0.0.1:8001/memory/search/hybrid",
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            live = (json.loads(resp.read().decode("utf-8")) or {}).get("strategy")
    except Exception as exc:  # noqa: BLE001
        return {"state": "UNKNOWN",
                "detail": "代码层默认=%s；常驻服务不可达：%s" % (code_default, str(exc)[:120])}
    if live == "rrf":
        return {"state": "ACTIVE",
                "detail": "常驻服务响应 strategy=rrf（代码层默认=%s）⇒ 已生效" % code_default}
    return {"state": "PENDING",
            "detail": ("常驻服务响应 strategy=%s（代码层默认=%s）⇒ 仍是旧代码，待重启"
                       % (live, code_default))}


def check_doc_lexical_rerank_route() -> dict:
    """新增路由 `POST /memory/search/lexical-rerank` 是否已注册到常驻 api（§1179-§1180）。

    判据**照本条目自己声明的 `active_when`**（不另立一把尺）：
      · `GET /openapi.json` 路由数 ≥ 就绪阈值 **且** 该路径在 `paths` 里；
      · 路由数 < 阈值 ⇒ **UNKNOWN「半可用」**，不判「不存在」——
        §1091-§1093 实测过：API 重启窗口里 `/openapi.json` 能取到但只回少数路由，
        照「不在就报缺失」读会一次读出 39 条假问题（复核只有 3 条）。
    另外单独给一条**行为读数**（POST 一次，读响应里的 `lexical_rerank` 元数据），
    它**不参与 ACTIVE 判定**：`enabled=false` 的三种原因（empty_index /
    no_query_token_match / no_candidate_evidence）都是**正常业务态**，不是「没生效」；
    但要把 `enabled=True 却顺序没动` 这类无法归因的情况挡在门外，所以必须原样报出来。

    为什么还带代码层读数：把「代码没改」与「改了但服务陈旧」分开 —— 两者处置完全不同
    （前者是没做，后者只是没重启）。与 `check_api_strategy_default` 同一套写法。
    """
    code = ("import sys; sys.path.insert(0, %r);"
            "from trinity.api.server import _routers_search as m;"
            "print('YES' if hasattr(m, 'lexical_rerank_search') else 'NO')" % ROOT)
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    code_ok = "YES" in (r.stdout or "")
    route = "/memory/search/lexical-rerank"
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8001/openapi.json", timeout=20) as resp:
            spec = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"state": "UNKNOWN",
                "detail": "代码层有该路由=%s；常驻服务不可达（不判不存在）：%s"
                          % (code_ok, str(exc)[:120])}
    paths = spec.get("paths") or {}
    n = len(paths)
    if n < MIN_OPENAPI_ROUTES:
        return {"state": "UNKNOWN",
                "detail": ("openapi 只回 %d 条路由（< 就绪阈值 %d ⇒ 半可用，**不判「不存在」**）；"
                           "代码层有该路由=%s" % (n, MIN_OPENAPI_ROUTES, code_ok))}
    if route not in paths:
        return {"state": "PENDING",
                "detail": ("openapi 就绪（%d 条路由）但没有 %s ⇒ 常驻服务仍是旧代码，待重启"
                           "（代码层有该路由=%s）" % (n, route, code_ok))}
    # 行为读数（不参与判定）：失败只写进 detail，绝不把 ACTIVE 翻成 PENDING
    beh = "行为探针未执行"
    try:
        body = json.dumps({"query": "Trinity 记忆 检索", "top_k": 1,
                           "persona_id": "trinity-docs"}).encode()
        req = urllib.request.Request("http://127.0.0.1:8001" + route, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        lr = d.get("lexical_rerank") or {}
        beh = ("行为：enabled=%s reason=%s fetch_k=%s 候选进/出=%s/%s 索引文档=%s"
               % (lr.get("enabled"), lr.get("reason"), lr.get("fetch_k"),
                  lr.get("candidates_in"), lr.get("candidates_out"), lr.get("index_docs")))
    except Exception as exc:  # noqa: BLE001
        beh = "行为：调用失败（不影响本项判定）：%s" % str(exc)[:120]
    return {"state": "ACTIVE",
            "detail": "openapi 就绪（%d 条路由）且 %s 已注册（代码层=%s）；%s"
                      % (n, route, code_ok, beh)}


def check_ann_vector_persist_metrics() -> dict:
    """`/metrics` 是否已在**常驻** api 上发出索引持久化成色（§1270）。

    为什么这条要登记：§1270 新增的 `trinity_ann_vector_persist_total{outcome=...}` 是
    「池写了、索引文件没写」这条**写侧哑线**的唯一出口；代码层已生效（进程内 TestClient 验过），
    但常驻 api 起于改动之前 ⇒ 按定义它现在**还没有**这行 ⇒ 需要一个「待重启」的登记项，
    否则这件事会以「代码里明明有、线上就是查不到」的形态悄悄过期。

    判据（照本条目自己的 `active_when`，不另立一把尺）：
      · 代码层：`_routers_health.py` 里有该 series 名；
      · 常驻：`GET /metrics` 里有该 series；
      · **取不到 `/metrics` ⇒ UNKNOWN，不判「不存在」**（§13.2：取不到 ≠ 不存在）。
    """
    code_src = os.path.join(ROOT, "trinity", "api", "server", "_routers_health.py")
    try:
        code_ok = "trinity_ann_vector_persist_total" in open(code_src, encoding="utf-8").read()
    except OSError as exc:
        code_ok = False
        _code_err = str(exc)[:80]
    else:
        _code_err = ""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8001/metrics", timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return {"state": "UNKNOWN",
                "detail": "代码层有该 series=%s；常驻服务 /metrics 不可达（**不判不存在**）：%s"
                          % (code_ok, str(exc)[:120])}
    if "trinity_ann_vector_persist_total" not in text:
        return {"state": "PENDING",
                "detail": ("/metrics 可取（%d 字节）但没有该 series ⇒ 常驻服务仍是旧代码，待重启"
                           "（代码层有=%s%s）"
                           % (len(text), code_ok, ("；读源代码失败：" + _code_err) if _code_err else ""))}
    vals = [ln for ln in text.splitlines() if ln.startswith("trinity_ann_vector_persist_total")]
    outcome = "outcome=\"skipped_no_index\""
    skips = [ln for ln in vals if outcome in ln]
    return {"state": "ACTIVE",
            "detail": "常驻 /metrics 已发出该 series（%d 行）%s；代码层有=%s"
                      % (len(vals), ("；其中 %s" % skips[0]) if skips else "", code_ok)}


CHECKS = {"search_hybrid_scoped_topup": check_topup,
          "evidence_gate_persona_scope_and_atlas_allowlist": check_evidence_gate,
          # 通用判据：服务进程启动时间 vs 修复提交时间（MCP 无行为探针，用这个）
          "mcp_http_scoped_topup": lambda: check_service_after_commit(8003, "645c6b1"),
          # P0-1/P1-1：开场浮现的**来源分桶**判据（探针不算生效）
          "opening_recall_gate_on": check_opening_recall,
          # §806：REST 默认融合策略 fusion→rrf —— **行为**判据（读回响应体的 strategy 字段）
          "api_default_strategy_rrf": check_api_strategy_default,
          # §1179-§1180：新增路由是否已注册到常驻 api（就绪守卫 + 路由存在；
          # 行为读数只作证据、不参与判定）—— 补登记项缺失的核验函数（§1257）
          "doc_lexical_rerank_route": check_doc_lexical_rerank_route,
          # §1270：/metrics 的新 series（索引持久化成色）—— 常驻 api 需重启才可见
          "ann_vector_persist_metrics": check_ann_vector_persist_metrics}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    man = json.load(open(MANIFEST, encoding="utf-8"))
    rows = []
    for it in man.get("items") or []:
        fn = CHECKS.get(it["id"])
        res = fn() if fn else {"state": "NO_CHECK", "detail": "无自动核验（见 manual_note）"}
        # ACTIVE 判定：补齐项用 ACTIVE；判据项用 CODE_OK（代码已生效）+ 常驻待重启
        active = res["state"] in ("ACTIVE", "CODE_OK")
        rows.append({"id": it["id"], "affects": it.get("affects"),
                     "check_cmd": it.get("check_cmd"), "active_in_code": active,
                     "resident_pending": it.get("process_class") == "resident_service",
                     **res})

    pending = [r for r in rows if r["resident_pending"] and r["state"] not in ("ACTIVE",)]
    if a.json:
        print(json.dumps({"items": rows, "pending_count": len(pending)}, ensure_ascii=False))
    else:
        print("待重启生效项核验（%s）" % MANIFEST)
        for r in rows:
            print("  [%s] %s" % (r["state"], r["id"]))
            print("        影响：%s" % ", ".join(r.get("affects") or []))
            print("        %s" % r["detail"])
            print("        核验命令：%s" % r["check_cmd"])
        print()
        print("%d 项在**代码层已生效**；%d 项**待常驻服务重启**"
              % (sum(1 for r in rows if r["active_in_code"]), len(pending)))
        if pending:
            print("⇒ 重启 api(:8001)/mcp(:8003) 后复跑本命令，待生效项应转 ACTIVE。")
            print("   （DSH 路径**已生效**：每次会话新起 worker，读的是当前代码 —— "
                  "见 scripts/activation_scope_probe.py 的实测。）")
    return 1 if pending else 0


if __name__ == "__main__":
    sys.exit(main())
