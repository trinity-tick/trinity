#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""环境体检脚本（只读诊断）。

只读诊断：绝不修改任何文件，绝不打印任何密钥/密码内容。
检查项：
  1. Python 版本与解释器路径；项目 .venv 是否存在与系统 Python 并存提示
  2. faiss 导入状态（区分缺包与 'faiss.swigfaiss_avx2' 正常降级噪音）
  3. 服务端口监听 8000/8001/8002/5430（socket connect 探测，不启动服务）
  4. 关键进程存活（api/mcp/collector/engine_worker，只读按命令行匹配）
  5. 运行时库 ~/.trinity/store/trinity_store.db 及 -wal 的存在性/大小（只 stat）
  6. 凭证文件 ~/.dsh/.credentials.yaml 存在性 + 键名列表（只报键名）
  7. 维护日志 ~/.trinity/logs/dsh-maintenance.log 最近 200 行 FAILED/WARN/ERROR 关键行（≤10 条）
  8. C 盘剩余空间

退出码：全部正常 0；有警告 1；有错误 2。
  --quiet 只输出摘要。
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys

PY = sys.version.split()[0]
SYSTEM_HINT = "（项目 .venv 与系统 Python 并存：以系统 Python 为准）"

TRINITY_ROOT = r"C:\Users\Administrator\trinity"
VENV_DIR = os.path.join(TRINITY_ROOT, ".venv")
SYSTEM_PY = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
PORT_TARGETS = [8000, 8001, 8002, 5430]
STORE_DB = os.path.expanduser(r"~\.trinity\store\trinity_store.db")
CRED_FILE = os.path.expanduser(r"~\.dsh\.credentials.yaml")
MAINT_LOG = os.path.expanduser(r"~\.trinity\logs\dsh-maintenance.log")


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def read_yaml_keys(path):
    """极简键名解析：返回顶层键名列表（绝不读值）。"""
    keys = []
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped[:1] in ("-", "["):
                    continue
                if ":" in stripped:
                    key = stripped.split(":", 1)[0].strip().strip('"').strip("'")
                    if key:
                        keys.append(key)
    except (OSError, UnicodeDecodeError):
        return None
    return keys


def _force_utf8_stdout():
    """确保向控制台/管道输出 UTF-8（Windows 默认 GBK 无法编码 ✓/⚠/✗）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    ap = argparse.ArgumentParser(description="Trinity 环境体检（只读诊断）")
    ap.add_argument("--quiet", action="store_true", help="只输出摘要")
    args = ap.parse_args()

    _force_utf8_stdout()

    warn_msgs = []          # 摘要级警告说明
    err_msgs = []           # 摘要级错误说明
    exit_code = 0

    if args.quiet:
        print("= Trinity 环境体检（摘要） =")
    else:
        print("=" * 60)
        print("Trinity 环境体检（只读诊断，绝不修改任何文件）")
        print(f"运行时间: {__import__('datetime').datetime.now().isoformat(' ', 'seconds')}")
        print("=" * 60)

    # ---------------- 1. Python 版本 / 解释器 / .venv ----------------
    venv_exists = os.path.isdir(VENV_DIR)
    is_system = (os.name == "nt") and os.path.normpath(sys.executable).lower() == os.path.normpath(SYSTEM_PY).lower()
    venv_note = ""
    if venv_exists and is_system:
        venv_note = SYSTEM_HINT
    if not args.quiet:
        print(f"\n[1] Python {PY} {venv_note}")
        print(f"    解释器: {sys.executable}")
        if venv_exists:
            print(f"    项目 .venv 存在: {VENV_DIR}")
        else:
            print("    项目 .venv 不存在")

    # ---------------- 2. faiss 导入状态 ----------------
    faiss_status = "normal"
    if not args.quiet:
        print("\n[2] faiss 导入状态:", end=" ")
    try:
        import faiss  # noqa: F401
        faiss_status = "normal"
        if not args.quiet:
            print("✓ 导入成功")
    except ImportError as e:
        msg = str(e)
        if "faiss.swigfaiss_avx2" in msg:
            faiss_status = "degrade"   # 正常降级噪音，警告级
            if not args.quiet:
                print("⚠ 降级提示（正常，非错误）: faiss.swigfaiss_avx2 未加载，已回退")
            warn_msgs.append("[2] faiss 出现 swigfaiss_avx2 降级噪音（EXECUTION.md 记录为正常）")
        elif "No module named" in msg:
            faiss_status = "missing"   # 缺包，错误级
            if not args.quiet:
                print(f"✗ 缺少 faiss: No module named")
            err_msgs.append("[2] faiss 缺失（No module named），请安装")
        else:
            faiss_status = "missing"
            if not args.quiet:
                print(f"✗ faiss 导入失败: {msg}")
            err_msgs.append("[2] faiss 导入失败")

    # ---------------- 3. 端口监听 ----------------
    if not args.quiet:
        print("\n[3] 端口监听 (timeout 2s，仅探测不启动):")
    for port in PORT_TARGETS:
        ok = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            r = s.connect_ex(("127.0.0.1", port))
            s.close()
            ok = (r == 0)
        except OSError:
            ok = False
        if not args.quiet:
            mark = "✓" if ok else "⚠"
            print(f"    {mark} tcp/127.0.0.1:{port} {'监听中' if ok else '未监听'}")
        # 无端口是设计预期（服务可能未启动），不算错误，记为提示
        if not ok:
            warn_msgs.append(f"[3] 端口 {port} 未监听")

    # ---------------- 4. 关键进程存活 ----------------
    if not args.quiet:
        print("\n[4] 关键进程存活（按命令行匹配 python+trinity，只读）:")
    proc_rows = _scan_processes()
    for label in ("api", "mcp", "collector", "engine_worker"):
        hit = False
        for row in proc_rows:
            if label in row["text"]:
                hit = True
                break
        if not args.quiet:
            mark = "✓" if hit else "⚠"
            print(f"    {mark} trinity {label}{' 存活' if hit else ' 未发现'}")
        if not hit:
            warn_msgs.append(f"[4] trinity {label} 进程未发现")

    # ---------------- 5. 运行时库 ----------------
    if not args.quiet:
        print("\n[5] 运行时库 (只 stat，不开库连接):")
    db_missing = not os.path.exists(STORE_DB)
    wal_missing = not os.path.exists(STORE_DB + "-wal")
    if db_missing:
        err_msgs.append("[5] 大库 trinity_store.db 缺失")
    if not args.quiet:
        _report_stat(STORE_DB, "trinity_store.db", required=True)
    if not args.quiet:
        _report_stat(STORE_DB + "-wal", "trinity_store.db-wal", required=False)

    # ---------------- 6. 凭证文件 ----------------
    if not args.quiet:
        print("\n[6] 凭证文件 (~/.dsh/.credentials.yaml):")
    if not os.path.exists(CRED_FILE):
        err_msgs.append("[6] 凭证文件缺失")
        if not args.quiet:
            print("    ✗ 凭证文件不存在")
    else:
        keys = read_yaml_keys(CRED_FILE)
        if not args.quiet:
            print(f"    ✓ 存在；键名: {', '.join(keys) if keys else '(无/不可解析)'}")
        pg_keys = [k for k in (keys or []) if k.startswith("TRINITY_PG_")]
        api_key = any((keys or []) and k == "TRINITY_API_KEY" for k in keys or [])
        if keys is None:
            warn_msgs.append("[6] 凭证存在但键名解析失败（不读值）")
        else:
            if not pg_keys:
                warn_msgs.append("[6] 凭证缺少 TRINITY_PG_* 键名")
            if not api_key:
                warn_msgs.append("[6] 凭证缺少 TRINITY_API_KEY 键名（可选，提示）")

    # ---------------- 7. 维护日志 ----------------
    if not args.quiet:
        print(f"\n[7] 维护日志 (~/.trinity/logs/dsh-maintenance.log) 最近 200 行关键行:")
    hits = _scan_log(MAINT_LOG)
    if not args.quiet:
        if not hits:
            print("    无 FAILED/WARN/ERROR 关键行")
        else:
            for line, ts in hits:
                print(f"    {ts}  {line}")
    if hits:
        warn_msgs.append(f"[7] 维护日志含 {len(hits)} 条 FAILED/WARN/ERROR 关键行")

    # ---------------- 8. 磁盘剩余空间 ----------------
    if not args.quiet:
        print("\n[8] C 盘剩余空间:")
    free, total_l = _disk_space("C:\\")
    if free is None:
        warn_msgs.append("[8] C 盘剩余空间查询失败")
    else:
        pct = 100.0 * free / total_l if total_l else 0.0
        if not args.quiet:
            print(f"    可用 {fmt_size(free)} / 总 {fmt_size(total_l)} ({pct:.1f}% 可用)")
        if pct < 10.0:
            err_msgs.append(f"[8] C 盘剩余空间不足 {pct:.1f}%")

    # ---------------- 汇总 ----------------
    if err_msgs:
        exit_code = 2
    elif warn_msgs:
        exit_code = 1

    if args.quiet:
        print(f"退出码: {exit_code}")
        for m in err_msgs:
            print(f"  ERR: {m}")
        for m in warn_msgs:
            print(f"  WARN: {m}")
        if not err_msgs and not warn_msgs:
            print("  全部正常")
    else:
        print("\n" + "=" * 60)
        print("汇总")
        print("=" * 60)
        if err_msgs:
            print("  错误 (✗):")
            for m in err_msgs:
                print(f"    ✗ {m}")
        if warn_msgs:
            print("  警告 (⚠):")
            for m in warn_msgs:
                print(f"    ⚠ {m}")
        if not err_msgs and not warn_msgs:
            print("  全部正常 ✓")
        print(f"退出码: {exit_code}")
    return exit_code


def _scan_processes():
    rows = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' } | "
             "Select-Object ProcessId,CommandLine | Format-List"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        pid = None
        for raw in out.splitlines():
            line = raw.strip()
            if line.lower().startswith("processid"):
                pid = line.split(":", 1)[1].strip()
            elif line.lower().startswith("commandline"):
                cl = line.split(":", 1)[1].strip().lower()
                if "trinity" in cl:
                    rows.append({"pid": pid, "text": cl})
    except (subprocess.SubprocessError, OSError):
        pass
    return rows


def _report_stat(path, label, required):
    try:
        st = os.stat(path)
        print(f"    {'✓' if not required or st.st_size > 0 else '✗'} {label}: 存在, {fmt_size(st.st_size)}"
              + (" (0 字节)" if st.st_size == 0 else ""))
    except FileNotFoundError:
        print(f"    ⚠ {label}: 不存在")
    except OSError:
        print(f"    ⚠ {label}: stat 失败")


def _scan_log(path):
    hits = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-200:]
    except (OSError, UnicodeDecodeError):
        return hits
    keywords = ("FAILED", "WARN", "ERROR")
    for line in lines:
        if any(k in line.upper() for k in keywords):
            ts = _extract_ts(line)
            hits.append((line.strip(), ts))
            if len(hits) >= 10:
                break
    return hits


def _extract_ts(line):
    import re
    m = re.search(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", line)
    if m:
        return m.group(0).replace("T", " ")
    return "(no ts)"


def _disk_space(drive):
    try:
        usage = shutil.disk_usage(drive)
        return usage.free, usage.total
    except OSError:
        return None, None


if __name__ == "__main__":
    sys.exit(main())
