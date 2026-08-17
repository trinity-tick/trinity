"""Trinity client - multimodal & GPU search mixin (split from client.py, 2026-08-17).

Part of the Trinity client package decomposition. Behavior identical to
the pre-split single-file implementation.
"""

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from trinity.telemetry import traced
class _MultimodalMixin:
    @property
    def multimodal(self):
        """获取多模态记忆引擎（惰性初始化）"""
        if not hasattr(self, "_multimodal_memory"):
            self._multimodal_memory = None
        if self._multimodal_memory is None:
            from trinity.modules.multimodal.multimodal_memory import MultiModalMemory
            self._multimodal_memory = MultiModalMemory(
                storage_path=self._store_path,
                tenant_id=self.tenant_id,
            )
        return self._multimodal_memory
    def ingest_image(self, image_path: str, metadata: dict = None) -> dict:
        """摄取一张图片到多模态记忆"""
        from trinity.modules.multimodal.multimodal_memory import ModalityType
        result = self.multimodal.store(
            source_path=image_path,
            modality=ModalityType.IMAGE,
            metadata=metadata or {},
        )
        return {"engram_id": result.engram_id if result else None, "modality": "image"}
    def ingest_audio(self, audio_path: str, metadata: dict = None) -> dict:
        """摄取一段音频到多模态记忆"""
        from trinity.modules.multimodal.multimodal_memory import ModalityType
        result = self.multimodal.store(
            source_path=audio_path,
            modality=ModalityType.AUDIO,
            metadata=metadata or {},
        )
        return {"engram_id": result.engram_id if result else None, "modality": "audio"}
    def search_multimodal(self, query: str, top_k: int = 10,
                          reason: bool = False) -> list:
        """跨模态搜索记忆（文本→图像/音频/文本）"""
        results = self.multimodal.search(query=query, top_k=top_k, reason=reason)
        return [{"engram_id": r[0].engram_id, "score": r[1],
                 "modality": r[0].modality.value if hasattr(r[0], 'modality') else 'unknown'}
                for r in results]
    @property
    def gpu_index(self):
        """获取 GPU 加速向量索引（惰性初始化）"""
        if not hasattr(self, "_gpu_index"):
            self._gpu_index = None
        if self._gpu_index is None:
            from trinity.vector_index.index import (
                FaissIndex, HNSWConfig, NumpyBruteForceIndex,
            )
            try:
                import faiss
                has_gpu = hasattr(faiss, 'StandardGpuResources')
            except ImportError:
                has_gpu = False
            if has_gpu:
                self._gpu_index = FaissIndex(
                    dim=1024, metric="cosine", index_type="hnsw",
                    hnsw_config=HNSWConfig(M=32, efConstruction=200, efSearch=64),
                )
            else:
                # 回退到本地 numpy 作为精确搜索后端
                self._gpu_index = NumpyBruteForceIndex(dim=1024, metric="cosine")
        return self._gpu_index
    def search_with_gpu(self, query: str, top_k: int = 10) -> list:
        """使用 GPU/FAISS 加速向量搜索"""
        from trinity.embeddings.engine import EmbeddingEngine
        engine = EmbeddingEngine()
        query_vec = engine.embed(query)
        if query_vec is None:
            return self.search(query, top_k=top_k)
        results = self.gpu_index.search(query_vec, top_k)
        return [{"memory_id": r.id, "score": r.score, **r.metadata} for r in results]
    def _ensure_cross_modal_retriever(self):
        """Lazy-initialize the CrossModalRetriever.

        回归修复(2026-08-14): 首次构造可能因离线导入 torch/transformers 耗时 60s+，
        用后台线程 + 15s 上限，超时立即返回降级对象（客户端不阻塞），
        线程完成后下次调用自动换装完整检索器。
        """
        if self._cross_modal_retriever is None:
            import os as _os
            import threading
            from types import SimpleNamespace
            from trinity.retrieval.cross_modal import CrossModalRetriever

            prev = {k: _os.environ.get(k)
                    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
            _os.environ.setdefault("HF_HUB_OFFLINE", "1")
            _os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            holder: dict = {}

            def _build() -> None:
                try:
                    holder["cm"] = CrossModalRetriever(trinity_instance=self)
                except Exception as exc:  # noqa: BLE001
                    holder["err"] = exc
                finally:
                    for k, v in prev.items():
                        if v is None:
                            _os.environ.pop(k, None)
                        else:
                            _os.environ[k] = v

            t = threading.Thread(target=_build, daemon=True)
            t.start()
            t.join(timeout=15)
            if "cm" in holder:
                self._cross_modal_retriever = holder["cm"]
            else:
                # 降级占位：文本/CLIP 编码器均不可用；后台线程完成后下次请求换装
                self._cross_modal_retriever = SimpleNamespace(
                    _text_encoder=None, use_clip=False, _PIL_Image=None)
                self._cross_modal_pending_holder = holder
        elif getattr(self, "_cross_modal_pending_holder", None):
            # 后台线程已完成 → 换装完整检索器
            holder = self._cross_modal_pending_holder
            if "cm" in holder:
                self._cross_modal_retriever = holder["cm"]
            self._cross_modal_pending_holder = None
        return self._cross_modal_retriever
    def search_image_by_text(
        self,
        text: str,
        top_k: int = 10,
    ) -> Dict[str, Any]:
        """Search image_description memories by text query.

        Parameters
        ----------
        text : str
            Natural language query describing the image to find.
        top_k : int
            Max results.

        Returns
        -------
        dict with results / query_type='text_to_image' / total.
        """
        cm = self._ensure_cross_modal_retriever()
        return cm.search_image_by_text(text_query=text, top_k=top_k)
    def search_text_by_image(
        self,
        image_path: str,
        top_k: int = 10,
    ) -> Dict[str, Any]:
        """Search text memories by image query.

        Parameters
        ----------
        image_path : str
            Absolute path to the query image.
        top_k : int
            Max results.

        Returns
        -------
        dict with results / query_type='image_to_text' / total.
        """
        cm = self._ensure_cross_modal_retriever()
        return cm.search_text_by_image(image_path=image_path, top_k=top_k)
