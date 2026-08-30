#!/usr/bin/env python3
"""with_lease — 治理任务租约命令行封装（Codex 借鉴落地 P0-1，2026-08-21）。

用法：
    python scripts/with_lease.py --job decay -- <cmd...>
    python scripts/with_lease.py --job mirror --key global --lease 3600 -- python x.py
    python scripts/with_lease.py --list

语义：
- 认领成功 → 以子进程执行 -- 之后的命令（stdio 继承），完成后按退出码
  release（0 → completed，非 0 → failed），本进程退出码 = 子进程退出码。
- 认领失败（SKIP / 锁占用 / 异常）→ 打印 SKIP 原因，exit 0
  （SKIP 不是失败：并发重复任务应直接跳过，绝不在写锁上排队）。
- --list 打印当前全部租约行（诊断用）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

# 与 engine_worker 一致：禁用 import 期聚合器自举（横幅噪音 + GIL 饥饿），
# 租约模块只需引擎库与 SQLite 连接
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trinity.governance.job_lease import (  # noqa: E402
    DEFAULT_DB,
    acquire,
    list_jobs,
    release,
)


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="job lease wrapper for trinity maintenance tasks")
    p.add_argument("--job", default="", help="job_kind（如 decay / mirror / compact）")
    p.add_argument("--key", default="global", help="job_key（默认 global）")
    p.add_argument("--lease", type=int, default=3600, help="租约秒数（默认 3600）")
    p.add_argument("--db", default=DEFAULT_DB, help="租约库路径（默认运行时权威库）")
    p.add_argument("--list", action="store_true", help="列出当前租约并退出")
    p.add_argument("--owner", default="", help="自定义 owner（默认 hostname:pid:rand）")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="-- 之后为要执行的任务命令")
    return p.parse_args()


def main() -> int:
    args = _parse()
    if args.list:
        for row in list_jobs(args.db):
            print(
                f"{row['job_kind']}/{row['job_key']} status={row['status']} "
                f"owner={row['owner']} expires={row['lease_expires_at']:.0f} "
                f"detail={row['detail'][:80]}"
            )
        return 0

    if not args.job:
        print("with_lease: --job is required (or --list)", file=sys.stderr)
        return 2

    if not args.cmd or args.cmd[0] != "--":
        print("with_lease: expected '-- <command>' after options", file=sys.stderr)
        return 2
    cmd = args.cmd[1:]
    if not cmd:
        print("with_lease: empty command", file=sys.stderr)
        return 2

    print(f"with_lease: db_path={args.db!r} env={os.environ.get(chr(84)+chr(82)+chr(73)+chr(78)+chr(73)+chr(84)+chr(89)+chr(95)+chr(83)+chr(84)+chr(79)+chr(82)+chr(69), chr(45))}", file=sys.stderr)
    lease = acquire(
        args.job,
        job_key=args.key,
        lease_seconds=args.lease,
        owner=args.owner or None,
        db_path=args.db,
    )
    if not lease["acquired"]:
        reason = lease.get("reason", "skipped")
        held_by = lease.get("held_by") or ""
        held_until = lease.get("held_until") or 0
        print(
            f"with_lease: SKIP {args.job}/{args.key} (reason={reason}"
            + (f", held_by={held_by}" if held_by else "")
            + (f", held_until={held_until:.0f}" if held_until else "")
            + (f", detail={lease.get(chr(100)+chr(101)+chr(116)+chr(97)+chr(105)+chr(108))}" if lease.get(chr(100)+chr(101)+chr(116)+chr(97)+chr(105)+chr(108)) else "")
            + ")"
        )
        return 0

    print(f"with_lease: claimed {args.job}/{args.key} (owner={lease['owner']})")
    code = 0
    status = "completed"
    detail = ""
    try:
        proc = subprocess.run(cmd, cwd=os.getcwd())
        code = proc.returncode
        if code != 0:
            status = "failed"
            detail = f"exit={code}"
    except FileNotFoundError as exc:
        print(f"with_lease: command not found: {cmd[0]}", file=sys.stderr)
        status = "failed"
        detail = f"command not found: {cmd[0]}"
        code = 127
    except KeyboardInterrupt:
        status = "failed"
        detail = "interrupted"
        code = 130
    finally:
        release(args.job, job_key=args.key, status=status, detail=detail, db_path=args.db)
        print(f"with_lease: released {args.job}/{args.key} status={status}")
    return code


if __name__ == "__main__":
    sys.exit(main())
