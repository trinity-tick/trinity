"""
Trinity v6.37 — 一键初始化脚本
================================
作用：在任何窗口/MCP/Agent中只需执行一次，恢复全部记忆和进化状态

用法：
  python trinity_init.py           # 初始化/恢复
  python trinity_init.py check     # 查看状态
  python trinity_init.py reset     # 重置

设计原则：
  - 状态统一存储在 ~/.trinity/ (所有窗口共享)
  - 任何窗口运行本脚本 = 恢复全部记忆
  - 不需要每个窗口单独配置
"""

import os
import sys
import json
from pathlib import Path

# ── 全局路径 — 全部收进 Trinity 项目目录 ───────────────────────
# 之前数据分散在 ~/.trinity/ 和 ~/self-improving/
# 现在全部统一在 trinity/data/ 下，迁移备份只需一个文件夹
PROJECT_DIR = Path(r"C:\Users\Administrator\trinity")
DATA_DIR = PROJECT_DIR / "data"
STATE_FILE = DATA_DIR / "evolution" / "evolution_state.json"
HANDOFF_DIR = DATA_DIR / "handoffs"
SKILL_DIR = DATA_DIR / "skills"

for d in [DATA_DIR, STATE_FILE.parent, HANDOFF_DIR, SKILL_DIR]:
    d.mkdir(parents=True, exist_ok=True)


def init_trinity() -> dict:
    """初始化/恢复 Trinity 系统。调用这个就够。"""
    sys.path.insert(0, str(PROJECT_DIR))
    
    from trinity.evolution import MetaEvolution, CrossPlatformAdapter
    
    # 1. 恢复进化状态
    evo = MetaEvolution(state_path=str(STATE_FILE))
    
    # 2. 注册通用观察钩子（跨会话持久）
    evo.register_observation_hook(make_observation_hook())
    
    # 3. 执行一个恢复性 tick
    result = evo.tick({"action": "init", "workspace": "3PL仓储", "window": os.environ.get("GOOSE_SESSION_ID", "unknown")})
    
    # 4. 保存状态
    evo.save_state()
    
    # 5. 生成 handoff 文件（其他窗口可读取）
    cpa = CrossPlatformAdapter(work_dir=str(HANDOFF_DIR))
    handoff = cpa.prepare_handoff(evo.diagnostics())
    
    # 6. 清理旧 handoff
    clean_old_handoffs(str(HANDOFF_DIR))
    
    return {
        "status": "ok",
        "phase": result.get("phase"),
        "total_cycles": evo.diagnostics().get("total_cycles"),
        "handoff_file": handoff,
        "state_file": str(STATE_FILE),
        "preferences": len(evo.state.active_preferences),
        "patterns": len(evo.state.active_patterns),
        "corrections": len(evo.state.corrections_log),
    }


def make_observation_hook():
    """通用的观察钩子——捕获你的工作模式并记忆。"""
    def hook(context):
        observations = []
        
        # 检测仓库相关任务
        ctx_str = str(context).lower()
        
        if any(kw in ctx_str for kw in ["仓库", "货架", "货位", "彩棠", "仓储", "warehouse"]):
            observations.append({
                "type": "pattern",
                "key": "warehouse_operation",
                "description": "3PL仓储操作：彩棠货位、重品第一层、X轴优先扩展",
                "context": context,
            })
        
        # 检测SOP/流程相关
        if any(kw in ctx_str for kw in ["sop", "流程", "标准", "步骤"]):
            observations.append({
                "type": "preference",
                "key": "sop_style",
                "description": "SOP写作偏好：标准格式→特殊情况→合并验证，表格输出",
                "context": context,
            })
        
        # 检测编程/脚本相关
        if any(kw in ctx_str for kw in ["python", "脚本", "代码", "代码"]):
            observations.append({
                "type": "preference",
                "key": "coding_style",
                "description": "Python工作流：保存临时文件后执行，迭代式开发",
                "context": context,
            })
        
        return observations
    return hook


def clean_old_handoffs(handoff_dir: str):
    """只保留最新的 handoff 文件。"""
    import glob, os, time
    handoffs = glob.glob(os.path.join(handoff_dir, "handoff_*.json"))
    if len(handoffs) > 3:
        handoffs.sort(key=os.path.getmtime)
        for f in handoffs[:-3]:
            try:
                os.remove(f)
            except Exception:
                pass


def check_status() -> dict:
    """查看 Trinity 当前状态。"""
    if not STATE_FILE.exists():
        return {"status": "not_initialized"}
    
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    
    # 检查 handoff 文件
    handoff_files = list(HANDOFF_DIR.glob("handoff_*.json")) if HANDOFF_DIR.exists() else []
    
    return {
        "status": "active",
        "total_cycles": state.get("total_cycles", 0),
        "last_cycle": state.get("last_cycle_id"),
        "preferences": len(state.get("active_preferences", {})),
        "patterns": len(state.get("active_patterns", {})),
        "corrections": len(state.get("corrections_log", [])),
        "skill_scores": len(state.get("skill_scores", {})),
        "updated_at": state.get("updated_at"),
        "handoffs_available": len(handoff_files),
    }


def reset_state():
    """重置进化状态（保留备份）。"""
    if STATE_FILE.exists():
        backup = str(STATE_FILE) + ".bak"
        os.rename(str(STATE_FILE), backup)
        return {"status": "reset", "backup": backup}
    return {"status": "no_state_found"}        



# ── CLI 入口 ──────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "init"
    
    if cmd == "init":
        result = init_trinity()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"\n[OK] Trinity v6.37 已就绪")
        print(f"   状态文件: {STATE_FILE}")
        print(f"   数据目录: {DATA_DIR}")
        print(f"   进化周期: {result['total_cycles']}")
        
    elif cmd == "check":
        result = check_status()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result["status"] == "active":
            print(f"\n[OK] Trinity 正常运行中")
        else:
            print(f"\n⚠️  Trinity 尚未初始化，运行 python trinity_init.py")
            
    elif cmd == "reset":
        result = reset_state()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
    else:
        print(f"用法: python trinity_init.py [init|check|reset]")
