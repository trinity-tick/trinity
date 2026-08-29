# -*- coding: utf-8 -*-
"""switch_storage.py — PG/SQLite 主存储一键切换（2026-08-29）。

持久化 TRINITY_STORAGE_BACKEND 到 supervisor 凭证文件（~/.dsh/.credentials.yaml）
——重启 supervisor/API 后生效；回滚=切回 sqlite。

用法:
  python scripts/switch_storage.py status          # 当前存储
  python scripts/switch_storage.py to postgresql   # 切 PG（提示重启）
  python scripts/switch_storage.py to sqlite       # 回 SQLite（默认）
"""
import os
import sys
import re
import argparse

_CRED = os.path.expanduser("~/.dsh/.credentials.yaml")


def _read_cred() -> str:
    if os.path.exists(_CRED):
        with open(_CRED, "r", encoding="utf-8-sig") as f:
            return f.read()
    return ""


def _write_cred(text: str) -> None:
    with open(_CRED, "w", encoding="utf-8-sig") as f:
        f.write(text)


def _current() -> str:
    m = re.search(r"^TRINITY_STORAGE_BACKEND\s*[:=]\s*([\w]+)", _read_cred(), re.M)
    return m.group(1) if m else "sqlite (default)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["status", "to"])
    ap.add_argument("target", nargs="?", choices=["postgresql", "sqlite"])
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.action == "status":
        print("storage:", _current())
        return 0

    text = _read_cred()
    line = "TRINITY_STORAGE_BACKEND: " + args.target
    if re.search(r"^TRINITY_STORAGE_BACKEND.*$", text, re.M):
        text = re.sub(r"^TRINITY_STORAGE_BACKEND.*$", line, text, flags=re.M)
    else:
        text = text.rstrip() + chr(10) + line + chr(10)
    _write_cred(text)
    print("switched to " + args.target + " (persisted to " + _CRED + ")")
    print("NOTE: restart supervisor/API to apply (env picked at startup)")
    print("      rollback: python scripts/switch_storage.py to sqlite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
