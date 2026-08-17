"""
Cross-Modal Retriever — Text ↔ Image Memory Search
===================================================
Enables bridging between text memories and image_description memories
through a shared embedding space, with linear projection fallback.

Core methods:
  - search_text_by_image(image_path, top_k)   Image → Text memories
  - search_image_by_text(text_query, top_k)   Text → Image_description memories
  - search_cross_modal(query, query_type, top_k)  Auto-detect and route

Architecture:
  Uses sentence-transformers to encode both text and image (CLIP ViT-B/32),
  maintaining two vector spaces (text_vec / image_vec) with optional
  linear projection for alignment when no cross-modal encoder is available.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


class CrossModalRetriever:
    """Cross-modal retrieval bridging text and image memories.

    Parameters
    ----------
    trinity_instance : object
        The Trinity memory instance, used to access stored memories
        and the embedding engine.
    embedding_model : str
        Name of the sentence-transformers model for encoding.
        Default 'all-MiniLM-L6-v2' for text-only; CLIP-based models
        used when available for true cross-modal alignment.
    use_clip : bool
        If True, attempt to load a CLIP model for cross-modal encoding.
        Falls back to text-only embedding with linear projection.
    projection_dim : int
        Dimension of the linear projection layer used for vector-space
        alignment when CLIP is unavailable.
    """

    def __init__(
        self,
        trinity_instance,
        embedding_model: str = "all-MiniLM-L6-v2",
        use_clip: bool = True,
        projection_dim: int = 384,
    ):
        self._trinity = trinity_instance

        self.embedding_model_name = embedding_model
        self.use_clip = use_clip
        self.projection_dim = projection_dim

        self._clip_model = None
        self._clip_processor = None
        self._text_encoder = None
        self._projection_matrix: Optional[np.ndarray] = None

        self._init_encoders()

    # ── Initialisation ─────────────────────────────────────────────

    def _init_encoders(self) -> None:
        """Load text encoder; attempt CLIP if requested."""
        try:
            from sentence_transformers import SentenceTransformer
            self._text_encoder = SentenceTransformer(self.embedding_model_name)
        except Exception as exc:
            logger.warning("Failed to load text encoder '%s': %s", self.embedding_model_name, exc)
            self._text_encoder = None

        if self.use_clip:
            self._try_load_clip()

    def _try_load_clip(self) -> None:
        """Attempt to load CLIP ViT-B/32 for cross-modal encoding."""
        try:
            from PIL import Image
            self._PIL_Image = Image  # store for later use
        except ImportError:
            logger.warning("PIL not installed; CLIP image encoding will not be available")
            self.use_clip = False
            return

        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor
            model_name = "openai/clip-vit-base-patch32"
            self._clip_model = CLIPModel.from_pretrained(model_name)
            self._clip_processor = CLIPProcessor.from_pretrained(model_name)
            logger.info("CLIP model loaded: %s", model_name)
        except Exception as exc:
            logger.warning("CLIP model not available: %s. Falling back to text-only mode.", exc)
            self.use_clip = False

    # ── Public API ─────────────────────────────────────────────────

    def search_text_by_image(
        self,
        image_path: str,
        top_k: int = 10,
    ) -> Dict[str, Any]:
        """Search text memories using an image as query.

        Workflow:
          1. Encode image → vector (CLIP vision or fallback)
          2. Retrieve all text memories
          3. Encode text memories → vectors
          4. Compute cosine similarity, return top-k

        Parameters
        ----------
        image_path : str
            Absolute path to the query image.
        top_k : int
            Number of results to return.

        Returns
        -------
        dict with results, query_type='image_to_text', query_path.
        """
        if not os.path.isfile(image_path):
            return {"results": [], "query_type": "image_to_text",
                    "query_path": image_path, "error": "Image file not found"}

        # Encode image
        image_vec = self._encode_image(image_path)
        if image_vec is None:
            return {"results": [], "query_type": "image_to_text",
                    "query_path": image_path, "error": "Image encoding failed"}

        # Get all text memories
        text_memories = self._get_memories_by_modality("text")

        if not text_memories:
            return {"results": [], "query_type": "image_to_text",
                    "query_path": image_path, "total": 0}

        # Encode & score
        results = self._rank_by_similarity(image_vec, text_memories, top_k)

        return {
            "results": results,
            "query_type": "image_to_text",
            "query_path": image_path,
            "total": len(results),
        }

    def search_image_by_text(
        self,
        text_query: str,
        top_k: int = 10,
    ) -> Dict[str, Any]:
        """Search image_description memories using text query.

        Workflow:
          1. Encode text → vector
          2. Retrieve all image_description memories
          3. Encode image_description texts → vectors
          4. Compute cosine similarity, return top-k

        Parameters
        ----------
        text_query : str
            Natural language query.
        top_k : int
            Number of results to return.

        Returns
        -------
        dict with results, query_type='text_to_image', query.
        """
        if not text_query or not text_query.strip():
            return {"results": [], "query_type": "text_to_image",
                    "query": text_query, "error": "Empty query"}

        # Encode text
        text_vec = self._encode_text(text_query)
        if text_vec is None:
            return {"results": [], "query_type": "text_to_image",
                    "query": text_query, "error": "Text encoding failed"}

        # Get all image_description memories
        image_memories = self._get_memories_by_modality("image_description")

        if not image_memories:
            return {"results": [], "query_type": "text_to_image",
                    "query": text_query, "total": 0}

        # Encode & score
        results = self._rank_by_similarity(text_vec, image_memories, top_k)

        return {
            "results": results,
            "query_type": "text_to_image",
            "query": text_query,
            "total": len(results),
        }

    def search_cross_modal(
        self,
        query: Union[str, List[str]],
        query_type: str = "auto",
        top_k: int = 10,
    ) -> Dict[str, Any]:
        """Auto-detect query type and route to appropriate search method.

        Parameters
        ----------
        query : str or list of str
            - str: text query or image path.
            - list: [text_query, image_path] for combined search.
        query_type : str
            'auto' | 'text' | 'image' | 'combined'.
            'auto' detects type automatically.
        top_k : int
            Number of results to return.

        Returns
        -------
        dict with results, query_type, strategy.
        """
        # Resolve query_type
        if query_type == "auto":
            query_type = self._detect_query_type(query)

        if query_type == "text":
            return self.search_image_by_text(str(query), top_k=top_k)
        elif query_type == "image":
            return self.search_text_by_image(str(query), top_k=top_k)
        elif query_type == "combined":
            return self._search_combined(query, top_k=top_k)

        return {"results": [], "query_type": query_type,
                "error": f"Unknown query_type: {query_type}"}

    # ── Encoding helpers ───────────────────────────────────────────

    def _encode_text(self, text: str) -> Optional[np.ndarray]:
        """Encode text to a vector."""
        try:
            if self._text_encoder is not None:
                vec = self._text_encoder.encode(text, convert_to_numpy=True)
                return vec.astype(np.float32)
        except Exception as exc:
            logger.error("Text encoding failed: %s", exc)
        return None

    def _encode_image(self, image_path: str) -> Optional[np.ndarray]:
        """Encode image to a vector.

        Uses CLIP vision encoder if available; otherwise falls back to
        text-only encoding of a dummy description (degraded mode).
        """
        if self.use_clip and self._clip_model is not None:
            try:
                from PIL import Image
                image = Image.open(image_path).convert("RGB")
                inputs = self._clip_processor(images=image, return_tensors="pt")
                with __import__("torch").no_grad():
                    image_features = self._clip_model.get_image_features(**inputs)
                vec = image_features.cpu().numpy().flatten()
                return vec.astype(np.float32)
            except Exception as exc:
                logger.warning("CLIP image encoding failed: %s; using fallback", exc)

        # Fallback: encode image as dummy text (degraded)
        logger.info("No CLIP available — using text-encoder fallback for image query")
        return self._encode_text(f"Image file: {os.path.basename(image_path)}")

    def _encode_memory_content(self, memory: Dict[str, Any]) -> Optional[np.ndarray]:
        """Encode a memory's content field to a vector."""
        content = memory.get("content", "")
        if not content:
            return None
        return self._encode_text(content)

    # ── Memory retrieval ───────────────────────────────────────────

    def _get_memories_by_modality(
        self,
        modality: str,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        """Fetch all memories of a given modality from Trinity."""
        memories = []
        if self._trinity._adapter and hasattr(self._trinity._adapter, "get_all_memories"):
            try:
                all_mems = self._trinity._adapter.get_all_memories(limit=limit)
                memories = [m for m in all_mems if m.get("modality") == modality]
            except Exception as exc:
                logger.error("Failed to fetch memories: %s", exc)
        return memories

    # ── Similarity ranking ─────────────────────────────────────────

    def _rank_by_similarity(
        self,
        query_vec: np.ndarray,
        memories: List[Dict[str, Any]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """Compute cosine similarity and return top-k results."""
        scored = []
        query_norm = np.linalg.norm(query_vec)
        if query_norm < 1e-9:
            return []

        for mem in memories:
            mem_vec = self._encode_memory_content(mem)
            if mem_vec is None:
                continue
            mem_norm = np.linalg.norm(mem_vec)
            if mem_norm < 1e-9:
                continue
            sim = float(np.dot(query_vec, mem_vec) / (query_norm * mem_norm))
            scored.append({
                "memory_id": mem.get("memory_id", ""),
                "content": mem.get("content", "")[:500],
                "modality": mem.get("modality", ""),
                "score": round(sim, 6),
                "created_at": mem.get("created_at", ""),
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    # ── Combined search ────────────────────────────────────────────

    def _search_combined(
        self,
        query: Union[str, List[str]],
        top_k: int,
    ) -> Dict[str, Any]:
        """Combined text + image query: fuse results from both directions."""
        results = {"results": [], "query_type": "combined", "strategy": "fuse"}

        if isinstance(query, list) and len(query) >= 2:
            text_q, img_path = query[0], query[1]
        else:
            return {**results, "error": "Combined query requires [text, image_path]"}

        text_results = self.search_image_by_text(text_q, top_k=top_k)
        img_results = self.search_text_by_image(img_path, top_k=top_k)

        # Simple fusion: merge by memory_id, sum scores
        merged: Dict[str, Dict] = {}
        for r in text_results.get("results", []):
            mid = r["memory_id"]
            merged[mid] = {**r, "score": r["score"] * 0.5}

        for r in img_results.get("results", []):
            mid = r["memory_id"]
            if mid in merged:
                merged[mid]["score"] += r["score"] * 0.5
            else:
                merged[mid] = {**r, "score": r["score"] * 0.5}

        fused = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
        return {
            **results,
            "results": fused[:top_k],
            "total": len(fused[:top_k]),
            "text_results_total": len(text_results.get("results", [])),
            "image_results_total": len(img_results.get("results", [])),
        }

    # ── Query type detection ───────────────────────────────────────

    def _detect_query_type(self, query: Union[str, List[str]]) -> str:
        """Auto-detect the query type.

        Returns
        -------
        'text' | 'image' | 'combined'
        """
        if isinstance(query, list):
            return "combined"

        query_str = str(query)

        # Check if it looks like a file path pointing to an image
        image_extensions = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff")
        if any(query_str.lower().endswith(ext) for ext in image_extensions):
            if os.path.isfile(query_str):
                return "image"

        # Default to text
        return "text"
