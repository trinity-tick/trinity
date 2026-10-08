# -*- coding: utf-8 -*-
r"""G20（t163）：**行尾守护**（先加守护，再修）

## 为什么要有它（本仓已实测三次）

· **t135**：`dsh-ops/trinity-dsh-maintenance.ps1` 被某个 CRLF 无感工具**整文件转成 CRLF** ⇒
  判据红；且该缺陷**对 `git` 隐形**（`core.autocrlf=true` 会把工作树 CRLF 归一 ⇒ `git diff` 看不见）。
· **t139/t141**：`i/lf w/crlf` 是 **autocrlf 的设计行为**（1357 个文件）⇒ ⭐ **不能当缺陷**；
  真正有害的是 **"混行尾"**（同一文件里既有 CRLF 又有孤立 LF）。
· **t155**：**同一目录三种行尾**（`adapters/sqlite/_search.py` LF · `_hybrid_search.py` LF ·
  `client/_search.py` CRLF）⇒ 行尾在本仓**真的有人踩**。

## 本判据守住四件事

① `dsh-ops/trinity-dsh-maintenance.ps1` **必须是纯 LF**（AGENTS §11/§989 的既有事实；**并有 BOM**）；
② `AGENTS.md` **若声明了 `eol=lf`**（`git check-attr eol`）⇒ 工作树**必须真是 LF**；
③ ⭐ **全仓"混行尾"不得超过已知基线**（棘轮；基线 = **2026-10-08 14:02:35 实测的 26 个**，见下）；
④ ⭐ **口径要分得开**：「纯 CRLF」与「纯 LF」**都不算混**（`trinity-supervisor.ps1` 是 CRLF 契约、
   `trinity-dsh-maintenance.ps1` 是 LF 契约 ⇒ 两者都**不得**出现在混行尾清单里）。

⭐ **仪器**：混行尾清单用 **`git ls-files --eol`** 的 `w/mixed`（git 自己的口径，会排除二进制）。
⚠️ 字节级扫描会多报两类**假阳性**（已实测）：**二进制**（`.npz` 里随机 CR/LF 字节）与
**裸 CR 进度行**（pytest 输出的 `\r` 不带 `\n`）⇒ 故**不以字节扫描作权威清单**。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOM = b"\xef\xbb\xbf"

#: ⭐ 棘轮基线：**2026-10-08 14:02:35 实测**（`git ls-files --eol` 的 `w/mixed`，共 26 个）
#: 允许**减少**（修好了就少一个），**新增必须红**。
MIXED_BASELINE_AT = "2026-10-08 14:02:35"
MIXED_BASELINE = (
    "docs/AUTO_EVOLVE_OBSERVATION.md",
    "docs/MECH_FLOW_20260914.md",
    "docs/OPSBOT_REVIEW_0909_preview.md",
    "docs/RAG_SERVICE_20260827.md",
    "docs/SCALE_REPORT_20260909.md",
    "docs/SCORES_20260908.md",
    "docs/TUNE_OBSERVATION_20260828.md",
    "dsh-ops/AUDIT-CHECKLIST.md",
    "dsh-ops/COMMIT_MSG_P0-0.txt",
    "dsh-ops/COMMIT_MSG_P0-2-fix.txt",
    "dsh-ops/COMMIT_MSG_P0-2.txt",
    "dsh-ops/EXECUTION-archive-202609.md.snap-20260923-1327",
    "dsh-ops/EXECUTION.md.bak-rotate-20260914-135156",
    "dsh-ops/evidence/p0_0_gates.txt",
    "dsh-ops/evidence/p0_r1_evidence.txt",
    "dsh-ops/evidence/p0_r1_gates.txt",
    "dsh-ops/evidence/p1_0_preexisting_failures.txt",
    "dsh-ops/evidence/p1_1_s1_red.txt",
    "dsh-ops/evidence/winprobe/unreg-winprobe.ps1",
    "dsh-ops/trinity-autostart.ps1.bak-20260911-healthrc",
    "dsh-ops/trinity-autostart.ps1.bak-20260911-healthsnap",
    "dsh-ops/trinity-autostart.ps1.bak-20260911-maintadit",
    "dsh-ops/trinity-autostart.ps1.bak-20260911-r3",
    "scripts/run_all_self_tests.py.bak-20260914-flakediag",
    "trinity/api/server/_routers_cognition.py",
    "trinity/api/server/_routers_recall.py",
)

#: 两条**行尾契约**（§11/§989 + `tests/test_ops_layer_20260901.py` 的参数表）
CONTRACT_LF = ROOT / "dsh-ops" / "trinity-dsh-maintenance.ps1"
CONTRACT_CRLF = ROOT / "dsh-ops" / "trinity-supervisor.ps1"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True
                          ).stdout.decode("utf-8", "replace")


def eol_kind(raw: bytes) -> str:
    """字节级行尾形态：MIXED / CRLF / LF / NONE。

    ⭐ **只把"同一文件里既有 CRLF 又有孤立 LF"（或含孤立 CR）算 MIXED** ——
    纯 CRLF 与纯 LF **都不算混**（这正是 t139 的口径教训）。
    """
    crlf = raw.count(b"\r\n")
    lone_cr = raw.count(b"\r") - crlf
    lone_lf = raw.count(b"\n") - crlf
    if lone_cr or (crlf and lone_lf):
        return "MIXED"
    return "CRLF" if crlf else ("LF" if lone_lf else "NONE")


def mixed_set() -> set:
    """当前工作树的**混行尾**文件集合（权威口径：`git ls-files --eol` 的 `w/mixed`）。"""
    out = set()
    for line in _git("ls-files", "--eol").splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and parts[1] == "w/mixed":
            out.add(parts[3] if len(parts) > 3 else "")
    return out


def declared_eol(rel: str) -> str:
    """`git check-attr eol -- <rel>` 的声明值（`unspecified` 表示没声明）。"""
    m = re.search(r":\s*eol:\s*(\S+)", _git("check-attr", "eol", "--", rel))
    return m.group(1) if m else "unspecified"


# ── ① 契约：dsh-ops/trinity-dsh-maintenance.ps1 必须纯 LF（且有 BOM）──────────

def test_维护脚本必须是纯_LF_且有_BOM() -> None:
    raw = CONTRACT_LF.read_bytes()
    assert raw[:3] == BOM, "§11：该 .ps1 必须带 UTF-8 BOM（PS5.1 否则按 ANSI 读）"
    assert raw.count(b"\r") == 0, (
        "§11/§989：该文件必须**纯 LF**（实测 CR 计数 %d）——被 CRLF 无感的工具改过"
        % raw.count(b"\r"))


def test_对照组_supervisor_是纯_CRLF_契约() -> None:
    """反向：§11 登记的另一侧（CRLF）不得被"顺手改成 LF"（它不是缺陷）。"""
    raw = CONTRACT_CRLF.read_bytes()
    assert raw.count(b"\r\n") > 0, "§11：supervisor.ps1 是 CRLF 契约，不该变成 LF"


# ── ② AGENTS.md：声明了 eol=lf 就必须真是 LF ────────────────────────────────

def test_AGENTS_md_若声明_eol_lf_则必须是_LF() -> None:
    rel = "AGENTS.md"
    declared = declared_eol(rel)
    raw = (ROOT / rel).read_bytes()
    if declared == "lf":
        assert raw.count(b"\r") == 0, (
            "已声明 eol=lf，但工作树里仍有 CR（%d 个）⇒ 声明没生效"
            % raw.count(b"\r"))
    assert eol_kind(raw) in ("LF", "CRLF", "MIXED"), "AGENTS.md 形态异常：%s" % eol_kind(raw)


# ── ③ 棘轮：全仓混行尾 ⊆ 基线（新增必须红）──────────────────────────────────

def test_混行尾不得超过基线() -> None:
    cur = mixed_set()
    new = cur - set(MIXED_BASELINE)
    assert not new, (
        "出现**新增的混行尾**文件（基线取自 %s）：\n  %s"
        % (MIXED_BASELINE_AT, "\n  ".join(sorted(new))))


def test_混行尾数量只降不升() -> None:
    cur = mixed_set()
    assert len(cur) <= len(MIXED_BASELINE), (
        "混行尾从基线 %d 个涨到 %d 个（棘轮只许降）"
        % (len(MIXED_BASELINE), len(cur)))


# ── ④ 口径分辨：纯 CRLF / 纯 LF 都不算"混"（前后两条契约文件都不该在清单里）──

def test_纯_CRLF_与纯_LF_都不算混() -> None:
    cur = mixed_set()
    assert str(CONTRACT_CRLF.relative_to(ROOT)).replace("\\", "/") not in cur, (
        "纯 CRLF 文件被误报成混行尾 ⇒ 口径没分开（t139 的教训）")
    assert str(CONTRACT_LF.relative_to(ROOT)).replace("\\", "/") not in cur, (
        "纯 LF 文件被误报成混行尾 ⇒ 口径没分开")


# ── ③ 的牙齿：把一段"人造混行尾"喂给分类器 ⇒ 必须报 MIXED ────────────────────

def test_牙齿_人造混行尾必须被识别() -> None:
    assert eol_kind(b"a\r\nb\nc\r\n") == "MIXED", "CRLF + 孤立 LF 必须判 MIXED"
    assert eol_kind(b"a\rb\r\n") == "MIXED", "孤立 CR 也必须判 MIXED"
    # 对照：纯的两种都不得判 MIXED（否则"混"这个口径没有判别力）
    assert eol_kind(b"a\r\nb\r\n") == "CRLF"
    assert eol_kind(b"a\nb\n") == "LF"
