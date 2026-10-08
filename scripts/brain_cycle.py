#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""brain_cycle.py — 认知周期 orchestrator（2026-09-08，大脑化路线 1/2/3/4/5/6 集成）。

每日"认知周期"：FSRS 遗忘调度 → Hebbian 共激活 → 情感基调标注 → 认知自评 →
预测校验与自主提议。产物：提议写入记忆(category=brain-proposal)供宿主检索消费；
状态与预测存 ~/.trinity/brain/cycle_state.json；审计动作 REVIEW_SCHEDULED /
HEBBIAN_UPDATE / EMOTIONAL_STATE / BRAIN_PROPOSAL。

用法:
  python scripts/brain_cycle.py [--steps fsrs,hebbian,emotion,eval,propose] [--dry-run]
建议经计划任务每日 03:40 运行（schtasks /create /tn Trinity-BrainCycle ...）。
"""
import argparse
import json
import json as _j  # EXECUTION 639: 模块级别名（step_eval 解析用）
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:
    def swallow(*_a, **_k):
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(*_a, **_k)
        except Exception:
            return None

# 2026-09-11（V2 评估修复）：本模块此前**引用了未定义的 logger**（步骤超预算分支
# logger.warning 会 NameError，且被外层 except 吃成"步骤失败"——静默失效的又一实例）。
logger = logging.getLogger("trinity.brain_cycle")

ROOT = r"C:\Users\Administrator\trinity"
STATE_DIR = os.path.expanduser("~/.trinity/brain")
STATE_FILE = os.path.join(STATE_DIR, "cycle_state.json")
os.makedirs(STATE_DIR, exist_ok=True)
sys.path.insert(0, ROOT)


def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception as _e:
            swallow(__name__, _e)
    return {"last_run": None, "last_prediction": None, "counts": {}}


def _save_state(st):
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# 步骤 → 该步"真实产出"的键（2026-09-19，EXECUTION §915.4）
_STEP_COUNT_KEYS = {"hebbian": "links_updated", "propose": "proposals_written", "teach": "due_today"}


def derive_counts(report: dict) -> dict:
    """把一轮 brain_cycle 的 report 折算成 counts（纯函数，**只在真有产出时才非 0**）。

    2026-09-11 的旧实现是 `{k: v.get("links_updated", v.get("proposals_written", v.get("due_today", 0)))}`：
    **verify 步没有这三个键 ⇒ 恒 0**，于是 `cycle_state.counts={"verify":0,...}` 被读成
    "校验步空转"（2026-09-19 体检实测：verify_log 有 14 条、末条 verdict_source=assert，
    校验其实一直在跑）。旧注释只是"加注说明"，但**读数仍然是 0** —— 加注不能替代说真话。

    现在的口径：`verify` = 本轮**是否真的产出了判定**（有 verdict ⇒ 1；skipped/无键 ⇒ 0）。
    其余步骤保持既有语义（缺键即 0，不夸大）；report 里没有的步骤**不凭空补键**。
    """
    out = {}
    for k, v in (report or {}).items():
        if not isinstance(v, dict) or "error" in v:
            continue
        if k == "verify":
            out[k] = 1 if v.get("verdict") else 0
        else:
            out[k] = v.get(_STEP_COUNT_KEYS.get(k, ""), 0)
    return out


def _pg():
    import psycopg2
    creds = {}
    try:
        import yaml
        with open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig") as fh:
            # 2026-10-03（**修一个"看起来修过、实际没生效"的缺陷**）：
            # 原写法是**先按顶层键过滤、之后才取 `refs`**：
            #     creds = {k: v for k, v in (yaml.safe_load(fh) or {}).items()
            #              if k.startswith("TRINITY_PG_")}
            #     creds = {**(creds.get("refs") or {}), **creds}     # ← refs 已被上一步丢掉 ⇒ 恒空
            # 而凭证文件自 2026-09-18 起是**版本化结构**（顶层只有 `version`/`refs`/`records`，
            # 所有 `TRINITY_PG_*` 都缩进在 `refs` 下）⇒ 第一步过滤后 **creds 恒为空 dict**，
            # 于是 `or "postgres"` / `or ""` 兜底生效 ⇒
            # `FATAL: password authentication failed for user "postgres"`。
            # 实测（2026-10-03）：`teach_status.py` ⇒ `teach_common.ensure_schema`
            # ⇒ 本函数 ⇒ 认证失败 ⇒ `loop_health` 的 `teach` 环长期 `status_rc=1`。
            # 同型缺陷另有 `scripts/question_search.py`、`scripts/reweight_relations_cage.py`。
            # 正解：**先把 refs 合并进完整 dict，之后再过滤**（与同仓已正确的
            # `scripts/_pg_std.py:31-32`、`scripts/backfill_audit_checksum.py:25-26`、
            # `trinity/brain/precision_tiers.py:65-66` 逐字一致）。
            raw = yaml.safe_load(fh) or {}
            raw = {**(raw.get("refs") or {}), **raw}
            creds = {k: v for k, v in raw.items() if k.startswith("TRINITY_PG_")}
    except Exception as _e:
        swallow(__name__, _e)
    return psycopg2.connect(
        host=os.environ.get("TRINITY_PG_HOST") or creds.get("TRINITY_PG_HOST") or "127.0.0.1",
        port=int(os.environ.get("TRINITY_PG_PORT") or creds.get("TRINITY_PG_PORT") or 5432),
        dbname=os.environ.get("TRINITY_PG_DB") or creds.get("TRINITY_PG_DB") or "trinity",
        user=os.environ.get("TRINITY_PG_USER") or creds.get("TRINITY_PG_USER") or "postgres",
        password=os.environ.get("TRINITY_PG_PASSWORD") or creds.get("TRINITY_PG_PASSWORD") or "")


try:  # 与适配器同源的链锁键（H2-4）；取不到时退回硬编码常量
    from trinity.adapters._pg_audit import _AUDIT_CHAIN_LOCK_KEY as _AUDIT_LOCK_KEY
except Exception:  # noqa: BLE001
    _AUDIT_LOCK_KEY = 0x5411A0D1


def _audit(cur, mid, action, details):
    """写审计（H2-4 修复 2026-09-13）。

    **原实现的缺陷**：本函数自己 `SELECT prev → 算 checksum → INSERT`，**不持链锁**；
    而调用方传进来的连接是 `autocommit=True`（`main()` 里设置的）——即便加
    `pg_advisory_xact_lock` 也会**随语句立即释放，等于没加锁**。
    于是它与适配器路径（`_pg_audit.write_audit_log`，**持锁**）并发时，两者可能读到同一个
    `prev_checksum` → **链分叉** → `/audit/integrity` 报 tampered。
    这与 `audit_chain_repair.py` 文件头记录的 09-13 两处断裂属同一根因族。

    **现实现**：改走**独立的一次性事务连接**（非 autocommit）：取链锁 → 读 prev → 插入 → 提交；
    锁键与适配器同源（`trinity.adapters._pg_audit._AUDIT_CHAIN_LOCK_KEY`），因此二者互斥。
    失败时退回原路径（可用性优先，但**如实不影响**：退回路径仍是原语义）。
    """
    import hashlib
    import uuid
    from datetime import datetime as _dt, timezone as _tz
    nid = str(uuid.uuid4())
    _base = {"id": nid, "memory_id": mid, "action": action, "agent_id": "brain-cycle",
             "persona_id": None, "details": details}

    def _write(c, lock: bool):
        with c.cursor() as _c:
            if lock:
                _c.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_LOCK_KEY,))
            # 2026-09-15（R41-P23）：链序键统一为可比较的时间键（TEXT 字典序会选错前驱）。
            _c.execute("SELECT checksum FROM audit_log "
                       "ORDER BY timestamp::timestamptz DESC, id DESC LIMIT 1")
            _pr = _c.fetchone()
            _prev = _pr[0] if _pr and _pr[0] else ""
            # ⚠️ 时间戳必须在**取锁并读到 prev 之后**才取：
            # 全链校验按 (timestamp ASC, id ASC) 排序，而链是按写入次序串起来的。
            # 若先取 ts 再去读 prev，两次之间若有并发写入落库，会得到
            # 「本行 ts 早于它所指的 prev 行 ts」的倒挂 → 校验序里本行排在 prev 之前
            # → 无论 checksum 怎么重算都对不上（这正是 09-13 两处断裂的形态，
            #   也是 audit_chain_repair.py 文件头记录的同一条根因）。
            ts = _dt.now(_tz.utc).isoformat()
            _p = dict(_base, timestamp=ts, prev_checksum=_prev)
            _chk = hashlib.sha256(json.dumps(_p, sort_keys=True, ensure_ascii=False)
                                  .encode("utf-8")).hexdigest()
            _c.execute("INSERT INTO audit_log(id, memory_id, action, agent_id, persona_id, details, "
                       "checksum, timestamp, prev_checksum) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       (nid, mid, action, "brain-cycle", None, json.dumps(details), _chk, ts, _prev))

    try:
        _conn2 = _pg()          # 默认非 autocommit：xact 锁在整个事务内有效
        try:
            _write(_conn2, lock=True)
            _conn2.commit()
        finally:
            try:
                _conn2.close()
            except Exception as _e:
                swallow(__name__, _e)
        return
    except Exception as _e:
        swallow(__name__, _e)
    # 退回原路径（同一连接，无锁）——仅在新建连接失败时发生
    _write_cur = cur
    _write_cur.execute("SELECT checksum FROM audit_log "
                       "ORDER BY timestamp::timestamptz DESC, id DESC LIMIT 1")
    pr = _write_cur.fetchone()
    prev = pr[0] if pr and pr[0] else ""
    ts = _dt.now(_tz.utc).isoformat()
    chk = hashlib.sha256(json.dumps(dict(_base, timestamp=ts, prev_checksum=prev), sort_keys=True,
                                    ensure_ascii=False).encode("utf-8")).hexdigest()
    _write_cur.execute("INSERT INTO audit_log(id, memory_id, action, agent_id, persona_id, details, "
                       "checksum, timestamp, prev_checksum) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       (nid, mid, action, "brain-cycle", None, json.dumps(details), chk, ts, prev))


def llm(prompt: str, max_tokens: int = 600):
    import urllib.request
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("TRINITY_LLM_API_KEY")
    if not key:
        yp = os.path.expanduser("~/.dsh/.credentials.yaml")
        if os.path.exists(yp):
            for line in open(yp, encoding="utf-8-sig"):
                if ":" in line and line.split(":", 1)[0].strip() in ("DEEPSEEK_API_KEY", "TRINITY_LLM_API_KEY"):
                    key = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        return None
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}],
                         "temperature": 0.3, "max_tokens": max_tokens}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        body = json.loads(r.read().decode("utf-8"))
    return (body["choices"][0]["message"].get("content") or "").strip()


# ── S1: FSRS 遗忘调度（初始化/推进复习计划 + 到期报告）────────────────
# 2026-09-08（U1 控制面化）：间隔天数从 forgetting_policy.json 读取（fsrs tiers），
# 决策位置统一记录 policy_source=forgetting_policy（对齐 Control-Plane 论文视角）。
def _fsrs_sqlite(dry: bool) -> dict:
    """把**同一套** FSRS 应用到常驻 API 服务的 **SQLite** 库（Step 4b / D28）。

    ## 为什么需要这一步（2026-10-05 实测）

    本模块只连 PostgreSQL（`psycopg2`，见 `pg_connect()`），而**常驻 API 服务的是 SQLite**
    （`TRINITY_STORE=~/.trinity/store-restored`，见 `dsh-ops/trinity-autostart.ps1:35`；D28 已定保持 SQLite）。
    后果：SQLite 侧**长期零复习覆盖** —— 实测 27,034 条 active 里 **26,848 条无任何排期**，
    仅存的 186 条还是 `trinity/brain/memory_revival.py` 的一次性产物、值停在 2026-08-04~08-22 且**全部逾期**。

    ⇒ 「机制存在但打在了另一个库上」。修法是**复用**同一策略与同一实现
    （`scripts/review_schedule_sqlite.py`，其间隔天数同样取自 `forgetting_policy`），
    **不是**再写一份调度器（AGENTS.md §1050）。

    ## 失败语义
    任何异常都返回 `status=error` **并带原因**，绝不抛给调用方 —— 每日链上的一个子步骤失败
    不该终止整轮，但**必须留痕**（§13.5：写侧错误不会报错，只会「没有效果」）。
    """
    try:
        import review_schedule_sqlite as _rs
    except Exception as e:  # noqa: BLE001
        return {"status": "import_error",
                "detail": "%s: %s" % (type(e).__name__, str(e)[:150])}
    try:
        store, src = _rs.resolve_store()
        if dry:
            p = _rs.plan(store)
            return {"status": "dry_run", "store": store, "store_source": src,
                    "verdict": p.get("verdict"),
                    "schedulable": p.get("schedulable_rows"),
                    "due_now": p.get("due_now"),
                    "error": p.get("error")}
        init = _rs.apply_init(store)
        con = _rs.consume(store, _rs.load_budget())
        return {"status": "ok", "store": store, "store_source": src,
                "interval_written": init.get("interval_written"),
                "next_written": init.get("next_written"),
                "picked": con.get("picked"), "reviewed": con.get("reviewed"),
                "budget": con.get("budget"),
                "touch_access_count": con.get("touch_access_count"),
                "backup": init.get("backup") or con.get("backup")}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "detail": "%s: %s" % (type(e).__name__, str(e)[:150])}


def step_fsrs(conn, dry: bool) -> dict:
    cur = conn.cursor()
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from forgetting_policy import load_policy, fsrs_days
        _pol = load_policy()
        _tiers = sorted(_pol.get("fsrs", {}).get("tiers", []), key=lambda t: t["min_imp"], reverse=True)
    except Exception:
        _tiers = [{"min_imp": 0.8, "days": 30}, {"min_imp": 0.6, "days": 60}]
    _case = "CASE " + " ".join(
        f"WHEN importance::float8 >= {t['min_imp']} THEN '{t['days']}'" for t in _tiers) +         " ELSE '90' END"
    # review_interval_days/next_review_at 为 text 列 → 显式 ::int / ::timestamptz
    cur.execute("UPDATE memories SET review_interval_days = " + _case + " "
                "WHERE status='active' AND (review_interval_days IS NULL OR review_interval_days::int <= 0)")
    inited = cur.rowcount
    cur.execute("UPDATE memories SET next_review_at = to_char(now() + (review_interval_days::int || ' days')::interval, "
                "'YYYY-MM-DD HH24:MI:SS+00') "
                "WHERE status='active' AND (next_review_at IS NULL OR next_review_at = '')")
    inited2 = cur.rowcount
    cur.execute("SELECT count(*) FROM memories WHERE status='active' AND next_review_at::timestamptz <= now()")
    due = cur.fetchone()[0]
    if not dry and (inited or inited2 or due):
        _audit(cur, None, "REVIEW_SCHEDULED",
               {"inited_interval": inited, "inited_next": inited2, "due_today": due,
                "policy_source": "forgetting_policy", "decision_plane": "data-plane-scheduler"})
        conn.commit()
    # 2026-09-08（③ 复习动作闭环）：到期记忆自动复习——touch(access+1/updated) 并
    # 按 FSRS 简版推进 next_review（间隔×1.5，上限 180 天，预算受策略 review_budget 约束）。
    reviewed = 0
    if due > 0:
        try:
            _budget = int(_pol.get("schedule", {}).get("review_budget_per_day", 500))
        except Exception:
            _budget = 500
        if not dry:
            cur.execute(
                "UPDATE memories SET access_count = access_count + 1, "
                "next_review_at = to_char(now() + (interval '1 day' * LEAST(review_interval_days::int * 1.5, 180)::int), "
                "'YYYY-MM-DD HH24:MI:SS+00'), "
                # 2026-09-27（大脑化核查 P0-1）：本列是 **timestamptz**，不是 text。
                # 同一条语句里 next_review_at/review_interval_days 是 text（要 to_char），
                # 而 memories.updated_at 实测为 timestamp with time zone —— 写 to_char()
                # 会 DatatypeMismatch 打死整条复习语句。实测代价：该语句 09-08 引入、
                # 09-24 首次真执行即报错，连续 4 天 error、REVIEW_DONE 自 09-08 起再未出现、
                # 2,618 条到期记忆无人复习（判据 tests/unit/test_brain_cycle_fsrs_pg_types.py）。
                "updated_at = now() "
                "WHERE status='active' AND next_review_at::timestamptz <= now() "
                "AND memory_id IN (SELECT memory_id FROM memories WHERE status='active' "
                "AND next_review_at::timestamptz <= now() ORDER BY importance DESC LIMIT %s)",
                (_budget,))
            reviewed = cur.rowcount
            if reviewed:
                _audit(cur, None, "REVIEW_DONE",
                       {"reviewed": reviewed, "budget": _budget, "interval_x": 1.5, "cap_days": 180})
                conn.commit()
    # 2026-10-05（Step 4b / D28）：同一套 FSRS 也应用到**服务中的那个库**（SQLite）。
    # 失败留痕但不拖垮 PG 路径 —— 见 `_fsrs_sqlite` 的失败语义。
    _sq = _fsrs_sqlite(dry)
    return {"inited_interval": inited, "inited_next": inited2, "due_today": due,
            "reviewed": reviewed, "sqlite": _sq}


# ── S2: Hebbian 共激活（检索审计同现 → memory_links strength 增量）────
def step_hebbian(conn, dry: bool) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT details FROM audit_log
                   WHERE (action='search' OR action='search_hybrid')
                     AND timestamp::timestamptz > now() - interval '24 hours' LIMIT 200""")
    pairs = {}
    for (det,) in cur.fetchall():
        try:
            d = json.loads(det) if isinstance(det, str) else (det or {})
            mids = d.get("memory_ids") or []
        except Exception:
            continue
        mids = [str(m) for m in mids if m][:10]
        for i in range(len(mids)):
            for j in range(i + 1, len(mids)):
                k = tuple(sorted([mids[i], mids[j]]))
                pairs[k] = pairs.get(k, 0) + 1
    updated = 0
    for (a, b), n in pairs.items():
        if n < 1:
            continue
        cur.execute("SELECT id FROM memory_links WHERE (source_id=%s AND target_id=%s) OR (source_id=%s AND target_id=%s)",
                    (a, b, b, a))
        row = cur.fetchone()
        if row:
            if not dry:
                cur.execute("UPDATE memory_links SET strength = LEAST(1.0, strength + %s) WHERE id=%s",
                            (min(0.02 * n, 0.1), row[0]))
            updated += 1
    if not dry and updated:
        _audit(cur, None, "HEBBIAN_UPDATE", {"pairs_seen": len(pairs), "links_updated": updated})
        conn.commit()
    return {"pairs_seen": len(pairs), "links_updated": updated}


# ── S3: 情感基调（近 24h 高价值记忆 → 周期情绪价）────────────────────
def step_emotion(conn, dry: bool) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT memory_id, left(content, 400) FROM memories
                   WHERE status='active' AND importance::float8 >= 0.7
                     AND updated_at::timestamptz > now() - interval '24 hours'
                   ORDER BY importance DESC LIMIT 12""")
    rows = cur.fetchall()
    if not rows:
        return {"skipped": "no high-value memories in 24h"}
    blob = "\n".join(f"- {c}" for _, c in rows)[:5000]
    out = llm("根据以下近期高价值记忆，用一行 JSON 输出总体情绪价与基调："
              '{"valence": -1到1, "arousal": 0到1, "tone": "一句话中文基调"}。\n记忆：\n' + blob)
    tone = None
    try:
        if out:
            s = out[out.find("{"): out.rfind("}") + 1]
            tone = json.loads(s)
    except Exception as _e:
        swallow(__name__, _e)
    # 2026-09-11（V2 评估修复）：_skip_emotion 原先只在下面的分支里赋值，
    # 而函数末尾第 240 行无条件引用它 → 只要 tone 为真且 dry=True 就
    # UnboundLocalError（实测 dry-run。非 dry 路径因先赋值而掩盖了该缺陷）。
    _skip_emotion = False
    if not dry and tone:
        _audit(cur, None, "EMOTIONAL_STATE", tone)
        # 2026-09-08（① 情感基调接入）：tone 写为 emotional-state 记忆供检索消费
        _c = json.dumps({"valence": tone.get("valence"), "arousal": tone.get("arousal"),
                         "tone": tone.get("tone")}, ensure_ascii=False)
        # 2026-09-08（P0#3 写确认门）：近 24h 已有同基调记忆则跳过（去重）
        _skip_emotion = False
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import write_gate as _wg2
            _skip_emotion = not _wg2.check(conn, "[当前情绪基调] " + _c[:200],
                                           "emotional-state", dry=True)["allow"]
        except Exception as _e:
            swallow(__name__, _e)
        if not _skip_emotion:
            cur.execute(
                "INSERT INTO memories (memory_id, session_id, persona_id, agent_id, content, role, importance, "
                "tags, category, status, version, created_at, updated_at, access_count) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (f"emotion_{int(time.time())}", "brain-cycle", "default", "brain-cycle",
                 "[当前情绪基调] " + _c[:500], "assistant", 0.4,
                 json.dumps(["emotional-state"], ensure_ascii=False), "emotional-state", "active", 1,
                 datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), 0))
        conn.commit()
    return {"tone": tone or out, "emotion_skipped": _skip_emotion if tone else False}


# ── S4: 认知自评（cognitive-eval 四维，subprocess 复用维护链）─────────
def step_eval(dry: bool) -> dict:
    if dry:
        return {"dry": True}
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "cognitive_eval.py")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        full = (r.stdout or "")
        tail = full.strip().splitlines()
        # EXECUTION 639: 解析 cognitive_eval JSON 报告 → 结构化观测键（供元层日窗）
        parsed = {}
        # EXECUTION 639 fix: cognitive_eval 输出为 indent=1 多行 JSON——整块截取首 { 至末 }
        _s0 = full.find("{")
        _s1 = full.rfind("}")
        if _s0 >= 0 and _s1 > _s0:
            try:
                _d = _j.loads(full[_s0:_s1 + 1])
                if isinstance(_d, dict) and _d.get("gap"):
                    parsed = _d
            except Exception:
                parsed = {}
        gr = float((parsed.get("gap") or {}).get("gap_recall") or 0.0)
        gp = float((parsed.get("gap") or {}).get("gap_precision") or 0.0)
        wm = float((parsed.get("wm") or {}).get("wm_hit") or 0.0)
        cog_score = round(0.4 * gr + 0.3 * gp + 0.3 * wm, 3) if parsed else None
        return {"rc": r.returncode, "elapsed_s": round(time.time() - t0, 1),
                "tail": (tail[-3:] if tail else []),
                "gap_recall": gr, "gap_precision": gp, "wm_hit": wm,
                "cog_score": cog_score, "parsed": bool(parsed)}
    except Exception as e:
        return {"error": str(e)}


# ── EXECUTION 630: 元层外环日闭环（verify 后只读 check + CYCLE_VERIFY 审计）──
def _meta_strategy_check() -> dict:
    """调用元层外环只读 check（指标窗/停滞/注册表规模）并审计 CYCLE_VERIFY 到策略 journal。

    设计：认知周期每日 03:40 与元层策略外环形成"感知→校验→记账"闭环的第一步；
    只读 + 降级（任何异常不打断主循环）；TRINITY_BRAIN_CYCLE_META=off 可关。
    """
    if os.environ.get("TRINITY_BRAIN_CYCLE_META", "on").lower() in ("off", "0", "false"):
        return {"skipped": "disabled by env"}
    try:
        import sys as _s
        _s.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
        from trinity.evolution import meta_strategy as _ms
        _chk = _ms.check()
        summary = {
            "stagnant": _chk.get("stagnant", []),
            "watches": {k: {"sufficient": v.get("sufficient"), "stagnated": v.get("stagnated"),
                            "j": v.get("j")} for k, v in _chk.get("watches", {}).items()},
            "registry_n": len((_ms.load_registry().get("strategies") or {})),
        }
        _ms.audit("CYCLE_VERIFY", summary, apply=True)
        # 2026-09-13（P1/P2 接线）：把 **meta_improvement** 的评估并进元层摘要 ——
        # 它是「校验方法（assert/llm）哪个更可靠」的状态，原为 317h 未更新的无消费者模块。
        try:
            from trinity.brain.meta_improvement import evaluate_method as _mi_eval
            summary["verify_method"] = _mi_eval()
        except Exception as _e3:
            swallow(__name__, _e3)
        return {"status": "ok", **summary}
    except Exception as _e:
        return {"status": "degraded", "error": f"{type(_e).__name__}: {str(_e)[:120]}"}


# ── S4.5: 预测校验器（对上轮预测自动判定 yes/no/partial，存 state.verify_log）──
def _record_regret(verdict: str, reason: str, pred: str, source: str = "") -> dict:
    """把**预测校验的结论**喂给 regret_learning（P1/P2 接线，2026-09-13）。

    动机：trinity/brain/regret_learning.py 的状态文件实测 **318 小时（13 天）未更新**，
    且**没有任何内容消费者** —— 典型的「建了但没接线」。而校验步骤每天都会给出
    yes/no/partial 的**真实结果**，正是 regret 需要的输入；
    它的消费者是 step_propose（把近期预测失误写进提议提示，避免重复犯错）。

    闭环：**propose → assertion → verify → regret → propose**。
    """
    try:
        from trinity.brain.regret_learning import learn_from_regret
        _out = 1.0 if verdict == "yes" else (0.5 if verdict == "partial" else 0.0)
        r = learn_from_regret(str(pred)[:80], _out, 1.0)
        # 2026-09-13（P1/P2 接线）：同一处再喂 **meta_improvement** —— 记录「校验方法」的成败，
        # 让系统学到 **哪种校验方式更可靠**（assert 机检 vs llm 判定）。
        # 生产者=本函数；消费者=_meta_strategy_check()（已由 verify/propose 每轮调用）。
        # 同处再喂 **critique_learning**（本轮新接线：该模块此前**零 import = 死代码**，
        # 状态停 13.2 天）。它有**现成输入源**：verify 判否时的 (预测, 原因) 本身就是一条批评。
        # 生产者=本函数；消费者=step_propose（近期批评 → 提议时避开同类问题）。
        try:
            if verdict != "yes":
                from trinity.brain.critique_learning import learn_from_critique as _cl
                _cl(str(pred)[:60], str(reason)[:60])
        except Exception as _e1:
            swallow(__name__, _e1)
        _mi = None
        try:
            from trinity.brain.meta_improvement import record_outcome as _mi_rec
            _method = ("assert" if source == "assert" else "llm")
            _mi = _mi_rec(_method, verdict == "yes")
        except Exception as _e2:
            swallow(__name__, _e2)
        return {"recorded": True, "outcome": _out, "state": r if isinstance(r, dict) else None,
                "meta": _mi}
    except Exception as _e:
        swallow(__name__, _e)
        return {"recorded": False, "reason": str(_e)[:80]}


def step_verify(dry: bool) -> dict:
    state = _load_state()
    pred = state.get("last_prediction")
    if not pred:
        return {"skipped": "no previous prediction to verify"}
    vlog = state.get("verify_log") or []
    if vlog and vlog[-1].get("pred") == str(pred)[:120]:
        return {"skipped": "already verified", "last": vlog[-1].get("verdict")}
    if dry:
        return {"would_verify": True}
    import json as _j
    # ── H2-2（2026-09-13）：**机器可核验断言优先** ─────────────────────────
    # 有合法 assertion 且 metric 可读 → **确定性判定**（不调 LLM、不猜），verdict_source=assert。
    # 否则退回 LLM 判定，并如实标 verdict_source=llm（不假装可证伪）。
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import predict_assert as _pa
        _asrt = state.get("last_prediction_assertion")
        if _pa.machine_checkable(_asrt):
            _ev = _pa.evaluate(_asrt)
            if _ev.get("ok") is not None:
                _v = "yes" if _ev["ok"] else "no"
                _info = state.get("last_prediction_assertion_info") or {}
                vlog.append({"pred": str(pred)[:120], "verdict": _v,
                             "reason": _ev.get("reason"), "verdict_source": "assert",
                             "assertion": _asrt,
                             "informative": _info.get("informative"),
                             "informative_reason": _info.get("reason"),
                             "ts": datetime.now(timezone.utc).isoformat()})
                vlog = vlog[-30:]
                state["verify_log"] = vlog
                _save_state(state)
                _corr = _meta_correction(_v, str(_ev.get("reason"))[:200])
                _meta = _meta_strategy_check()
                _rate = sum(1 for e in vlog if e.get("verdict_source") == "assert") / max(1, len(vlog))
                _rg = _record_regret(_v, str(_ev.get("reason"))[:200], pred, source="assert")
                _irate = sum(1 for e in vlog
                             if e.get("verdict_source") == "assert"
                             and e.get("informative") is True) / max(1, len(vlog))
                return {"verdict": _v, "reason": _ev.get("reason"), "verdict_source": "assert",
                        "log_len": len(vlog), "machine_checkable_rate": round(_rate, 3),
                        "informative_rate": round(_irate, 3),
                        "assertion_informative": (state.get("last_prediction_assertion_info") or {}).get("informative"),
                        "correction": _corr, "meta": _meta, "regret": _rg}
    except Exception as _e:
        swallow(__name__, _e)
    # EXECUTION 641: 校验事实源增强（健康+PG 计数+近期运维告警）——替代单一 /health 文本
    _facts_parts = []
    try:
        import urllib.request as _ur
        _req = _ur.Request("http://127.0.0.1:8001/health", headers={"Content-Type": "application/json"})
        with _ur.urlopen(_req, timeout=5) as _resp:
            _health = _j.loads(_resp.read().decode("utf-8", "replace"))
            _facts_parts.append("健康: status=" + str(_health.get("status")) +
                                " engine=" + str((_health.get("components") or {}).get("engine")))
    except Exception:
        _facts_parts.append("健康: (不可达)")
    try:
        _conn = _pg()
        _cur = _conn.cursor()
        _cur.execute("SELECT count(*) FILTER (WHERE status='active'), count(*) FROM memories")
        _r = _cur.fetchone()
        _cur.execute("SELECT max(updated_at::timestamptz) FROM memories")
        _mx = _cur.fetchone()[0]
        _conn.close()
        _facts_parts.append("PG: active=" + str(_r[0]) + " total=" + str(_r[1]) +
                            " last_updated=" + str(_mx)[:16])
    except Exception:
        _facts_parts.append("PG: (不可达)")
    try:
        _slog = os.path.expanduser("~/.trinity/logs/dsh-supervisor.log")
        if os.path.exists(_slog):
            _tail = open(_slog, encoding="utf-8", errors="ignore").read().splitlines()[-200:]
            _warn = sum(1 for l in _tail if " WARN" in l or " FAILED" in l or "ERROR" in l)
            _facts_parts.append("近期运维告警(尾200行): " + str(_warn))
    except Exception as _e:
        swallow(__name__, _e)
    _facts = "；".join(_facts_parts)
    _p = ("你是预测校验器。上轮认知周期预测了系统状态。基于当前事实，判定该预测："
          "输出 JSON {verdict: yes|no|partial, reason: 一句话}。\n预测: "
          + str(pred)[:400] + "\n当前事实: " + _facts[:600])
    _o = llm(_p, max_tokens=300)
    _d = {}
    try:
        if _o:
            _s = _o[_o.find("{"): _o.rfind("}") + 1]
            _d = _j.loads(_s)
    except Exception as _e:
        swallow(__name__, _e)
    verdict = _d.get("verdict") or "unknown"
    vlog.append({"pred": str(pred)[:120], "verdict": verdict,
                 "reason": str(_d.get("reason"))[:200],
                 "verdict_source": "llm",
                 "ts": datetime.now(timezone.utc).isoformat()})
    vlog = vlog[-30:]
    state["verify_log"] = vlog
    _save_state(state)
    _corr = _meta_correction(verdict, str(_d.get("reason"))[:200])
    _meta = _meta_strategy_check()
    _rate = sum(1 for e in vlog if e.get("verdict_source") == "assert") / max(1, len(vlog))
    _rg = _record_regret(verdict, str(_d.get("reason"))[:200], str(pred or ""), source="llm")
    return {"verdict": verdict, "reason": str(_d.get("reason"))[:200], "log_len": len(vlog),
            "verdict_source": "llm", "machine_checkable_rate": round(_rate, 3),
            "correction": _corr, "meta": _meta, "regret": _rg}


# ── S6: 教学包（知识传授闭环：缺卡自动补生成 + 每日包落盘）─────────
def step_teach(dry: bool) -> dict:
    _py = sys.executable
    _dir = os.path.dirname(os.path.abspath(__file__))
    try:
        _conn = _pg()
        _cur = _conn.cursor()
        _cur.execute("SELECT count(*) FROM teach_cards")
        _total = _cur.fetchone()[0]
        _cur.execute("SELECT count(*) FROM teach_cards WHERE status='new'")
        _new = _cur.fetchone()[0]
        _cur.execute("SELECT count(*) FROM teach_cards WHERE status<>'new' AND next_review_at IS NOT NULL "
                     "AND next_review_at::timestamptz <= now()")
        _due = _cur.fetchone()[0]
        _conn.close()
    except Exception:
        _total = _new = _due = 0
    _made = 0
    if not dry and _total < 5:
        subprocess.run([_py, os.path.join(_dir, "teach_gen.py"), "--count", "15"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
        _made = 15
    r2 = subprocess.run([_py, os.path.join(_dir, "teach_daily.py")],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    # EXECUTION 646: 每日自教消费（闭环常驻; TRINITY_TEACH_CONSUME=off 可关）
    _consume_rc = -1
    if not dry and os.environ.get("TRINITY_TEACH_CONSUME", "on").lower() not in ("off", "0", "false"):
        r3 = subprocess.run([_py, os.path.join(_dir, "teach_consume.py")],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
        _consume_rc = r3.returncode
    return {"cards_total": _total, "new_ready": _new, "due_now": _due,
            "gen_fallback": _made, "pack_rc": r2.returncode, "consume_rc": _consume_rc,
            "path": "~/.trinity/teach/daily_<today>.md"}


# ── S5: 预测校验 + 自主提议（产物写记忆=brain-proposal，供检索消费）──
def step_propose(conn, dry: bool) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT left(content, 500) FROM memories
                   WHERE status='active' AND category IN ('consolidation','session','emotional-state')
                   ORDER BY updated_at DESC LIMIT 6""")
    recent = [r[0] for r in cur.fetchall()]
    state = _load_state()
    last_pred = state.get("last_prediction")
    dream_txt = ""
    dr = os.path.expanduser("~/.trinity/dream/report.json")
    if os.path.exists(dr) and time.time() - os.path.getmtime(dr) < 86400 * 2:
        try:
            dream_txt = "\n(dream 近期报告摘录)\n" + json.dumps(json.load(open(dr, encoding="utf-8")))[:600]
        except Exception as _e:
            swallow(__name__, _e)
    # H2-2（2026-09-13）：预测必须**可被机器证伪**——散文之外再给一条断言。
    # 实测动机：verify_log 长期以 partial 为主，机器可核验率 0%；不能证伪的预测=没有预测-误差回路。
    _metric_hint = ""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import predict_assert as _pa
        _metric_hint = ("\nassertion 的 metric **只能**取以下之一（其余一律不合法）："
                        + ", ".join(sorted(_pa.METRICS)))
    except Exception as _e:
        swallow(__name__, _e)
    parts = [
        "你是 Trinity 认知周期的自主提议层。基于：近期记忆摘录、上一轮预测(如有)、dream 产物、",
        "当前系统规模，只输出 JSON：{prediction: 对明日系统状态的一条可核验预测（散文，一句）, ",
        "assertion: {metric: <见下方允许列表>, op: >=|<=|==|!=|>|<, value: <数字>}（**机器判定用**）, ",
        "proposals: [1-3 条对 Trinity 自身优化的具体行动建议(可执行、含路径或任务名)]}。",
        "要求：prediction 与 assertion 必须**说同一件事**；给不出合法 assertion 时省略该键。",
        _metric_hint,
        "\n近期记忆：\n" + "\n".join(recent)[:4000]
    ]
    if last_pred:
        parts.append("\n上轮预测: " + str(last_pred)[:300])
    # EXECUTION 640: 未决校验纠偏纳入提议上下文（校验→提议闭环）
    try:
        _tp = os.path.expanduser("~/.trinity/state/verify_corrections_todo.jsonl")
        if os.path.exists(_tp):
            _open = [l for l in open(_tp, encoding="utf-8").read().splitlines() if l][-3:]
            if _open:
                parts.append("\n近期未决校验纠偏(提议应优先回应): " + "; ".join(_open))
    except Exception as _e:
        swallow(__name__, _e)
    # P1/P2（2026-09-13）：把 **regret 状态**喂进提议提示 —— 给 regret_learning 一个**真正的消费者**。
    # 闭环：propose → assertion → verify → regret → propose（避免重复已被证伪的预测/提议）。
    try:
        from trinity.brain.regret_learning import regret_report
        _rr = regret_report() or {}
        # regret_report() 实测形如 {regrets_learned, adjustments_made, improving}
        _n = int(_rr.get("regrets_learned") or 0)
        if _n:
            parts.append("\n历史预测失误累计 %d 次，修正 %s 次，趋势%s —— 提议请避免重复同类预测"
                         % (_n, str(_rr.get("adjustments_made")),
                            "改善中" if _rr.get("improving") else "未改善"))
    except Exception as _e:
        swallow(__name__, _e)
    # P1/P2（2026-09-13）：把 **goal_commitment** 喂进提议提示 —— 给承诺模块一个具名消费者。
    try:
        from trinity.brain.goal_commitment import commitment_report
        _gc = commitment_report() or {}
        _strong = _gc.get("strong_commitments") or []
        if _strong:
            parts.append("\n在建目标（已有承诺，提议请勿重复）: " +
                         "; ".join(str(x)[:60] for x in _strong[:3]))
        elif _gc.get("goals"):
            parts.append("\n在建目标数: %s（提议请与既有目标互补）" % _gc.get("goals"))
    except Exception as _e:
        swallow(__name__, _e)
    # P1/P2（2026-09-13）：把 **experience_feedback** 喂进提议提示（该模块的具名消费者）。
    try:
        from trinity.brain.experience_feedback import feedback_report as _ef_rep
        _efr = _ef_rep() or {}
        _best = _efr.get("best") or _efr.get("top") or []
        if _best:
            parts.append("\n历史经验（哪些做法靠谱）: " + "; ".join(str(x)[:60] for x in _best[:3]))
        elif _efr.get("strategies"):
            parts.append("\n历史经验条目数: %s" % _efr.get("strategies"))
    except Exception as _e:
        swallow(__name__, _e)
    # P1/P2（2026-09-13）：把 **critique_learning** 喂进提议提示（该模块的具名消费者）。
    try:
        from trinity.brain.critique_learning import critique_report as _cl_rep
        _clr = _cl_rep() or {}
        _recent = _clr.get("recent") or _clr.get("critiques") or []
        if _recent:
            parts.append("\n近期自我批评类别: " + "; ".join(
                str(x.get("issue") if isinstance(x, dict) else x)[:40] for x in _recent[-3:]))
    except Exception as _e:
        swallow(__name__, _e)
    # P1「血流」接线（2026-09-14 R41）：机制状态 → 决策 的**批量消费者**。
    # 动机：82 个机制状态文件里 28 个只被 digest 观测（读完即沉没）；本接线把它们的
    # 内容按 hard/soft/context 三档映射成"本轮该怎么做"，并把消费写成运行期证据。
    # 回滚：TRINITY_MECH_FLOW=off（assemble 返回空，行为与本轮之前逐字一致）。
    _mech_meta: dict = {}
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import mech_flow as _mf
        _mctx, _mech_meta = _mf.assemble()
        if _mctx:
            parts.append(_mctx)
    except Exception as _e:
        swallow(__name__, _e)
    prompt = "".join(parts) + dream_txt
    out = llm(prompt, max_tokens=900)
    data = {}
    try:
        if out:
            s = out[out.find("{"): out.rfind("}") + 1]
            data = json.loads(s)
    except Exception as _e:
        swallow(__name__, _e)
    n = 0
    _gated = 0
    _path_rejected = 0  # H2-1：路径落地核验未通过的提议数
    # R41-Q（2026-09-14）：把"提议上限"记进返回值 —— 供 mech_flow_quality 的
    # attention_budget 节流 off/on 对照取证（否则只能看到"写了几条"，看不到门控本身）。
    _prop_limit = 3
    _offered = len(data.get("proposals") or [])
    if not dry and data.get("proposals"):
        # 2026-09-08（P0#3 写确认门）：自动写入过门（近似重复/过短 skip）
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            import write_gate as _wg
        except Exception:
            _wg = None
        # 2026-09-20（§918）：淘汰压力与预算节流同杠杆（都收敛到 1 条）——
        # 注意力面正在丢信号时继续增产，只会生产更多会被丢弃的提议。
        _flags = (_mech_meta.get("flags") or {})
        _prop_limit = 1 if (_flags.get("throttle") or _flags.get("evict_pressure")) else 3
        for p in data["proposals"][:_prop_limit]:
            # H2-1（2026-09-13）：**提议引用的路径必须真实存在**。
            # 实测：goals.json 12 条自主目标共声明 12 个路径，只有 2 个能解析——
            # 10/12 指向不存在的路径 ⇒ 提议从一开始就不可执行。
            try:
                import proposal_guard as _pgd
                if _pgd.should_reject(str(p)):
                    _path_rejected += 1
                    continue
            except Exception as _e:
                swallow(__name__, _e)
            _text = "[认知周期自主提议] " + str(p)[:500]
            if _wg is not None and not _wg.check(conn, _text, "brain-proposal", dry=False)["allow"]:
                _gated += 1
                continue
            cur.execute(
                "INSERT INTO memories (memory_id, session_id, persona_id, agent_id, content, role, importance, tags, category, status, version, sha256_hash, created_at, updated_at, access_count) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (f"brain_prop_{int(time.time())}_{n}", "brain-cycle", "default", "brain-cycle",
                 _text, "assistant", 0.5,
                 json.dumps(["brain-proposal"], ensure_ascii=False), "brain-proposal", "active", 1,
                 "", datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), 0))
            n += 1
    if not dry and n:
        _audit(cur, None, "BRAIN_PROPOSAL", {"proposals": n, "has_prediction": bool(data.get("prediction"))})
        conn.commit()
    if not dry and _mech_meta.get("enabled"):
        try:
            import mech_flow as _mf2
            _mf2.emit_evidence(_mech_meta, conn=conn)
        except Exception as _e:
            swallow(__name__, _e)
    _asrt_ok = False
    if data.get("prediction"):
        state["last_prediction"] = data["prediction"]
        # H2-2：断言随预测一起存；不合法就不存（_verify 会退回 LLM 路径并如实标注来源）
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import predict_assert as _pa
            _a = _pa.parse_assertion(data.get("assertion"))
            if _a:
                state["last_prediction_assertion"] = _a
                _asrt_ok = True
                # 2026-09-13（679）：断言不仅要"机器可核验"，还要**有信息量**——
                # 恒真阈值（如 memory.active >= 当前值）形式上可核验、实质永不报警。
                try:
                    state["last_prediction_assertion_info"] = _pa.informativeness(_a)
                except Exception as _e:
                    swallow(__name__, _e)
            else:
                state.pop("last_prediction_assertion", None)
                state.pop("last_prediction_assertion_info", None)
        except Exception as _e:
            swallow(__name__, _e)
        _save_state(state)
    return {"proposals_written": n, "prediction": (data.get("prediction") or "")[:200],
            "assertion_machine_checkable": _asrt_ok,
            "proposals_path_rejected": _path_rejected,
            "proposal_limit": _prop_limit, "proposals_offered": _offered}


def _append_daily_observation(ts: str, eval_res: dict) -> dict:
    """EXECUTION 639: brain-cycle 每日观测点 → ~/.trinity/strategy/daily_observations.jsonl。

    内容: 认知自评结构化键（cog_score/gap_recall/gap_precision/wm_hit）；失败/门关降级跳过。
    供元层 watch 与 metrics-history 合并形成日级窗口（加速停滞判定）。
    """
    if os.environ.get("TRINITY_BRAIN_CYCLE_OBS", "on").lower() in ("off", "0", "false"):
        return {"skipped": "disabled by env"}
    if not isinstance(eval_res, dict) or not eval_res.get("parsed") or eval_res.get("rc") != 0:
        return {"skipped": "no parsed eval", "rc": (eval_res or {}).get("rc")}
    try:
        obs_dir = os.environ.get("TRINITY_META_STRATEGY_DIR",
                                 os.path.join(os.path.expanduser("~"), ".trinity", "strategy"))
        os.makedirs(obs_dir, exist_ok=True)
        line = _j.dumps({"ts": ts,
                         "cog_score": eval_res.get("cog_score"),
                         "gap_recall": eval_res.get("gap_recall"),
                         "gap_precision": eval_res.get("gap_precision"),
                         "wm_hit": eval_res.get("wm_hit")}, ensure_ascii=False)
        with open(os.path.join(obs_dir, "daily_observations.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return {"status": "ok", "cog_score": eval_res.get("cog_score")}
    except Exception as e:
        return {"status": "degraded", "error": str(e)[:120]}


def _meta_correction(verdict: str, reason: str) -> dict:
    """EXECUTION 639: verify 非 yes（no/partial）→ VERIFY_CORRECTION 审计 + 状态纠偏记录。

    目的: 预测校验结果消费化——"校验→动作"闭环（供周报/质量门禁读取）。
    """
    if verdict in ("yes", "unknown", ""):
        return {"skipped": "no correction needed"}
    try:
        import sys as _s
        _s.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
        from trinity.evolution import meta_strategy as _ms
        _ms.audit("VERIFY_CORRECTION", {"verdict": verdict, "reason": str(reason)[:240]}, apply=True)
    except Exception as _e:
        swallow(__name__, _e)
    try:
        st = _load_state()
        vc = st.get("verify_corrections") or []
        vc.append({"verdict": verdict, "reason": str(reason)[:240],
                   "ts": datetime.now(timezone.utc).isoformat()})
        st["verify_corrections"] = vc[-20:]
        _save_state(st)
    except Exception as _e:
        swallow(__name__, _e)
    # EXECUTION 640: 开放票据（执行侧输入——供 step_propose 等消费）
    try:
        _tickets = os.path.expanduser("~/.trinity/state/verify_corrections_todo.jsonl")
        os.makedirs(os.path.dirname(_tickets), exist_ok=True)
        _lines = []
        if os.path.exists(_tickets):
            _lines = [l for l in open(_tickets, encoding="utf-8").read().splitlines() if l][-199:]
        _lines.append(_j.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                "verdict": verdict, "reason": str(reason)[:240],
                                "status": "open"}, ensure_ascii=False))
        with open(_tickets, "w", encoding="utf-8") as _fh:
            _fh.write("\n".join(_lines) + "\n")
    except Exception as _e:
        swallow(__name__, _e)
    return {"status": "ok", "verdict": verdict}


# 2026-09-11（V2 评估修复）：断点续跑此前只看 status == "ok"，于是"三天前成功过一次"
# 被当成"现在健康"——步骤被永久跳过、status 永远显示 ok（实测 fsrs/teach/propose
# 陈旧 64~79h 仍报 ok）。改为**带时效的断点**：超过该步骤 max_age_h 即视为陈旧，
# 不再计入断点（本次会重新执行），同时把磁盘状态如实标为 stale 并保留 last_status。
# 2026-09-13（实测算术修正）：**阈值 ≥ 日链间隔(≈24h) ⇒ 该步骤变成「每两天跑一次」**。
# 推导：日链 03:40 本地固定跑一次；步骤在 age > T 时才不被断点跳过。
#   T=12（emotion/hebbian/verify）→ 每次 tick 的 age≈24h > 12 → **每天跑** ✓
#   T=36（fsrs/propose）        → 第一次 tick age≈24h < 36 → 跳过；第二次 age≈48h > 36 → 跑 ⇒ **每两天**
# 实测印证（09-13 03:40 日志）：propose 当时 age=31.7h < 36h ⇒ "skipped: checkpoint ok"；
# emotion age=24h > 12h ⇒ 真跑了（有 tone、1.2s）。
# 处置：把**需要每日推进**的 fsrs / propose / eval 降到 20（<24 才能每天跑）；
# teach(72h，周级，预算 420s) 保持不动。回滚：把 20 改回原值（36/36/24）即可。
# 复核算术（合成 now=日链 tick）：T=20 → age=24h 判 stale（当天重跑）；T=24 → age=24h 判 fresh（隔天）。
STEP_MAX_AGE_H = {"fsrs": 20, "hebbian": 12, "emotion": 12, "verify": 12,
                  "eval": 20, "propose": 20, "teach": 72}
STEP_MAX_AGE_DEFAULT = 36.0


def _step_threshold_h(name: str) -> float:
    """该步骤的陈旧阈值（小时）——唯一阈值来源。"""
    try:
        return float(STEP_MAX_AGE_H.get(name, STEP_MAX_AGE_DEFAULT))
    except Exception:
        return STEP_MAX_AGE_DEFAULT


def _step_age_h(entry, now=None) -> "float | None":
    """条目 ts 距 now 的小时数（now 缺省=当前 UTC）；无/不可解析 ts 返回 None。"""
    if not isinstance(entry, dict) or not entry.get("ts"):
        return None
    try:
        ts = datetime.fromisoformat(str(entry["ts"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return (now.timestamp() - ts.timestamp()) / 3600.0
    except Exception:
        return None


# 2026-09-13（V2 评估修复·续）：**拆分被重载的 status 字段**。
# 实测反例：fsrs 与 teach 的 ts **完全相同**（2026-09-11T11:59:22.508427+00:00），
# 一个 stale 一个 ok —— 同一时间戳得出两种状态。
# 根因**不是**阈值算错，而是**一个字段扛了两个语义**：
#   (1) 执行结果：这次跑完是 ok / fail / error（"跑完了"≠"通过了"）
#   (2) 时效性  ：距上次成功是否已超该步骤的重跑周期（栈点/断点判定）
# 旧实现把两者塞进 status：既用 status 参与"是否过期"判断（读自己的旧值，
# 非纯函数），又要让 error/fail 不被时间洗掉（_STEP_TERMINAL_STATUSES 补丁）
# ——那个补丁本身就是"一个字段两个主子"的证据。
# 处置：**按语义拆成两个字段**，各归其位：
#   status    = 执行结果（ok/fail/error/skipped/unknown），**不随时间变化**
#   freshness = 时效（fresh/stale），**是 (ts, now, threshold) 的纯函数**
#   last_status = 上一次的 status（兼容既有消费者；与 status 同源自不可能矛盾）
# 时间维度只能由 freshness 表达：同一 ts + 同一阈值 → 必然同一 freshness。
def _derive_freshness(entry, name: str, now=None) -> str:
    """由 (ts, now, threshold) 唯一决定时效 —— 时间维度**唯一**真源。

        - 非 dict / 无 ts / ts 不可解析 → "stale"（无时间戳的栈点不可信）
        - age > 该步骤阈值               → "stale"
        - 否则                           → "fresh"

    注意：本函数**不读 entry["status"]**，故对同 (ts, now, threshold) 恒定。
    """
    age_h = _step_age_h(entry, now)
    if age_h is None:
        return "stale"
    if age_h > _step_threshold_h(name):
        return "stale"
    return "fresh"


# 执行结果型状态：表达"这次执行的结果"，不随时间变化（时效由 freshness 表达）。
_STEP_RESULT_STATUSES = ("ok", "fail", "error", "skipped", "unknown")


def _derive_result_status(status) -> str:
    """执行结果归一化（**与时间无关**）——保证 status 不被时效污染。"""
    s = str(status) if status is not None else ""
    return s if s in _STEP_RESULT_STATUSES else "ok"


def _normalize_step_entry(entry, name: str, now=None) -> dict:
    """把磁盘条目规范化为语义清晰的形态（status 与 last_status 永不矛盾）。

    - 旧条目（含 status="stale" 的）→ 迁移：stale 属时效语义，迁到 freshness，
      执行结果取 last_status（旧实现已保留），缺省视为 ok。
    - last_status = 上一次的 status（同源，故不可能与之矛盾）。
    """
    out = dict(entry) if isinstance(entry, dict) else {}
    _prev = out.get("status")
    if "last_status" not in out:
        # 旧字段无 last_status：只有当旧 status 不是时效值时才可视为执行结果
        if _prev and _prev not in ("stale", "fresh"):
            out["last_status"] = _prev
    out["status"] = _derive_result_status(out.get("last_status"))
    out["freshness"] = _derive_freshness(entry, name, now)
    return out


def _step_is_fresh(entry, name: str) -> bool:
    """该步骤是否仍在时效内（可以跳过重跑）。

    2026-09-13：改为**按语义**判定——必须"上次成功跑完"(status 非 fail/error)
    且"仍在时效内"(freshness=fresh)。旧实现要求 status=="ok"，与时效语义混用，
    正是 fsrs/teach 同 ts 不同 status 的第二个源头。
    """
    if not isinstance(entry, dict):
        return False
    if _derive_result_status(entry.get("last_status") or entry.get("status")) in ("fail", "error"):
        return False
    return _derive_freshness(entry, name) == "fresh"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="fsrs,hebbian,emotion,verify,eval,propose,teach")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略已成功断点重跑全部")
    args = ap.parse_args()
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    report = {"ts": datetime.now(timezone.utc).isoformat(), "dry": args.dry_run}
    state = _load_state()
    # 2026-09-08（U3 图化内核）：断点续跑——已成功步骤默认跳过（--force 重跑）；
    # 失败步骤自动重试一次后记录 error 状态并继续（checkpoint/retry 语义）。
    # 2026-09-11：断点改为**带时效**（见 _step_is_fresh）；过期步骤不再算断点，
    # 并如实转 stale（保留 last_status），使磁盘状态不再撒谎。
    done = {}
    _stale_marked = []
    # 2026-09-13：**全表重推导**（不再只处理"上次是 ok"的条目）——freshness 是
    # (ts, now, threshold) 的纯函数，因此每次运行都把每个步骤规范化一遍，
    # 同一时间戳必然得出同一时效，磁盘状态不可能自相矛盾。
    for _k, _v in list((state.get("steps") or {}).items()):
        if _step_is_fresh(_v, _k):
            done[_k] = _v
            continue
        if _derive_freshness(_v, _k) == "stale":
            _stale_marked.append(_k)
        if not args.dry_run:  # dry-run 必须零副作用：不写状态
            state.setdefault("steps", {})[_k] = _normalize_step_entry(_v, _k)
    if _stale_marked:
        logger.info("brain-cycle: 断点过期转 stale（本次将重跑）: %s", ",".join(_stale_marked))
        report["stale_steps"] = _stale_marked
    # 2026-09-08（P0#5 注意力预算）：每步预算秒（超时警示而非硬限制）与近期耗时统计
    BUDGET_S = {"fsrs": 180, "hebbian": 90, "emotion": 120, "verify": 120,
                "eval": 700, "propose": 180, "teach": 420}
    _attention = state.get("attention") or {}
    conn = _pg()
    conn.autocommit = True
    try:
        for s in steps:
            t0 = time.time()
            if s in done and not args.force and not args.dry_run:
                report[s] = {"skipped": "checkpoint ok from earlier run"}
                continue
            try:
                fn = {"fsrs": step_fsrs, "hebbian": step_hebbian, "emotion": step_emotion,
                      "verify": step_verify, "eval": step_eval, "propose": step_propose,
                      "teach": step_teach}[s]
                r = (step_verify(args.dry_run) if s == "verify" else
                     (step_teach(args.dry_run) if s == "teach" else
                      (fn(conn, args.dry_run) if s != "eval" else step_eval(args.dry_run))))
                report[s] = {**r, "elapsed_s": round(time.time() - t0, 1)}
                # 2026-09-11（V2 评估修复）：status=ok 此前只表示"脚本没崩"——
                # cognitive-eval 的 PASS=false 也会被记成 ok（实测 2026-09-11 16:02
                # 自评 PASS=false、injection_recall 0.0，cycle_state 仍写 eval: ok）。
                # 这里把**步骤自身的判定**纳入状态，避免"跑完了"被读成"通过了"。
                _verdict = None
                if isinstance(r, dict):
                    if "PASS" in r:
                        _verdict = "ok" if r.get("PASS") else "fail"
                    elif r.get("error"):
                        _verdict = "error"
                # 2026-09-13：新条目同样过唯一真源（刚跑完 → 纯函数必判 ok/fail/error），
                # last_status 与 status 同源，杜绝二者矛盾。
                _st = _normalize_step_entry({"status": _verdict or "ok", "ts": report["ts"]}, s)
                if _verdict == "fail":
                    _st["note"] = "步骤执行成功但自评未通过（PASS=false），需人工处置"
                state.setdefault("steps", {})[s] = _st
                # 2026-09-13（P1/P2 接线）：把**每个步骤的实际结局**喂给 experience_feedback ——
                # 该模块实测 317h 未更新且无消费者。生产者=本循环；消费者=step_propose
                # （提议时参考「哪些认知步骤历史上靠谱」）。闭环：step 运行 → experience → propose。
                try:
                    from trinity.brain.experience_feedback import feedback as _ef
                    _ef(s, 1.0 if _st.get("status") == "ok" else 0.0)
                except Exception as _e:
                    swallow(__name__, _e)
                _el = round(time.time() - t0, 1)
                if _el > BUDGET_S.get(s, 600):
                    report[s]["over_budget"] = True
                    logger.warning("brain-cycle step %s over budget: %.1fs > %ds", s, _el, BUDGET_S.get(s, 600))
                _hist = _attention.setdefault(s, {}).setdefault("recent_s", [])
                _hist.append(_el)
                _attention[s]["recent_s"] = _hist[-7:]
                _attention[s]["avg_s"] = round(sum(_hist[-7:]) / len(_hist[-7:]), 1)
            except Exception as e:
                err1 = f"{type(e).__name__}: {e}"
                # retry once
                try:
                    time.sleep(2)
                    r2 = (step_verify(args.dry_run) if s == "verify" else
                          (step_teach(args.dry_run) if s == "teach" else
                           (fn(conn, args.dry_run) if s != "eval" else step_eval(args.dry_run))))
                    report[s] = {**r2, "elapsed_s": round(time.time() - t0, 1), "retried": True}
                    state.setdefault("steps", {})[s] = _normalize_step_entry(
                        {"status": "ok", "ts": report["ts"], "retried": True}, s)
                except Exception as e2:
                    report[s] = {"error": err1, "retry_error": f"{type(e2).__name__}: {e2}"}
                    state.setdefault("steps", {})[s] = _normalize_step_entry(
                        {"status": "error", "ts": report["ts"], "error": err1}, s)
    finally:
        conn.close()
    # EXECUTION 639: 日观测点入元层 watch 文件（eval 结构化结果）
    if not args.dry_run and isinstance(report.get("eval"), dict):
        _obs_r = _append_daily_observation(report["ts"], report.get("eval"))
        report["daily_obs"] = _obs_r
    st = _load_state()
    st["last_run"] = report["ts"]
    # 2026-09-19（§915.4）：折算改为纯函数 derive_counts —— verify 步按"本轮是否真的判定"计数，
    # 不再恒 0（旧口径把"没这三个键"读成"没干活"）。
    st["counts"] = derive_counts(report)
    _v = report.get("verify") if isinstance(report.get("verify"), dict) else {}
    st["verify_state"] = {"counted_this_run": st["counts"].get("verify", 0),
                          "last_verdict": _v.get("verdict"),
                          "last_source": _v.get("verdict_source"),
                          "log_entries": len(st.get("verify_log") or [])}
    st["stale_steps"] = report.get("stale_steps", [])
    st["counts_note"] = ("counts 覆盖 links_updated/proposals_written/due_today + verify（按本轮判定）；"
                         "无产出的步骤即为 0，不代表该步骤未执行（详见 stale_steps 与 steps.*.status）")
    if state.get("steps") or (not args.dry_run):
        # 2026-09-13：落盘前再统一推导一次（含"步骤被跳过未覆盖"的条目），
        # 保证写出的每个 status 都出自同一纯函数。
        st["steps"] = {_k: _normalize_step_entry(_v, _k)
                       for _k, _v in (state.get("steps") or {}).items()}
    if _attention:
        st["attention"] = _attention
    _save_state(st)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
