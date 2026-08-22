"""Unit tests: decay 压缩接真实 LLM（scripts/run_decay_compress.py）.

覆盖：
  - auto 模式 key 存在性解析（TRINITY_DECAY_API_KEY / TRINITY_API_KEY）
  - 显式 real 无 key 降级 mock、不崩溃
  - 注入假 LLM 的四种输入（JSON / 纯文本 / 截断 JSON / 抛异常）
  - prompt 含事实/时间/数值保留指令
  - dry-run 不写库
  - 摘要生成可注入 _llm_summarize（monkeypatch，绝不真调外部 API）
说明：本文件直接加载 scripts/run_decay_compress.py（非包，用 importlib）。
"""

import importlib.util
import os
import sys

import pytest

_SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "run_decay_compress.py")
)


@pytest.fixture
def mod():
    """加载 scripts/run_decay_compress.py 为模块对象。"""
    spec = importlib.util.spec_from_file_location("run_decay_compress_under_test", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    assert spec.loader is not None
    spec.loader.exec_module(m)
    return m


def _real_cfg(mod, model="deepseek-chat"):
    """构造 real 模式 cfg（带假 key）。"""
    return {
        "mode": "real",
        "api_key": "test-decay-key",
        "base_url": "https://api.deepseek.com/v1",
        "model": model,
    }


# ── 无 key 时 auto→mock ──────────────────────────────────────────────

class TestAutoResolution:
    def test_auto_no_key_resolves_mock(self, mod, monkeypatch):
        monkeypatch.delenv("TRINITY_DECAY_API_KEY", raising=False)
        monkeypatch.delenv("TRINITY_API_KEY", raising=False)
        assert mod._resolve_llm_mode("auto") == "mock"

    def test_auto_with_decay_key_resolves_real(self, mod, monkeypatch):
        monkeypatch.setenv("TRINITY_DECAY_API_KEY", "k")
        assert mod._resolve_llm_mode("auto") == "real"

    def test_auto_with_trinity_api_key_resolves_real(self, mod, monkeypatch):
        monkeypatch.delenv("TRINITY_DECAY_API_KEY", raising=False)
        monkeypatch.setenv("TRINITY_API_KEY", "k")
        assert mod._resolve_llm_mode("auto") == "real"

    def test_explicit_mock_forces_mock(self, mod, monkeypatch):
        monkeypatch.setenv("TRINITY_DECAY_API_KEY", "k")
        assert mod._resolve_llm_mode("mock") == "mock"

    def test_explicit_real_forces_real_even_without_key(self, mod, monkeypatch):
        monkeypatch.delenv("TRINITY_DECAY_API_KEY", raising=False)
        monkeypatch.delenv("TRINITY_API_KEY", raising=False)
        assert mod._resolve_llm_mode("real") == "real"


# ── 显式 real 无 key 降级 mock，不崩 ──────────────────────────────────

class TestExplicitRealNoKey:
    def test_explicit_real_without_key_degrades_to_mock(self, mod, monkeypatch):
        cfg = {
            "mode": "real",
            "api_key": None,  # 无 key
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
        }
        out = mod._llm_summarize(["fact A", "fact B"], cfg)
        assert out.startswith("[AUTO-COMPRESSED]")


# ── 注入假 LLM 的摘要生成 / 降级 ──────────────────────────────────────

class TestLlmSummarize:
    def test_json_summary_parsed(self, mod, monkeypatch):
        monkeypatch.setattr(
            mod,
            "_llm_chat",
            lambda sysp, user, cfg: '{"summary": "这是结构化中文摘要。"}',
        )
        out = mod._llm_summarize(["m1", "m2"], _real_cfg(mod))
        assert out == "这是结构化中文摘要。"

    def test_plain_text_used_directly(self, mod, monkeypatch):
        monkeypatch.setattr(
            mod,
            "_llm_chat",
            lambda sysp, user, cfg: "第一行\n第二行（保留 2026-08-18）",
        )
        out = mod._llm_summarize(["m1"], _real_cfg(mod))
        assert out == "第一行\n第二行（保留 2026-08-18）"

    def test_truncated_json_falls_back_to_raw_text(self, mod, monkeypatch):
        # 截断的 JSON（缺右花括号），解析容错失败后整段文本降级
        truncated = '{"summary": "未完成'
        monkeypatch.setattr(mod, "_llm_chat", lambda *a: truncated)
        out = mod._llm_summarize(["m1", "m2"], _real_cfg(mod))
        # 截断 JSON 无法解析出 summary → 使用原始文本（含内容）
        assert "未完成" in out

    def test_exception_degrades_to_mock(self, mod, monkeypatch):
        def boom(system, user, cfg):
            raise RuntimeError("network down")

        monkeypatch.setattr(mod, "_llm_chat", boom)
        out = mod._llm_summarize(["m1", "m2"], _real_cfg(mod))
        assert out.startswith("[AUTO-COMPRESSED]")
        assert "2 memories merged" in out

    def test_empty_response_degrades_to_mock(self, mod, monkeypatch):
        monkeypatch.setattr(
            mod,
            "_llm_chat",
            lambda sysp, user, cfg: "   ",
        )
        out = mod._llm_summarize(["m1"], _real_cfg(mod))
        assert out.startswith("[AUTO-COMPRESSED]")

    def test_mock_mode_returns_extractive_summary(self, mod):
        cfg = {"mode": "mock", "api_key": None}
        out = mod._llm_summarize(["内容甲", "内容乙"], cfg)
        assert out.startswith("[AUTO-COMPRESSED]")
        assert "2 memories merged" in out


# ── prompt 构建含事实/时间/数值保留指令 ────────────────────────────────

class TestPrompt:
    def test_prompt_contains_preserve_instructions(self, mod):
        for kw in ("事实", "时间", "数值", "编造"):
            assert kw in mod._DECAY_SUMMARY_SYSTEM_PROMPT

    def test_build_user_prompt_lists_entries(self, mod):
        p = mod._build_summary_user_prompt(["A", "B"], memory_type="handoff")
        assert "handoff" in p
        assert "[1] A" in p
        assert "[2] B" in p

    def test_llm_summarize_prompt_passed_through(self, mod, monkeypatch):
        captured = {}

        def spy(system, user, cfg):
            captured["system"] = system
            captured["user"] = user
            return '{"summary":"s"}'

        monkeypatch.setattr(mod, "_llm_chat", spy)
        mod._llm_summarize(["事实X 2026-08-18 金额100元"], _real_cfg(mod))
        assert "事实" in captured["system"] and "时间" in captured["system"]
        assert "2026-08-18" in captured["user"] or "事实X" in captured["user"]


# ── 可见注入形态：_make_llm_callable real 走 _llm_summarize，mock 走原 ctor ─

class TestMakeCallable:
    def test_real_callable_routes_to_summarize(self, mod, monkeypatch):
        sent = []
        fake_sum = lambda texts, cfg: sent.append((texts, cfg)) or "摘要"

        def spy_ctor(system_prompt, user_prompt):  # noqa: ARG001
            return "not used"

        call = mod._make_llm_callable(
            fake_sum, {"mode": "real", "api_key": "k"}, spy_ctor
        )
        # 传入参数无关的 prompt；内容从 entries 里抽出
        out = call("sys", "[1] 记忆一\n[2] 记忆二")
        assert out == "摘要"
        assert sent[0][0] == ["记忆一", "记忆二"]
        assert sent[0][1]["mode"] == "real"

    def test_mock_mode_returns_mock_ctor(self, mod):
        ctor_called = {}

        def mock_ctor(system_prompt, user_prompt):
            ctor_called["x"] = True
            return "mock-out"

        call = mod._make_llm_callable(
            lambda texts, cfg: "sum", {"mode": "mock"}, mock_ctor
        )
        assert call("s", "u") == "mock-out"
        assert ctor_called


# ── dry-run 不写库 ────────────────────────────────────────────────────

class TestDryRunNoWrite:
    def test_dry_run_does_not_compress_or_write(self, mod):
        # 用假 adapter/engine/compressor：dry-run 必须早退，compressor 不被调用。
        fake_engine = _make_engine(3)
        exploding_compressor = _ExplodingCompressor()

        stats = mod.run_decay_compress(
            adapter=_FakeAdapter(rows=3),
            engine=fake_engine,
            compressor=exploding_compressor,
            dry_run=True,
            limit=10,
            store="sqlite",
        )
        assert stats["dry_run"] is True
        assert stats["compressed_summaries"] == 0
        assert stats["compression_batches"] == 0
        assert exploding_compressor.called == 0


# ── 辅助 fake ─────────────────────────────────────────────────────────

class _ExplodingCompressor:
    """若被调用即失败——dry-run 不应触发压缩。"""

    def __init__(self):
        self.called = 0

    def compress_batch(self, *a, **kw):
        self.called += 1
        raise AssertionError("compress_batch 不应在 dry-run 中被调用")


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=()):
        return self

    def fetchall(self):
        return [
            {
                "memory_id": f"m{i}",
                "session_id": "s",
                "persona_id": "p",
                "tenant_id": "t",
                "content": f"记忆内容{i}",
                "role": "user",
                "importance": 0.1,
                "tags": "[]",
                "category": "general",
                "sha256_hash": "h",
                "status": "active",
                "version": 1,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "access_count": 0,
                "last_accessed_at": None,
            }
            for i in range(self._rows)
        ]


class _FakeAdapter:
    def __init__(self, rows):
        self._conn = _FakeConn(rows)


def _make_engine(pending_count):
    """构造最小 MemoryDecayEngine 替身，返回含 pending 扫描报告的引擎。"""
    from trinity.daemon.memory_decay import (
        DecayScanReport,
        DecayStatus,
        DecayResult,
        DecayConfig,
    )

    class _Engine:
        def __init__(self):
            self.config = DecayConfig()

        def scan_memories(self, memories):
            results = [
                DecayResult(
                    memory_id=f"m{i}",
                    memory_type="general",
                    importance=0.1,
                    decay_lambda=0.02,
                    days_since_creation=100.0,
                    decay_score=0.05,
                    status=DecayStatus.PENDING_COMPRESSION,
                    created_at="2026-01-01T00:00:00+00:00",
                )
                for i in range(pending_count)
            ]
            return DecayScanReport(
                scan_id="t",
                scanned_at=0.0,
                total_scanned=pending_count,
                healthy_count=0,
                decaying_count=pending_count,
                pending_compression_count=pending_count,
                results=results,
            )

        def get_pending_compression(self, report):
            return [r for r in report.results if r.status == DecayStatus.PENDING_COMPRESSION]

        def create_compression_batches(self, pending):
            return [[r] for r in pending]

    return _Engine()
