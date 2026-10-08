# routine-D1b：本文件属于**例行判据集**（`scripts/checklist_run.py` 的 D1b 会跑它）—— 删掉这行会让 D1b 判红（登记表与标记必须双向一致）
"""判据：本仓自有代码**不得有编译期 SyntaxWarning**（2026-09-19 §881）。

（节号更正，2026-09-23 §1296：本行原先把这一轮写成 **873 号**，而 873 号在本仓**不存在**
（`dsh-ops/EXECUTION-archive-20260920.md` 的编号在 872 号之后直接跳到 880 号）⇒ 引用无法解析。
按内容定位到真正那一轮：**§881**「2026-09-19 按建议执行（第六轮）：**编译告警清零** + 被仓门抓住的自身结构违规」，
这八处正是该轮 `compile()` 扫出来的清单。写这条更正时**不再写带节号符号的旧号**，
否则新加的 `.py` 扫描面（`scripts/xref_check.py`）会把这段更正本身判成一条新悬挂。）

## 为什么值得一条判据

实测（2026-09-19，用 `compile()` 权威判定而不是正则猜）：

    真实编译期 SyntaxWarning: **8 处**
      trinity/memory/dsh_events_source.py:272   'return' in a 'finally' block   ← 会吞掉在途异常
      trinity/retrieval/question_channel.py:13  "\\w" 非法转义（docstring）
      scripts/field_consumer_audit.py:44/49/50  "\\s" 非法转义（正则拼接里的非 raw 段）
      scripts/structure_gate.py:179             "\\s"（docstring）
      dsh-ops/_wal_econ_probe.py:112            "\\s"（SQL 串）
      tests/unit/test_guard_self_exclusion.py:8 "\\."（docstring 里的 Windows 路径）

**代价是真实的、不是洁癖**：这些告警会**混进每日指标产物**。实测
`~/.trinity/logs/metrics-20260919.out.log` 里就夹着 PowerShell 对这两条告警的
错误对象格式化输出（`NativeCommandError` 整段），把"当日六项指标"的读数**噪音化**
—— 而那个文件正是 W4/W5 一等指标的每日落数处。

## 修法（全部行为等价）

非法转义在 Python 里**本来就按字面量保留**（`"\\s"` == `r"\\s"`）⇒ 双写反斜杠即等价；
`field_consumer_audit` 的两条正则改完用**指纹比对**证明逐字节不变
（BEFORE/AFTER 都是 `b832171b36440756`）。`finally` 里的 `return` 改为
"关连接失败显式记录 + 统计照写 + return 移到 finally 之后"，三条可观察行为不变。

## 本文件判据

  ① 四个自有目录（trinity/scripts/dsh-ops/tests）里**没有任何** .py 产生 SyntaxWarning；
  ② 反向锁：判据必须能**真的抓到一个**已知坏样本（现场合成 `x = "\\s"`），否则"永远绿"。
"""
from __future__ import annotations

import os
import sys
import warnings

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIRS = ("trinity", "scripts", "dsh-ops", "tests")
SKIP_PARTS = ("__pycache__", ".git", "node_modules", "evidence")


def scan(root: str = ROOT) -> list:
    """返回 [(相对路径, 行号, 消息)]；只编译文本，不导入、不执行。"""
    out = []
    for d in DIRS:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dp, dn, fns in os.walk(base):
            if any(x in dp for x in SKIP_PARTS):
                continue
            for fn in fns:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dp, fn)
                try:
                    src = open(p, encoding="utf-8", errors="replace").read()
                except Exception:  # noqa: BLE001
                    continue
                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    try:
                        compile(src, p, "exec")
                    except Exception:  # noqa: BLE001 —— 语法错由别的判据管
                        continue
                for x in w:
                    if issubclass(x.category, SyntaxWarning):
                        out.append((p.replace(root + os.sep, ""), x.lineno, str(x.message)[:60]))
    return out


def test_no_syntax_warnings_in_own_code():
    """判据①：自有代码零编译期 SyntaxWarning。"""
    bad = scan()
    assert not bad, (
        "以下文件产生编译期 SyntaxWarning（会混进每日指标日志、且 finally-return 那类会吞异常）：\n"
        + "\n".join("  %s:%s  %s" % b for b in bad[:12]))


def test_scan_has_discriminating_power(tmp_path):
    """判据②反向锁：合成已知坏样本，scan 必须抓到它。

    2026-10-06（测试归因轮 T1）：原实现只用**非法转义**样本 `x = "\\s"`。
    该样本产生的告警**类别随解释器而变**：

        · CPython ≥ 3.12：非法转义升为 `SyntaxWarning`（本判据原来就是这么绿的）；
        · CPython ≤ 3.11（**本仓 .venv 与 CI 的版本**）：只报 `DeprecationWarning`
          ⇒ `hits == []` ⇒ 反向锁**必然假红**。

    更糟的是它会反过来污染判据①：在 3.11 上"零 SyntaxWarning"是**结构性**成立的
    （那条正则根本不进这个类别），少了一条能戳破假绿的线。故改为：

      ① 用**与版本无关**的坏样本（`is` 字面量比较，3.8 起即 SyntaxWarning）钉住
         scan 的捕捉能力 —— 这是这条反向锁真正要证明的东西（scan 抓得到 SyntaxWarning）；
      ② 只有当解释器确实把非法转义升为 SyntaxWarning（≥3.12）时，才追加断言非法转义
         样本也被抓到；否则**显式 skip 并说明**（不静默通过，也不假装通过）。
    """
    pkg = tmp_path / "trinity"
    pkg.mkdir()

    # ① 版本无关的坏样本：`is` 与字面量比较 ⇒ SyntaxWarning（3.8+，与解释器无关）
    vfree = pkg / "bad.py"
    vfree.write_text("x = 1\nif x is 1:\n    pass\n", encoding="utf-8")
    hits = scan(str(tmp_path))
    assert hits and hits[0][0].endswith("bad.py"), (
        "反向锁失效：scan 抓不到已知的 `is`-字面量 SyntaxWarning 样本 ⇒ 判据①是假绿：%r"
        % (hits,))

    # ② 非法转义样本：只在解释器会把它升为 SyntaxWarning 时才断言
    vfree.write_text("y = 2\n", encoding="utf-8")
    esc = pkg / "escape_bad.py"
    esc.write_text('z = "\\s"\n', encoding="utf-8")
    if sys.version_info >= (3, 12):
        hits2 = scan(str(tmp_path))
        assert any(h[0].endswith("escape_bad.py") for h in hits2), (
            "本解释器（%s）把非法转义算 SyntaxWarning，却没收进 scan：%r"
            % (sys.version.split()[0], hits2))
    else:
        pytest.skip("本解释器 %s 对非法转义只报 DeprecationWarning（3.12 起才是 "
                    "SyntaxWarning）⇒ 该样本在此无法证明判别力；已由 ① 的版本无关样本覆盖"
                    % sys.version.split()[0])
