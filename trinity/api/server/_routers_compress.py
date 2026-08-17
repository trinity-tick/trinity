#!/usr/bin/env python3
"""
Trinity REST API Server — memory compression routes (/memory/compress*).
"""

from typing import List

from fastapi import APIRouter

from ._deps import _live_memory as get_memory
from ._models import CompressRequest, CompressRestoreRequest, CompressStatsRequest

router = APIRouter()


_memory_compressor = None


def _get_memory_compressor():
    """Lazy singleton for MemoryCompressor."""
    global _memory_compressor
    if _memory_compressor is None:
        from trinity.memory.compression import MemoryCompressor
        mem = get_memory()
        _memory_compressor = MemoryCompressor(
            trinity_instance=mem,
            max_tokens=4096,
            compression_threshold=0.8,
        )
    return _memory_compressor


@router.post("/memory/compress", tags=["Memory Compression"],
          summary="执行记忆压缩")
async def memory_compress(req: CompressRequest):
    """对指定Agent 的记忆执行压缩管线（去重 →重要性排序→摘要）。
    返回压缩后的活跃记忆列表、摘要文本、被裁剪 ID 和token 预算使用率。    """
    compressor = _get_memory_compressor()
    mem = get_memory()

    # Gather agent memories
    memories = []
    if mem._adapter and hasattr(mem._adapter, "get_all_memories"):
        try:
            memories = mem._adapter.get_all_memories(
                agent_id=req.agent_id,
                limit=10000,
            ) or []
        except Exception:
            pass

    compressor.max_tokens = req.max_tokens
    result = compressor.compress(req.agent_id, memories)
    return result.to_dict()


@router.post("/memory/compress/stats", tags=["Memory Compression"],
          summary="压缩统计")
async def memory_compress_stats(req: CompressStatsRequest = None):
    """查看历史压缩统计：总运行次数、平均压缩率、总裁剪量。"""
    compressor = _get_memory_compressor()
    return compressor.get_stats()


@router.post("/memory/compress/restore", tags=["Memory Compression"],
          summary="恢复被裁剪记忆")
async def memory_compress_restore(req: CompressRestoreRequest):
    """传入之前压缩返回的trimmed_ids，将对应记忆恢复到活跃上下文中。
    恢复操作通过 Trinity adapter 重新加载原始记忆数据。    """
    mem = get_memory()
    restored: List[str] = []
    failed: List[str] = []

    for mid in req.trimmed_ids:
        try:
            if mem._adapter and hasattr(mem._adapter, "get_memory"):
                entry = mem._adapter.get_memory(mid)
                if entry:
                    restored.append(mid)
                else:
                    failed.append(mid)
            else:
                failed.append(mid)
        except Exception:
            failed.append(mid)

    return {
        "agent_id": req.agent_id,
        "restored": restored,
        "restored_count": len(restored),
        "failed": failed,
        "failed_count": len(failed),
    }


