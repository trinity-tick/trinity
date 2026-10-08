#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开关使用台账（t64 / G8-R1）—— **`SCAN=off` / `REDACT=0` / `ADAPTER_GUARD=0` 谁在什么时候关过**。

## 为什么需要
t49 裁定：`TRINITY_SENSITIVE_SCAN=off` = **所有层都不扫描** ⇒ 误关期间的写入**原文落库且不可逆**。
G8-R1 要求进"开关使用台账"。本脚本只做**读取与记账**，不改变任何开关语义。

## 三个开关（全部走**产品自己的读法**，不另造指标名）
| 环境变量 | 产品读法 |
|---|---|
| `TRINITY_SENSITIVE_SCAN` | `trinity.security.sensitive.sensitive_scan_enabled()` |
| `TRINITY_SENSITIVE_REDACT` | `trinity.security.sensitive.sensitive_redact_enabled()` |
| `TRINITY_SENSITIVE_REDACT_SCOPE` | `trinity.security.sensitive.sensitive_redact_scope()` |
| `TRINITY_ADAPTER_GUARD` | `trinity.adapters._pii_guard.adapter_guard_state()` / `adapter_guard_enabled()` |

## 怎么查（一句话）
```
python scripts/switch_ledger.py --record     # 记一次快照（进程启动/诊断/收口时跑）
python scripts/switch_ledger.py --show       # 看：当前状态 + 历史上哪些时刻被显式关过、关的是哪个、哪个进程
```
台账文件：`%USERPROFILE%\\.trinity\\state\\switch_ledger.jsonl`（可用 `--ledger` 覆盖；测试用临时路径）。
每条记录形如：
```json
{"ts": "...", "pid": 1234, "ppid": 5678, "who": "dsh-session/agent 或 argv0", "cwd": "...",
 "switches": {"SCAN": {"env": null, "explicit": false, "effective": true, "off": false}, ...},
 "explicit_off": ["ADAPTER_GUARD"]}
```
**消费者**：① 本脚本 `--show`（人/运维）；② `tests/unit/test_audit_landing_and_switch_ledger_20261006.py`
（判据：快照字段必须与产品读法一致；显式关闭必须出现在 `explicit_off`；台账可追加可汇总）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

DEFAULT_LEDGER = Path(os.path.expanduser("~")) / ".trinity" / "state" / "switch_ledger.jsonl"

#: 开关定义：(台账键, 环境变量, 产品读法(可调用), 关闭判定)
_SWITCHES = (
    ("SCAN", "TRINITY_SENSITIVE_SCAN"),
    ("REDACT", "TRINITY_SENSITIVE_REDACT"),
    ("REDACT_SCOPE", "TRINITY_SENSITIVE_REDACT_SCOPE"),
    ("ADAPTER_GUARD", "TRINITY_ADAPTER_GUARD"),
)
_OFF_VALUES = ("0", "off", "false", "no", "")


def _read_effective(name: str):
    """用**产品自己的读法**取有效值（读不到就返回 None，绝不臆造）。"""
    try:
        from trinity.security import sensitive as S
        if name == "SCAN":
            return bool(S.sensitive_scan_enabled())
        if name == "REDACT":
            return bool(S.sensitive_redact_enabled())
        if name == "REDACT_SCOPE":
            return str(S.sensitive_redact_scope())
        if name == "ADAPTER_GUARD":
            from trinity.adapters import _pii_guard as G
            if hasattr(G, "adapter_guard_state"):
                return bool(G.adapter_guard_state()[0])
            return bool(G.adapter_guard_enabled())
    except Exception as _e:                     # noqa: BLE001 — 读不到就是 None，如实记
        return "UNREADABLE:%s" % type(_e).__name__
    return None


def _declared_off(raw) -> bool:
    """环境变量是否**显式声明为关**（`None` = 未设置 ⇒ 不算显式关）。"""
    if raw is None:
        return False
    return str(raw).strip().lower() in _OFF_VALUES


def snapshot() -> dict:
    """当前进程的开关快照（含来源、**显式关闭**与**有效关闭**两栏）。

    · `explicit_off`：环境变量**被显式设成关闭值**的开关（= "谁关过" 的判据，
      只有这一栏进 `summarize()` 的历史，避免把"级联失效"误报成"有人关过"）；
    · `effective_off`：**产品读法**当前说"关"的开关（可能是级联，
      例如 t49 的 `SCAN=off` ⇒ 策略层整体不扫描，REDACT 的有效值也会变成关）。
    """
    sw = {}
    explicit_off, effective_off = [], []
    for key, env_name in _SWITCHES:
        raw = os.environ.get(env_name)
        eff = _read_effective(key)
        declared = _declared_off(raw)
        entry = {"env": raw, "explicit": raw is not None, "declared_off": declared,
                 "effective": eff, "off": (eff is False) if isinstance(eff, bool) else None}
        if key == "REDACT_SCOPE":
            entry["off"] = None                  # 作用域不是开关
        sw[key] = entry
        if key != "REDACT_SCOPE":
            if declared:
                explicit_off.append(key)
            if entry["off"]:
                effective_off.append(key)
    rec = {
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "who": os.environ.get("TRINITY_SWITCH_LEDGER_WHO") or " ".join(sys.argv[:2]) or "unknown",
        "cwd": os.getcwd(),
        "switches": sw,
        "explicit_off": explicit_off,
        "effective_off": effective_off,
    }
    return rec


def append_record(ledger_path=None, rec: dict | None = None) -> dict:
    """把一条快照**追加**到台账（JSONL，一行一条）。返回写入的那条记录。"""
    path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER
    rec = rec or snapshot()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load_records(ledger_path=None) -> list:
    path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:                        # noqa: BLE001 — 坏行跳过但计数
            out.append({"ts": None, "corrupt": True})
    return out


def summarize(records: list) -> dict:
    """汇总成"**什么时候被关过、关的是哪个、谁关的**"。"""
    offs = []
    for r in records:
        if r.get("corrupt"):
            continue
        for key in (r.get("explicit_off") or []):
            offs.append({"ts": r.get("ts"), "switch": key, "who": r.get("who"),
                         "pid": r.get("pid"), "env": ((r.get("switches") or {}).get(key) or {}).get("env")})
    last = records[-1] if records else None
    return {"records": len(records), "off_events": len(offs),
            "off_history": offs[-20:], "last": last,
            "any_off_now": sorted(((last or {}).get("effective_off") or [])),
            "explicit_off_now": sorted(((last or {}).get("explicit_off") or []))}


def main() -> int:
    ap = argparse.ArgumentParser(description="开关使用台账（只读 + 追加记账；不改任何开关语义）")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="台账 JSONL 路径")
    ap.add_argument("--record", action="store_true", help="记一条当前进程快照")
    ap.add_argument("--show", action="store_true", help="看当前状态 + 历史关闭记录")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if a.record:
        rec = append_record(a.ledger)
        print("已记账：%s explicit_off=%s pid=%s" % (rec["ts"], rec["explicit_off"], rec["pid"]))
    if a.show or not a.record:
        snap, records = snapshot(), load_records(a.ledger)
        summary = summarize(records)
        payload = {"now": snap, "summary": summary}
        if a.json:
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        else:
            print("=== 当前开关（产品读法）===")
            for k, v in snap["switches"].items():
                print("  %-14s env=%-6s effective=%-6s explicit=%s%s"
                      % (k, v["env"], v["effective"], v["explicit"],
                         "  ← 显式关闭!" if k in snap["explicit_off"] else ""))
            print("=== 历史（台账 %s，共 %d 条 / 关闭事件 %d 次）==="
                  % (a.ledger, summary["records"], summary["off_events"]))
            for ev in summary["off_history"]:
                print("  %s  关的是 %-14s who=%s pid=%s env=%s"
                      % (ev["ts"], ev["switch"], ev["who"], ev["pid"], ev["env"]))
            if not summary["off_history"]:
                print("  （无显式关闭记录）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
