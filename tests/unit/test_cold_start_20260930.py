"""冷启动回归闸门（2026-09-30，外部审计目标项：首个 full 查询 15–20s）。

现场（cProfile + 直测，**生产后端 `TRINITY_EMBED_BACKEND=onnx`**）：

* 首个 full 查询 9.62s（服务侧 14.7–20.1s）/ 热查询 0.56s。
* 冷查询的热点是**首次嵌入时的惰性导入**：
  `_vec_budget.py:54(_run) → engine.py:536(embed)`
  → `transformers/__init__.py` **4.195s**、`transformers/utils/chat_template_utils.py`
  **3.366s**、`torch/functional.py` **1.023s**。
* 而查询嵌入只有 `TRINITY_QUERY_EMBED_TIMEOUT_S=3` 的墙钟上限 ⇒ 首个查询
  **必然超时、向量通道被丢弃**，却仍白付 3s。
* 直测同一份工作：**first embed 21.80s → second embed 0.071s（307×）**。

修复：把这一次性成本从"首个用户查询"挪到**启动期同步预热**
（`api/server/__init__.py:main()`，`TRINITY_PREWARM_EMBED=0` 可关）。
实测首个 full 查询的 `breakdown` 从"无 vector 通道"变成
`{"vector": 5, "bm25": 5, ...}`，且 `api.err.log` 不再出现「查询嵌入超 3.0s」。

附带（对 Ollama 后端有效、对本机 ONNX 后端无害）：给 `/api/embed` 请求体加
`keep_alive`（默认 `60m`），因为 Ollama 默认只驻留 5 分钟，卸载后实测
冷嵌入 **6.53s** vs 驻留 **0.07s**。

运行：``python -m pytest tests/unit/test_cold_start_20260930.py -q``
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


# ── 1. Ollama 请求必须能延长模型驻留 ────────────────────────────────────

class _Resp:
    def raise_for_status(self):  # noqa: D102
        pass

    def json(self):  # noqa: D102
        return {"embeddings": [[0.0] * 4]}


class _Recorder:
    payloads: list = []

    @classmethod
    def post(cls, url, json=None, timeout=None):  # noqa: A002 - 对齐 requests 签名
        cls.payloads.append(dict(json or {}))
        return _Resp()


def _engine_with_recorder(monkeypatch: pytest.MonkeyPatch):
    from trinity.embeddings.engine import OllamaEmbeddingEngine

    eng = OllamaEmbeddingEngine(dim=4)
    monkeypatch.setattr(eng, "_ensure_imports", lambda: _Recorder)
    _Recorder.payloads = []
    return eng


def test_ollama_payload_carries_keep_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRINITY_OLLAMA_KEEP_ALIVE", raising=False)
    eng = _engine_with_recorder(monkeypatch)
    eng._call_ollama_api(["x"])
    assert _Recorder.payloads[0].get("keep_alive") == "60m", (
        "缺少 keep_alive ⇒ Ollama 5 分钟后卸载模型，之后每次查询都要重付冷加载"
    )


def test_ollama_keep_alive_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRINITY_OLLAMA_KEEP_ALIVE", "0")
    eng = _engine_with_recorder(monkeypatch)
    eng._call_ollama_api(["x"])
    assert "keep_alive" not in _Recorder.payloads[0], "回滚开关必须能逐字恢复旧 payload"


def test_ollama_keep_alive_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRINITY_OLLAMA_KEEP_ALIVE", "-1")
    eng = _engine_with_recorder(monkeypatch)
    eng._call_ollama_api(["x"])
    assert _Recorder.payloads[0].get("keep_alive") == "-1"


# ── 2. 启动期必须**同步**预热嵌入器 ─────────────────────────────────────

def test_main_prewarms_embedder_synchronously() -> None:
    """`main()` 必须在**真正的** `uvicorn.run(...)` 调用之前完成嵌入预热。

    为什么必须是同步：`_deps._warm` 里等价的预热跑在**后台守护线程**，
    而 CPython 的 import 锁会让同时在跑的查询嵌入线程**阻塞在导入上**
    ⇒ "预热"救不了恰好撞上它的首个查询（这正是修复前的现场）。

    检查按**行**做且跳过注释行 —— 否则注释里提到的 `uvicorn.run` 会先被匹配到
    （本判据第一版就是这么误报的）。
    """
    src = (ROOT / "trinity/api/server/__init__.py").read_text(encoding="utf-8")
    main_src = src[src.index("def main("):]
    lines = main_src.splitlines()
    code_lines = [_ln for _ln in lines if not _ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)

    assert "_get_embedding_engine" in code, "main() 未预热嵌入引擎（代码行，非注释）"
    assert "startup-warmup" in code, "缺少可识别的预热调用"

    warm_at = code.index("startup-warmup")
    call_at = next(
        (code.index(_ln) for _ln in code_lines if re.match(r"\s*uvicorn\.run\(", _ln)),
        None,
    )
    assert call_at is not None, "未找到真正的 uvicorn.run(...) 调用"
    assert warm_at < call_at, (
        "嵌入预热必须在 uvicorn.run(...) 之前（否则端口先监听，首个查询仍会撞上惰性导入）"
    )


def test_prewarm_is_switchable() -> None:
    src = (ROOT / "trinity/api/server/__init__.py").read_text(encoding="utf-8")
    assert 'TRINITY_PREWARM_EMBED' in src, "预热必须可关闭（回滚位）"


def test_prewarm_failure_is_non_fatal() -> None:
    """预热失败不得让服务起不来（与原 `_deps._warm` 的语义一致）。"""
    src = (ROOT / "trinity/api/server/__init__.py").read_text(encoding="utf-8")
    main_src = src[src.index("def main("):]
    blk = main_src[main_src.index("TRINITY_PREWARM_EMBED"):]
    blk = blk[: blk.index("uvicorn.run")]
    assert "except Exception" in blk, "预热块必须吞异常（失败静默降级）"


# ── 3. 不得退回"请求路径里嵌语料" ────────────────────────────────────────

def test_request_path_does_not_embed_corpus_by_default() -> None:
    """`TRINITY_VEC_CORPUS_EMBED_BUDGET` 默认必须是 0（请求路径不嵌语料）。"""
    from trinity.core.client._vec_budget import corpus_embed_budget

    os.environ.pop("TRINITY_VEC_CORPUS_EMBED_BUDGET", None)
    assert corpus_embed_budget() == 0, (
        "请求路径若嵌语料，冷启动会把 ~2 万行正文压进关键路径（现场实测 163s 争用）"
    )
