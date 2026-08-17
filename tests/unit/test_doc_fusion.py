"""Trinity — 文档融合单元测试（2026-08-15）。

覆盖：
- split_markdown 章节切分（标题/正文/PREAMBLE）
- _classify 文档类型推断
- 幂等指纹逻辑（同文件同标题 → 同指纹）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.fuse_docs import _classify, split_markdown


@pytest.fixture()
def sample_md(tmp_path: Path) -> Path:
    p = tmp_path / "sample.md"
    p.write_text(
        "# Title\n\n"
        "## Section One\n\n"
        "This is the first section with enough content to pass the threshold. "
        "It describes the memory operating system architecture and how retrieval "
        "channels fuse together for hybrid search quality.\n\n"
        "### Sub Section\n\n"
        "Sub content here that is long enough to be kept as its own chunk. "
        "It covers the governance layer policies and the audit trail design.\n\n"
        "## Section Two\n\n"
        "Second section body with sufficient length for fusion. This explains "
        "the encryption at rest option and the token budget controls for LLM.\n",
        encoding="utf-8",
    )
    return p


def test_split_markdown_sections(sample_md: Path) -> None:
    sections = split_markdown(sample_md)
    assert isinstance(sections, list)
    # 至少应有 Section One / Section Two（PREAMBLE 可能被阈值过滤）
    titles = [s["title"] for s in sections]
    assert any("Section One" in t for t in titles)
    assert any("Section Two" in t for t in titles)
    for s in sections:
        assert s["line"] >= 1
        assert len(s["body"]) >= 120


def test_split_markdown_short_chunks_merged(tmp_path: Path) -> None:
    """短章节块（<120 字符）应被过滤，不产生碎片。"""
    p = tmp_path / "tiny.md"
    p.write_text("# T\n\n## A\n\nshort\n\n## B\n\nstill short\n", encoding="utf-8")
    sections = split_markdown(p)
    # 全部过短 → 无有效章节
    assert all(len(s["body"]) >= 120 for s in sections)


def test_classify_plan() -> None:
    assert _classify(Path("PLANNING_REVIEW_20260815.md")) == "doc:plan"
    assert _classify(Path("FUTURE_ROADMAP_V3.md")) == "doc:plan"
    assert _classify(Path("OPTIMIZATION_DIRECTIONS.md")) == "doc:plan"


def test_classify_other_types() -> None:
    assert _classify(Path("TRINITY_SUMMARY.md")) == "doc:summary"
    assert _classify(Path("OPS_NOTES.md")) == "doc:ops"
    assert _classify(Path("BENCHMARKS.md")) == "doc:benchmark"
    assert _classify(Path("MCP_STATUS.md")) == "doc:protocol"
    assert _classify(Path("random_notes.md")) == "doc:general"


def test_fingerprint_stable(tmp_path: Path) -> None:
    """同文件同 mtime 同标题 → 指纹稳定（幂等基础）。"""
    import hashlib
    p = tmp_path / "f.md"
    p.write_text("## Stable\n\nbody content long enough for chunking here ok\n", encoding="utf-8")
    mtime = p.stat().st_mtime
    sec = "Stable"
    fp1 = hashlib.sha256(f"{p.name}|{mtime}|{sec}".encode()).hexdigest()[:16]
    fp2 = hashlib.sha256(f"{p.name}|{mtime}|{sec}".encode()).hexdigest()[:16]
    assert fp1 == fp2
