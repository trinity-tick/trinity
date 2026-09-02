"""Reranker 预加载（2026-09-02 brain fix）。

Windows DLL 冲突：onnxruntime/libpq 已加载后再导入 sentence_transformers/torch 会
硬崩溃（access violation 0xC0000005，try/except 无法拦截；实测 search_hybrid light
路径首调 100% 崩）。实测安全顺序：sentence_transformers → psycopg2 → onnx。
因此 worker/API 启动入口必须在任何原生库加载前先 preload()。
"""
from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_RERANKER_PRELOADED = False
_PRELOAD_LOCK = threading.Lock()


def is_preloaded() -> bool:
    return _RERANKER_PRELOADED


def preload(timeout_s: float = 90.0) -> bool:
    """进程启动早期调用：在 onnx/libpq 之前导入 sentence_transformers。幂等。"""
    global _RERANKER_PRELOADED
    if _RERANKER_PRELOADED:
        return True
    with _PRELOAD_LOCK:
        if _RERANKER_PRELOADED:
            return True
        try:
            # 2026-09-02：强制离线——CE 模型加载时的 HF 新鲜度检查（HEAD 请求）
            # 在网络不可达时会长时间重试（实测挂死整个请求，WinError 10060）。
            # 模型已在本地缓存时离线加载无网络开销；缓存缺失则快速失败降级。
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            import sentence_transformers  # noqa: F401
            _RERANKER_PRELOADED = True
            logger.info(
                "reranker preload ok: sentence_transformers %s",
                getattr(sentence_transformers, "__version__", "?"),
            )
        except Exception as exc:
            logger.warning("reranker preload failed: %s", exc)
    return _RERANKER_PRELOADED


def prewarm_model(model_name: str = "chinese") -> None:
    """后台线程预热 CE 模型（首次下载/加载不阻塞请求；失败静默降级 ollama）。"""
    def _warm() -> None:
        try:
            from trinity.vector_index.reranker import CrossEncoderReranker
            rk = CrossEncoderReranker(model_name=model_name)
            rk._load_model()
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True, name="reranker-prewarm").start()
