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
    # t31：① 键缩进在版本化文件的 `refs` 下 ⇒ 必须允许前导空白；
    # ② 值在**实际文件里是带引号的**（`'postgresql'`）⇒ 捕获组前必须容忍引号，否则 status 仍会谎报
    #    （只修锚是不够的 —— 这一条是 t31 的功能探针实测出来的第二层原因）。
    m = re.search(r"^\s*TRINITY_STORAGE_BACKEND\s*[:=]\s*['\"]?([\w]+)", _read_cred(), re.M)
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
    # t31（**写者**修复）：① 锚 + `\s*` ⇒ 能匹配 `refs` 下的缩进行；
    # ② 替换**保留缩进**（原写法即便匹配上也会吃掉 YAML 缩进 ⇒ 破坏 refs 嵌套）。
    if re.search(r"^\s*TRINITY_STORAGE_BACKEND.*$", text, re.M):
        text = re.sub(r"^(\s*)TRINITY_STORAGE_BACKEND.*$",
                      lambda mo: mo.group(1) + line, text, flags=re.M)
    else:
        text = text.rstrip() + chr(10) + line + chr(10)
    _write_cred(text)
    print("switched to " + args.target + " (persisted to " + _CRED + ")")
    print("NOTE: restart supervisor/API to apply (env picked at startup)")
    print("      rollback: python scripts/switch_storage.py to sqlite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
