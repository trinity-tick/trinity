"""
Trinity v6.37 — 日常工作流工具箱
===================================
用法：
  1. 每次开始工作：python trinity_work.py start "货架设计"
  2. 工作中记录：   python trinity_work.py note "彩棠重品放第一层" --tags warehouse,rule
  3. 找以前记录：   python trinity_work.py find --query "彩棠货位规则"
  4. 结束工作时：   python trinity_work.py end "完成彩棠货架布局设计"
  5. 查看进度：     python trinity_work.py status
"""

import sys
import os
import json
import argparse
from datetime import datetime

# 抑制 SecondBrain 初始化日志
os.environ["TRINITY_SILENT"] = "1"

# 锁定 Trinity 项目路径
TRINITY_DIR = r"C:\Users\Administrator\trinity"
sys.path.insert(0, TRINITY_DIR)

from trinity.evolution import MetaEvolution, CrossPlatformAdapter, SkillSystemAdapter

# ── 常量 — 统一在 Trinity 项目目录内 ────────────────────────────
# 之前数据分散在 ~/.trinity/ 和 ~/self-improving/
# 现在全部收进 trinity/data/ — 迁移备份只需一个文件夹
DATA_DIR = os.path.join(TRINITY_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "evolution", "evolution_state.json")
SESSION_LOG = os.path.join(DATA_DIR, "session_log.json")
SKILL_DIR = os.path.join(DATA_DIR, "skills")
HANDOFF_DIR = os.path.join(DATA_DIR, "handoffs")

for d in [DATA_DIR, os.path.dirname(STATE_FILE), SKILL_DIR, HANDOFF_DIR]:
    os.makedirs(d, exist_ok=True)


def init_evolution():
    """初始化进化引擎（保留跨会话状态）"""
    evo = MetaEvolution(state_path=STATE_FILE)
    
    # 通用观察钩子
    def work_hook(context):
        obs = []
        ctx = str(context).lower() if context else ""
        if any(kw in ctx for kw in ["仓库", "货架", "货位", "彩棠", "仓储", "3pl"]):
            obs.append({"type": "pattern", "key": "warehouse", "description": "3PL仓储工作"})
        if any(kw in ctx for kw in ["sop", "流程", "标准"]):
            obs.append({"type": "pattern", "key": "sop", "description": "SOP流程写作"})
        if any(kw in ctx for kw in ["python", "脚本", "代码"]):
            obs.append({"type": "pattern", "key": "coding", "description": "Python开发工作"})
        return obs
    
    evo.register_observation_hook(work_hook)
    return evo


def log_session(action, data):
    """记录每次操作到会话日志"""
    log = []
    if os.path.exists(SESSION_LOG):
        try:
            with open(SESSION_LOG, "r", encoding="utf-8") as f:
                log = json.load(f)
        except:
            log = []
    
    log.append({
        "time": datetime.now().isoformat(),
        "action": action,
        **data
    })
    
    # 只保留最近 200 条
    if len(log) > 200:
        log = log[-200:]
    
    with open(SESSION_LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def _fix_encoding():
    """解决 Windows GBK 编码问题"""
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

def cmd_start(args):
    """开始一个工作任务"""
    _fix_encoding()
    evo = init_evolution()
    evo.tick({"task": args.task, "action": "start"})
    evo.save_state()
    
    log_session("start", {"task": args.task})
    
    d = evo.diagnostics()
    print(f"[开始] 任务: {args.task}")
    print(f"       进化周期: {d['total_cycles']} | 已记忆模式: {d['patterns_count']}")


def cmd_note(args):
    """记录一条关键信息"""
    evo = init_evolution()
    evo.tick({"task": "recording", "content": args.content[:50]})
    evo.save_state()
    
    log_session("note", {"content": args.content, "tags": args.tags})
    
    # 也写入 Trinity 记忆
    try:
        from trinity.core.client import Trinity
        mem = Trinity()
        result = mem.ingest(
            content=args.content,
            tags=args.tags.split(",") if args.tags else [],
            category=args.category,
            importance=args.importance,
        )
        mem_id = result.get("memory_id", "?")
    except Exception as e:
        mem_id = f"error: {e}"
    
    print(f"[记录] {args.content[:60]}..." if len(args.content) > 60 else f"[记录] {args.content}")
    print(f"       标签: {args.tags} | 类别: {args.category} | 重要性: {args.importance}")
    print(f"       记忆ID: {mem_id}")


def cmd_find(args):
    """搜索之前记录的信息"""
    evo = init_evolution()
    evo.tick({"task": "searching", "query": args.query})
    evo.save_state()
    
    try:
        from trinity.core.client import Trinity
        mem = Trinity()
        results = mem.search(query=args.query, top_k=args.top_k)
        
        total = len(results) if isinstance(results, list) else results.get("total", 0)
        items = results if isinstance(results, list) else results.get("results", [])
        
        print(f"[搜索] \"{args.query}\" → 找到 {total} 条")
        print()
        for i, item in enumerate(items[:args.top_k]):
            content = ""
            if isinstance(item, dict):
                content = item.get("content", item.get("content_preview", json.dumps(item, ensure_ascii=False)))
            else:
                content = str(item)
            score = item.get("score", item.get("final_score", "?")) if isinstance(item, dict) else "?"
            print(f"  #{i+1} [得分: {score}] {str(content)[:80]}...")
        
        if not items:
            print("  (没有找到匹配的记忆)")
            
    except Exception as e:
        print(f"[搜索] 错误: {e}")


def cmd_end(args):
    """结束工作任务，触发进化周期"""
    evo = init_evolution()
    
    # 完成一个完整进化周期（5 tick）
    for _ in range(5):
        evo.tick({"task": args.summary, "action": "complete"})
    
    evo.save_state()
    log_session("end", {"summary": args.summary})
    
    d = evo.diagnostics()
    print(f"[结束] 任务完成: {args.summary}")
    print(f"       总进化周期: {d['total_cycles']}")
    print(f"       已记忆模式: {d['patterns_count']}")
    
    # 生成 handoff 用于下次
    cpa = CrossPlatformAdapter(work_dir=HANDOFF_DIR)
    handoff = cpa.prepare_handoff(d)
    print(f"       交接文件: {handoff}")
    print(f"       数据目录: {DATA_DIR}")


def cmd_status(args):
    """查看 Trinity 当前状态和最近工作记录"""
    import locale
    # 解决 GBK 编码问题
    sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None
    
    evo = init_evolution()
    d = evo.diagnostics()
    
    print("=" * 50)
    print("  Trinity v6.37 - 工作状态")
    print("=" * 50)
    print(f"  进化周期:        {d['total_cycles']}")
    print(f"  已记忆偏好:      {d['preferences_count']}")
    print(f"  已识别模式:      {d['patterns_count']}")
    print(f"  修正记录:        {d['corrections_count']}")
    print(f"  技能得分:        {d['skills_count']}")
    print(f"  状态文件:        {STATE_FILE}")
    
    # 最近的会话记录
    if os.path.exists(SESSION_LOG):
        with open(SESSION_LOG, "r", encoding="utf-8") as f:
            log = json.load(f)
        
        print(f"\n  最近工作记录 (最近10条):")
        for entry in log[-10:]:
            t = entry.get("time", "").split("T")[1][:8] if "T" in entry.get("time", "") else ""
            a = entry.get("action", "")
            if a == "start":
                print(f"    [开始] {t} {entry.get('task', '')}")
            elif a == "note":
                print(f"    [记录] {t} {str(entry.get('content', ''))[:50]}")
            elif a == "end":
                print(f"    [结束] {t} {entry.get('summary', '')}")
    
    cpa = CrossPlatformAdapter(work_dir=HANDOFF_DIR)
    handoffs = cpa.list_handoffs()
    if handoffs:
        print(f"\n  待处理交接文件: {len(handoffs)} 个")
        for h in handoffs[:3]:
            print(f"    {os.path.basename(h['path'])}")


# ── CLI 入口 ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Trinity 工作流工具箱")
    sub = parser.add_subparsers(dest="cmd")
    
    # start
    p_start = sub.add_parser("start", help="开始工作任务")
    p_start.add_argument("task", help="任务描述")
    
    # note
    p_note = sub.add_parser("note", help="记录关键信息")
    p_note.add_argument("content", help="要记录的内容")
    p_note.add_argument("--tags", default="general", help="逗号分隔的标签")
    p_note.add_argument("--category", default="work", help="类别")
    p_note.add_argument("--importance", type=float, default=0.5, help="重要性 0-1")
    
    # find
    p_find = sub.add_parser("find", help="搜索记忆")
    p_find.add_argument("--query", "-q", required=True, help="搜索关键词")
    p_find.add_argument("--top-k", type=int, default=5, help="返回条数")
    
    # end
    p_end = sub.add_parser("end", help="结束工作任务")
    p_end.add_argument("summary", help="工作总结")
    
    # status
    sub.add_parser("status", help="查看当前状态")
    
    args = parser.parse_args()
    
    if args.cmd == "start":
        cmd_start(args)
    elif args.cmd == "note":
        cmd_note(args)
    elif args.cmd == "find":
        cmd_find(args)
    elif args.cmd == "end":
        cmd_end(args)
    elif args.cmd == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
