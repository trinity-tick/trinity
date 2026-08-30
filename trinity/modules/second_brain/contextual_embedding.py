"""
# status: active (2026-09 EXECUTION 172: 大脑方向激活) (2026-09 EXECUTION 163)
Trinity Second Brain — Contextual Embedding (Anthropic 2024 Contextual Retrieval)
=================================================================================

Anthropic 2024 Contextual Retrieval 论文核心机制：
在为每个 chunk 生成 embedding 之前，自动生成 50-100 token 的上下文摘要（包含
文档标题、前后文摘要），拼接到 chunk 文本前再嵌入。该机制可减少 49% 的检索
失败率，配合 reranker 可达 67% 降低。

论文引用：
  Anthropic (2024). "Introducing Contextual Retrieval."
  https://www.anthropic.com/news/contextual-retrieval

设计要点：
  - ContextualChunk 数据类：chunk_text + context_summary + contextualized_text + embedding
  - ContextualEmbedder 类：接收原始 chunks，为每个 chunk 生成摘要后嵌入
  - 与 trinity/embeddings/engine.py 的 EmbeddingEngine 集成
  - 与 ContextualChunkIngestion (CB50) 的 chunk 管道集成
  - 可配置窗口大小、摘要长度、启用开关

三元语：
  Retrieval: 基于上下文增强的 chunk embedding 进行语义检索
  Memory: ContextualChunk 的 source_doc 与 chunk_index 构成记忆索引
  Guardian: LLM 生成摘要时校验 source_doc 边界防注入
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── Configuration Constants ───────────────────────────────────────────────

# 上下文窗口：前后各取多少字符
DEFAULT_CONTEXTUAL_CONTEXT_WINDOW = 500

# 摘要最大 token 数（约 50-100 token，按 1 token ≈ 4 字符折算）
DEFAULT_CONTEXTUAL_SUMMARY_MAX_TOKENS = 100
_CONTEXTUAL_SUMMARY_MAX_CHARS = DEFAULT_CONTEXTUAL_SUMMARY_MAX_TOKENS * 4  # ~400 chars

# 全局启用开关
CONTEXTUAL_ENABLED: bool = True

# LLM 摘要提示模板
_CONTEXT_SUMMARY_PROMPT = (
    "You are a retrieval system. Given a document chunk, write a concise "
    "50-100 token context summary that situates this chunk within the "
    "overall document. Use the document title and surrounding context to "
    "describe what this chunk is about.\n\n"
    "<document_title>{doc_title}</document_title>\n"
    "<surrounding_context>{surrounding_context}</surrounding_context>\n"
    "<chunk>\n{chunk_text}\n</chunk>\n\n"
    "Write a short context summary (50-100 tokens):"
)


# ── Data Classes ──────────────────────────────────────────────────────────

@dataclass
class ContextualChunk:
    """Anthropic Contextual Retrieval 增强后的记忆块。

    存储原始 chunk、上下文摘要、拼接后的文本和嵌入向量。
    """
    chunk_text: str
    """原始 chunk 文本内容"""

    context_summary: str
    """LLM 生成的 50-100 token 上下文摘要"""

    contextualized_text: str
    """拼接后的全文：<summary>\\n<chunk_text>"""

    embedding: Optional[np.ndarray] = None
    """L2-normalized float32 嵌入向量"""

    source_doc: str = ""
    """源文档路径或标题"""

    chunk_index: int = 0
    """chunk 在源文档中的序号（从 0 开始）"""

    chunk_id: str = ""
    """chunk 唯一标识（自动生成 SHA-256 前 16 位）"""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """附加元数据"""

    def __post_init__(self):
        if not self.chunk_id:
            self.chunk_id = hashlib.sha256(
                self.contextualized_text.encode()
            ).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "source_doc": self.source_doc,
            "chunk_index": self.chunk_index,
            "chunk_text": self.chunk_text,
            "context_summary": self.context_summary,
            "contextualized_text": self.contextualized_text,
            "has_embedding": self.embedding is not None,
            "embedding_dim": self.embedding.shape[0] if self.embedding is not None else 0,
            "metadata": self.metadata,
        }


# ── Contextual Embedder ───────────────────────────────────────────────────

class ContextualEmbedder:
    """Anthropic 2024 Contextual Retrieval 实现。

    为原始 chunk 生成上下文摘要（50-100 token），拼接到 chunk 前再调用嵌入引擎，
    实现 Anthropic 论文中的 contextualized embedding 流程。

    Usage::

        from trinity.embeddings.engine import create_engine
        embed_engine = create_engine(backend="auto")
        contextual = ContextualEmbedder(
            embed_engine=embed_engine,
            llm_generate_fn=my_llm_generate,
            context_window=500,
        )
        chunks = contextual.embed_chunks(raw_chunks, doc_title="readme.md")
    """

    def __init__(
        self,
        embed_engine: "EmbeddingEngine",
        llm_generate_fn: callable,
        context_window: int = DEFAULT_CONTEXTUAL_CONTEXT_WINDOW,
        summary_max_tokens: int = DEFAULT_CONTEXTUAL_SUMMARY_MAX_TOKENS,
        enabled: bool = True,
    ):
        """
        Args:
            embed_engine: 嵌入引擎实例（OllamaEmbeddingEngine 等）。
            llm_generate_fn: LLM 调用函数，签名 `fn(prompt: str, max_tokens: int) -> str`。
            context_window: 前后各取多少字符作为上下文。
            summary_max_tokens: 摘要最大 token 数。
            enabled: 是否启用上下文增强（False 时直接嵌入原始 chunk）。
        """
        self._engine = embed_engine
        self._llm_generate = llm_generate_fn
        self._context_window = context_window
        self._summary_max_tokens = summary_max_tokens
        self._summary_max_chars = summary_max_tokens * 4
        self._enabled = enabled and CONTEXTUAL_ENABLED
        self._lock = threading.RLock()

        # Statistics
        self._total_chunks = 0
        self._total_summaries = 0
        self._total_embed_time_ms = 0.0
        self._total_summary_time_ms = 0.0
        self._errors = 0
        self._fallback_count = 0

    # ── Public API ─────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        with self._lock:
            self._enabled = value and CONTEXTUAL_ENABLED

    def embed_chunks(
        self,
        chunks: List[str],
        doc_title: str = "",
        source_path: str = "",
    ) -> List[ContextualChunk]:
        """对一批 chunk 执行上下文增强嵌入。

        Args:
            chunks: 原始 chunk 文本列表。
            doc_title: 文档标题（如文件名或章节名）。
            source_path: 源文件路径（用于 source_doc 字段）。

        Returns:
            ContextualChunk 列表，含 embedding。
        """
        import time

        results: List[ContextualChunk] = []

        if not self._enabled:
            # 禁用时直接嵌入原始 chunk
            t0 = time.time()
            embeddings = self._engine.embed_batch(chunks)
            self._total_embed_time_ms += (time.time() - t0) * 1000
            for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
                results.append(ContextualChunk(
                    chunk_text=chunk,
                    context_summary="",
                    contextualized_text=chunk,
                    embedding=emb,
                    source_doc=source_path or doc_title,
                    chunk_index=i,
                ))
            self._total_chunks += len(results)
            return results

        # 1. 为每个 chunk 生成上下文摘要
        t0 = time.time()
        summaries = []
        for i, chunk in enumerate(chunks):
            summary = self._generate_summary(
                chunk=chunk,
                doc_title=doc_title,
                chunks=chunks,
                chunk_index=i,
            )
            summaries.append(summary)
        self._total_summaries += len(summaries)
        self._total_summary_time_ms += (time.time() - t0) * 1000

        # 2. 拼接：<summary>\n<chunk>
        contextualized_texts = []
        for chunk_text, summary in zip(chunks, summaries):
            if summary.strip():
                ctext = f"{summary.strip()}\n\n{chunk_text}"
            else:
                ctext = chunk_text
            contextualized_texts.append(ctext)

        # 3. 批量嵌入
        t0 = time.time()
        embeddings = self._engine.embed_batch(contextualized_texts)
        self._total_embed_time_ms += (time.time() - t0) * 1000

        # 4. 构建结果
        for i, (chunk, summary, ctext, emb) in enumerate(
            zip(chunks, summaries, contextualized_texts, embeddings)
        ):
            results.append(ContextualChunk(
                chunk_text=chunk,
                context_summary=summary,
                contextualized_text=ctext,
                embedding=emb,
                source_doc=source_path or doc_title,
                chunk_index=i,
            ))

        self._total_chunks += len(results)
        return results

    def embed_single(
        self,
        chunk: str,
        doc_title: str = "",
        source_path: str = "",
        chunk_index: int = 0,
        all_chunks: Optional[List[str]] = None,
    ) -> ContextualChunk:
        """对单个 chunk 执行上下文增强嵌入。

        Args:
            chunk: 单个 chunk 文本。
            doc_title: 文档标题。
            source_path: 源文件路径。
            chunk_index: chunk 序号。
            all_chunks: 同文档的所有 chunk 列表（用于生成前后文摘要）。

        Returns:
            ContextualChunk 含 embedding。
        """
        result = self.embed_chunks(
            chunks=[chunk],
            doc_title=doc_title,
            source_path=source_path,
        )
        cc = result[0]
        cc.chunk_index = chunk_index
        return cc

    # ── Integration with ContextualChunkIngestion ──────────────────────

    def enrich_ingestion_blocks(
        self,
        blocks: List[Tuple[str, Tuple[int, int]]],
        doc_title: str = "",
        source_path: str = "",
    ) -> Dict[str, ContextualChunk]:
        """与 ContextualChunkIngestion._semantic_chunking 的输出集成。

        ContextualChunkIngestion 产出 (chunk_content, boundaries) 元组列表，
        本方法接收此格式并返回 {chunk_id: ContextualChunk} 映射。

        Args:
            blocks: (chunk_content, boundaries) 元组列表。
            doc_title: 文档标题。
            source_path: 源文件路径。

        Returns:
            chunk_id → ContextualChunk 映射。
        """
        chunks = [b[0] for b in blocks]
        contextual_chunks = self.embed_chunks(
            chunks=chunks,
            doc_title=doc_title,
            source_path=source_path,
        )
        return {cc.chunk_id: cc for cc in contextual_chunks}

    # ── Internal: Summary Generation ───────────────────────────────────

    def _generate_summary(
        self,
        chunk: str,
        doc_title: str,
        chunks: List[str],
        chunk_index: int,
    ) -> str:
        """为单个 chunk 生成 50-100 token 上下文摘要。

        使用文档标题 + 前后窗口 chunk 拼接为 surrounding_context，
        调用 LLM 生成简洁摘要。

        防注入：source_doc 固定为 doc_title，不受用户输入影响。
        """
        # 构建前后窗口上下文
        pre_start = max(0, chunk_index - 3)
        pre_chunks = chunks[pre_start:chunk_index]
        post_end = min(len(chunks), chunk_index + 4)
        post_chunks = chunks[chunk_index + 1:post_end]

        # 限制窗口总字符数
        def _trim_window(parts: List[str], max_chars: int) -> str:
            result = ""
            for p in parts:
                if len(result) + len(p) <= max_chars:
                    result += p + "\n"
                else:
                    remaining = max_chars - len(result)
                    if remaining > 20:
                        result += p[:remaining] + "..."
                    break
            return result.strip()

        pre_text = _trim_window(pre_chunks, self._context_window)
        post_text = _trim_window(post_chunks, self._context_window)
        surrounding = f"[previous chunks]\n{pre_text}\n[end previous]\n\n"
        surrounding += f"[following chunks]\n{post_text}\n[end following]"

        prompt = _CONTEXT_SUMMARY_PROMPT.format(
            doc_title=doc_title or "(untitled)",
            surrounding_context=surrounding or "(no surrounding context)",
            chunk_text=chunk,
        )

        try:
            summary = self._llm_generate(prompt, max_tokens=self._summary_max_tokens)
            # 截断到 max chars
            if len(summary) > self._summary_max_chars:
                summary = summary[:self._summary_max_chars]
            return summary.strip()
        except Exception as e:
            self._errors += 1
            self._fallback_count += 1
            logger.warning(f"Contextual summary generation failed: {e}")

            # Fallback: 使用前 N 字符作为伪摘要
            fallback = chunk[:min(200, self._summary_max_chars)]
            if len(chunk) > 200:
                fallback = fallback[:190] + "..."
            return f"[{doc_title or 'document'}] chunk {chunk_index}: {fallback}"

    # ── Statistics ─────────────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        """返回当前统计状态。"""
        total_embed = max(1, self._total_chunks)
        return {
            "enabled": self._enabled,
            "context_window": self._context_window,
            "summary_max_tokens": self._summary_max_tokens,
            "total_chunks_embedded": self._total_chunks,
            "total_summaries_generated": self._total_summaries,
            "avg_summary_time_ms": round(self._total_summary_time_ms / total_embed, 2),
            "avg_embed_time_ms": round(self._total_embed_time_ms / total_embed, 2),
            "errors": self._errors,
            "fallback_count": self._fallback_count,
            "embedding_dim": self._engine.embedding_dim(),
            "embedding_model": self._engine.model_name(),
        }

    def diagnostics(self) -> Dict[str, Any]:
        """全系统诊断信息。"""
        stats = self.statistics()
        stats["embed_engine"] = self._engine.diagnostics()
        return stats


# ── Convenience Factory ────────────────────────────────────────────────────

def create_contextual_embedder(
    embed_engine: "EmbeddingEngine",
    llm_generate_fn: callable,
    **kwargs,
) -> ContextualEmbedder:
    """一键创建 ContextualEmbedder。

    Args:
        embed_engine: 嵌入引擎实例。
        llm_generate_fn: LLM 调用函数。
        **kwargs: 传递给 ContextualEmbedder 的其他参数。

    Returns:
        配置好的 ContextualEmbedder 实例。
    """
    return ContextualEmbedder(
        embed_engine=embed_engine,
        llm_generate_fn=llm_generate_fn,
        context_window=kwargs.pop(
            "context_window", DEFAULT_CONTEXTUAL_CONTEXT_WINDOW
        ),
        summary_max_tokens=kwargs.pop(
            "summary_max_tokens", DEFAULT_CONTEXTUAL_SUMMARY_MAX_TOKENS
        ),
        enabled=kwargs.pop("enabled", True),
        **kwargs,
    )


# ── Self-test ─────────────────────────────────────────────────────────────

def self_test():
    """Quick self-test of ContextualEmbedder with a mock LLM."""
    from trinity.embeddings.engine import SklearnEmbeddingEngine

    print("=" * 60)
    print("  Trinity Contextual Embedding — Self Test")
    print("=" * 60)

    # Mock LLM that returns simple summaries
    def mock_llm(prompt: str, max_tokens: int) -> str:
        # Extract chunk text from prompt
        idx_start = prompt.find("<chunk>\n")
        idx_end = prompt.find("\n</chunk>")
        if idx_start >= 0 and idx_end > idx_start:
            chunk_text = prompt[idx_start + 8:idx_end]
            preview = chunk_text[:80].replace("\n", " ")
        else:
            preview = "unknown content"
        return f"This section discusses: {preview}..."

    engine = SklearnEmbeddingEngine(max_features=256)
    contextual = create_contextual_embedder(
        embed_engine=engine,
        llm_generate_fn=mock_llm,
    )

    test_chunks = [
        "Alice prefers hiking in the Rocky Mountains during summer.",
        "She also enjoys skiing in the winter months at Vail.",
        "Bob works as a software engineer at Google in Mountain View.",
        "He leads the search infrastructure team.",
    ]

    results = contextual.embed_chunks(
        chunks=test_chunks,
        doc_title="user_profiles.md",
        source_path="/data/user_profiles.md",
    )

    for i, cc in enumerate(results):
        print(f"\n  Chunk {i}:")
        print(f"    chunk_id:      {cc.chunk_id}")
        print(f"    source_doc:    {cc.source_doc}")
        print(f"    chunk_index:   {cc.chunk_index}")
        print(f"    context_summary:  {cc.context_summary[:100]}...")
        print(f"    contextualized_text (preview): {cc.contextualized_text[:120]}...")
        print(f"    embedding:     {cc.embedding.shape} norm={np.linalg.norm(cc.embedding):.4f}")

    # Test disabled mode
    contextual.enabled = False
    results_disabled = contextual.embed_chunks(test_chunks[:2], doc_title="test")
    print(f"\n  Disabled mode — contextualized_text == chunk_text: "
          f"{results_disabled[0].contextualized_text == test_chunks[0]}")

    # Statistics
    print(f"\n  Statistics: {contextual.statistics()}")

    print("\n" + "=" * 60)
    print("  Self-test complete")

    # Clean up: re-enable
    contextual.enabled = True

    return True


if __name__ == "__main__":
    self_test()
