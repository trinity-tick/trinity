#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gate_set.py —— 标准闸门集 runner（P0-2）。

## 为什么需要本文件（问题实测）

本仓长期使用一个"标准闸门集"：`fake_green_audit` / `retrieval_wiring_audit` /
`rationale_audit` / `memory_utilization_audit --ratchet`（EXECUTION §785–§787 记为
「三把闸门 + 利用率闸门 4/4 exit=0」）。**它只是口头约定，不是可执行产物。**

后果（2026-09-17 全面评价实测）：

    scripts/structure_gate.py 在 HEAD = 26 passed / 1 failed
      [FAIL] budget:trinity/core/client/_hybrid_search.py  1427 / 1400
    §781（09-16 19:3x）曾把它按预算政策修绿（27/0），此后回涨 38 行；
    因为 structure_gate **不在那个口头集合里**，回涨没有任何一节登记。

这与本仓自己写下的前科完全一致：**恒红的闸门会被忽略**。
根因不是"有人偷懒"，而是**闸门集没有棘轮** —— 集合可以静默缩小，而缩小不留痕。

## 修法

把集合变成机器可读、可执行、**只增不减**的产物：

    docs/GATE_SET.json   清单（id / cmd / required / why）；`must_include` = 冻结集
    scripts/gate_set.py  按清单逐条跑；**少跑一个冻结 id 即 FAIL**（不是"少跑=过"）

三条纪律写在代码里：

1. **fail-closed**：清单不可读 / 脚本文件不存在 ⇒ 判失败。绝不把"没测"读成"过了"
   （§779 的 `doc_retrieval_eval` 缺臂返回 2 是同一纪律）。
2. **没跑过的闸门不得进 `passed`**：`state` 必须可分辨（passed / failed /
   missing_script / error），`passed` 只列真跑过且退 0 的。
3. **冻结集只增不减**：往 `must_include` 里加 id 是本工具鼓励的；**删**会立刻判 FAIL。

用法：

    python scripts/gate_set.py                 # 跑全部
    python scripts/gate_set.py --list          # 只列集合（"标准闸门集到底是哪些"）
    python scripts/gate_set.py --only structure --only scores
    python scripts/gate_set.py --json-out dsh-ops/evidence/gate_set.json

退出码：0=全过；1=有 required 闸门失败/缺失；2=清单本身不可用（fail-closed）。
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "docs", "GATE_SET.json")
#: 仓内**规范解释器**（服务与维护链用的那个）。本 runner 用 `[sys.executable] + cmd` 跑闸门 ⇒
#: 解释器选错会把"依赖缺失"读成"判据不通过"（2026-09-21 实测两次假红）。
#: 可用环境变量 TRINITY_SYS_PY 覆盖（换机/换版本时不必改代码）。
CANONICAL_PY = os.environ.get("TRINITY_SYS_PY") or\
    r"C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"


# ── 清单 ────────────────────────────────────────────────────────────────────


def load_manifest(path: str) -> dict:
    """读清单；任何异常都抛出（调用方一律 fail-closed）。"""
    with io.open(path, encoding="utf-8") as fh:
        m = json.load(fh)
    if not isinstance(m.get("gates"), list) or not m["gates"]:
        raise ValueError("gates 缺失或为空：%s" % path)
    return m


# ── 执行 ────────────────────────────────────────────────────────────────────


def _child_env() -> dict:
    """闸门子进程的环境：**强制 UTF-8**（2026-09-20 实测假红）。

    现象（本文件 2026-09-20 实跑）：不带 PYTHONIOENCODING 直接跑本 runner，
    pg_encrypt_preflight 与 doc_truth 双双 rc=1，tail 是
    UnicodeEncodeError: 'gbk' codec can't encode character ... ——**子进程往管道
    打中文/数学符号时按 cp936 编码崩掉**，而判据本身是好的。维护链里没暴露，
    只因为 trinity-dsh-maintenance.ps1:220 设了 PYTHONIOENCODING=utf-8；
    **任何绕过维护链的调用都会读到假红**（实测 14/16，两条 required 假失败）。

    修法：本 runner 给每个闸门子进程显式注入 UTF-8（父进程 encoding="utf-8"
    只管「读」，子进程「写」用的是它自己的 locale）。回滚开关：
    TRINITY_GATE_NO_FORCE_UTF8=1 ⇒ 恢复旧行为（反向测试用）。
    """
    env = os.environ.copy()
    if str(env.get("TRINITY_GATE_NO_FORCE_UTF8", "")).strip().lower() in ("1", "on", "true", "yes"):
        return env
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


_JUDGE_RE = re.compile(r"\[FAIL\]|\[STALE\]|\[MISS\]|Traceback|没出现|失效|不一致|超出|超限|拒绝|FAIL")


def keep_lines(text, last=6, judge_max=8, cap=1800):
    """保留**承载判定的行** + 末尾若干行（纯函数，可单测）。

    ## 为什么（2026-09-23 §1284 实测：一次红**无法归因**）

    原实现是 `lines[-6:][:900]` —— 只留最后 6 行。而闸门的判定行常常**不在末尾**：
    实测 `doc_truth` 红时，末尾三行是 `[SKIP]…` / `doc_truth: 逐行核对 …` / `[采样时刻]`，
    真正的 `[FAIL] <行> 文档写的签名没出现：…` 在第 4 行起 —— **被截掉了**，
    于是 running 输出里只剩「看起来没事」的汇总行，**归因方向被结构性带偏**。
    （§16 第 5 条「看日志要看完整行」的同一族错误，只是发生在**工具内部**。）

    现在：判定行优先保留（最多 `judge_max` 行），再补末尾 `last` 行，整体按 `cap` 截断。
    """
    lines = [_ln for _ln in (text or "").splitlines() if _ln.strip()]
    key = [_ln for _ln in lines if _JUDGE_RE.search(_ln)][:judge_max]
    _n_key = len(key)
    tail = lines[-last:]
    # 再补末尾行时，判定行**只补到 judge_max 为止**（否则「上限」名存实亡 —— §1284 自测抓到）
    out = list(key)
    for _ln in tail:
        if _ln in out:
            continue
        if _JUDGE_RE.search(_ln) and _n_key >= judge_max:
            continue
        out.append(_ln)
    return "\n".join(out)[:cap]


def _default_runner(cmd, cwd, timeout):
    """跑一条闸门命令，返回 (returncode, tail)。

    返回 127 表示"命令根本起不来"（与闸门自己退非零**区分开**：前者是基础设施问题，
    后者是判据不通过；混成一个数会让排查方向错）。
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, keep_lines(proc.stdout)
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT after %ss" % timeout
    except Exception as exc:  # noqa: BLE001 —— 起不来也算失败，不静默
        return 127, "runner exception: %s" % exc


def evaluate(manifest_path: str, root: str, runner=None, timeout: int = 1800,
             only=None) -> dict:
    """按清单逐条求值。**纯函数式**：`runner` 可注入，便于反向测试。

    返回字典的键是**契约**（用例压着它们，不许改名）：
      ok / error / gates / passed / skipped / required_failed / missing_frozen / ts
    """
    runner = runner or _default_runner
    out = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "manifest": manifest_path,
        "gates": [],
        "passed": [],
        "skipped": [],
        "required_failed": [],
        "missing_frozen": [],
        "error": None,
        "ok": False,
    }
    try:
        m = load_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001 —— fail-closed
        out["error"] = "manifest unreadable: %s" % exc
        return out

    gates = m["gates"]
    if only:
        # §1284：**未知 id 必须 fail-closed**。原实现直接按 id 过滤 ⇒ 写错一个 id 会得到
        # 「一条都没跑，passed=0，-> PASS」（实测：`--only no_such_gate` 返回 0）
        # —— 这正是 GATE_SET 策略①禁止的「把没测读成过了」，而且**错得没有声音**。
        _known = {g["id"] for g in gates}
        _unknown = [x for x in only if x not in _known]
        if _unknown:
            out["error"] = ("--only 里有清单里不存在的 id：%s（可用 id 见 --list；"
                            "拒绝在「一条都没跑」的情况下报 PASS）" % _unknown)
            return out
        gates = [g for g in gates if g["id"] in set(only)]

    # ① 冻结集：只增不减
    listed = {g["id"] for g in m["gates"]}
    out["missing_frozen"] = [gid for gid in m.get("must_include", []) if gid not in listed]

    # ② 逐条跑
    for g in gates:
        rel = g["cmd"][0]
        rec = {
            "id": g["id"],
            "cmd": " ".join(g["cmd"]),
            "required": bool(g.get("required")),
            "why": g.get("why", ""),
            "state": "pending",
            "ok": False,
            "rc": None,
            "tail": "",
        }
        if not os.path.exists(os.path.join(root, rel)):
            rec["state"] = "missing_script"
            rec["tail"] = "脚本不存在：%s" % rel
        else:
            t0 = datetime.datetime.now()
            rc, tail = runner([sys.executable] + g["cmd"], root, timeout)
            rec["rc"] = rc
            rec["tail"] = tail
            rec["seconds"] = round((datetime.datetime.now() - t0).total_seconds(), 1)
            rec["state"] = "passed" if rc == 0 else "failed"
            rec["ok"] = rc == 0
        out["gates"].append(rec)
        if rec["ok"]:
            out["passed"].append(rec["id"])
        elif rec["required"]:
            out["required_failed"].append(rec["id"])
        else:
            out["skipped"].append(rec["id"])

    out["ok"] = not out["required_failed"] and not out["missing_frozen"]
    return out


# ── 展示 ────────────────────────────────────────────────────────────────────


def cmd_list(manifest_path: str) -> int:
    m = load_manifest(manifest_path)
    print("标准闸门集（docs/GATE_SET.json）—— 冻结集只增不减")
    print("-" * 78)
    for g in m["gates"]:
        frozen = "冻结" if g["id"] in m.get("must_include", []) else "可选"
        req = "required" if g.get("required") else "optional"
        print("  %-18s %-8s %-9s %s" % (g["id"], frozen, req, " ".join(g["cmd"])))
        if g.get("why"):
            print("      └ %s" % g["why"])
    print("-" * 78)
    print("must_include（少了任一个即 FAIL）：%s" % ", ".join(m.get("must_include", [])))
    return 0


def _trinity_importable(root: str) -> bool:
    """用**子进程** `find_spec('trinity')` 探一次（不 import 模块本身 ⇒ 无副作用、秒级）。

    为什么要探：本 runner 用 `[sys.executable] + g["cmd"]` 跑每一条闸门（见 evaluate），
    所以**每条闸门用的解释器就是跑本文件的解释器**。解释器选错时，闸门会给出**假红**
    （2026-09-21 实测：本会话 shell 的 3.11 上 `structure_gate` 报 24p/1f（缺 strawberry），
    换规范 3.14 是 **28p/0f**；`plaintext_ratio` 报 `No module named 'trinity'`，
    把**真原因**（6h 速率超限）整个盖住了）。
    """
    try:
        # 探法必须与**闸门的实际调用形态**一致：闸门是 `python scripts/x.py`（sys.path[0]=脚本目录，
        # **不含 cwd**）。第一版探针用 `python -c`（sys.path[0]='' = cwd）⇒ 在 cwd=仓根时**恒为真**，
        # 于是 3.11 上那条"trinity 不可导入"的警告被漏报（真实情形：那条闸门就是因此报假红）。
        r = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util as u,sys,os;"
             "sys.path[:] = [p for p in sys.path if p not in ('', '.', os.getcwd())];"
             "sys.exit(0 if u.find_spec('trinity') else 3)"],
            cwd=root, env=_child_env(), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        return r.returncode == 0
    except Exception:  # noqa: BLE001 —— 探不了就当作"不可用"，宁可多报一句
        return False


def interpreter_warnings(exe: str, trinity_ok: bool, canonical: str = None,
                         canonical_exists: bool = True) -> list:
    """纯判据（可单测）：本 runner 的解释器是否与仓内规范一致；不一致会把闸门读数变成假红。

    返回**警告列表**（空 = 没问题）。注意它**不改 rc**：正常跑仍按闸门结果判 ——
    但对"解释器不是规范那个"这件事必须在跑之前说出来，否则红的原因会被误读（本轮实测两次）。
    """
    canonical = canonical or CANONICAL_PY
    w = []
    try:
        same = os.path.normcase(os.path.abspath(exe)) == os.path.normcase(os.path.abspath(canonical))
    except Exception:  # noqa: BLE001
        same = False
    if canonical_exists and not same:
        w.append("解释器不是仓内规范解释器（%s；本次是 %s）⇒ 闸门可能读到**假红**"
                 "（实测：structure_gate 在 3.11 因缺 strawberry 报 24p/1f，3.14 上是 28p/0f）" % (canonical, exe))
    if not trinity_ok:
        w.append("该解释器 `find_spec('trinity')` 为假 ⇒ `import trinity` 会失败"
                 "（实测：plaintext_ratio 报 No module named 'trinity'，把真原因（速率超限）盖住）")
    return w


def interpreter_note(warns: list, exe: str = None, canonical: str = None) -> str:
    """把「解释器不是规范那个」变成**可照做的下一步**（返回空串 = 这一行不用打）。纯函数，不改 rc。

    为什么要它（2026-09-23 §1296 实测）：警告打在**输出最前面**，判定行在**最后** —— 而读尾段的人
    （我，`Select-Object -Last N`）看到 `-> FAIL` 时，手上只有一句「请改用规范解释器复跑」，
    **没有那个解释器的字面路径**，于是四条假红被当成真红读了一整轮（structure 缺 strawberry /
    plaintext_ratio `No module named 'trinity'` / doc_truth 连带 / ann_persist_keep 行为面）。
    修法不是"再警告一次"，而是**把规范解释器与可粘贴的命令写进尾行** —— 判据见
    `tests/unit/test_gate_set_interpreter_note.py`（合规时**不得**打这一行；不合规时必须含路径与命令）。
    """
    if not warns:
        return ""
    exe = exe or sys.executable
    canonical = canonical or CANONICAL_PY
    return ("注：上面 %d 条解释器警告成立时，'FAIL' 可能是假红 —— 先**换解释器复跑**再看判据：\n"
            "    本次用的解释器：%s\n"
            "    仓内规范解释器：%s\n"
            "    复跑命令：& \"%s\" scripts\\gate_set.py"
            % (len(warns), exe, canonical, canonical))


def main() -> int:
    ap = argparse.ArgumentParser(description="标准闸门集 runner（P0-2）")
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--list", action="store_true", help="只列出集合，不执行")
    ap.add_argument("--only", action="append", default=None, help="只跑指定 id（可重复）")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--strict-interpreter", action="store_true",
                    help="解释器不是规范那个时直接判失败（默认只警告，不改判）")
    args = ap.parse_args()

    if args.list:
        return cmd_list(args.manifest)

    _warns = interpreter_warnings(sys.executable, _trinity_importable(ROOT),
                                 canonical_exists=os.path.exists(CANONICAL_PY))
    for _w in _warns:
        print("  [WARN] 解释器：%s" % _w)

    res = evaluate(args.manifest, ROOT, timeout=args.timeout, only=args.only)
    res["interpreter"] = {"exe": sys.executable, "warnings": _warns}

    print("=" * 78)
    print("标准闸门集 —— %s" % res["ts"])
    print("=" * 78)
    for rec in res["gates"]:
        mark = {"passed": "[PASS]", "failed": "[FAIL]",
                "missing_script": "[MISS]", "pending": "[????]"}.get(rec["state"], "[????]")
        extra = "" if rec["rc"] is None else " rc=%s %ss" % (rec["rc"], rec.get("seconds", "?"))
        print("  %s %-18s %s%s" % (mark, rec["id"], rec["cmd"], extra))
        if not rec["ok"] and rec["tail"]:
            # §1284：`tail` 已经被 `keep_lines()` 挑成「判定行 + 末尾」，这里**全量打出来** ——
            # 再截一次（原先是 `[-3:]`）等于把刚保住的原因又丢掉。
            for line in rec["tail"].splitlines():
                print("        | %s" % line)
    if res["missing_frozen"]:
        print("  [FAIL] 冻结集缺项（标准闸门集被静默缩小）：%s" % res["missing_frozen"])
    if res["error"]:
        print("  [FAIL] %s" % res["error"])
    print("-" * 78)
    print("passed=%d  required_failed=%s  skipped=%s  -> %s" % (
        len(res["passed"]), res["required_failed"], res["skipped"],
        "PASS" if res["ok"] else "FAIL"))
    if _warns:
        print(interpreter_note(_warns, sys.executable, CANONICAL_PY))

    if args.json_out:
        d = os.path.dirname(os.path.abspath(args.json_out))
        if d:
            os.makedirs(d, exist_ok=True)
        with io.open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        print("证据 -> %s" % args.json_out)
    elif not res["ok"]:
        # §1284：**失败必须留证**。原实现只在显式给 `--json-out` 时落盘 ⇒ `--only` 红了一次
        # 之后**没有任何证据可查**（本轮实测：一次 `--only doc_truth` 红，事后无法归因）。
        _auto = os.path.join(ROOT, "output", "gates_fail_%s.json"
                             % res["ts"].replace("-", "").replace(":", "").replace(" ", "_"))
        try:
            os.makedirs(os.path.dirname(_auto), exist_ok=True)
            with io.open(_auto, "w", encoding="utf-8") as fh:
                json.dump(res, fh, ensure_ascii=False, indent=1)
            print("证据（失败自动落盘）-> %s" % _auto)
        except Exception as _e:  # noqa: BLE001 —— 落盘失败不改变判定，但必须说出来
            print("  [WARN] 失败证据落盘失败（不影响判定）：%s" % str(_e)[:120])
    else:
        # §1287：**通过也必须留证**（§1284 只补了失败那一半）。原实现下一次「全绿」只活在滚动输出里：
        # 本轮实测 —— 21/21 跑完，`output/` 里最新的还是 **11:44** 的旧 `gates_…_full.json`，
        # 引用时**差点把上一轮的旧读数当成本轮读数**（§16 第 7 条同族：判据的产物必须是本次那份）。
        # 沿用本仓既有的证据命名 `<日期>_full.json`，让「最新的一份」与「最后一次跑」一致。
        # §1296：`--only` 跑的是**子集**，不许占用 `_full` 这个名字 —— 否则「最新的一份」
        # 会是一条闸门的证据，而下一个引用它的人以为那是全量（同族误读：把子集当全集读）。
        # 实测触发点：本轮我想用 `--only rationale` 演示解释器尾行，那样会把当天的全量证据
        # `gates_20260923_full.json` 直接覆盖成 1 条闸门的结果 —— 而文档正引用着那份。
        _kind = "partial" if args.only else "full"
        _ok_out = os.path.join(ROOT, "output",
                               "gates_%s_%s.json" % (res["ts"][:10].replace("-", ""), _kind))
        if args.only:
            res["subset"] = sorted(args.only)
        try:
            os.makedirs(os.path.dirname(_ok_out), exist_ok=True)
            with io.open(_ok_out, "w", encoding="utf-8") as fh:
                json.dump(res, fh, ensure_ascii=False, indent=1)
            print("证据（通过自动落盘）-> %s" % _ok_out)
        except Exception as _e:  # noqa: BLE001 —— 同上：落盘失败不改变判定，但必须说出来
            print("  [WARN] 通过证据落盘失败（不影响判定）：%s" % str(_e)[:120])

    if res["error"]:
        return 2
    if args.strict_interpreter and _warns:
        return 3          # 3 = 解释器不合规（与"闸门红=1"分开，避免混读）
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
