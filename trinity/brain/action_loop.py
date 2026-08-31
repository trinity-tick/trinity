# -*- coding: utf-8 -*-
"""trinity/brain/action_loop.py — 行动回路（EXECUTION 181）大脑化：刺激→反应。

感知（信号）→ 评估（严重度）→ 决策（动作映射）→ 执行（修复）→ 经验回写。

大脑对应：反射弧——感觉输入不经"高层认知"直接触发运动反应，
但每次反应后经验回写（习惯化/学习）。这里用规则引擎模拟：
  - 异常信号（完整性缺失/服务挂/任务失败）→ 映射到修复动作
  - 执行动作 → 记录行动日志（经验记忆）
  - 行动有效性影响下次决策（学到的反应）

使用：scripts/action_loop.py 入口；维护链 action-loop 任务每日驱动。
"""
import os
import sys
import json
import time
from datetime import datetime


# ── 刺激→反应映射（规则引擎）──────────────────────────
# 信号模式 → (动作名, 动作函数名, 严重度)
STIMULUS_ACTIONS = {
    "missing_vectors": ("backfill", "_fix_missing_vectors", 0.8),
    "audit_integrity_break": ("audit_rebuild", "_fix_audit", 0.9),
    "service_down": ("service_restart", "_restart_service", 0.9),
    "task_stalled": ("task_notify", "_notify_stall", 0.6),
}


def _fix_missing_vectors(severity: float) -> dict:
    """动作：回填缺失向量（调用完整性巡检修复）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        import runpy
        _old = sys.argv
        sys.argv = ["pg_integrity_monitor"]
        runpy.run_path(r"D:\\trinity-code\\scripts\\pg_integrity_monitor.py", run_name="__main__")
        sys.argv = _old
        return {"done": True, "action": "backfill"}
    except Exception as e:
        return {"done": False, "action": "backfill", "error": str(e)[:80]}


def _fix_audit(severity: float) -> dict:
    """动作：审计链重建（幂等脚本）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.adapters.postgresql import PostgreSQLAdapter
        a = PostgreSQLAdapter(auto_connect=True)
        a.connect()
        try:
            ok = a.rebuild_audit_chain() if hasattr(a, "rebuild_audit_chain") else False
            return {"done": ok, "action": "audit_rebuild"}
        finally:
            a.disconnect()
    except Exception as e:
        return {"done": False, "action": "audit_rebuild", "error": str(e)[:80]}


def _restart_service(severity: float) -> dict:
    """动作：服务重启（触发 API 守护）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.api.server import restart as _restart
        _restart()
        return {"done": True, "action": "service_restart"}
    except Exception:
        return {"done": False, "action": "service_restart", "error": "no restart fn"}


def _notify_stall(severity: float) -> dict:
    """动作：任务停滞告警（审计记录）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.adapters.postgresql import PostgreSQLAdapter
        a = PostgreSQLAdapter(auto_connect=True)
        a.connect()
        try:
            a.write_audit_log(memory_id=None, action="task_stalled_notify",
                              agent_id="action-loop",
                              details={"severity": severity, "ts": time.time()})
            return {"done": True, "action": "task_notify"}
        finally:
            a.disconnect()
    except Exception:
        return {"done": False, "action": "task_notify"}


_ACTIONS = {
    "backfill": _fix_missing_vectors,
    "audit_rebuild": _fix_audit,
    "service_restart": _restart_service,
    "task_notify": _notify_stall,
}


class ActionLoop:
    """行动回路：感知信号 → 评估 → 动作 → 经验回写。"""

    def __init__(self, action_log=None):
        self.action_log = action_log or os.path.expanduser("~/.trinity/action_loop.json")
        self.history = self._load()

    def _load(self) -> list:
        try:
            with open(self.action_log, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save(self):
        try:
            with open(self.action_log, "w", encoding="utf-8") as f:
                json.dump(self.history[-100:], f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def detect_stimuli(self) -> dict:
        """检测刺激：扫描系统状态找异常信号。"""
        stimuli = {}
        try:
            import psycopg2
            conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                    user="trinity", password="trinity")
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM memories WHERE embedding IS NULL AND status='active'")
            missing = cur.fetchone()[0]
            if missing > 20:
                stimuli["missing_vectors"] = {"count": missing}
            cur.execute("SELECT count(*) FROM audit_log WHERE timestamp > NOW() - interval '24 hours'")
            audit_24h = cur.fetchone()[0]
            conn.close()
            if audit_24h == 0:
                stimuli["audit_integrity_break"] = {"note": "no audit in 24h"}
        except Exception:
            pass
        return stimuli

    def respond(self, stimuli: dict) -> list:
        """对刺激做出反应（执行动作 + 经验回写）。"""
        results = []
        for signal, info in stimuli.items():
            entry = STIMULUS_ACTIONS.get(signal)
            if not entry:
                continue
            action_name, fn_name, severity = entry
            fn = _ACTIONS.get(action_name)
            if not fn:
                continue
            result = fn(severity)
            log_entry = {
                "ts": datetime.now().isoformat(),
                "stimulus": signal,
                "action": action_name,
                "severity": severity,
                "result": result,
            }
            self.history.append(log_entry)
            self._save()
            results.append(log_entry)
        return results

    def report(self) -> dict:
        return {
            "stimuli_detected": self.detect_stimuli(),
            "actions_taken": len(self.history),
            "history_tail": self.history[-5:],
        }


    # ── 2026-09 (EXECUTION 182): 行动经验学习（条件反射）──
    def learn(self, results: list) -> dict:
        """从行动结果学习：更新刺激-动作成功率（巴甫洛夫式关联强化）。

        每次行动后调用：成功 → 关联权重上升；失败 → 下降。
        未来同类刺激优先选成功率高的动作。
        """
        stats = self._load_stats()
        for r in results:
            stim = r.get("stimulus", "?")
            action = r.get("action", "?")
            done = bool(r.get("result", {}).get("done"))
            key = stim + "|" + action
            s = stats.get(key, {"ok": 0, "fail": 0})
            if done:
                s["ok"] += 1
            else:
                s["fail"] += 1
            stats[key] = s
        self._save_stats(stats)
        return stats

    def _load_stats(self) -> dict:
        try:
            with open(self.action_log.replace(".json", "_stats.json"), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_stats(self, stats: dict):
        try:
            with open(self.action_log.replace(".json", "_stats.json"), "w", encoding="utf-8") as f:
                json.dump(stats, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def best_action(self, stimulus: str) -> str:
        """按成功率选最优动作（有学习时）；无记录回退规则映射。"""
        stats = self._load_stats()
        cands = [(k.split("|")[1], s) for k, s in stats.items() if k.startswith(stimulus + "|")]
        if not cands:
            entry = STIMULUS_ACTIONS.get(stimulus)
            return entry[0] if entry else None
        cands.sort(key=lambda x: (x[1]["ok"] / max(x[1]["ok"] + x[1]["fail"], 1)), reverse=True)
        return cands[0][0]

    def experience_to_memory(self) -> bool:
        """行动经验写入记忆（经验闭环到记忆层）。"""
        try:
            stats = self._load_stats()
            if not stats:
                return False
            summary = "；".join(
                f"{k} 成功率{int(v['ok'] * 100 / max(v['ok'] + v['fail'], 1))}%" for k, v in stats.items()
            )
            sys.path.insert(0, r"D:\trinity-code")
            from trinity import Trinity
            m = Trinity(adapter="postgresql")
            m.ingest("[action-experience] " + summary[:200], category="action-experience",
                     tags=["action", "learning"], importance=0.7, wait_backfill=True)
            return True
        except Exception:
            return False


    # ── 2026-09 (EXECUTION 197): 内感受注意（Interoceptive Attention 借鉴）──
    # 健康异常时感知注意力转向内部（优先内部检查而非外部感知）
    def interoceptive_check(self) -> dict:
        """内感受：健康异常 → 返回内部优先信号（驱动内部检查动作）。"""
        internal_signals = {}
        try:
            import psycopg2
            conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                    user="trinity", password="trinity")
            cur = conn.cursor()
            # 缺失向量（内部状态）
            cur.execute("SELECT count(*) FROM memories WHERE embedding IS NULL AND status='active'")
            missing = cur.fetchone()[0]
            # 审计 24h 活跃（内部健康）
            cur.execute("SELECT count(*) FROM audit_log WHERE timestamp > NOW() - interval '24 hours'")
            audit_24h = cur.fetchone()[0]
            conn.close()
            if missing > 20:
                internal_signals["internal_missing_vectors"] = missing
            if audit_24h == 0:
                internal_signals["internal_audit_silent"] = True
        except Exception:
            pass
        return {"internal_priority": bool(internal_signals), "signals": internal_signals}
