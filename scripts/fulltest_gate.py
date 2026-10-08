# -*- coding: utf-8 -*-
"""fulltest_gate.py — 全量测试门禁（2026-08-28 阶段1, v2）。

关键：文件重定向 + cwd=trinity 根 + 继承 env（不最小化、不 capture_output）——
与手动跑完全一致的环境（capture_output/最小 env 会导致环境干扰误失败）。

用法:
    python scripts/fulltest_gate.py            # 跑全量 pytest + eval 12（**重型**，分钟级）
    python scripts/fulltest_gate.py --status   # 只读：上次证据在不在、新不新鲜、上次 verdict
    python scripts/fulltest_gate.py --selftest # 判据自身可失败（陈旧/失败/缺失三态 + 代码面两态 + 解释器四态）

## 为什么要落证据（2026-09-23 §1300）

`fulltest` 是**按需手动**跑的重型门禁（`dsh-ops/trinity-autostart.ps1:837` 原文：`fulltest（重型回归，按需手动跑）`）。
而在 §1300 之前它**一个字的证据都不落盘** —— `output/` 下没有任何 `*fulltest*`，日志里最近一次真实尝试是
**2026-08-28（且全是 SKIP）**。后果：**「全量 pytest N passed」这类载重声明在仓里没有产物可查**，
只能靠 EXECUTION 正文或 commit message 里的一句话。

这正是 §1284（失败必须留证）与 §1287（**通过也必须留证**）立的纪律 —— `gate_set.py` 当天补了，`fulltest` 被漏了。
本文件现在把每次运行写成 `output/fulltest_last.json` + `output/fulltest_<ts>.json`（含 pytest 摘要、eval 结论、
耗时、解释器、`git HEAD`）—— 最后一项是为了让证据**钉在某个版本上**（本仓长期「不发布」，HEAD 是唯一锚点）。

## `--status` 的四态（边界写清，别让状态把判据弄红）

    evidence 缺失              → `全量门禁证据: 尚无（第一次运行前，不判）`        rc=0
    新鲜(≤ STALE_DAYS) 且 PASS → `全量门禁证据: <ts>（N.N 天前）verdict=PASS …`   rc=0
    陈旧(> STALE_DAYS)         → `[STALE] …`                                     rc=1
    上次 verdict≠PASS          → `[FAIL] …`                                      rc=1
    上次 verdict=TIMEOUT       → `[FAIL] …跑超时了…（跑到 N%，其间已见 M 个 F）`  rc=1
    新鲜且 PASS，但**代码面**在证据之后变过 → `[COVERAGE-GAP] …`（§1319）        rc=1

来由同族：U1a 的「样本不足时**明说不判**」（§955）与 §1130 的「状态 vs 形状」——
**「还没跑过」不是失败，是还没有证据**；只有「有证据但过期/是红的」才该红。

## §1319：第三态曾经是**假绿** —— 「新鲜」只看墙钟，不看它覆盖的是哪份代码

来由（2026-09-24 实测）：`STALE_DAYS = 30` 意味着「11:29 跑的那次 PASS」在接下来的 30 天里
一直算「新鲜」，**哪怕其间 `trinity/` 的代码改了十几处**。当天就是这样：11:29 的 PASS 之后我改了
`memory_compressor.py` / `_hybrid_search.py` / `evidence_gate.py` / 多个判据文件，而检查单 **D4 仍然绿**
—— 它回答的是「最近跑过没有」，却被人读成「**当前这份代码**全量回归绿」。

修法（**清单比对**，不是墙钟）：
  ① 每次运行**开跑时**就把代码面（`trinity/` `tests/` `scripts/` 的 `.py` + `pytest.ini` 等）的
     `size/mtime_ns/sha256` 落成 `output/fulltest_tree_manifest[_<ts>].json`，路径写进证据记录；
     **开跑时**取而不是跑完取：跑的过程中改过的文件会**显式**变成缺口（fail-closed）。
  ② `--status` 重算清单并与之比对：新增/删除/**内容改动**都算缺口；**只被 touch、内容没变的不算**
     （§13.0「剔除要报数」：这类计数照打出来，不许静默丢）。
  ③ 证据没有清单（建于 §1319 之前）⇒ 按 `mtime > 证据时间戳` **粗判**，并在输出里写明这是粗判
     —— 「取不到清单」与「清单说没变」是两件事（§13.2）。


## §1302：超时这条路径曾经**什么都留不下**（实测事故）

§1300 给本文件加了「每次运行都落证据」，但只在**跑完**的路径上：
`subprocess.run(..., timeout=1500)` 抛出 `TimeoutExpired` 时**没人接** ⇒ traceback ⇒ 脚本崩在 `main()` 里、
**不写任何 JSON**。后果与 §1300 要修的病**一模一样**，只是藏在失败路径上：
判据 D4 永远看到「尚无（第一次运行前，不判）」——**把「门禁跑不完」读成「还没跑过」**（§13.5 写侧哑线；§16「失败路径比成功路径更该被设计」）。

实测（2026-09-23 15:14:30–15:39:30，本机 56 逻辑核 / 负载 0–1 ⇒ **不是机器被榨干**）：
跑到 **20%** 被杀、其间已有 **15 个 `F`**、`output/` 下 `*fulltest*` **0 个**。
修法：接住 `TimeoutExpired` ⇒ `verdict=TIMEOUT`（**与 `PYTEST_FAILED` 分开**，§13.2）+ 落
`pytest_progress` / `pytest_f_count` / `pytest_timeout_s`；`--status` 对 TIMEOUT 打**不同的话**
（「它没有结论，别读成用例全挂了」）。
**上限默认 9000s**（2026-09-24 §1322 按实测改的：1500s 会让每次照文档起跑都停在 ~25%；
实测完成需 ~62 分钟）；想只探「有没有明显崩」仍可 `--timeout-s 1500`。

## §1376：解释器守卫 —— 「谁启动的」不许决定判据（2026-09-27 实测：白烧一次重型跑 + 9 条假红）

**现场**：本会话 shell 里敲 `python scripts/fulltest_gate.py`，而本机 PATH 上的 `python` 是
`…\\hermes\\hermes-agent\\venv\\…`（3.11，**缺 strawberry**），仓内规范解释器是 `…\\Python314\\python.exe`。
本脚本用 `[sys.executable] -m pytest` 跑 ⇒ 当场 **9 条 collection ERROR**
（`ModuleNotFoundError: No module named 'strawberry'`）、`verdict=PYTEST_FAILED`、**33 秒**结束；
同一份代码在 13:51 用规范解释器跑的是 `2 failed, 3656 passed`（**66 分钟**）。
更坏的副作用：那次假红**覆盖了 `fulltest_last.json`**，把检查单 D4 一起染红。

**为什么 fail-closed**（与 `gate_set.py` 的「只警告」不同）：那里的闸门是秒级的，警告后照样读判定；
这里一次 ~66 分钟且是 D4 的证据源 ⇒ 放它跑完 = 交出「66 分钟 + 9 条假红 + 被覆盖的真证据」。
**工具级拒绝胜过「我会记得」**。

**判据**（`tests/unit/test_fulltest_interpreter_gate.py`，8 条 + `--selftest` 四态）：
①外来解释器 ⇒ `interpreter_refusal()` 非空、`main()` **在 `subprocess.run` 之前**返回 `REFUSED_RC=3`
且**不落证据**（接线面用 monkeypatch 把 `subprocess.run` 换成会抛的桩来钉）；
②理由里必须带**规范解释器的字面路径 + 可粘贴的复跑命令**（§1296：只说「换一个」等于没说）；
③规范路径不存在（换机）⇒ **放行**（拒绝的理由是「有更该用的那个」，不是「路径不同」，否则把工具锁死）；
④`--allow-foreign-interpreter` 是逃生门（谁按的谁知道读数只在那个解释器上成立）。
证据里落 `interpreter` 块（`exe/canonical/warnings/trustworthy`），`--status` 读到
`trustworthy=false` 就打 `[FOREIGN-INTERPRETER]` —— 否则**写了没人读**（§13.5 写侧哑线）。
**刻意不做**的事：不在守卫里探「`import trinity` 能不能过」—— 实测那次**它是能过的**
（引擎初始化日志照打），真缺的是 `strawberry`；探针形状（`python -c`）与 pytest（cwd 在 `sys.path`）
也不一致 ⇒ 那种探法在这里既漏报又误报（§13.2：不同原因不许合并）。将来若真出现
「规范解释器但环境坏了」的现场，正确的补法是**收集期预检**（`-m pytest --collect-only`，~25 秒），
而不是再加一条启发式。
"""
import argparse
import datetime
import hashlib
import io
import json
import os
import re
import subprocess
import sys

import gate_set as _gate_set          # 兄弟脚本（`sys.path[0]` 就是 `scripts/`，仓内 200+ 处同款写法）

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT = os.path.join(_TRINITY_ROOT, "temp", "fulltest_out.txt")
_EVIDENCE_DIR = os.path.join(_TRINITY_ROOT, "output")
_EVIDENCE_LAST = os.path.join(_EVIDENCE_DIR, "fulltest_last.json")
STALE_DAYS = 30.0          # 证据陈旧阈值（重活手动跑 ⇒ 给得宽；判据是"有没有、是不是红的"）
# 2026-09-24（§1319）：**代码面**清单的范围 —— 只收 pytest 真会跑到的那些输入。
# 为什么不把全仓都算进来：`dsh-ops/*.md`、`docs/*.json`、`output/` 的证据文件天天变，
# 把它们算进缺口会让判据**每天都红**（那不是判据，那是噪声）。
COVERAGE_DIRS = ("trinity", "tests", "scripts")
COVERAGE_FILES = ("pytest.ini", "pyproject.toml", "setup.cfg", "conftest.py")
MANIFEST_LAST = os.path.join(_EVIDENCE_DIR, "fulltest_tree_manifest.json")
GAP_LABEL = "[COVERAGE-GAP]"
# pytest 内部上限（2026-09-23 §1302 立；**2026-09-24 §1322 按实测改默认值**）。
#
# §1302 的原文是「默认保持 1500s（25 分钟）不动 —— 改运行时长属于资源行为，不该由一次诊断顺手改掉」。
# 那条**保守默认**当时是对的（只有一次外推证据）。**现在有两组独立实测**：
#   · 09-23 15:14 起跑 ⇒ 1500s 被杀时到 **20%**（≈107 分钟的外推）
#   · 09-24 12:53 起跑 ⇒ 1500s 被杀时到 **26%**（同量级）；而当天 11:29 那次**真跑完**只用了 **3727s（62 分钟）**
# ⇒ 默认值的效果是「**照着文档跑一次 = 25 分钟后拿到 TIMEOUT（没有结论）**」，白烧一次机器
# （12:53 那次就是这么烧掉的，见 §1321 第四节）。
# 所以默认改成 **9000s（150 分钟）≈ 实测完成时长的 2.4 倍**（给负载波动留头寸）；
# 想快速探「有没有明显崩」仍可显式收窄：`--timeout-s 1500`。
# **回滚**：把这一行改回 1500（一个常量；判据不依赖它的值）。
PYTEST_TIMEOUT_S = 9000


def _git_head() -> str:
    """读 `.git/HEAD`（**不调 git 子进程**）—— 取不到就返回 `?`，**不抛**（少一个字段好过整个门禁跑不起来）。

    为什么不用 `subprocess.run(["git", ...])`（2026-09-23 实测）：那会被 `scripts/child_stdio_face_audit.py`
    计成**一个未钉编码的捕获点**（`kind=other` —— 外部程序的输出**没法用 `PYTHONIOENCODING` 钉**），
    于是检查单 **E11 从 90 → 91 直接变红**。而这里只需要一个短号 ⇒ 读文件既够用、又少一个子进程依赖
    （git 不在 PATH 上也照样工作）。顺带处理 `.git` 是**文件**的情形（worktree / submodule）。
    """
    try:
        dot_git = os.path.join(_TRINITY_ROOT, ".git")
        if os.path.isfile(dot_git):                      # worktree：`.git` 是个文件，内容形如 `gitdir: …`
            with io.open(dot_git, encoding="utf-8", errors="replace") as fh:
                line = fh.read().strip()
            if line.lower().startswith("gitdir:"):
                dot_git = line.split(":", 1)[1].strip()
        with io.open(os.path.join(dot_git, "HEAD"), encoding="utf-8", errors="replace") as fh:
            raw = fh.read().strip()
        if raw.startswith("ref:"):                       # 未分离 HEAD ⇒ 再读 ref 文件
            ref = raw.split(":", 1)[1].strip()
            with io.open(os.path.join(dot_git, ref.replace("/", os.sep)),
                         encoding="utf-8", errors="replace") as fh:
                return (fh.read().strip() or "?")[:7]
        return (raw or "?")[:7]
    except Exception:  # noqa: BLE001
        return "?"


def _summary(text: str) -> str:
    """从 pytest 尾巴里挑一行摘要（含 passed/failed/error 的最后一行）。"""
    for line in reversed([_ln.strip() for _ln in text.splitlines() if _ln.strip()]):
        if ("passed" in line or "failed" in line or "error" in line) and "=" not in line[:3]:
            return line[:200]
    return ""


# 2026-09-23（§1302）：超时/被杀时 pytest **不会**打印摘要行 ⇒ 光靠 `_summary` 只能得到空串，
# 现场信息（跑到哪、有几个 F）全丢。这两个读数就是给那条路径用的：**「跑到哪一步」必须留证**，
# 否则下一次只能重新花 25 分钟去撞同一堵墙。
_PROGRESS_CHARS = ".FEsxX"


def _failed_lines(text: str, cap: int = 60) -> list:
    """从 `-q` 的 short test summary 里取出 `FAILED …` 行（§1302：证据要能直接说清**哪些**用例红了）。

    边界（§13.3）：只认行首就是 `FAILED `/`ERROR ` 的（pytest 的 short summary 形态）；
    进度行、traceback 正文、`1 failed, 347 passed` 摘要行都进不来。取不到就返回空列表（**明说没有**，不猜）。
    """
    out = [_ln.strip()[:200] for _ln in text.splitlines()
           if _ln.startswith("FAILED ") or _ln.startswith("ERROR ")]
    return out[:cap]


def _progress(text: str) -> str:
    """pytest `-q` 进度里**最后一个** `[ NN%]` 标记（取不到返回 `?`）。"""
    last = None
    for m in re.finditer(r"\[\s*(\d+)%\]", text):
        last = m.group(1)
    return (last + "%") if last is not None else "?"


def _marker_count(text: str, ch: str) -> int:
    """只数**进度行**里的标记字符（`F` 失败 / `E` 错误 / `s` 跳过）。

    边界（§13.3）：只认「整行字符都属于进度字母表 + `[NN%]`」的行 ⇒ 测试名、摘要行、traceback 都不进计数
    （否则 `Failed` 里的 F、路径里的 s 都会被算进来）。
    """
    ok = set(_PROGRESS_CHARS + "[]0123456789% ")
    return sum(line.strip().count(ch) for line in text.splitlines()
               if line.strip() and set(line.strip()) <= ok)


def _write_evidence(rec: dict, ts: str) -> str:
    os.makedirs(_EVIDENCE_DIR, exist_ok=True)
    stamp = ts.replace("-", "").replace(":", "").replace("T", "_")[:15]
    per = os.path.join(_EVIDENCE_DIR, "fulltest_%s.json" % stamp)
    for p in (per, _EVIDENCE_LAST):
        with io.open(p, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)
    return per


def _persist_pytest_log(text: str, ts: str, evidence_dir: str = None) -> str:
    """把**整份** pytest 输出落到证据目录，返回路径（2026-09-23 §1308 实测新增）。

    来由（§13.5 的写侧哑线，只是发生在**失败路径**上）：`pytest_tail` 只存 2000 字符，
    而 2026-09-23 17:15 那次「2 failed, 3391 passed」的运行里，这 2000 字符**全部**是
    警告汇总 + 摘要行 ⇒ `output/` 里那份「证据」事后**连一条 traceback 都取不出来**
    （两条红分别是 `assert 0 > 0` 与 `OperationalError: server closed the connection`，
    是我另去 `temp/fulltest_out.txt` 才读到的）；而那个文件**每次运行都被覆盖**
    ⇒ 同一个问题下次还得再花 60 分钟复现一次。`-q --tb=line` 全文约 26 KB，落盘不心疼。
    失败路径也要有产物 —— 同族纪律见 §16「设计工具时，失败路径比成功路径更该被设计」。
    """
    stamp = ts.replace("-", "").replace(":", "").replace("T", "_")[:15]
    d = evidence_dir or _EVIDENCE_DIR
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "fulltest_pytest_%s.log" % stamp)
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(text or "")
    return path


def _tree_manifest(root: str = None) -> dict:
    """代码面清单：`{相对路径: [size, mtime_ns, sha256]}`（2026-09-24 §1319）。

    只收 `COVERAGE_DIRS` 下的 `.py`（跳过 `__pycache__`/`.pyc`）与 `COVERAGE_FILES` 这几份配置
    —— 它们才是「pytest 这一跑**测的是哪份代码**」的答案。**存哈希**是为了把「只被 touch、
    内容没变」的文件与「真的改了」分开（否则一个 no-op 保存就会把 D4 判红）。
    """
    base = root or _TRINITY_ROOT
    out: dict = {}
    for d in COVERAGE_DIRS:
        top = os.path.join(base, d)
        if not os.path.isdir(top):
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, base).replace("\\", "/")
                try:
                    st = os.stat(full)
                    with open(full, "rb") as fh:
                        h = hashlib.sha256(fh.read()).hexdigest()
                    out[rel] = [st.st_size, st.st_mtime_ns, h]
                except Exception:  # noqa: BLE001 —— 单个文件读不到就跳过它（不许把整个判据带走）
                    continue
    for fn in COVERAGE_FILES:
        full = os.path.join(base, fn)
        if not os.path.isfile(full):
            continue
        try:
            st = os.stat(full)
            with open(full, "rb") as fh:
                h = hashlib.sha256(fh.read()).hexdigest()
            out[fn] = [st.st_size, st.st_mtime_ns, h]
        except Exception:  # noqa: BLE001
            continue
    return out


def _write_manifest(ts: str, root: str = None, evidence_dir: str = None) -> "tuple[str, dict]":
    """把开跑时刻的代码面清单落盘（返回 `(路径, 清单)`）；同时写 `MANIFEST_LAST` 便于人肉比对。"""
    man = _tree_manifest(root)
    stamp = ts.replace("-", "").replace(":", "").replace("T", "_")[:15]
    d = evidence_dir or _EVIDENCE_DIR
    os.makedirs(d, exist_ok=True)
    per = os.path.join(d, "fulltest_tree_manifest_%s.json" % stamp)
    last = os.path.join(d, "fulltest_tree_manifest.json")
    for p in (per, last):
        with io.open(p, "w", encoding="utf-8") as fh:
            json.dump(man, fh, ensure_ascii=False, sort_keys=True)
    return per, man


def _manifest_diff(old: dict, new: dict) -> dict:
    """比对两份清单：新增 / 删除 / **内容改动** / 只被 touch（内容没变）。纯函数，好测。"""
    o, n = old or {}, new or {}
    added = sorted(set(n) - set(o))
    removed = sorted(set(o) - set(n))
    modified, touched_same = [], []
    for k in sorted(set(o) & set(n)):
        a, b = o[k], n[k]
        if len(a) >= 3 and len(b) >= 3 and a[2] == b[2]:
            if a[0] != b[0] or a[1] != b[1]:
                touched_same.append(k)          # 时间戳变了、内容一字未动 ⇒ **不算缺口**
            continue
        modified.append(k)
    return {"added": added, "removed": removed, "modified": modified,
            "touched_same": touched_same,
            "n_total": len(n), "n_changed": len(added) + len(removed) + len(modified)}


def coverage_status(rec: dict, root: str = None) -> int:
    """代码面覆盖判定（§1319）：证据覆盖不到当前代码 ⇒ 打 `GAP_LABEL` 且 rc=1。

    只在「证据新鲜且 PASS」这条路径上调用 —— 已经红了/陈旧了再叠一条缺口只是噪声。
    """
    base = root or _TRINITY_ROOT
    cur = _tree_manifest(base)
    man = str(rec.get("tree_manifest") or "")
    old = None
    if man and os.path.exists(man):
        try:
            with io.open(man, encoding="utf-8") as fh:
                old = json.load(fh)
        except Exception as exc:  # noqa: BLE001 —— 读不出清单 ≠ 清单说没变（§13.2）
            print("      [COVERAGE] 清单读不出来（%s: %s）⇒ 退回按 mtime 粗判"
                  % (type(exc).__name__, str(exc)[:60]))
            old = None
    if old is None:
        # 旧证据（建于 §1319 之前）没有清单 ⇒ 粗判：mtime 晚于证据时间戳的 `.py` 就算可能变过。
        try:
            t = datetime.datetime.fromisoformat(str(rec.get("ts") or ""))
        except Exception:  # noqa: BLE001
            print("      [COVERAGE] 证据时间戳不可解析 ⇒ 不判代码面（**不是**「覆盖到了」）")
            return 0
        cut = t.timestamp()
        newer = sorted(k for k, v in cur.items() if (v[1] / 1e9) > cut)
        if newer:
            print("%s 证据**没有代码面清单**（建于 §1319 之前），粗判 mtime 后有 %d 个 .py 变过 ⇒ "
                  "覆盖不到当前代码：%s" % (GAP_LABEL, len(newer), "、".join(newer[:5])))
            return 1
        print("[COVERAGE] 本条证据没有代码面清单（建于 §1319 之前）⇒ 按 mtime **粗判**："
              "证据之后无更新的 .py（扫了 %d 个；下一跑起用清单精确比对）" % len(cur))
        return 0
    d = _manifest_diff(old, cur)
    if d["n_changed"] == 0:
        print("[COVERAGE] 代码面与证据一致（%d 个 .py 逐个比哈希；只被 touch、内容未变的 %d 个不计）"
              % (d["n_total"], len(d["touched_same"])))
        return 0
    samples = d["modified"][:4] + ["+" + x for x in d["added"][:3]] + ["-" + x for x in d["removed"][:3]]
    print("%s 证据之后**代码面变过**：内容改动 %d / 新增 %d / 删除 %d"
          "（只被 touch、内容未变的 %d 个不计）⇒ 它覆盖的是**旧代码**：%s"
          % (GAP_LABEL, len(d["modified"]), len(d["added"]), len(d["removed"]),
             len(d["touched_same"]), "、".join(samples[:8])))
    print("      ⇒ 重跑 `python scripts/fulltest_gate.py`（**重型**；先 `python scripts/resource_window_check.py`）"
          "；本条**不是**说用例挂了，是说「全量回归绿」这句话**不覆盖当前代码**（§1319）")
    return 1


def _verdict(pytest_rc, eval_rc, timed_out: bool = False) -> str:
    """两段合起来给一个 verdict（§1313：**分开报告、合并定性**）。

    为什么要合并定性：证据里只有一个 `verdict` 字段，D4 判据也只读它；
    旧写法「pytest 红 ⇒ 直接返回、`EVALS SKIPPED`」会让 **eval 段整段消失**
    —— 实测代价：`goals-no-stall` 那条红在 `EVALS SKIPPED (PYTEST_FAILED)` 后面**盲了两周**
    （pytest 从 8 条红降到 0，它才露出来）。
    `eval_rc is None` = 这一段没跑（或没结论）⇒ **不当作失败**，但不许静默（由 `eval_skipped_reason` 报）。
    """
    if timed_out:
        return "TIMEOUT"
    p_bad = pytest_rc not in (0, None)
    e_bad = eval_rc not in (0, None)
    if p_bad and e_bad:
        return "PYTEST+EVAL_FAILED"
    if p_bad:
        return "PYTEST_FAILED"
    if e_bad:
        return "EVAL_FAILED"
    return "PASS"


def _eval_should_run(timed_out: bool) -> "tuple[bool, str]":
    """eval 段该不该跑（§1313）。返回 `(要不要跑, 不跑的原因)`。

    唯一不跑的情形是 **pytest 被超时杀掉**：那时机器状态可疑（可能是资源被榨干，
    §1184 实测过「跑重活把 API 一起赔进去」），按保守默认跳过 —— 但**必须把原因写进证据**，
    不许像旧版那样只留一句 `EVALS SKIPPED`。
    """
    if timed_out:
        return False, "pytest 被超时杀掉 ⇒ 机器状态可疑，按保守默认跳过 eval 段（不是「eval 通过」）"
    return True, ""


# ── 解释器守卫（2026-09-27 §1376：白烧一次重型跑 + 9 条假红）────────────────────────
#: 规范解释器：**从 `gate_set.py` 取**，不另立一份（§13.0 的同款纪律：口径只留一处；
#: 它还带 `TRINITY_SYS_PY` 覆盖，换机不必改两个文件）。
CANONICAL_PY = _gate_set.CANONICAL_PY
#: 拒绝开跑的 rc。**独立取值**：拒绝 ≠ 门禁红（§13.2「不同失败原因不许合并计数」）——
#: 读了 1 会去查用例，读了 3 才知道「这一次根本没跑，上一次的证据还在」。
REFUSED_RC = 3
FOREIGN_LABEL = "[FOREIGN-INTERPRETER]"


def _same_interpreter(exe: str, canonical: str) -> bool:
    """两个解释器路径是不是同一个（取不到路径就当**不同** ⇒ fail-closed）。纯函数。"""
    try:
        return os.path.normcase(os.path.abspath(exe)) == os.path.normcase(os.path.abspath(canonical))
    except Exception:  # noqa: BLE001
        return False


def interpreter_refusal(exe: str = None, canonical: str = None, canonical_exists: bool = None,
                        allow_foreign: bool = False) -> str:
    """返回**拒绝开跑的理由**（空串 = 放行）。纯判据：不起子进程、不落盘、不看环境变量。

    三条边界都写死在这里（每条都有反例测试）：
      · 外来解释器 + 规范路径存在 ⇒ **拒绝**（本脚本用 `sys.executable` 跑 pytest/eval）；
      · 规范路径**不存在**（换机/换版本）⇒ **放行** —— 拒绝的理由是「有更该用的那个」，
        不是「路径长得不一样」，否则会把工具在别的机器上锁死；
      · `allow_foreign`（命令行 `--allow-foreign-interpreter`）⇒ 放行：这是人**显式按下去**的。
    """
    exe = exe or sys.executable
    canonical = canonical or CANONICAL_PY
    if canonical_exists is None:
        canonical_exists = os.path.exists(canonical)
    if allow_foreign or not canonical_exists:
        return ""
    if _same_interpreter(exe, canonical):
        return ""
    return ("解释器不是仓内规范解释器 ⇒ **拒绝开跑**（fail-closed）：本脚本用 `sys.executable` 跑 "
            "pytest 与 eval，解释器选错时「依赖缺失」会被读成「用例挂」\n"
            "    本次用的解释器：%s\n"
            "    仓内规范解释器：%s\n"
            "    复跑命令：& \"%s\" scripts\\fulltest_gate.py\n"
            "    （实测代价：外来 3.11 上 9 条 collection ERROR、33 秒、verdict=PYTEST_FAILED，"
            "并**覆盖上一次真跑的证据**；同一份代码在规范解释器上是 3656 passed）\n"
            "    （确实要用别的解释器：加 `--allow-foreign-interpreter`，"
            "并知道读数只在那个解释器上成立）" % (exe, canonical, canonical))


def _interpreter_block(exe: str = None, canonical: str = None, canonical_exists: bool = None) -> dict:
    """写进证据的「这一跑是哪个解释器、可不可信」。**可信度是算出来的**，不是恒真（可失败）。

    为什么证据里只写 `python` 路径不够（那一个键 §1300 起就有）：读的人**看不出它该不该信** ——
    本轮我正是拿着 `"python": …hermes…\\venv\\…` 这份证据，先把它当成了真红。
    """
    exe = exe or sys.executable
    canonical = canonical or CANONICAL_PY
    if canonical_exists is None:
        canonical_exists = os.path.exists(canonical)
    warns = []
    if canonical_exists and not _same_interpreter(exe, canonical):
        warns.append("解释器不是仓内规范解释器（%s；本次是 %s）⇒ 读数可能是**假红**" % (canonical, exe))
    if not canonical_exists:
        warns.append("规范解释器路径不存在（%s）⇒ 只按「跑起来了没」判，不判解释器" % canonical)
    return {
        "exe": exe,
        "canonical": canonical,
        "canonical_exists": canonical_exists,
        "version": "%d.%d.%d" % sys.version_info[:3],
        "warnings": warns,
        "trustworthy": interpreter_refusal(exe, canonical, canonical_exists) == "",
        "sampled_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


# ── 仓内 output/ 可写性守卫（2026-09-27 §1394.19：一次白烧的重型跑 + 19 条**环境**假红）──────
#: 为什么守的是**仓内 `output/`** 而不是 `_EVIDENCE_DIR`：证据目录**可以**被重定向（自测与包装脚本会那么干），
#: 但**被测代码自己**会往仓内 `output/` 写产物 —— 最典型的是基准：
#: `tests/benchmark/test_benchmark.py` 把结果写成 `output/bench_*.json`。
#: 在写不了 `output/` 的上下文里（低完整性 / 受限令牌），那些用例**确定性地**红
#: ⇒ 这一跑既不会绿、也不能当验证，只是白烧一次重型跑。**fail-closed：拒跑并说清。**
#: 独立常量（**不跟随 `_EVIDENCE_DIR` 的重定向**）：守的对象是「仓内 output/」这个事实。
REPO_OUTPUT = os.path.join(_TRINITY_ROOT, "output")
OUTPUT_GUARD_LABEL = "[UNWRITABLE-OUTPUT]"


def output_refusal(writable: bool, out_dir: str = None, allow_unwritable: bool = False) -> str:
    """返回**拒绝开跑的理由**（空串 = 放行）。纯判据：不落盘、不看环境变量。

    边界（每条都有反例测试）：
      · 仓内 `output/` **不可写** ⇒ **拒绝** —— 本跑必然含环境性假红，verdict 不可能是绿；
      · `allow_unwritable`（`--allow-unwritable-output`）⇒ 放行：这是人**显式按下去**的。
    """
    if allow_unwritable or writable:
        return ""
    out_dir = out_dir or REPO_OUTPUT
    return ("仓内 output/ **不可写** ⇒ **拒绝开跑**（fail-closed）：被测代码里有用例**自己**往仓内 "
            "output/ 落产物（实测：`tests/benchmark/test_benchmark.py` 的 7 条把结果写成 "
            "`output/bench_*.json`）\n"
            "    探针目录：%s\n"
            "    ⇒ 在写不了它的上下文里，这一跑**必然**含环境性假红：既不会绿，也不能当验证\n"
            "    （实测代价：受限 shell 里跑到 26%% 被预算杀掉、已记录 19 条红且**全部**归因到上下文；"
            "若不设预算，67 分钟只会换来 verdict=PYTEST_FAILED）\n"
            "    （换一个能写它的 shell；确实要在受限上下文里跑：加 `--allow-unwritable-output`，"
            "并知道这次的读数**不能**当门禁证据）" % out_dir)


def probe_writable(d: str) -> bool:
    """真去试一次「在 d 里建临时文件再删」；任何异常都当**不可写**（fail-closed）。"""
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "_write_probe_%d.tmp" % os.getpid())
        with io.open(p, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.remove(p)
        return True
    except Exception:  # noqa: BLE001
        return False


def cmd_status(evidence: str = None, root: str = None, check_coverage: bool = True) -> int:
    """只读四态判定；返回 rc（0 = 有证据且新鲜且 PASS 且**覆盖当前代码面**，或**尚无证据**；1 = 其余）。

    `root` / `check_coverage` 只为**自测**而存在（合成证据 + 临时代码面），真实调用不带它们。
    """
    p = evidence or _EVIDENCE_LAST
    if not os.path.exists(p):
        print("全量门禁证据: 尚无（第一次运行前，不判）—— 建它就跑 `python scripts/fulltest_gate.py`"
              "（**重型**；跑前先 `python scripts/resource_window_check.py`）")
        return 0
    try:
        with io.open(p, encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception as exc:  # noqa: BLE001 —— 读不了 ≠ 没有：分开说，且按"证据不可用"判红（fail-closed）
        print("[FAIL] 全量门禁证据**读不出来**（%s: %s）⇒ 不是「没有证据」，是证据坏了"
              % (type(exc).__name__, str(exc)[:80]))
        return 1
    ts_raw = str(rec.get("ts") or "")
    try:
        age_d = (datetime.datetime.now() - datetime.datetime.fromisoformat(ts_raw)).total_seconds() / 86400.0
    except Exception:  # noqa: BLE001
        print("[FAIL] 全量门禁证据的时间戳不可解析：%r ⇒ 位置就知道它不是本次的产物" % ts_raw[:40])
        return 1
    verdict = str(rec.get("verdict") or "?")
    line = ("全量门禁证据: %s（%.1f 天前）verdict=%s pytest=%s eval=%s 耗时=%.0fs HEAD=%s"
            % (ts_raw, age_d, verdict, rec.get("pytest_summary") or "-",
               rec.get("eval_rc", "-"), float(rec.get("elapsed_s") or 0), rec.get("head") or "?"))
    # §1376：**这条证据可不可信**要先说 —— 外来解释器跑的绿和红都不可信，别让人去猜。
    # 判据只有一条（路径比对）；证据里没有新块时按 §1300 起就有的老键 `python` 兜底 ——
    # 那个键**一直写在证据里却从来没人读**，本轮我正是拿着它把假红当真红读了一遍（§13.5）。
    _ib = rec.get("interpreter") or {}
    _exe, _src = _ib.get("exe"), "interpreter 块"
    if not _ib:
        _exe, _src = rec.get("python"), "老键 `python`（本条证据建于 §1376 之前）"
    try:
        _foreign = bool(_exe) and os.path.exists(CANONICAL_PY) and\
            not _same_interpreter(str(_exe), CANONICAL_PY)
    except Exception:  # noqa: BLE001 —— 判不了就不打标签（宁可不说话，也不说错话）
        _foreign = False
    if _foreign:
        print("%s 本条证据是**非规范解释器**跑出来的（%s；规范：%s；判定依据：%s）"
              "⇒ 绿不可信、红也不可信：先按 §1376 的解释器守卫复跑（**别**照着这条红去改代码）"
              % (FOREIGN_LABEL, _exe, CANONICAL_PY, _src))
    if verdict == "TIMEOUT":
        # §13.2：**超时 ≠ 跑了但没过** —— 上一次连结果都没有，别让人去 JSON 里找不存在的失败用例。
        print("[FAIL] " + line + " ⇒ 上一次全量门禁**跑超时了**（限 %ss，跑到 %s，其间已见 %s 个 F）"
              " ⇒ 它**没有结论**：要么查为什么这么慢，要么显式放宽 `--timeout-s` 再跑；"
              "**别把这条读成「用例全挂了」**"
              % (rec.get("pytest_timeout_s", "?"), rec.get("pytest_progress", "?"), rec.get("pytest_f_count", "?")))
        # §1313：超时是唯一会跳过 eval 的情形 ⇒ 把**原因**打出来（否则又是一处「静默没跑」）。
        if rec.get("eval_skipped_reason"):
            print("      eval 段：**没跑** —— %s" % rec["eval_skipped_reason"])
        return 1
    if verdict != "PASS":
        # §1308：红的同时**指路到全文**（此前只说「看 tail」，而 tail 里恰恰没有 traceback）。
        _log = str(rec.get("pytest_log") or "")
        _hint = ""
        if _log:
            if os.path.exists(_log):
                # 相对路径只为好读；**跨盘会抛 ValueError**（实测：本仓在 C:、临时目录在 D:）
                # ⇒ 判据自己崩会被读成「你又违规了」（§1212 同族）⇒ 取不到相对路径就用绝对路径。
                try:
                    _shown = os.path.relpath(_log, _TRINITY_ROOT).replace("\\", "/")
                except Exception:  # noqa: BLE001
                    _shown = _log
                _hint = "；失败全文（含 traceback）：%s" % _shown
            else:
                _hint = "；证据里记了 pytest 全文路径但文件不在了：%s" % _log
        else:
            _hint = "（本条证据建于 §1308 之前 ⇒ 只留了 2000 字符尾巴，重跑一次才有全文）"
        print("[FAIL] " + line + " ⇒ 上一次全量门禁**没过**：先看 output/fulltest_*.json 的 "
              "pytest_summary 与 tail" + _hint)
        return 1
    if age_d > STALE_DAYS:
        print("[STALE] " + line + " ⇒ 超过 %.0f 天没跑过 ⇒ 「全量回归绿」这句话目前**没有新鲜证据**" % STALE_DAYS)
        return 1
    print(line)
    if check_coverage:
        # §1319：**新鲜 ≠ 覆盖当前代码** —— 只在绿路径上加这一条（红/陈旧的路径已够清楚，别叠噪声）。
        return coverage_status(rec, root)
    return 0


def cmd_selftest() -> int:
    """判据自身可失败：三态各造一份合成证据（陈旧 / 失败 / 新鲜），外加"缺失"态。"""
    import tempfile
    checks = []
    with tempfile.TemporaryDirectory() as td:
        def _mk(name, rec):
            p = os.path.join(td, name)
            with io.open(p, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False)
            return p

        now = datetime.datetime.now()
        old = (now - datetime.timedelta(days=STALE_DAYS + 5)).isoformat(timespec="seconds")
        fresh = (now - datetime.timedelta(days=1)).isoformat(timespec="seconds")
        p_old = _mk("old.json", {"ts": old, "verdict": "PASS", "pytest_summary": "2669 passed"})
        p_bad = _mk("bad.json", {"ts": fresh, "verdict": "FAIL", "pytest_summary": "1 failed"})
        p_fresh = _mk("fresh.json", {"ts": fresh, "verdict": "PASS", "pytest_summary": "2669 passed"})
        p_miss = os.path.join(td, "nope.json")
        buf = []
        for name, path, want_rc in (("陈旧", p_old, 1), ("上次是红的", p_bad, 1),
                                    ("新鲜且 PASS", p_fresh, 0), ("文件缺失", p_miss, 0)):
            real = sys.stdout
            sys.stdout = io.StringIO()
            # 合成证据没有代码面清单 ⇒ 这四态**关掉覆盖判定**，各判各的（覆盖面另有专条，见下）
            rc = cmd_status(path, root=td, check_coverage=False)
            out = sys.stdout.getvalue()
            sys.stdout = real
            buf.append((name, rc, out.strip()[:110]))
            checks.append(rc == want_rc and len(out.strip()) > 0)

        # §1319 的两态：同一个合成代码面上，「没变」与「变过」必须给出**相反**的 rc。
        tdir = os.path.join(td, "codeface")
        os.makedirs(os.path.join(tdir, "trinity"), exist_ok=True)
        f = os.path.join(tdir, "trinity", "x.py")
        with io.open(f, "w", encoding="utf-8") as fh:
            fh.write("a = 1\n")
        mpath, _ = _write_manifest("2026-09-24T00:00:00", root=tdir, evidence_dir=td)
        cov_rec = {"ts": now.isoformat(timespec="seconds"), "verdict": "PASS", "tree_manifest": mpath}
        real = sys.stdout
        sys.stdout = io.StringIO()
        rc_same = coverage_status(cov_rec, root=tdir)
        out_same = sys.stdout.getvalue()
        sys.stdout = real
        with io.open(f, "w", encoding="utf-8") as fh:
            fh.write("a = 2\n")                      # 内容改了（不是 touch）
        sys.stdout = io.StringIO()
        rc_gap = coverage_status(cov_rec, root=tdir)
        out_gap = sys.stdout.getvalue()
        sys.stdout = real
        buf.append(("代码面未变", rc_same, out_same.strip()[:110]))
        buf.append(("代码面变过", rc_gap, out_gap.strip()[:110]))
        checks.append(rc_same == 0 and GAP_LABEL not in out_same)
        checks.append(rc_gap == 1 and GAP_LABEL in out_gap)
        # 反事实：把**原内容**写回去（哈希与清单一致）再 touch ⇒ 必须**不**判缺口。
        # （第一版这里只做了 `os.utime`，而文件里还是 `a = 2` ⇒ 当场被判缺口 —— 判据抓住了写它的人。）
        with io.open(f, "w", encoding="utf-8") as fh:
            fh.write("a = 1\n")
        os.utime(f, None)
        sys.stdout = io.StringIO()
        rc_touch = coverage_status(cov_rec, root=tdir)
        out_touch = sys.stdout.getvalue()
        sys.stdout = real
        buf.append(("只 touch", rc_touch, out_touch.strip()[:110]))
        checks.append(rc_touch == 0 and GAP_LABEL not in out_touch)

        # §1376：解释器守卫四态（**刻意不依赖 pytest** —— 解释器错了的现场，pytest 恰恰收集不起来：
        # 本轮实测 9 条 collection ERROR。「守卫的证明不许依赖被守卫的东西」。）
        _cases = (("解释器=规范⇒放行", dict(exe=CANONICAL_PY, canonical_exists=True), True),
                  ("解释器=外来⇒拒绝", dict(exe=r"C:\other\python.exe", canonical_exists=True), False),
                  ("规范路径缺失⇒放行", dict(exe=r"C:\other\python.exe", canonical_exists=False), True),
                  ("显式逃生门⇒放行", dict(exe=r"C:\other\python.exe", canonical_exists=True,
                                          allow_foreign=True), True))
        for _name, _kw, _want_pass in _cases:
            _why = interpreter_refusal(**_kw)
            _pass = (_why == "") == _want_pass
            buf.append((_name, 0 if _pass else 1, (_why or "放行").replace("\n", " ")[:110]))
            checks.append(_pass)
        # 拒绝理由必须**可照做**（§1296：只说「换个解释器」而不给字面路径与命令 = 读的人还是动不了）
        _bad = interpreter_refusal(exe=r"C:\other\python.exe", canonical_exists=True)
        checks.append(CANONICAL_PY in _bad and "fulltest_gate.py" in _bad)

        # §1394.19：**仓内 output/ 可写性守卫**的两侧（同一形状：可写⇒放行 / 不可写⇒拒绝 / 逃生门⇒放行）
        _wcases = (("output=可写⇒放行", dict(writable=True), True),
                   ("output=不可写⇒拒绝", dict(writable=False), False),
                   ("不可写+逃生门⇒放行", dict(writable=False, allow_unwritable=True), True))
        for _name, _kw, _want_pass in _wcases:
            _why = output_refusal(out_dir=r"D:\x\output", **_kw)
            _pass = (_why == "") == _want_pass
            buf.append((_name, 0 if _pass else 1, (_why or "放行").replace("\n", " ")[:110]))
            checks.append(_pass)
        # 拒绝理由必须可照做：要点名探针目录、给出逃生门开关名（否则读的人只能猜）
        _wbad = output_refusal(writable=False, out_dir=r"D:\x\output")
        checks.append(r"D:\x\output" in _wbad and "--allow-unwritable-output" in _wbad)
        # 探针本身要能失败：写一个**不存在的盘符**必须返回 False（否则它恒真、守卫等于没有）
        checks.append(probe_writable(r"Z:\__no_such_dir__\__probe__") is False)
    for name, rc, out in buf:
        print("selftest(%-12s): rc=%d ｜ %s" % (name, rc, out))
    print("selftest: %s（四态 + 代码面覆盖两态 + touch 反事实 + 解释器四态 + output 可写性三态；任一态不符即 FAIL）"
          % ("PASS" if all(checks) else "FAIL"))
    return 0 if all(checks) else 1


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--status", action="store_true", help="只读：上次证据的新鲜度与 verdict")
    ap.add_argument("--evidence", default=None, help="配合 --status：指定证据文件（自测用）")
    ap.add_argument("--selftest", action="store_true", help="判据自身可失败（四态）")
    ap.add_argument("--timeout-s", type=int, default=PYTEST_TIMEOUT_S,
                    help="pytest 内部上限秒数（默认 %d；超时也会**落证据**，verdict=TIMEOUT。"
                         "想只探「有没有明显崩」给 1500）" % PYTEST_TIMEOUT_S)
    ap.add_argument("--allow-foreign-interpreter", action="store_true",
                    help="显式放行非规范解释器（默认**拒绝开跑**：解释器选错会把「依赖缺失」读成"
                         "「用例挂」并覆盖上一次真跑的证据，见 §1376）")
    ap.add_argument("--allow-unwritable-output", action="store_true",
                    help="显式放行「仓内 output/ 不可写」（默认**拒绝开跑**：基准等用例自己要往 output/ "
                         "落产物，写不了就必然是一批**环境假红**，见 §1394.19）")
    a = ap.parse_args()
    if a.selftest:
        return cmd_selftest()
    if a.status:
        return cmd_status(a.evidence)

    # §1376：**开跑之前**判解释器（放在这里而不是跑完：跑完再判等于把 66 分钟与 9 条假红一起交出去）。
    _why = interpreter_refusal(allow_foreign=a.allow_foreign_interpreter)
    if _why:
        print(_why)
        print("⇒ 未开跑（rc=%d）—— **这不是门禁红**：本次一个用例都没跑，"
              "`output/fulltest_last.json` 里仍是上一次的证据" % REFUSED_RC)
        return REFUSED_RC

    # §1394.19：同一条纪律的第二个入口 —— **仓内 output/ 写不了**时也拒绝开跑
    # （实测：受限 shell 里会得到 19 条**环境**假红，且 verdict 永远不可能是绿）。
    _wy = output_refusal(writable=probe_writable(REPO_OUTPUT),
                         allow_unwritable=a.allow_unwritable_output)
    if _wy:
        print(_wy)
        print("⇒ 未开跑（rc=%d）—— **这不是门禁红**：本次一个用例都没跑，"
              "`output/fulltest_last.json` 里仍是上一次的证据" % REFUSED_RC)
        return REFUSED_RC

    t0 = datetime.datetime.now()
    tests_dir = os.path.join(_TRINITY_ROOT, "tests")
    # 2026-09-24（§1319）：**开跑时**就把代码面清单钉下来（跑的过程中改动 ⇒ 显式变成缺口）。
    # 放在这里而不是跑完：跑完再取会把「跑到一半有人改了代码」这种情形**静默吞掉**（fail-open）。
    _manifest_path = ""
    try:
        _manifest_path, _man = _write_manifest(t0.isoformat(timespec="seconds"))
    except Exception as _e:  # noqa: BLE001 —— 清单落不下来不影响跑（但证据里会缺这个键 ⇒ 退回粗判）
        print("[WARN] 代码面清单落盘失败：%r" % (_e,))
    timed_out = False
    try:
        with open(_OUT, "w", encoding="utf-8") as f:
            rc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--tb=line", tests_dir],
                cwd=_TRINITY_ROOT, timeout=a.timeout_s, stdout=f, stderr=subprocess.STDOUT)
        rc_code = rc.returncode
    except subprocess.TimeoutExpired:
        # 2026-09-23（§1302 实测事故）：**超时这条路径此前会当场抛出去 ⇒ 一个字的证据都不留**
        # `subprocess.TimeoutExpired` 没被接住 ⇒ traceback ⇒ 脚本崩在 main() 里、不写 JSON
        # ⇒ 判据 D4 永远看到「尚无（第一次运行前，不判）」= **把「门禁跑不完」读成「还没跑过」**。
        # 这就是 §1300 自己要修的那一类（写侧哑线，§13.5），只是漏在了**失败路径**上
        # ——同族先例写在 §16：「设计工具时，失败路径比成功路径更该被设计」。
        # 边界（§13.2）：超时与「跑了但没过」是**两件事**，verdict 分开写（`TIMEOUT` vs `PYTEST_FAILED`）。
        timed_out = True
        rc_code = None
    tail = open(_OUT, encoding="utf-8", errors="replace").read()
    # §1308：**先把全文落进证据目录**（失败时它就是唯一能解释红的东西；落盘失败要出声，但不改结论）。
    pytest_log = ""
    try:
        pytest_log = _persist_pytest_log(tail, t0.isoformat(timespec="seconds"))
    except Exception as _e:  # noqa: BLE001
        print("[WARN] pytest 全文落盘失败：%r" % (_e,))
    print(tail[-600:])
    print("pytest rc: %s%s" % (rc_code, "（**超时被杀**：%ds 内没跑完）" % a.timeout_s if timed_out else ""))
    failed = _failed_lines(tail)
    if failed:
        # §1302：把「哪些用例红了」打在这一屏 —— 否则读的人只能从 tail[-400:] 里猜（那段几乎总是被摘要行占满）。
        print("失败/错误用例 %d 条（前 10）：" % len(failed))
        for _ln in failed[:10]:
            print("  " + _ln)
    rec = {
        "ts": t0.isoformat(timespec="seconds"),
        "verdict": _verdict(rc_code, None, timed_out),   # 先给结论；eval 段跑完再**重算**（§1313）
        "pytest_rc": rc_code,
        "pytest_summary": _summary(tail) or ("超时未跑完（限 %ds）" % a.timeout_s if timed_out else ""),
        "pytest_tail": tail[-2000:],
        "pytest_log": pytest_log,
        "pytest_failed": failed,
        "pytest_timeout_s": a.timeout_s,
        "pytest_progress": _progress(tail),
        "pytest_f_count": _marker_count(tail, "F"),
        "eval_rc": None,
        "eval_tail": "",
        "eval_skipped_reason": "",
        "elapsed_s": (datetime.datetime.now() - t0).total_seconds(),
        "python": sys.executable,
        # 2026-09-27（§1376）：这一跑**用的解释器可不可信**（`--status` 会读它 —— 写了要有人读）。
        "interpreter": _interpreter_block(),
        "head": _git_head(),
        "cwd": _TRINITY_ROOT,
        # 2026-09-24（§1319）：这一跑**测的是哪份代码**（路径指向开跑时落盘的清单）
        "tree_manifest": _manifest_path,
    }
    # §1313：**eval 段不再被 pytest 的红遮住** —— 只有「pytest 被超时杀掉」才跳过（并写明原因）。
    # 旧写法 `if timed_out or rc_code != 0: EVALS SKIPPED; return` 让 eval 的红整段消失（盲了两周）。
    _run_evals, _skip_reason = _eval_should_run(timed_out)
    if not _run_evals:
        rec["eval_skipped_reason"] = _skip_reason
        print("EVALS SKIPPED：%s" % _skip_reason)
    else:
        try:
            with open(_OUT + "_eval", "w", encoding="utf-8") as f:
                rc2 = subprocess.run(
                    [sys.executable, "-X", "utf8",
                     os.path.join(_TRINITY_ROOT, "scripts", "run_evals.py"), "--all"],
                    cwd=_TRINITY_ROOT, timeout=300, stdout=f, stderr=subprocess.STDOUT)
            eval_tail = open(_OUT + "_eval", encoding="utf-8", errors="replace").read()
            print(eval_tail[-400:])
            rec["eval_rc"] = rc2.returncode
            rec["eval_tail"] = eval_tail[-400:]
        except Exception as _e:  # noqa: BLE001 —— eval 段自己崩了也要留证，不许把整份证据带走
            rec["eval_skipped_reason"] = "eval 段异常：%r" % (_e,)
            print("[WARN] eval 段异常：%r" % (_e,))
    rec["verdict"] = _verdict(rec["pytest_rc"], rec["eval_rc"], timed_out)
    rec["elapsed_s"] = (datetime.datetime.now() - t0).total_seconds()
    per = _write_evidence(rec, rec["ts"])
    print("verdict = %s（pytest rc=%s ｜ eval rc=%s）" % (rec["verdict"], rec["pytest_rc"], rec["eval_rc"]))
    print("证据 -> %s ｜ %s" % (_EVIDENCE_LAST, os.path.basename(per)))
    return 0 if rec["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
