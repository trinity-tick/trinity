#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""delivery_slot.py —— H2（t58）：投递槽位「**紧凑索引 + 便宜 pull**」的候选形态与 A/B。

## 动机（外部前沿 + 本地实测，两条都要看）

- **外部**（`FRONTIER-DELIVERY.md` §3/F2）：Vercel（2026-01）Next.js 16 评测里
  **8KB 文档索引常驻 `AGENTS.md` ⇒ 100% 通过**，而**按需 skill ⇒ 79%**（skill 默认与"无文档"基线
  **同为 53%、+0pp**，**56% 用例根本没触发**）⇒ **push 一个紧凑索引**优于**指望模型主动 pull**。
  Trinity 的注入块实测只有 ~2.1K 字符/≈520 token ⇒ **离 8KB 还有很大余量**，但同时
  **块内是全文而不是索引** ⇒ 单条占用高、条数少。
- **本地**（t23 实测）：`DELIVERY-LAYER.md` v2 的冷槽位把**相关性**从 0.0417 拉到 0.0325
  （配对 t=−3.17, p=0.0030；**slot5 −88%**）⇒ 这是"为覆盖牺牲信号密度"的已知反模式。
  ⇒ 本实验**必须同时报覆盖与相关性**，不得只报覆盖。

## 候选形态 B（紧凑索引）长什么样（有界、可解释、可回滚）

```
相关记忆（紧凑索引，仅作参考语境；需要全文时按 id 取）:
- [episodic] 投递层回路的覆盖优先策略… id=summ_auto_session-3ca8_1789613927
- [procedural] 注入块必须保持系统提示前缀稳定… id=mem_1b0e2c16cd7c4421
取全文：trinity_search(query="<该条关键词>") 或按 id 读；本索引不含全文。
```

三条纪律：
1. **默认 off**：`TRINITY_DELIVERY_SLOT`（默认 `full`）—— `full` 时 `apply_slot` **原样返回入参对象**
   （`is` 断言），投递行为与今天**逐字节一致**；回滚 = 删开关（**不需改代码**）。
2. **有界**：总字符 ≤ `TRINITY_DELIVERY_SLOT_MAX_CHARS`（默认 900）、单条摘要 ≤ 60 字符、
   条数 ≤ `top_k`、超限**整行丢弃**并记 `truncated`。
3. **可解释**：返回 `plan`（每条 `memory_id` / `why` / `snippet_chars` / `position`）+
   `mode` / `chars` / `tokens_est` / `dropped_for_budget`。

**与 t9 的 A/B 缺陷的区别（本题已修）**：t9 的 `--selftest` 给每个臂**新建账本** ⇒
`recent_ids_seen=0`、轮换**零触发**。本模块的 A/B（`--ab`）**每臂只用一个账本、跨轮次复用**
（`--rounds`），并把 `recent_ids_seen` / `swapped_out_recent` 一并报出 ⇒
**要么看到轮换被触发，要么明确写"未触发"**。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import subprocess
import sys
import time
from collections import Counter
from itertools import product
from typing import Any, Dict, List, Optional, Sequence

#: T94/C2：**静默必须是显式选择**（Debezium 3.7 的 error/ignore 纪律）。
#: 原先本文件有 2 处 `except: pass`（清理探针临时文件、worker 收尾 kill）——现在一律**留痕**（debug 级）。
logger = logging.getLogger(__name__)

#: 槽位形态门控：`full`（默认，= 今天的行为）/ `index`（紧凑索引 + 提示按需 pull）
SLOT_GATE = "TRINITY_DELIVERY_SLOT"
#: 索引块总字符上限
SLOT_MAX_CHARS_ENV = "TRINITY_DELIVERY_SLOT_MAX_CHARS"
_SLOT_MAX_CHARS_DEFAULT = 900
#: 单条摘要字符数
_SNIPPET_CHARS = 60
_HEADER = "相关记忆（紧凑索引，仅作参考语境；需要全文时按 id 取）:\n"
_PULL_HINT = "\n取全文：trinity_search(query=\"<该条关键词>\")（本索引不含全文）"
_OFF = ("full", "off", "0", "false", "no", "")


def slot_mode() -> str:
    """`full`（默认）或 `index`。任何异常/非法值 ⇒ `full`（= 不改行为）。"""
    try:
        v = str(os.environ.get(SLOT_GATE, "full")).strip().lower()
    except Exception:  # noqa: BLE001
        return "full"
    return "index" if v == "index" else "full"


def slot_max_chars() -> int:
    try:
        return max(200, int(str(os.environ.get(SLOT_MAX_CHARS_ENV,
                                               _SLOT_MAX_CHARS_DEFAULT)).strip()))
    except Exception:  # noqa: BLE001
        return _SLOT_MAX_CHARS_DEFAULT


def _tokens(text: str) -> int:
    return (len(text) + 3) // 4          # chars/4 口径（与 U4 / t9 一致，便于横向对齐）


def _snippet(content: str, n: int = _SNIPPET_CHARS) -> str:
    s = " ".join(str(content or "").split())
    return s[:n]


def build_slot_block(items: Sequence[Dict[str, Any]], *, top_k: int = 5,
                     max_chars: Optional[int] = None,
                     pull_hint: bool = True) -> Dict[str, Any]:
    """把已选条目编成**紧凑索引块**（纯函数，fail-open：任何异常都返回空块 + reason）。"""
    out: Dict[str, Any] = {"mode": "index", "block": "", "chars": 0, "tokens_est": 0,
                           "entries": [], "dropped_for_budget": [], "truncated": False}
    try:
        if not items:
            out["reason"] = "no-items"
            return out
        budget = int(max_chars if max_chars is not None else slot_max_chars())
        lines: List[str] = []
        for i, it in enumerate(list(items)[:max(0, int(top_k))], 1):
            if not isinstance(it, dict):
                continue
            mid = str(it.get("memory_id") or it.get("id") or "")
            if not mid:
                continue
            cat = str(it.get("category") or "memory")[:24]
            body = _snippet(it.get("content") or it.get("content_preview") or "")
            line = "- [%s] %s id=%s" % (cat, body, mid)
            cand = _HEADER + "\n".join(lines + [line]) + (_PULL_HINT if pull_hint else "")
            if len(cand) > budget:
                out["dropped_for_budget"].append(mid)
                out["truncated"] = True
                continue
            lines.append(line)
            out["entries"].append({"memory_id": mid, "why": it.get("why") or it.get("_why") or "",
                                   "snippet_chars": len(body), "position": len(lines)})
        if not lines:
            out["reason"] = "budget-too-small"
            return out
        block = _HEADER + "\n".join(lines) + (_PULL_HINT if pull_hint else "")
        out.update({"block": block, "chars": len(block), "tokens_est": _tokens(block)})
        return out
    except Exception as e:  # noqa: BLE001 —— 索引形态出错 ⇒ 空块（调用方应回退成全文）
        out["reason"] = "%s: %s" % (type(e).__name__, str(e)[:120])
        return out


def apply_slot(surface: Dict[str, Any], *, mode: Optional[str] = None, top_k: int = 5,
               items: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """把 surface 的 `surface_md` 换**紧凑索引**（仅 `mode == "index"`）。

    · `full`（默认）⇒ **返回入参同一对象**（逐字节等价，回滚杠杆是真的）；
    · `index` ⇒ 返回新 dict：`surface_md` 换成索引块，**其余字段（`delivered_ids`/`cold_ids`/…）不动**
      ⇒ 账本与冷归因口径不受影响；
    · `items` 未给时从 `surface` 自身恢复（`_kept` / `delivered_ids`），拿不到内容就**返回入参**（fail-open）。
    """
    m = mode or slot_mode()
    if m != "index":
        return surface
    try:
        if not isinstance(surface, dict) or int(surface.get("sources") or 0) <= 0:
            return surface
        src_items = items
        if src_items is None:
            src_items = [it for it in (surface.get("_kept") or []) if isinstance(it, dict)]
        if not src_items:
            return surface          # 拿不到条目内容 ⇒ 不改（fail-open，绝不制造空面）
        plan = build_slot_block(src_items, top_k=top_k)
        if not plan.get("block"):
            return surface
        new = dict(surface)
        new["surface_md"] = plan["block"]
        new["slot_plan"] = plan
        new["slot_mode"] = "index"
        return new
    except Exception:  # noqa: BLE001
        return surface


# ── A/B 探针 ─────────────────────────────────────────────────────────────
def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _grams(text: str, n: int) -> Counter:
    t = " ".join(str(text).split())
    if n == 1:
        return Counter(t)
    return Counter(t[i:i + n] for i in range(max(0, len(t) - n + 1)))


def _cos(c1: Counter, c2: Counter, binary: bool = False) -> float:
    if not c1 or not c2:
        return 0.0
    if binary:
        c1, c2 = Counter(set(c1)), Counter(set(c2))
    common = set(c1) & set(c2)
    dot = sum(c1[k] * c2[k] for k in common)
    n1 = math.sqrt(sum(v * v for v in c1.values()))
    n2 = math.sqrt(sum(v * v for v in c2.values()))
    return dot / (n1 * n2) if n1 and n2 else 0.0


def _signflip(deltas: List[float]) -> Dict[str, Any]:
    n = len(deltas)
    if n == 0:
        return {"n": 0}
    obs = sum(deltas) / n
    if n <= 20:
        ge = le = 0
        total = 2 ** n
        for bits in product((1, -1), repeat=n):
            m = sum(b * d for b, d in zip(bits, deltas)) / n
            ge += 1 if m >= obs else 0
            le += 1 if m <= obs else 0
        return {"method": "exact", "n": n, "n_permutations": total,
                "observed_mean": round(obs, 6), "p_one_sided_ge": round(ge / total, 6),
                "p_one_sided_le": round(le / total, 6),
                "p_two_sided": round(min(1.0, 2 * min(ge, le) / total), 6)}
    rnd = random.Random(20261006)
    ge = le = 0
    for _ in range(20000):
        m = sum((1 if rnd.random() < 0.5 else -1) * d for d in deltas) / n
        ge += 1 if m >= obs else 0
        le += 1 if m <= obs else 0
    return {"method": "monte-carlo", "n": n, "observed_mean": round(obs, 6),
            "p_two_sided": round(min(1.0, 2 * min(ge, le) / 20000), 6)}


ARMS = {
    #: A = 现状（全文、V2 off）；B = 紧凑索引（全文→索引，V2 off）；B2 = 索引 + 覆盖优先（V2 on）
    "A_full_v1": {"TRINITY_DELIVERY_SLOT": "full", "TRINITY_DELIVERY_V2": "off"},
    "B_index_v1": {"TRINITY_DELIVERY_SLOT": "index", "TRINITY_DELIVERY_V2": "off"},
    "B2_index_v2": {"TRINITY_DELIVERY_SLOT": "index", "TRINITY_DELIVERY_V2": "on"},
}


def _plain_content(ids: List[str], home: str) -> Dict[str, str]:
    """只读取正文（PG），`enc:v1:` 用仓内 decrypt_content 在进程内解密、**不落盘**。"""
    out: Dict[str, str] = {}
    if not ids:
        return out
    try:
        from trinity.security.crypto import decrypt_content
    except Exception:  # noqa: BLE001
        decrypt_content = None
    creds: Dict[str, Any] = {}
    try:
        #: T87b（门禁驱动的修正）：凭证**走仓内统一入口** `trinity.security.credentials`，
        #: 不再自己读 `~/.dsh/.credentials.yaml`（判据 `test_credentials_readers_20261006`
        #: 把它归入「提到凭证路径但没有 I/O 调用点」—— 它的探测只认
        #: `open(<字面路径或模块级常量>)`，看不到「上一行 `p = …`、下一行 `open(p)`」的局部变量写法）。
        #: 统一入口还顺带统一了"env > 凭证文件 > 默认值"的优先级（t31 的同类修正）。
        from trinity.security.credentials import resolve_credentials   # noqa: PLC0415
        creds = dict(resolve_credentials() or {})
    except Exception:  # noqa: BLE001
        creds = {}
    try:
        import psycopg2
        con = psycopg2.connect(host=creds.get("host") or "127.0.0.1",
                               port=int(creds.get("port") or 5432),
                               dbname=creds.get("dbname") or "trinity",
                               user=creds.get("user") or "trinity",
                               password=creds.get("password") or "", connect_timeout=5)
        con.set_session(readonly=True, autocommit=True)
        cur = con.cursor()
        cur.execute("select memory_id, coalesce(content,'') from memories where memory_id = any(%s)",
                    (ids,))
        for mid, txt in cur.fetchall():
            d = txt
            if decrypt_content is not None and isinstance(txt, str) and txt.startswith("enc:v1:"):
                try:
                    d = decrypt_content(txt)
                except Exception:  # noqa: BLE001
                    d = ""
            out[str(mid)] = d if isinstance(d, str) and not str(d).startswith("enc:v1:") else ""
        cur.close()
        con.close()
    except Exception:  # noqa: BLE001
        return {}
    return out


def _arm_run(name: str, env_extra: Dict[str, str], queries: List[str], rounds: int,
             top_k: int, root: str, py: str, worker: str, tmp: str) -> Dict[str, Any]:
    env = dict(os.environ)
    env.update(env_extra)
    env["TRINITY_AUTO_RECALL"] = "on"
    env["TRINITY_MEMORY_ENABLED"] = "0"
    env["TRINITY_STORAGE_BACKEND"] = "postgresql"
    env["TRINITY_ROUTE_REASONER"] = "on"
    env["TRINITY_OPENING_ORIGIN"] = "probe:t58_" + name
    env["TRINITY_OPENING_COUNTERS"] = os.path.join(tmp, "%s_counters.json" % name)
    #: ⭐ 本臂**唯一账本、跨轮次复用**（t9 的缺陷是每臂新建 ⇒ 轮换零触发）
    env["TRINITY_DELIVERY_LEDGER"] = os.path.join(tmp, "%s_ledger.jsonl" % name)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for f in (env["TRINITY_OPENING_COUNTERS"], env["TRINITY_DELIVERY_LEDGER"]):
        try:
            os.remove(f)
        except FileNotFoundError:
            #: **显式的预期缺失**（首次运行没有这些探针文件）⇒ 留痕但不报错
            logger.debug("t58 ab: 探针文件不存在（首次运行，预期）：%s", f)
        except Exception as exc:  # noqa: BLE001
            logger.debug("t58 ab: 清理探针文件失败（继续）：%r", exc)
    proc = subprocess.Popen([py, worker], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", env=env, cwd=root)
    sessions: List[Dict[str, Any]] = []
    errors: List[str] = []
    try:
        for rnd in range(1, rounds + 1):
            for qi, q in enumerate(queries, 1):
                sid = "t58-%s-q%d-r%d" % (name, qi, rnd)
                req = {"id": rnd * 100 + qi, "method": "opening",
                       "params": {"query": q, "top_k": top_k, "session_id": sid,
                                  "origin": "probe:t58_" + name}}
                try:
                    proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                    line = proc.stdout.readline()
                    payload = json.loads(line) if line else {}
                    if not payload or payload.get("error"):
                        errors.append("q%d r%d: %s" % (qi, rnd, str(payload.get("error"))[:100]))
                        continue
                    res = payload.get("result") or {}
                except Exception as e:  # noqa: BLE001
                    errors.append("q%d r%d: %s" % (qi, rnd, str(e)[:100]))
                    continue
                md = str(res.get("surface_md") or "")
                ids = [str(x) for x in (res.get("delivered_ids") or [])]
                plan = res.get("delivery_plan") or {}
                sessions.append({
                    "round": rnd, "q": qi, "sid": sid, "query": q,
                    "sources": int(res.get("sources") or 0),
                    "cold_ids": [str(x) for x in (res.get("cold_ids") or [])],
                    "ids": ids, "md_sha256": _sha(md), "md_chars": len(md),
                    "tokens_est": _tokens(md), "md": md,
                    "recent_ids_seen": int(plan.get("recent_ids_seen") or 0),
                    "swapped_out_recent": list(plan.get("swapped_out_recent") or []),
                    "policy": plan.get("policy"),
                })
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=25)
        except Exception as exc:  # noqa: BLE001
            logger.debug("t58 ab: worker 收尾失败，转 kill：%r", exc)
            try:
                proc.kill()
            except Exception as exc2:  # noqa: BLE001
                logger.debug("t58 ab: worker kill 也失败（可能已退出）：%r", exc2)
    return {"arm": name, "env": env_extra, "sessions": sessions, "errors": errors[:5],
            "ledger_path": env["TRINITY_DELIVERY_LEDGER"]}


def apply_index_to_sessions(sessions: List[Dict[str, Any]], plain: Dict[str, str],
                            top_k: int = 5) -> List[Dict[str, Any]]:
    """对**已取回的 surface**施加确定性索引变换（把 `md` 换成索引块，`ids` 不变）。

    ⚠️ **为什么需要这一步（t58 首轮实测抓到的我自己的设计缺陷）**：索引是**宿主侧**的呈现层变换，
    而 `engine_worker` 不认识它（本任务写域不含 engine_worker）⇒ 首轮 A/B 里 **B 臂的块与 A 完全相同**
    （`tokens_per_session` 501.0 vs 502.7 —— 可见"没换"），于是我给出的 A-vs-B 相关性差
    只是"60 字摘要的字符 n-gram 余弦天然更低"这个**度量属性**，**不是**"模型看到的东西变了"。
    ⇒ 现在把变换**显式、确定性地**作用在同一次取回的 surface 上（同一份 ids、同一份正文），
    并把 `md` 换成**块级**文本，使相关性/成本都在"模型真正看到的那段文本"上度量。
    """
    out: List[Dict[str, Any]] = []
    for s in sessions:
        items = [{"memory_id": m, "content": plain.get(m, ""), "category": "memory"}
                 for m in s.get("ids") or [] if plain.get(m)]
        s2 = dict(s)
        if items:
            plan = build_slot_block(items, top_k=top_k)
            if plan.get("block"):
                s2["md_full"] = s.get("md", "")
                s2["md"] = plan["block"]
                s2["chars_full"] = len(s.get("md") or "")
                s2["md_chars"] = len(plan["block"])
                s2["tokens_est"] = plan["tokens_est"]
                s2["index_applied"] = True
                s2["index_entries"] = len(plan["entries"])
                s2["index_dropped"] = len(plan.get("dropped_for_budget") or [])
        out.append(s2)
    return out


def block_level_metrics(sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """**块级**（= 模型真正看到的文本）的相关性与成本：每会话一段 query↔block 的余弦。"""
    b2, b1, tk, ch = [], [], [], []
    per_q: Dict[int, List[float]] = {}
    for s in sessions:
        md = str(s.get("md") or "")
        if not md:
            continue
        v2 = _cos(Counter(set(_grams(s["query"], 2))), _grams(md, 2), binary=True)
        v1 = _cos(_grams(s["query"], 1), _grams(md, 1))
        b2.append(v2)
        b1.append(v1)
        tk.append(s.get("tokens_est") or _tokens(md))
        ch.append(len(md))
        per_q.setdefault(s["q"], []).append(v2)
    return {
        "block_rel_bigram2_mean": round(sum(b2) / len(b2), 6) if b2 else None,
        "block_rel_unigram_mean": round(sum(b1) / len(b1), 6) if b1 else None,
        "block_tokens_mean": round(sum(tk) / len(tk), 1) if tk else None,
        "block_chars_mean": round(sum(ch) / len(ch), 1) if ch else None,
        "block_rel_per_query": {("q%d" % k): round(sum(v) / len(v), 6)
                                for k, v in sorted(per_q.items())},
    }
    """覆盖 + 相关性两类指标（相关性用两套自实现度量，不依赖 sklearn）。"""
    ses = arm["sessions"]
    ids_all = [m for s in ses for m in s["ids"]]
    distinct = len(set(ids_all))
    #: 相关性：对**真正进上下文的那段文本**算（B 臂 = 索引行；A 臂 = 全文行）
    rel_b2: List[Optional[float]] = []
    rel_u1: List[Optional[float]] = []
    per_q: Dict[int, List[float]] = {}
    for s in ses:
        qg2 = Counter(set(_grams(s["query"], 2)))
        qg1 = _grams(s["query"], 1)
        vals2, vals1 = [], []
        for mid in s["ids"]:
            txt = plain.get(mid, "")
            if index_mode:
                txt = _snippet(txt)               # B 臂上下文里只有摘要
            if not txt:
                continue
            vals2.append(_cos(qg2, _grams(txt, 2), binary=True))
            vals1.append(_cos(qg1, _grams(txt, 1)))
        if vals2:
            m2 = sum(vals2) / len(vals2)
            rel_b2.append(m2)
            rel_u1.append(round(sum(vals1) / len(vals1), 6))
            per_q.setdefault(s["q"], []).append(m2)
    return {
        "sessions": len(ses),
        "deliveries": len(ids_all),
        "distinct": distinct,
        "repeat_rate": (round((len(ids_all) - distinct) / len(ids_all), 4) if ids_all else None),
        "per_session_hits": (round(len(ids_all) / len(ses), 3) if ses else None),
        "cold_deliveries": sum(len(s["cold_ids"]) for s in ses),
        "tokens_per_session": (round(sum(s["tokens_est"] for s in ses) / len(ses), 1) if ses else None),
        "chars_per_session": (round(sum(s["md_chars"] for s in ses) / len(ses), 1) if ses else None),
        "md_unique_blocks": len({s["md_sha256"] for s in ses}),
        "rel_bigram2_mean": (round(sum(rel_b2) / len(rel_b2), 6) if rel_b2 else None),
        "rel_unigram_mean": (round(sum(rel_u1) / len(rel_u1), 6) if rel_u1 else None),
        "rel_per_query": {("q%d" % k): round(sum(v) / len(v), 6) for k, v in sorted(per_q.items())},
        "recent_ids_seen_max": max((s["recent_ids_seen"] for s in ses), default=0),
        "rotation_triggered": any(s["swapped_out_recent"] for s in ses),
        "rotation_events": sum(len(s["swapped_out_recent"]) for s in ses),
        "errors": arm.get("errors"),
    }


def _metrics(arm: Dict[str, Any], plain: Dict[str, str], index_mode: bool) -> Dict[str, Any]:
    """覆盖 + 相关性两类指标（**条目级**；块级另见 `block_level_metrics`）。"""
    ses = arm["sessions"]
    ids_all = [m for s in ses for m in s["ids"]]
    distinct = len(set(ids_all))
    rel_b2: List[Optional[float]] = []
    rel_u1: List[Optional[float]] = []
    per_q: Dict[int, List[float]] = {}
    for s in ses:
        qg2 = Counter(set(_grams(s["query"], 2)))
        qg1 = _grams(s["query"], 1)
        vals2, vals1 = [], []
        for mid in s["ids"]:
            txt = plain.get(mid, "")
            if index_mode:
                txt = _snippet(txt)               # 索引臂：条目级看"摘要"的余弦
            if not txt:
                continue
            vals2.append(_cos(qg2, _grams(txt, 2), binary=True))
            vals1.append(_cos(qg1, _grams(txt, 1)))
        if vals2:
            m2 = sum(vals2) / len(vals2)
            rel_b2.append(m2)
            rel_u1.append(round(sum(vals1) / len(vals1), 6))
            per_q.setdefault(s["q"], []).append(m2)
    return {
        "sessions": len(ses),
        "deliveries": len(ids_all),
        "distinct": distinct,
        "repeat_rate": (round((len(ids_all) - distinct) / len(ids_all), 4) if ids_all else None),
        "per_session_hits": (round(len(ids_all) / len(ses), 3) if ses else None),
        "cold_deliveries": sum(len(s["cold_ids"]) for s in ses),
        "tokens_per_session": (round(sum(s["tokens_est"] for s in ses) / len(ses), 1) if ses else None),
        "chars_per_session": (round(sum(s["md_chars"] for s in ses) / len(ses), 1) if ses else None),
        "md_unique_blocks": len({s["md_sha256"] for s in ses}),
        "rel_bigram2_mean": (round(sum(rel_b2) / len(rel_b2), 6) if rel_b2 else None),
        "rel_unigram_mean": (round(sum(rel_u1) / len(rel_u1), 6) if rel_u1 else None),
        "rel_per_query": {("q%d" % k): round(sum(v) / len(v), 6) for k, v in sorted(per_q.items())},
        "recent_ids_seen_max": max((s["recent_ids_seen"] for s in ses), default=0),
        "rotation_triggered": any(s["swapped_out_recent"] for s in ses),
        "rotation_events": sum(len(s["swapped_out_recent"]) for s in ses),
        "errors": arm.get("errors"),
    }


def _per_query(arm: Dict[str, Any], plain: Dict[str, str], index_mode: bool) -> Dict[int, Dict[str, Any]]:
    """按**查询**（= 有效独立单元）聚合：覆盖 / 命中 / token / 相关性。"""
    out: Dict[int, List[Dict[str, Any]]] = {}
    for s in arm["sessions"]:
        out.setdefault(s["q"], []).append(s)
    detail: Dict[int, Dict[str, Any]] = {}
    for q, ses in out.items():
        ids = [m for s in ses for m in s["ids"]]
        qg2 = Counter(set(_grams(ses[0]["query"], 2)))
        qg1 = _grams(ses[0]["query"], 1)
        v2, v1 = [], []
        for mid in ids:
            txt = plain.get(mid, "")
            if index_mode:
                txt = _snippet(txt)
            if not txt:
                continue
            v2.append(_cos(qg2, _grams(txt, 2), binary=True))
            v1.append(_cos(qg1, _grams(txt, 1)))
        detail[q] = {
            "sessions": len(ses),
            "distinct": len(set(ids)),
            "hits_mean": round(len(ids) / max(1, len(ses)), 3),
            "tokens_mean": round(sum(s["tokens_est"] for s in ses) / max(1, len(ses)), 2),
            "rel_bigram2": round(sum(v2) / len(v2), 6) if v2 else None,
            "rel_unigram": round(sum(v1) / len(v1), 6) if v1 else None,
        }
    return detail


def _paired(detail_a: Dict[int, Dict[str, Any]], detail_b: Dict[int, Dict[str, Any]],
            field: str) -> Dict[str, Any]:
    qs = sorted(set(detail_a) & set(detail_b))
    deltas = []
    for q in qs:
        va, vb = detail_a[q].get(field), detail_b[q].get(field)
        if va is None or vb is None:
            continue
        deltas.append(round(float(vb) - float(va), 6))
    st = _signflip(deltas)
    st.update({"field": field, "n_units": len(deltas), "deltas": deltas,
               "positive": sum(1 for d in deltas if d > 0),
               "negative": sum(1 for d in deltas if d < 0),
               "zero": sum(1 for d in deltas if d == 0)})
    if deltas:
        rnd = random.Random(20261006)
        n = len(deltas)
        boot = sorted(sum(deltas[rnd.randrange(n)] for _ in range(n)) / n for _ in range(2000))
        st["bootstrap"] = {"n": 2000, "mean": round(sum(boot) / len(boot), 6),
                           "p2_5": round(boot[49], 6), "p97_5": round(boot[1949], 6)}
    return st


def cmd_ab(sessions_per_query: int, top_k: int, queries_file: str, out_path: str) -> int:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    py = os.path.join(root, ".venv", "Scripts", "python.exe")
    worker = os.path.join(root, "trinity", "engine_worker.py")
    queries = [ln.strip() for ln in open(queries_file, encoding="utf-8") if ln.strip()]
    tmp = os.path.join(os.environ.get("TEMP", "."), "t58_ab")
    os.makedirs(tmp, exist_ok=True)
    index_arms = {"A_full_v1": False, "B_index_v1": True, "B2_index_v2": True}
    report: Dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "design": {
            "arm_A": "现状（全文投递，TRINITY_DELIVERY_V2=off）",
            "arm_B": "候选（紧凑索引，V2=off）",
            "arm_B2": "候选 + 覆盖优先（紧凑索引，V2=on）",
            "rounds": sessions_per_query, "n_queries": len(queries),
            "sessions_per_arm": sessions_per_query * len(queries),
            "ledger": "**每臂单个账本、跨轮次复用**（专治 t9 的'每臂新建 ⇒ 轮换零触发'）",
            "effective_units": len(queries),
            "min_possible_p_query_level": round(2 ** (-len(queries)), 6),
            "can_test": ["投递形态对**进上下文的文本**的相关性与 token 成本的影响（确定性、可复算）",
                         "覆盖/命中/重复率（同臂单账本、跨轮次）",
                         "轮换是否真的被触发（recent_ids_seen / swapped_out_recent）",
                         "'如果按索引 pull 一次能拿到什么'（**确定性模拟**的 pull yield）"],
            "cannot_test": ["**模型会不会真的去 pull**（Vercel 的 56% 未触发正是这一条；本实验 pull 腿是确定性模拟）",
                            "端到端答案正确率（未跑 LLM 生成与判分）",
                            "生产宿主接线后的真实计费/缓存效应（KV cache 命中率）"],
        },
        "arms": {}, "paired": {}, "pull_yield": {},
    }
    arms_out: Dict[str, Dict[str, Any]] = {}
    all_ids: set = set()
    for name, env_extra in ARMS.items():
        arm = _arm_run(name, env_extra, queries, sessions_per_query, top_k, root, py, worker, tmp)
        arms_out[name] = arm
        all_ids |= {m for s in arm["sessions"] for m in s["ids"]}
        print("[t58] arm %s done: sessions=%d errors=%d" % (name, len(arm["sessions"]),
                                                           len(arm["errors"])))
    plain = _plain_content(sorted(all_ids), tmp)
    report["decrypt"] = {"ids": len(all_ids), "readable": sum(1 for v in plain.values() if v)}
    details: Dict[str, Dict[int, Dict[str, Any]]] = {}
    report["raw"] = {}
    for name, arm in arms_out.items():
        #: ⭐ 索引臂：**显式施加确定性变换**（首轮漏掉这一步 ⇒ B 臂与 A 臂块相同，见函数 docstring）
        if index_arms[name]:
            arm = dict(arm)
            arm["sessions"] = apply_index_to_sessions(arm["sessions"], plain, top_k)
        report["arms"][name] = _metrics(arm, plain, index_arms[name])
        report["arms"][name]["block_level"] = block_level_metrics(arm["sessions"])
        report["arms"][name]["index_applied_sessions"] = sum(
            1 for s in arm["sessions"] if s.get("index_applied"))
        details[name] = _per_query(arm, plain, index_arms[name])
        report["arms"][name]["per_query"] = {("q%d" % k): v for k, v in sorted(details[name].items())}
        #: 逐会话原文留档（**审计用**：块级相关性可事后复算；首轮就是因为没存 md 而无法离线重算）
        report["raw"][name] = [{k: s.get(k) for k in
                                ("round", "q", "sid", "query", "ids", "cold_ids", "md",
                                 "md_full", "index_applied", "index_entries", "tokens_est",
                                 "md_chars", "recent_ids_seen", "swapped_out_recent")}
                               for s in arm["sessions"]]
    for field in ("rel_bigram2", "rel_unigram", "tokens_mean", "distinct", "hits_mean"):
        report["paired"]["A_vs_B:" + field] = _paired(details["A_full_v1"], details["B_index_v1"], field)
        report["paired"]["A_vs_B2:" + field] = _paired(details["A_full_v1"], details["B2_index_v2"], field)
    #: pull yield：对索引臂，模拟"按 id 取全文一次"（确定性；**不代表模型真会去取**）
    for name in ("B_index_v1", "B2_index_v2"):
        per_q = {}
        for s in arms_out[name]["sessions"]:
            qg2 = Counter(set(_grams(s["query"], 2)))
            qg1 = _grams(s["query"], 1)
            pulled = [m for m in s["ids"] if plain.get(m)]
            if not pulled:
                continue
            v2 = [_cos(qg2, _grams(plain[m], 2), binary=True) for m in pulled]
            v1 = [_cos(qg1, _grams(plain[m], 1)) for m in pulled]
            per_q.setdefault(s["q"], []).append({
                "pulled": len(pulled),
                "tokens_full": _tokens(" ".join(plain[m] for m in pulled)),
                "rel2": sum(v2) / len(v2), "rel1": sum(v1) / len(v1),
                "index_tokens": s["tokens_est"]})
        agg = {}
        for q, rows in per_q.items():
            agg["q%d" % q] = {
                "pulled_mean": round(sum(r["pulled"] for r in rows) / len(rows), 2),
                "full_tokens_mean": round(sum(r["tokens_full"] for r in rows) / len(rows), 1),
                "index_tokens_mean": round(sum(r["index_tokens"] for r in rows) / len(rows), 1),
                "rel2_full": round(sum(r["rel2"] for r in rows) / len(rows), 6),
                "rel1_full": round(sum(r["rel1"] for r in rows) / len(rows), 6)}
        report["pull_yield"][name] = agg
    out = out_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "t58_ab_result.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(json.dumps({"ts": report["ts"], "arms": {k: {kk: v[kk] for kk in
                                                       ("distinct", "repeat_rate", "per_session_hits",
                                                        "tokens_per_session", "cold_deliveries",
                                                        "rel_bigram2_mean", "rel_unigram_mean",
                                                        "md_unique_blocks", "recent_ids_seen_max",
                                                        "rotation_triggered", "rotation_events")}
                                                       for k, v in report["arms"].items()},
                      "paired": {k: {kk: v.get(kk) for kk in ("n_units", "observed_mean",
                                                              "p_two_sided", "bootstrap")}
                                 for k, v in report["paired"].items()}},
                     ensure_ascii=False, indent=1))
    print("out ->", out)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="H2/t58 投递槽位（紧凑索引 + 便宜 pull）")
    ap.add_argument("--ab", action="store_true", help="跑 A / B / B2 三臂")
    ap.add_argument("--rounds", type=int, default=3, dest="rounds")
    ap.add_argument("--top-k", type=int, default=5, dest="top_k")
    ap.add_argument("--queries-file", default="", dest="queries_file")
    ap.add_argument("--out", default="")
    a = ap.parse_args(list(argv) if argv is not None else None)
    if a.ab:
        if not a.queries_file:
            print("--queries-file required")
            return 2
        return cmd_ab(a.rounds, a.top_k, a.queries_file, a.out)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
