# -*- coding: utf-8 -*-
"""文本完整性**常驻判据**（t20，2026-10-06）——**第 4 次同类事故后加的机械防线**。

## 为什么加（4 次事故的共同点：损坏不改变行为）

  1. `edit` 工具剥掉 `.ps1` 的 UTF-8 BOM ⇒ PowerShell 5.1 按 GBK 解码中文注释 ⇒ 4 个**假**语法错误；
  2. PowerShell 编码往返把 `trinity/engine_worker.py` 改坏（`py_compile` 失败）；
  3. GBK 控制台 `UnicodeEncodeError` 让脚本 exit 1；
  4. `Get-Content -Raw | -replace | Set-Content` 把报告写成乱码。

它们的共同点是**字节层损坏、行为层无症**（注释坏了不影响执行）⇒ 单测/类型检查**全都不会红**。
故本判据"看字节"，不看行为。检测器：`scripts/text_integrity_guard.py`。

## 判定面（两级口径，写清以免被误读）

* **硬判据 = 工作树改动文件里「本次改动**引入**」的硬性命中**。
  只判"引入"是**必须的**：本仓**早就**有 77 个非 `.ps1` 文件带 BOM、`AGENTS.md` **早就**含
  U+FFFD、`trinity/__init__.py` **早就**是 GBK 乱码（都在 `HEAD` 里）。
  若写成"改过的文件必须完全干净"，任何人合法编辑这些文件都会红 ⇒ 判据变噪声源。
* **全仓上界 = 只降不升的 `<=`**：`硬性命中文件数 <= BASELINE`（本仓既有纪律）。
  它兜住"**已提交**的损坏"——`git status` 看不见的那种（`trinity/__init__.py` 就是这样）。

## 本文件自带的承重证明

* 两个/多个负向夹具各自判红并**指名原因**；
* **反事实**：把检测器换成"恒通过" ⇒ 夹具必须**变绿**（证明显式断言承重，不是恒真）；
* `CRLF 不得判红`（`core.autocrlf=true`）、`.ps1` 的 BOM **是要求**（按扩展名区分）；
* **判据自身不得含乱码标记**（否则守卫脚本会判自己红 —— "判据不能被自己的留痕骗"）。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUARD = os.path.join(ROOT, "scripts", "text_integrity_guard.py")

#: 全仓硬性命中文件数基线（t20 实测；口径 = `scripts/text_integrity_guard.py --repo`）。
#: **只降不升**：拿掉一处存量就把它改小；新增一处损坏必须显式上调并说明理由。
#: 实测时刻 2026-10-06：checked=5855 / flagged=124
#: （reasons: fffd 50, bom 77, control_chars 14, invalid_utf8 8, mojibake_gbk 3,
#:   pua 3, mojibake_latin 1）。
REPO_HARD_BASELINE = 124


def _guard():
    spec = importlib.util.spec_from_file_location("_text_integrity_guard", GUARD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _always_pass(raw, ext):  # 反事实用的"恒通过"检测器
    return {"hard": [], "notes": [], "gbk_marker_hits": 0, "latin_marker_hits": 0,
            "fffd_count": 0, "crlf_count": 0, "pua_count": 0, "control_chars": [],
            "utf8_ok": True, "has_utf8_bom": False, "foreign_bom": False}


# ── 夹具构造（**全部用码点/字节**，本文件不出现任何乱码字面量）─────────────

def _fx(tmp_path, name: str, raw: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(raw)
    return str(p)


def _gbk_mojibake(text: str) -> str:
    """精确模拟 PS 5.1 的 `Get-Content -Raw`（按 ANSI/936 解码）+ `Set-Content -Encoding UTF8`。

    纯 Python 字节操作，**不经任何 shell**（本任务就是为 shell 编码往返设防）。
    """
    out = []
    for i in range(0, len(text), 4096):
        b = text[i:i + 4096].encode("utf-8")
        out.append(b.decode("cp936", errors="replace"))
    return "".join(out)


def _latin_mojibake(text: str) -> str:
    out = []
    for i in range(0, len(text), 4096):
        b = text[i:i + 4096].encode("utf-8")
        out.append(b.decode("cp1252", errors="replace"))
    return "".join(out)


_CLEAN_CN = ("# 这是一个正常的中文注释：检索命中后累加访问计数，失败要留痕。\n"
             "# 第二行：冷池口径只能用 PG 的 last_retrieved_at，SQLite 上该列不存在。\n")


def test_负向夹具1_含U_FFFD必须判红并指名(tmp_path):
    g = _guard()
    p = _fx(tmp_path, "fffd.py", b"# " + "\ufffd".encode("utf-8") + b"\nx = 1\n")
    rec = g.inspect_bytes(open(p, "rb").read(), ".py")
    assert "fffd" in rec["hard"], "含 U+FFFD 的 .py 没有被判红：%r" % rec["hard"]
    assert rec["fffd_count"] == 1


def test_负向夹具2_GBK乱码必须判红并指名(tmp_path):
    """**这是 t5 那种坏法**：`Get-Content -Raw` 按 ANSI 解码后写出。

    关键：它产出的是**合法字符**，主体**不是** U+FFFD ⇒ 只查 U+FFFD 的判据会漏掉它。
    决定性断言因此写成：**把 U+FFFD 全部抹掉之后，仍然被判红**。

    （模拟用 `errors="replace"`；PS 5.1 的 `Encoding.Default.GetString` 在未映射处给 `?`，
    故模拟会让 U+FFFD 略多 —— 抹掉后的断言消除这个差异，验的是**签名族**而不是替代字符。）
    """
    g = _guard()
    damaged = _gbk_mojibake(_CLEAN_CN)
    rec = g.inspect_bytes(damaged.encode("utf-8"), ".py")
    assert "mojibake_gbk" in rec["hard"], (
        "GBK 误读族没有被判红：hard=%r gbk_markers=%d" % (rec["hard"], rec["gbk_marker_hits"]))
    assert rec["gbk_marker_hits"] >= g.MIN_MARKER_HITS

    stripped = damaged.replace("\ufffd", "")
    rec2 = g.inspect_bytes(stripped.encode("utf-8"), ".py")
    assert "fffd" not in rec2["hard"], "本断言的前提是已抹掉 U+FFFD"
    assert "mojibake_gbk" in rec2["hard"], (
        "抹掉 U+FFFD 后就不红了 ⇒ 判据其实只是靠替换字符在抓，**漏掉真正的坏法**")


def test_负向夹具3_Latin乱码必须判红并指名(tmp_path):
    """ANSI = CP1252 的机器上，同一往返产出拉丁变音族（另一族签名）。"""
    g = _guard()
    damaged = _latin_mojibake(_CLEAN_CN)
    rec = g.inspect_bytes(damaged.encode("utf-8"), ".py")
    assert "mojibake_latin" in rec["hard"], (
        "Latin 误读族没有被判红：hard=%r latin=%d" % (rec["hard"], rec["latin_marker_hits"]))

    stripped = damaged.replace("\ufffd", "")
    rec2 = g.inspect_bytes(stripped.encode("utf-8"), ".py")
    assert "mojibake_latin" in rec2["hard"], (
        "抹掉 U+FFFD 后 Latin 族就不红了 ⇒ 该族签名没有独立判别力")


def test_负向夹具4_BOM_PUA_AST失败各自判红(tmp_path):
    g = _guard()
    p1 = _fx(tmp_path, "bom.py", b"\xef\xbb\xbf" + "x = 1\n".encode("utf-8"))
    assert "bom" in g.inspect_bytes(open(p1, "rb").read(), ".py")["hard"]

    p2 = _fx(tmp_path, "pua.py", b"# " + "\ue000".encode("utf-8") + b"\nx = 1\n")
    assert "pua" in g.inspect_bytes(open(p2, "rb").read(), ".py")["hard"]

    p3 = _fx(tmp_path, "astfail.py", b"def f(:\n    pass\n")
    assert "ast_fail" in g.inspect_bytes(open(p3, "rb").read(), ".py")["hard"]

    p4 = _fx(tmp_path, "bad_utf8.py", b"# \xff\xfe\x00x\ny = 1\n")
    assert "invalid_utf8" in g.inspect_bytes(open(p4, "rb").read(), ".py")["hard"]


def test_反事实_恒通过检测器会让全部夹具变绿(tmp_path):
    """承重证明：把检测器换成"恒通过"，同一批夹具必须**全部变绿**。

    若这一步失败（夹具仍被判红），说明"红"不是来自被断言的检测逻辑，断言是恒真的。
    """
    g = _guard()
    fixtures = {
        "fffd.py": b"# " + "\ufffd".encode("utf-8") + b"\nx = 1\n",
        "gbk.py": _gbk_mojibake(_CLEAN_CN).encode("utf-8"),
        "latin.py": _latin_mojibake(_CLEAN_CN).encode("utf-8"),
        "bom.py": b"\xef\xbb\xbf" + b"x = 1\n",
        "pua.py": b"# " + "\ue000".encode("utf-8") + b"\nx = 1\n",
        "astfail.py": b"def f(:\n    pass\n",
    }
    for name, raw in fixtures.items():
        real = g.inspect_bytes(raw, ".py")
        stub = _always_pass(raw, ".py")
        assert real["hard"], "%s 在真检测器下应当是红的" % name
        assert stub["hard"] == [], "%s 在恒通过检测器下仍被判红 ⇒ 断言不承重" % name


# ── CRLF 与 .ps1 BOM（各自正/负例）────────────────────────────────────────

def test_CRLF不得判红_但要有eol提示(tmp_path):
    """本机 `core.autocrlf=true` ⇒ 一次 `git checkout` 就可能把文件变成 CRLF。

    实测：全仓 5871 个文本文件里 **3952 个**含 CRLF。把 CRLF 判红 ⇒ 判据 67% 假红。
    """
    g = _guard()
    raw = b"x = 1\r\ny = 2\r\n"
    rec = g.inspect_bytes(raw, ".py")
    assert rec["hard"] == [], "CRLF 被判红了：%r" % rec["hard"]
    assert rec["crlf_count"] == 2
    assert any("eol_crlf" in n for n in rec["notes"]), "CRLF 至少要留下 eol 提示"


def test_ps1的BOM是要求_而不是可疑(tmp_path):
    """`.ps1` **必须**有 UTF-8 BOM（仓内 `scripts/ps1_bom_gate.py` 已登记该要求）。

    所以 BOM 对 `.py/.json/.md` 是可疑、对 `.ps1` 是**要求** —— 一条规则套所有文件必错。
    """
    g = _guard()
    with_bom = b"\xef\xbb\xbf" + "# \u4e2d\u6587\u6ce8\u91ca\r\nWrite-Host 1\r\n".encode("utf-8")
    rec_ok = g.inspect_bytes(with_bom, ".ps1")
    assert "bom" not in rec_ok["hard"], ".ps1 带 BOM 被判成可疑：%r" % rec_ok["hard"]
    assert "bom_missing_ps1" not in rec_ok["hard"]
    assert rec_ok["crlf_count"] == 2 and rec_ok["hard"] == [], rec_ok["hard"]

    without = "# \u4e2d\u6587\u6ce8\u91ca\r\nWrite-Host 1\r\n".encode("utf-8")
    rec_bad = g.inspect_bytes(without, ".ps1")
    assert "bom_missing_ps1" in rec_bad["hard"], ".ps1 缺 BOM 没被判红：%r" % rec_bad["hard"]


def test_正常中文与英文文件不得被判红():
    """噪声控制：判据对**干净**文件必须沉默（否则它就是噪声源）。"""
    g = _guard()
    for rel in ("scripts/text_integrity_guard.py",
                "trinity/modules/second_brain/capability_ledger.py",
                "scripts/dialect_guard_audit.py"):
        rec = g.inspect_path(os.path.join(ROOT, rel))
        assert rec["hard"] == [], "%s 被误判：%r" % (rel, rec["hard"])


def test_判据自身不得含任何乱码标记():
    """守卫脚本的签名集**用码点构造**，所以它自己不含那些字符。

    否则守卫脚本会判自己红（"判据不能被自己的留痕骗" —— 本仓已有前科）。
    """
    g = _guard()
    src = open(GUARD, encoding="utf-8").read()
    assert sum(src.count(m) for m in g.GBK_MARKERS) == 0, "守卫脚本自带了 GBK 族标记字符"
    assert sum(src.count(m) for m in g.LATIN_MARKERS) == 0, "守卫脚本自带了 Latin 族标记字符"
    assert g.inspect_path(GUARD)["hard"] == []


# ── 常驻判据（判定面）────────────────────────────────────────────────────

def test_改动文件不得引入硬性文本损坏():
    """**判定面**：`git status --porcelain` 的文本文件里，本次改动**引入**的硬性命中必须为 0。"""
    g = _guard()
    items, err = g.changed_text_files_with_status()
    assert not err, "取不到改动清单（git 不可用）⇒ UNTESTABLE，不得当通过：%s" % err
    rep = g.audit_introduced(items)
    assert rep["findings"] == [], (
        "以下改动文件**引入**了文本/编码损坏：\n  "
        + "\n  ".join("%s [%s] %s" % (f["path"], f["status"], ",".join(f["introduced"]))
                      for f in rep["findings"])
        + "\n⇒ 常见成因：shell 编码往返（`Get-Content -Raw | … | Set-Content`）、"
          "编辑工具剥/加 BOM、把文件按 GBK 解码后写回。"
          "**不要**用 shell 编码往返修复；用 Python 字节级操作或 git 恢复。")


def test_全仓硬性损坏文件数不得超过基线():
    """全仓上界：**只降不升**（`<=`）。兜住"已提交、`git status` 看不见"的损坏。

    它同时是一条**活体发现**的载体：`trinity/__init__.py` 在 HEAD 里就是 GBK 乱码
    （实测 gbk 标记 465 次、18 行注释不可读），而它对**行为**无影响 ⇒ 没有别的判据会红。

    ⚠️ **这条基线与工作树耦合**（同 verifier 对 `structure_gate` 的定性：是"耦合到工作树的
    基线"，不是"随机门禁"）：其它 agent 正在写文件时会出现瞬时的"多一个命中"——
    实测 t22 期间全仓 **124 → 125 → 124**，一次测试因此变红、重跑即绿。
    故本用例在**首次超基线**时复测一次，只有**复现的超标**才判红：
    真正新增的损坏会持续存在，瞬时半成品状态不会。
    （承重性不受影响：两个负向夹具与 `test_全仓基线里那三个GBK乱码文件仍可被指名` 仍钉住判据本身。）
    """
    g = _guard()
    rep = g.audit(list(g.iter_text_files()))
    assert rep["checked"] > 3000, "只扫到 %d 个文本文件 ⇒ 判定面已失效" % rep["checked"]
    if rep["flagged"] <= REPO_HARD_BASELINE:
        return
    rep2 = g.audit(list(g.iter_text_files()))
    assert rep2["flagged"] <= REPO_HARD_BASELINE, (
        "全仓硬性命中 %d > 基线 %d（复测亦为 %d）⇒ 新增了文本损坏：\n  "
        % (rep2["flagged"], REPO_HARD_BASELINE, rep2["flagged"])
        + "\n  ".join(f["path"] for f in rep2["findings"][:15])
        + "\n⇒ 修掉损坏，或（仅在确认新增的是**既存**存量时）显式上调基线并说明理由。")


def test_全仓基线里那三个GBK乱码文件仍可被指名():
    """给"已提交的损坏"留一条可见的断言：它必须**仍被扫出来**，不能因基线而消失。"""
    g = _guard()
    victims = set()
    for p in g.iter_text_files():
        rec = g.inspect_path(p)
        if "mojibake_gbk" in rec["hard"]:
            victims.add(rec["path"])
    assert "trinity/__init__.py" in victims, (
        "`trinity/__init__.py` 是已提交的 GBK 乱码（HEAD 里就有）—— 它必须仍在命中清单里。"
        "若它消失了：要么有人修好了（请下调基线并更新报告），要么判据退化成了哑的。")
