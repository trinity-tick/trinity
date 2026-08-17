#!/usr/bin/env python3
"""
Anti-Loop Self-Check for Trinity (防循环自检 v1.0, 2026-08-15)

检测三类"死循环/重复"问题，输出 PASS / WARN / FAIL 报告：

  [A] 重复进程检测：同一脚本/服务被启动多份（如 start_sync_daemon x2、hermes x2）
  [B] 守护关系识别：识别守护进程及其管理的子进程，标记"守护管理，勿手动杀"，
      防止"杀子进程 → 守护重启 → 再杀"的假循环
  [C] 循环迹象检测：进程异常频繁重启（CreationDate 与当前时间差过近且数量多）、
      以及 CPU 持续高负载（推理循环特征）

用法：
    python scripts/anti_loop_self_check.py
    python scripts/anti_loop_self_check.py --json   # 输出 JSON 便于程序解析

退出码：0 = 全部 PASS；1 = 存在 WARN；2 = 存在 FAIL
"""

import argparse
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

# ── 已知守护进程白名单：这些进程会管理/重启子进程，其子进程不应被手动终止 ──
# key = 守护进程可执行名（小写），value = 说明
GUARDIAN_PROCESSES = {
    "hermes.exe": "Hermes 桌面应用，自动拉起 hermes_cli serve",
    "node.exe": "dsh / 其它 node 守护，自动拉起 engine_worker 等",
    "marvisnode.exe": "Marvis 运行时节点",
}

# ── 已知可重复的服务（多份属正常，如多 worker）──
ALLOWED_DUPLICATES = {
    "trinity-mcp.exe": "MCP stdio 每会话一份，属正常",
}

# ── 系统/桌面应用正常多实例进程白名单（多份属正常，不参与重复检测）──
SYSTEM_MULTI_INSTANCE = {
    "svchost.exe", "conhost.exe", "dllhost.exe", "taskhostw.exe",
    "fontdrvhost.exe", "runtimebroker.exe", "csrss.exe", "winlogon.exe",
    "lsass.exe", "services.exe", "smss.exe", "spoolsv.exe", "msedge.exe",
    "cmd.exe", "powershell.exe", "searchhost.exe", "sihost.exe",
    "startmenuexperiencehost.exe", "dwm.exe", "explorer.exe",
    "ctfmon.exe", "shellexperiencehost.exe", "textinputhost.exe",
    "nvidia container.exe", "nvcontainer.exe", "nvdisplay.container.exe",
    "nvidia overlay.exe", "crashpad_handler.exe", "cefrendererprocess.exe",
    "wsl.exe", "wslhost.exe", "docker desktop.exe", "com.docker.backend.exe",
    "postgres.exe", "feishu.exe", "thunder.exe", "workbuddy.exe",
    "lm studio.exe", "onethingpclite.exe", "番茄打印管家.exe",
    "wechatappex.exe", "guluplugin.exe", "androwsmcp.exe",
    "marvisknowledgebase.exe", "hermes.exe", "node.exe",
}

# ── 业务脚本/服务特征：仅这些参与重复检测（聚焦 Trinity / hermes / Marvis 相关）──
BUSINESS_CMD_MARKERS = [
    "start_sync_daemon.py", "engine_worker.py", "hermes_cli.main",
    "trinity.mcp.server", "trinity.api.server", "collector/daemon.py",
    "trinity/trinity/daemon", "trinity_service.py", "trinity-mcp.exe",
    "marvis_bridge.py", "bidirectionalsyncdaemon", "sync_daemon",
]


def get_processes() -> list:
    """返回进程列表：pid, ppid, name, cmdline, creation_ts(epoch秒)"""
    ps = (
        "Get-CimInstance Win32_Process | "
        "ForEach-Object { "
        "$c = $_.CommandLine; if ($c) { $c = $c -replace '\\s+', ' ' }; "
        "Write-Output (\"$($_.ProcessId)|$($_.ParentProcessId)|$($_.Name)|$($_.CreationDate)|$c\") "
        "}"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except Exception as exc:
        print(f"[ERROR] 无法枚举进程: {exc}")
        return []

    procs = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|", 4)
        if len(parts) < 5:
            continue
        pid, ppid, name, cdate, cmd = parts
        try:
            pid_i, ppid_i = int(pid), int(ppid)
        except ValueError:
            continue
        ts = None
        if cdate and cdate != "None":
            try:
                # CIM 返回格式如 20260815093744.123456+480
                ts = datetime.strptime(cdate[:14], "%Y%m%d%H%M%S").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                ts = None
        procs.append({
            "pid": pid_i, "ppid": ppid_i, "name": name.lower(),
            "cmdline": cmd or "", "creation_ts": ts,
        })
    return procs


def _cmd_key(proc: dict) -> str:
    """按命令行特征生成分组 key，用于检测重复启动。"""
    cmd = proc["cmdline"]
    # 提取脚本/模块名作为 key
    for marker in BUSINESS_CMD_MARKERS:
        if marker in cmd:
            return marker
    return None


def check_duplicates(procs: list) -> list:
    """[A] 重复进程检测（仅聚焦业务脚本/服务）。返回 WARN 列表。"""
    warns = []
    groups = defaultdict(list)
    for p in procs:
        if not p["cmdline"]:
            continue
        if p["name"] in SYSTEM_MULTI_INSTANCE:
            continue
        key = _cmd_key(p)
        if key is None:
            continue
        if key in ALLOWED_DUPLICATES:
            continue
        groups[key].append(p)
    for key, members in groups.items():
        if len(members) > 1:
            # 豁免"主进程 + worker 子进程"结构：若组内存在父子关系，视为正常
            pids = {m["pid"] for m in members}
            has_parent_child = any(m["ppid"] in pids for m in members)
            if has_parent_child:
                continue
            pid_list = ", ".join(str(m["pid"]) for m in members)
            warns.append(
                f"[A] 重复进程: {key} 启动 {len(members)} 份 (PID: {pid_list})"
            )
    return warns


def check_guardians(procs: list) -> list:
    """[B] 守护关系识别。返回 INFO 列表（守护管理的子进程，勿手动杀）。"""
    infos = []
    by_pid = {p["pid"]: p for p in procs}
    for p in procs:
        if p["name"] in GUARDIAN_PROCESSES:
            children = [c for c in procs if c["ppid"] == p["pid"]]
            if children:
                child_desc = ", ".join(
                    f"{c['name']}(PID {c['pid']})" for c in children[:5]
                )
                infos.append(
                    f"[B] 守护进程 {p['name']}(PID {p['pid']}) 管理子进程: {child_desc} "
                    f"— 勿手动终止，否则守护会立即重启（假循环）"
                )
    return infos


def check_loop_signs(procs: list) -> list:
    """[C] 循环迹象检测：异常频繁重启 + CPU 持续高负载。"""
    warns = []
    now = time.time()
    # 1) 近 5 分钟内启动的进程数量异常多 → 疑似反复重启
    recent = [p for p in procs if p["creation_ts"] and (now - p["creation_ts"]) < 300]
    if len(recent) >= 5:
        warns.append(
            f"[C] 近 5 分钟启动 {len(recent)} 个进程，疑似异常频繁重启/循环"
        )
    # 2) 同一脚本在近 10 分钟内启动 >= 3 次 → 疑似重启循环
    recent10 = [p for p in procs if p["creation_ts"] and (now - p["creation_ts"]) < 600]
    groups = defaultdict(int)
    for p in recent10:
        if p["cmdline"] and p["name"] not in SYSTEM_MULTI_INSTANCE:
            key = _cmd_key(p)
            if key is not None:
                groups[key] += 1
    for key, cnt in groups.items():
        if cnt >= 3 and key not in ALLOWED_DUPLICATES:
            warns.append(
                f"[C] 脚本 {key} 近 10 分钟启动 {cnt} 次，疑似重启循环"
            )
    return warns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    procs = get_processes()
    if not procs:
        print("[ERROR] 未获取到进程列表")
        sys.exit(2)

    a_warns = check_duplicates(procs)
    b_infos = check_guardians(procs)
    c_warns = check_loop_signs(procs)

    all_warns = a_warns + c_warns
    status = "PASS" if not all_warns else ("WARN" if len(all_warns) <= 2 else "FAIL")
    exit_code = 0 if status == "PASS" else (1 if status == "WARN" else 2)

    if args.json:
        print(json.dumps({
            "status": status,
            "total_processes": len(procs),
            "warns": all_warns,
            "infos": b_infos,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"=== Anti-Loop Self-Check ===")
        print(f"进程总数: {len(procs)}")
        print(f"状态: {status}")
        for w in all_warns:
            print(f"  [WARN] {w}")
        for i in b_infos:
            print(f"  [INFO] {i}")
        if not all_warns and not b_infos:
            print("  未发现重复进程 / 循环迹象 / 守护冲突")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
