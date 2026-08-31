# -*- coding: utf-8 -*-
"""trinity/brain/hindsight_relabel.py — 事后重标记（EXECUTION 373）。

借鉴 Spinning Straw into Gold（2026：Relabeling Trajectories in
Hindsight）——事后重标记：失败轨迹事后重写为成功示范
（hindsight——事后视角把失败变教材）。

与后悔（反事实）互补：后悔=比较；本模块=重标记提炼。
Trinity 现在：
  relabel(trajectory, outcome): 事后重标记（失败→示范）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/hindsight_relabel.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"relabeled": 0, "lessons": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def relabel(trajectory: str, outcome: float = 0.2) -> dict:
    """事后重标记：失败轨迹 → 成功示范（hindsight）。"""
    # 失败轨迹分析（错误步骤→教训）
    steps = [s.strip() for s in str(trajectory).split("→") if s.strip()]
    # 重标记（找出可改进点→示范）
    improved = []
    for s in steps[:4]:
        if any(w in s for w in ("跳过", "直接", "未", "忘记")):
            improved.append(f"[教训] {s[:25]}（应先验证）")
    if not improved:
        improved.append(f"[示范] {str(trajectory)[:40]}（事后视角：完整流程）")
    st = _load()
    st["relabeled"] += 1
    st["lessons"].append({"trajectory": str(trajectory)[:40], "outcome": outcome,
                          "ts": __import__("time").time()})
    st["lessons"] = st["lessons"][-20:]
    _save(st)
    return {"relabeled": st["relabeled"], "improved": improved[:3],
            "outcome": round(outcome, 2),
            "note": f"事后重标记：失败轨迹 → {'、'.join(i[:15] for i in improved[:2])}（hindsight 提炼）"}


def relabel_report() -> dict:
    """重标记状态。"""
    st = _load()
    return {"relabeled": st.get("relabeled", 0),
            "lessons": len(st.get("lessons", [])),
            "note": "事后重标记：失败→示范（Spinning Straw into Gold）"}
