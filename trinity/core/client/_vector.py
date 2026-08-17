"""Trinity client - ANN vector index management mixin (split from client.py, 2026-08-17).

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
from ._helpers import _get_embedding_engine

class _VectorMixin:
    def _get_ann_index(self, dim: int):
        """获取或创建 ANN 索引实例（延迟初始化）。"""
        if self._ann_index is None:
            from trinity.retrieval.ann_index import ANNIndex
            self._ann_index = ANNIndex(
                dim=dim,
                space="cosine",
                max_elements=100000,
                M=16,
                ef_construction=200,
            )
        return self._ann_index
    def _ensure_ann_background(self) -> None:
        """后台线程预热 ANN 索引（2026-08-15）：首次构建约 30s（全量编码），
        放后台避免首查阻塞；构建完成前 use_ann 查询降级走 FTS。"""
        if getattr(self, "_ann_building", False):
            return
        self._ann_building = True
        import threading
        t = threading.Thread(target=self._build_ann_in_background, daemon=True)
        t.start()
    def _build_ann_in_background(self) -> None:
        import time as _time
        try:
            if self._embedding_engine is None:
                self._embedding_engine = _get_embedding_engine()
            if self._embedding_engine is None:
                return
            all_memories = self._adapter.get_all_memories(limit=20000) if self._adapter else []
            if not all_memories:
                return
            dim = self._embedding_engine.embedding_dim()
            ann = self._get_ann_index(dim)
            texts = [m["content"] for m in all_memories]
            vectors = self._embedding_engine.embed_batch(texts)
            mem_ids = [m["memory_id"] for m in all_memories]
            ann.add_vectors(mem_ids, vectors)
            mem_map = {m["memory_id"]: m for m in all_memories}
            max_upd = max((str(m.get("updated_at") or "") for m in all_memories), default="")
            self._ann_cache = ((dim, len(all_memories), max_upd), mem_map, _time.time())
            # 落盘持久化（跨进程/重启免重建）
            try:
                os.makedirs(os.path.dirname(self._ann_index_path) or ".", exist_ok=True)
                ann.save(self._ann_index_path)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning("ANN index save failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 构建失败则下次查询再试
            logger = sys.modules.get("logging", None)
            if logger:
                logging.getLogger(__name__).warning("ANN background build failed: %s", exc)
        finally:
            self._ann_building = False
    def _try_load_ann_from_disk(self, dim: int) -> bool:
        """启动/首次查询时从磁盘加载 ANN 索引（免全量编码重建）。

        成功 → 填充 _ann_cache（mem_map 拉全量一次 ~160ms，远快于 30s 编码）。
        """
        import time as _time
        try:
            meta_path = self._ann_index_path + ".meta.json"
            if not os.path.exists(meta_path):
                return False
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("dim") != dim or not meta.get("size"):
                return False
            self._ann_index = None
            ann = self._get_ann_index(dim)
            ann.load(self._ann_index_path)
            all_memories = self._adapter.get_all_memories(limit=20000) if self._adapter else []
            if not all_memories:
                return False
            mem_map = {m["memory_id"]: m for m in all_memories}
            max_upd = max((str(m.get("updated_at") or "") for m in all_memories), default="")
            self._ann_cache = ((dim, len(all_memories), max_upd), mem_map, _time.time())
            return True
        except Exception:  # noqa: BLE001
            self._ann_cache = None
            return False
    def _ann_incremental_add(self, memory_id: str, content: str) -> None:
        """ANN 增量维护：新/更新记忆写入后同步进索引（若已构建且 use_ann）。

        后台线程调用；embed 单条约 380ms 不影响写路径；脏计数阈值触发 save。
        """
        try:
            if self._ann_cache is None or not self.use_ann or not content or not memory_id:
                return
            if self._embedding_engine is None:
                self._embedding_engine = _get_embedding_engine()
            if self._embedding_engine is None:
                return
            dim = self._embedding_engine.embedding_dim()
            ann = self._get_ann_index(dim)
            if memory_id in self._ann_cache[1]:
                try:
                    ann.remove_vector(memory_id)
                except Exception:  # noqa: BLE001
                    pass
            vec = self._embedding_engine.embed(content)
            ann.add_vectors([memory_id], [vec])
            self._ann_cache[1][memory_id] = {
                "memory_id": memory_id, "content": content,
                "content_preview": content[:100], "importance": 0.5,
                "created_at": "", "score": 0.0,
            }
            self._ann_dirty += 1
            if self._ann_dirty >= 20:
                self._ann_dirty = 0
                try:
                    ann.save(self._ann_index_path)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    def _ann_incremental_remove(self, memory_id: str) -> None:
        """ANN 增量维护：删除记忆时从索引移除。"""
        try:
            if self._ann_cache is None or not memory_id:
                return
            if memory_id not in self._ann_cache[1]:
                return
            dim = self._ann_cache[0][0]
            ann = self._get_ann_index(dim)
            try:
                ann.remove_vector(memory_id)
            except Exception:  # noqa: BLE001
                pass
            self._ann_cache[1].pop(memory_id, None)
            self._ann_dirty += 1
        except Exception:  # noqa: BLE001
            pass
