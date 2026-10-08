#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""critical_paths_gate.py — 关键路径必须存在且可打开（EXECUTION 2026-10-02）。

## 为什么要有这道门（本次事故的机械成因）

2026-10-02 下午，Trinity 的 HTTP 记忆面**完全不可用**，而**整条链路没有一处报警**：

  1. SQLCipher 加密迁移把原权威库换掉后失败，留下 1 GiB 损坏文件
     （头 16 字节全 0、`btreeInitPage() returns error code 11`）。
  2. 灾难恢复从备份恢复到 `.trinity\\store-restored`，但**只改了 3 个消费方里的 1 个**
     （`trinity-autostart.ps1:35`），`trinity-supervisor.ps1:166` 与
     `trinity-dsh-maintenance.ps1:288` 的兜底值仍指旧库。
  3. **真正的根因**：`~/.dsh/.credentials.yaml` 把路径写成 `'...\\\\store'`（字面双反斜杠），
     而 `dsh-ops/dsh-credentials.ps1::Get-DshCredential` 的单引号分支**原样返回引号内字面量**
     ⇒ 注入进程环境的路径**指向一个不存在的目录**。

  后果不是报错，而是**静默回退**：
     · `_find_trinity_store()` 回退到 `~/.trinity/store`（= 坏库）
     · `POST /memory/search/hybrid` 返回 **HTTP 200 + `results: []`**（fail-open）
     · 插件 `trinity_structure_stats` 抛 `database disk image is malformed`

  ⇒ **"路径不存在"这件事，在整条链路里没有任何判据。** 本门补的就是它。

## 判据

对每个关键路径，逐条判定（任一硬违规 ⇒ rc=1）：

  1. **存在**：`TRINITY_STORE` 必须存在、是目录，且含 `trinity_store.db`；
     `TRINITY_SQLCIPHER_KEY_FILE` **若已配置**则必须存在、可读、非空。
  2. **不是明显损坏的库**：库文件头 16 字节不得全 0，且若以 `SQLite format 3` 开头则必须真的是 SQLite。
  3. **可打开**：`file:...?mode=ro` + `PRAGMA quick_check(1)` 必须返回 `ok`。
  4. **没有"应该存在但不存在"的旧值**：若 `TRINITY_STORE` 的**父目录**里存在一个
     `*-restored` / `*.CORRUPT-archive` 兄弟目录，而当前 `TRINITY_STORE` 指向的是
     **未标注恢复**的老名字，则判**软违规**并打印醒目警告（本次事故正是这个形状）。

## fail-closed

`TRINITY_STORE` 未设置 ⇒ **FAIL**（不允许"没配"读成"过了"）。这条是刻意的：
本仓历史上"未设置 ⇒ 静默回退到 cwd 小库"（已知坑 #9）造成过误写。

## 环境

本门**只读**，不创建、不修复任何文件。可在 CI 跑（无服务依赖），也可在服务机上跑。
用 `TRINITY_CRITICAL_PATHS_ALLOW_MISSING`（逗号分隔）显式豁免 —— 例如 CI 里没有
生产存储，就显式写明"本环境不判存储路径"，**不许静默**。

用法：
  python scripts/critical_paths_gate.py                 # 人类可读
  python scripts/critical_paths_gate.py --json          # JSON
  python scripts/critical_paths_gate.py --ratchet       # 棘轮（闸门用）
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, "docs", "CRITICAL_PATHS_BASELINE.json")
DB_NAME = "trinity_store.db"


def _expand(p: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(p)))


def _db_header(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(16)
    except OSError:
        return b""


def _probe_store(store: str) -> dict:
    """判定一个 store 目录。返回 {verdict, reasons[], detail{}}。"""
    out: dict = {"path": store, "exists": os.path.isdir(store),
                 "reasons": [], "detail": {}}
    if not out["exists"]:
        out["reasons"].append("STORE_DIR_MISSING")
        return out

    db = os.path.join(store, DB_NAME)
    out["detail"]["db"] = db
    if not os.path.isfile(db):
        out["reasons"].append("STORE_DB_MISSING")
        return out

    size = os.path.getsize(db)
    out["detail"]["db_size"] = size
    if size < 4096:
        out["reasons"].append("STORE_DB_TOO_SMALL(%d)" % size)

    head = _db_header(db)
    out["detail"]["header_hex"] = head.hex()
    if head == b"\x00" * 16:
        # 本次事故的确切形状：裸文件 + 全 0 头
        out["reasons"].append("STORE_DB_HEADER_ALL_ZERO")
    elif head[:15] != b"SQLite format 3":
        out["reasons"].append("STORE_DB_NOT_SQLITE(header=%r)" % head[:15])

    # 可打开 + quick_check（只读）
    try:
        uri = "file:%s?mode=ro" % db.replace("\\", "/")
        con = sqlite3.connect(uri, uri=True, timeout=8)
        try:
            row = con.execute("PRAGMA quick_check(1)").fetchone()
            qc = (row or ["<none>"])[0]
            out["detail"]["quick_check"] = str(qc)[:120]
            if qc != "ok":
                out["reasons"].append("STORE_DB_QUICK_CHECK_NOT_OK")
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 — 打不开本身就是判据
        out["detail"]["open_error"] = "%s: %s" % (type(e).__name__, str(e)[:100])
        out["reasons"].append("STORE_DB_OPEN_FAILED")
    return out


def _sibling_warning(store: str) -> list:
    """父目录里若存在 *-restored / *.CORRUPT-archive，而当前指向未标注恢复的老名字 ⇒ 软警告。

    本次事故的形状：好库在 `store-restored`，而配置仍指 `store`。
    """
    warn = []
    parent = os.path.dirname(store)
    name = os.path.basename(store)
    if not os.path.isdir(parent):
        return warn
    sibs = []
    for pat in ("*-restored", "*CORRUPT*", "*-BROKEN*", "*.pre-sqlcipher*"):
        sibs.extend(glob.glob(os.path.join(parent, pat)))
    sibs = [s for s in sibs if os.path.isdir(s) and os.path.abspath(s) != os.path.abspath(store)]
    if not sibs:
        return warn
    recovered_here = ("restored" in name.lower() or "broken" in name.lower())
    if not recovered_here:
        warn.append(
            "SIBLING_RECOVERY_DIR_EXISTS: 当前 TRINITY_STORE=%s 指向**未标注恢复**的名字，"
            "而同层存在恢复/损坏标记目录 %s —— 这形状与 2026-10-02 事故一致"
            "（好库在 restored 侧，配置忘了改）。请确认哪个是权威库。"
            % (name, ", ".join(sorted(os.path.basename(s) for s in sibs))))
    return warn


def scan(ci_safe: bool = False) -> dict:
    allow_missing = [x.strip() for x in
                     (os.environ.get("TRINITY_CRITICAL_PATHS_ALLOW_MISSING") or "").split(",")
                     if x.strip()]
    hard: list = []
    soft: list = []
    detail: dict = {}
    checked = 0

    # ── 1) TRINITY_STORE ────────────────────────────────────────────────
    store_env = os.environ.get("TRINITY_STORE", "").strip()
    if not store_env:
        if "TRINITY_STORE" in allow_missing:
            soft.append("TRINITY_STORE: 未设置，但被 TRINITY_CRITICAL_PATHS_ALLOW_MISSING 显式豁免")
            detail["trinity_store"] = {"skipped": "explicitly allowed missing"}
        elif ci_safe:
            # CI runner 上没有生产存储。--ci-safe 是**显式**声明本环境不判存储路径，
            # 而不是"没配就读成过了"（fail-closed 的对立面）。本地/服务机上不要加这个参数。
            soft.append("TRINITY_STORE: 未设置，--ci-safe 显式跳过（本环境声明无生产存储）")
            detail["trinity_store"] = {"skipped": "ci-safe"}
        else:
            hard.append(
                "TRINITY_STORE 未设置 —— fail-closed。未设置会让解析静默回退到 "
                "~/.trinity/store（历史上是 cwd 小库陷阱 #9 的来源）；"
                "若本环境确实没有生产存储，请显式设 TRINITY_CRITICAL_PATHS_ALLOW_MISSING=TRINITY_STORE "
                "或加 --ci-safe")
    else:
        store = _expand(store_env)
        checked += 1
        if os.path.isfile(store):          # 允许直接指到 db 文件
            store = os.path.dirname(store)
        r = _probe_store(store)
        detail["trinity_store"] = r
        hard.extend("TRINITY_STORE %s: %s" % (store, x) for x in r["reasons"])
        soft.extend(_sibling_warning(store))

    # ── 2) SQLCipher 密钥文件（仅当已配置）─────────────────────────────
    kf = os.environ.get("TRINITY_SQLCIPHER_KEY_FILE", "").strip()
    kenv = os.environ.get("TRINITY_SQLCIPHER_KEY", "").strip()
    cipher_off = (os.environ.get("TRINITY_SQLCIPHER", "").strip().lower()
                  in ("off", "0", "false", "no"))
    detail["sqlcipher"] = {"key_env_set": bool(kenv), "key_file": kf or None, "off": cipher_off}
    if kf and not cipher_off:
        checked += 1
        p = _expand(kf)
        detail["sqlcipher"]["resolved"] = p
        if not os.path.isfile(p):
            hard.append("TRINITY_SQLCIPHER_KEY_FILE %s: KEY_FILE_MISSING" % p)
        else:
            try:
                data = open(p, "rb").read()
                detail["sqlcipher"]["key_file_size"] = len(data)
                if not data.strip():
                    hard.append("TRINITY_SQLCIPHER_KEY_FILE %s: KEY_FILE_EMPTY" % p)
            except OSError as e:
                hard.append("TRINITY_SQLCIPHER_KEY_FILE %s: KEY_FILE_UNREADABLE(%s)" % (p, str(e)[:60]))
    elif kf and cipher_off:
        soft.append("TRINITY_SQLCIPHER_KEY_FILE 已配置但 TRINITY_SQLCIPHER=off ⇒ 本门不判（显式豁免）")

    # ── 3) 备份目录（若配置了）─────────────────────────────────────────
    for var in ("TRINITY_BACKUP_DIR", "TRINITY_BACKUP_PATH"):
        v = os.environ.get(var, "").strip()
        if v:
            checked += 1
            p = _expand(v)
            detail[var] = {"path": p, "exists": os.path.isdir(p)}
            if not os.path.isdir(p):
                hard.append("%s %s: BACKUP_DIR_MISSING" % (var, p))

    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "checked": checked,
            "hard": hard, "soft": soft, "detail": detail,
            "allow_missing": allow_missing, "ci_safe": ci_safe,
            "scope": "只判『配置指向的路径』；不判服务是否在跑、不判数据是否最新"}


def load_baseline():
    if not os.path.isfile(BASELINE):
        return None
    try:
        with io.open(BASELINE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ratchet", action="store_true")
    ap.add_argument("--ci-safe", action="store_true",
                    help="CI runner 无生产存储：把『TRINITY_STORE 未设置』显式降为软信息（不是静默通过）")
    ap.add_argument("--accept-baseline", action="store_true")
    ap.add_argument("--reason", default="")
    a = ap.parse_args(argv)

    out = scan(ci_safe=a.ci_safe)
    hard = out["hard"]
    soft = out["soft"]
    base = load_baseline()
    bset = set((base or {}).get("violations") or []) if base else set()
    if base is not None:
        out["baseline_count"] = len(bset)
        out["new"] = sorted(set(hard) - bset)
        out["fixed"] = sorted(bset - set(hard))

    # 报告模式也**必须**能失败：
    # 这道门的存在理由就是"路径坏了没人报"。若它只在 --ratchet 下才返回 1，
    # 那它在人工跑、CI 裸跑、以及任何忘了加 --ratchet 的地方都是**恒绿**的装饰。
    # （纪律：判据必须先证明自己有判别力 —— 本次事故的教训。）
    report_rc = 1 if hard else 0

    if a.accept_baseline:
        if not a.reason.strip():
            print("[FAIL] --accept-baseline 需要 --reason")
            return 1
        hist = list((base or {}).get("history") or [])
        hist.append({"ts": out["ts"], "from": len(bset), "to": len(hard),
                     "reason": a.reason.strip()})
        with io.open(BASELINE, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"updated": out["ts"],
                       "note": "关键路径硬违规（存在/可打开）。棘轮只降不升；新增必须修配置而不是抬基线。",
                       "violations": sorted(hard), "history": hist},
                      f, ensure_ascii=False, indent=1)
        print("[OK] baseline updated -> %d hard" % len(hard))
        return 0

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print("关键路径判据（存在 / 可打开）—— %s" % out["ts"])
        print("  探针数: %d   硬违规: %d   软警告: %d   基线: %s"
              % (out["checked"], len(hard), len(soft), out.get("baseline_count")))
        ts = out["detail"].get("trinity_store") or {}
        if ts:
            print("  TRINITY_STORE = %s" % ts.get("path"))
            if ts.get("detail"):
                d = ts["detail"]
                print("    db=%s size=%s quick_check=%s"
                      % (d.get("db"), d.get("db_size"), d.get("quick_check")))
                if d.get("open_error"):
                    print("    open_error=%s" % d["open_error"])
        for h in hard:
            print("  [FAIL] %s" % h)
        for s in soft:
            print("  [WARN] %s" % s)

    if not a.ratchet:
        # 报告模式也带真实退出码（见 report_rc 的说明）：有硬违规就 rc=1。
        return report_rc
    if base is None:
        print("[FAIL] 缺少基线 %s —— 先 --accept-baseline --reason" % BASELINE)
        return 1
    new = out.get("new") or []
    if new:
        print("[FAIL] 关键路径棘轮：新增 %d 条硬违规" % len(new))
        for n in new:
            print("         %s" % n)
        print("       修法：把配置指向真实存在的路径（改 ~/.dsh/.credentials.yaml / 启动脚本兜底值），"
              "而不是抬基线。")
        return 1
    print("[PASS] 关键路径棘轮：未新增硬违规（%d <= %d）"
          % (len(hard), out.get("baseline_count") or 0))
    return 0


if __name__ == "__main__":
    _rc = main()
    import datetime as _dt
    print("[采样时刻] %s（本读数只对该时刻的系统状态成立）"
          % _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    raise SystemExit(_rc)
