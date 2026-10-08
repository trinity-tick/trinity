"""能力名实一致回归闸门（2026-09-30，外部审计 P0）。

审计发现的原状（本机实测）::

    /diagnostics: total_modules=122  但 modules_real=22      （100 个 range() 占位名）
                  guardian_levels=50 但 guardian_levels_enforcing=0
                  retrieval_channels=47 但 retrieval_channels_contributing=0

问题不在"有没有派生字段"，而在**被消费者读的那个 headline 键报的是名字数**：
`total_modules` 正是 MCP `trinity_diagnostics` 与 skills 当"模块数"读的键，
README / `pyproject.toml` 也读 `guardian_levels` / 检索通道数当能力宣称。

本文件把修复契约钉住：

1. `total_modules == modules_real`（占位名**已移除**，不是"标注在旁边"）；
2. `guardian_levels == guardian_levels_enforcing`、
   `retrieval_channels == retrieval_channels_contributing`
   （headline 键 = 能力口径；注册名册完整保留在 `*_declared` / `*_registered`）；
3. `engine_core.py` 里**不得再有** `range()` 批量生成占位名的写法；
4. 对外物不得再宣称 "50-Layer Guardian Chain" / "129-Paper"；
5. `trinity --version` 必须与 `trinity/version.py` 单一源一致；
6. README 不得再有**静态** tests 徽章断言不可复算的通过数。

运行：``python -m pytest tests/unit/test_capability_name_reality_20260930.py -q``
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def diag():
    from trinity.modules.second_brain.engine_core import SecondBrainV636

    return SecondBrainV636().run_diagnostics()


# ── 1. 模块口径：headline == 真实 ────────────────────────────────────────

def test_total_modules_equals_real_modules(diag: dict) -> None:
    assert diag["modules_generated_placeholder"] == 0, (
        "占位模块数应为 0 —— 100 个 range() 生成的 M1..M100 已在 2026-09-30 移除"
    )
    assert diag["total_modules"] == diag["modules_real"], (
        f"headline total_modules({diag['total_modules']}) 必须等于 modules_real"
        f"({diag['modules_real']})，否则又是「名字数当能力数」"
    )


def test_no_range_generated_placeholder_names_in_source() -> None:
    src = (ROOT / "trinity/modules/second_brain/engine_core.py").read_text(encoding="utf-8")
    # 允许注释里引用旧写法（留痕），但**不得有活的注册语句**
    live = [
        ln for ln in src.splitlines()
        if "range(" in ln and "self.modules[" in ln and not ln.lstrip().startswith("#")
    ]
    assert live == [], f"仍在用 range() 批量生成模块名: {live}"


def test_capability_surface_note_mentions_placeholder(diag: dict) -> None:
    """既有契约（test_selfcert_surface）要求口径说明点名 placeholder。"""
    assert "placeholder" in diag["capability_surface_note"]


# ── 2. 守护层 / 通道：headline == 能力口径，名册保留 ─────────────────────

def test_guardian_headline_is_enforcing_count(diag: dict) -> None:
    assert diag["guardian_levels"] == diag["guardian_levels_enforcing"], (
        f"guardian_levels({diag['guardian_levels']}) 必须等于 enforcing"
        f"({diag['guardian_levels_enforcing']})；申报名册另见 guardian_levels_declared"
    )
    assert diag["guardian_levels_enforcing"] == 0, "50 个名字当前无执行体 ⇒ 必须为 0"


def test_guardian_roster_preserved_for_traceability(diag: dict) -> None:
    """名册（含论文出处）不得被删 —— 它是设计声明，冻结契约要求 declared==50。"""
    assert diag["guardian_levels_declared"] == 50


def test_retrieval_headline_is_contributing_count(diag: dict) -> None:
    assert diag["retrieval_channels"] == diag["retrieval_channels_contributing"], (
        f"retrieval_channels({diag['retrieval_channels']}) 必须等于 contributing"
        f"({diag['retrieval_channels_contributing']})"
    )
    assert diag["retrieval_channels_registered"] == 47, "注册名册必须保留"


# ── 3. 对外物：不得再宣称已证伪的能力数 ─────────────────────────────────

def test_pyproject_has_no_refuted_capability_claims() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    body = re.sub(r"(?m)^\s*#.*$", "", text)          # 去掉注释（留痕允许）
    for bad in ("50-Layer Guardian Chain", "129-Paper", "v8.2.0"):
        assert bad not in body, f"pyproject 描述仍在宣称 {bad!r}"


def test_readme_has_no_static_test_count_badge() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    imgs = re.findall(r"<img[^>]*>", text)
    offenders = [i for i in imgs if re.search(r"tests?[-_]?\d|1261|815|passed", i, re.I)]
    assert offenders == [], (
        f"README 仍有静态 tests 徽章断言不可复算的通过数: {offenders}"
    )


def test_readme_cites_the_authoritative_capability_field() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "capability_roster" in text, (
        "通道口径必须引用 /health 的 capability_roster（唯一可引用为能力宣称的字段）"
    )


# ── 4. 版本单一源 ───────────────────────────────────────────────────────

def test_cli_version_follows_single_source() -> None:
    from trinity.version import __version__

    out = subprocess.run(
        [sys.executable, "-m", "trinity.cli", "--version"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )
    blob = (out.stdout + out.stderr)
    assert __version__ in blob, (
        f"--version 必须报 version.py 的 {__version__}；实测输出: {blob.strip()[:200]}"
    )
    assert "v6.36.0" not in blob, "仍在硬编码 6 个大版本之前的版本号"
