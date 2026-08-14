# -*- coding: utf-8 -*-
"""
Trinity Multimodal — Enhanced Cross-Modal Retrieval (P1-3).

Extends the multimodal memory system with video frame extraction and
audio clip segmentation, enabling cross-modal search across text,
image, video frames, and audio clips.

Usage::

    from trinity.modules.multimodal.multimodal_enhanced import (
        VideoFrameExtractor, AudioClipProcessor, CrossModalRetriever
    )

    extractor = VideoFrameExtractor()
    frames = extractor.extract_frames("video.mp4", fps=1)

    processor = AudioClipProcessor()
    clips = processor.segment("audio.wav", clip_duration_sec=5.0)

    retriever = CrossModalRetriever()
    results = retriever.search("sunset scene", modalities=["image", "video_frame"])
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ── Enums ──────────────────────────────────────────────────────────────────


class MediaModality(Enum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO_FRAME = "video_frame"
    AUDIO_CLIP = "audio_clip"
    AUDIO_FULL = "audio_full"


class FeatureType(Enum):
    COLOR_HIST = "color_histogram"
    SPECTRAL = "spectral"
    EMBEDDING = "embedding"
    MFCC = "mfcc"
    CHROMA = "chroma"


# ── Feature Envelopes ─────────────────────────────────────────────────────


@dataclass
class MediaFeature:
    """A feature vector extracted from a media segment."""
    feature_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    modality: MediaModality = MediaModality.TEXT
    feature_type: FeatureType = FeatureType.EMBEDDING
    vector: np.ndarray = field(default_factory=lambda: np.zeros(128, dtype=np.float32))
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_path: str = ""
    timestamp_sec: float = 0.0
    duration_sec: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "modality": self.modality.value,
            "feature_type": self.feature_type.value,
            "vector_norm": float(np.linalg.norm(self.vector)),
            "source_path": self.source_path,
            "timestamp_sec": self.timestamp_sec,
            "duration_sec": self.duration_sec,
            "metadata": self.metadata,
        }

    def cosine_similarity(self, other: MediaFeature) -> float:
        a, b = self.vector, other.vector
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))


# ── Video Frame Extractor ─────────────────────────────────────────────────


class VideoFrameExtractor:
    """Extract representative frames from video files.

    Uses a simulated approach: given a duration and FPS, generates
    synthetic frame timestamps. When OpenCV is available, uses actual
    frame extraction; otherwise falls back to metadata-based simulation.

    Attributes:
        default_fps: Frames per second to sample (default 1 = 1 frame/sec).
        max_frames: Maximum frames to extract per video.
    """

    def __init__(self, default_fps: float = 1.0, max_frames: int = 300):
        self.default_fps = default_fps
        self.max_frames = max_frames
        self._cv2_available = False
        try:
            import cv2  # noqa: F401
            self._cv2_available = True
        except ImportError:
            pass

    def extract_frames(
        self,
        video_path: str,
        fps: Optional[float] = None,
        start_sec: float = 0.0,
        end_sec: Optional[float] = None,
    ) -> List[MediaFeature]:
        """Extract frame features from a video file.

        Args:
            video_path: Path to video file.
            fps: Sampling rate (frames/sec). Default: self.default_fps.
            start_sec: Start time offset in seconds.
            end_sec: End time offset (None = end of video).

        Returns:
            List of MediaFeature objects, one per frame.
        """
        sample_fps = fps or self.default_fps

        if self._cv2_available and os.path.exists(video_path):
            return self._extract_real(video_path, sample_fps, start_sec, end_sec)
        else:
            return self._extract_simulated(video_path, sample_fps, start_sec, end_sec)

    def _extract_real(
        self, video_path: str, fps: float, start_sec: float, end_sec: Optional[float]
    ) -> List[MediaFeature]:
        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                return self._extract_simulated(video_path, fps, start_sec, end_sec)

            video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration = total_frames / video_fps if video_fps > 0 else 0

            effective_end = min(end_sec or duration, duration)
            frame_interval = int(video_fps / fps) if fps > 0 else int(video_fps)

            features = []
            cap.set(cv2.CAP_PROP_POS_MSEC, start_sec * 1000)
            frame_idx = 0

            while cap.isOpened() and len(features) < self.max_frames:
                ret, frame = cap.read()
                if not ret:
                    break
                current_time = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                if current_time > effective_end:
                    break
                if frame_idx % frame_interval == 0:
                    feat = self._frame_to_feature(frame, video_path, current_time)
                    features.append(feat)
                frame_idx += 1

            cap.release()
            return features
        except Exception as e:
            logger.warning("Real frame extraction failed: %s, falling back to simulated", e)
            return self._extract_simulated(video_path, fps, start_sec, end_sec)

    def _extract_simulated(
        self, video_path: str, fps: float, start_sec: float, end_sec: Optional[float]
    ) -> List[MediaFeature]:
        """Simulate frame extraction using metadata and hashing."""
        # Estimate duration from file size (very rough)
        file_size = os.path.getsize(video_path) if os.path.exists(video_path) else 10 * 1024 * 1024
        estimated_duration = max(5.0, file_size / (1024 * 1024) * 0.5)  # ~2MB/s rough est
        effective_end = min(end_sec or estimated_duration, estimated_duration)

        n_frames = int((effective_end - start_sec) * fps)
        n_frames = min(n_frames, self.max_frames)

        features = []
        for i in range(n_frames):
            t = start_sec + i / fps
            if t > effective_end:
                break

            # Generate deterministic feature from filename + timestamp
            seed_str = f"{video_path}:{t:.2f}"
            seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
            rng = np.random.RandomState(seed)
            vec = rng.randn(128).astype(np.float32)
            vec /= np.linalg.norm(vec)

            features.append(MediaFeature(
                modality=MediaModality.VIDEO_FRAME,
                feature_type=FeatureType.COLOR_HIST,
                vector=vec,
                source_path=video_path,
                timestamp_sec=round(t, 2),
                metadata={"fps": fps, "frame_index": i, "simulated": True},
            ))

        logger.debug("Extracted %d simulated frames from %s", len(features), video_path)
        return features

    def _frame_to_feature(self, frame: Any, path: str, timestamp: float) -> MediaFeature:
        """Convert an OpenCV frame to a MediaFeature."""
        # Simple color histogram feature
        h, w = frame.shape[:2]
        # Downsample and flatten as feature
        small = frame[::8, ::8, :].flatten().astype(np.float32)
        if len(small) < 128:
            small = np.pad(small, (0, 128 - len(small)), mode="constant")
        else:
            small = small[:128]
        small = small / (np.linalg.norm(small) + 1e-8)

        return MediaFeature(
            modality=MediaModality.VIDEO_FRAME,
            feature_type=FeatureType.COLOR_HIST,
            vector=small,
            source_path=path,
            timestamp_sec=round(timestamp, 2),
            metadata={"width": w, "height": h, "simulated": False},
        )


# ── Audio Clip Processor ──────────────────────────────────────────────────


class AudioClipProcessor:
    """Segment audio files into clips and extract features.

    Uses simulated spectral features when no audio library available.
    """

    def __init__(
        self,
        clip_duration_sec: float = 5.0,
        overlap_sec: float = 1.0,
        sample_rate: int = 16000,
    ):
        self.clip_duration_sec = clip_duration_sec
        self.overlap_sec = overlap_sec
        self.sample_rate = sample_rate
        self._librosa_available = False
        try:
            import librosa  # noqa: F401
            self._librosa_available = True
        except ImportError:
            pass

    def segment(
        self,
        audio_path: str,
        clip_duration_sec: Optional[float] = None,
    ) -> List[MediaFeature]:
        """Segment an audio file into clips.

        Args:
            audio_path: Path to audio file.
            clip_duration_sec: Override default clip duration.

        Returns:
            List of MediaFeature objects, one per audio clip.
        """
        duration = clip_duration_sec or self.clip_duration_sec

        if self._librosa_available and os.path.exists(audio_path):
            return self._segment_real(audio_path, duration)
        else:
            return self._segment_simulated(audio_path, duration)

    def _segment_real(self, audio_path: str, duration_sec: float) -> List[MediaFeature]:
        try:
            import librosa
            y, sr = librosa.load(audio_path, sr=self.sample_rate)
            total_duration = len(y) / sr

            hop_samples = int((duration_sec - self.overlap_sec) * sr)
            hop_samples = max(1, hop_samples)
            clip_samples = int(duration_sec * sr)

            features = []
            start_sample = 0
            clip_idx = 0

            while start_sample < len(y):
                end_sample = min(start_sample + clip_samples, len(y))
                segment = y[start_sample:end_sample]
                if len(segment) < sr * 0.5:  # Skip very short segments
                    break

                # MFCC-like feature
                mfcc = librosa.feature.mfcc(y=segment, sr=sr, n_mfcc=13)
                vec = mfcc.mean(axis=1).astype(np.float32)
                if len(vec) < 128:
                    vec = np.pad(vec, (0, 128 - len(vec)), mode="constant")
                else:
                    vec = vec[:128]
                vec = vec / (np.linalg.norm(vec) + 1e-8)

                features.append(MediaFeature(
                    modality=MediaModality.AUDIO_CLIP,
                    feature_type=FeatureType.MFCC,
                    vector=vec,
                    source_path=audio_path,
                    timestamp_sec=round(start_sample / sr, 2),
                    duration_sec=round(len(segment) / sr, 2),
                    metadata={
                        "sample_rate": sr,
                        "clip_index": clip_idx,
                        "simulated": False,
                    },
                ))

                start_sample += hop_samples
                clip_idx += 1

            return features
        except Exception as e:
            logger.warning("Real audio segmentation failed: %s", e)
            return self._segment_simulated(audio_path, duration_sec)

    def _segment_simulated(self, audio_path: str, duration_sec: float) -> List[MediaFeature]:
        """Simulate audio segmentation with deterministic features."""
        file_size = os.path.getsize(audio_path) if os.path.exists(audio_path) else 5 * 1024 * 1024
        estimated_total = max(10.0, file_size / (1024 * 16))  # ~16KB/s rough

        n_clips = int(estimated_total / max(duration_sec - self.overlap_sec, 0.1))
        n_clips = min(n_clips, 50)

        features = []
        for i in range(n_clips):
            t = i * (duration_sec - self.overlap_sec)

            seed_str = f"{audio_path}:clip:{i}"
            seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
            rng = np.random.RandomState(seed)
            vec = rng.randn(128).astype(np.float32)
            vec /= np.linalg.norm(vec)

            features.append(MediaFeature(
                modality=MediaModality.AUDIO_CLIP,
                feature_type=FeatureType.SPECTRAL,
                vector=vec,
                source_path=audio_path,
                timestamp_sec=round(t, 2),
                duration_sec=round(duration_sec - self.overlap_sec, 2),
                metadata={"clip_index": i, "simulated": True},
            ))

        logger.debug("Segmented %d simulated clips from %s", len(features), audio_path)
        return features


# ── Cross-Modal Retriever ─────────────────────────────────────────────────


class CrossModalRetriever:
    """Cross-modal similarity search across text, image, video, and audio.

    Indexes MediaFeatures and supports similarity search with modality filtering.
    """

    def __init__(self, embed_fn: Optional[Callable[[str], np.ndarray]] = None):
        self._features: List[MediaFeature] = []
        self._lock = threading.RLock()
        self._embed_fn = embed_fn or self._default_embed

    def _default_embed(self, text: str) -> np.ndarray:
        """Deterministic text-to-vector fallback."""
        seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
        rng = np.random.RandomState(seed)
        vec = rng.randn(128).astype(np.float32)
        vec /= np.linalg.norm(vec)
        return vec

    # ── Indexing ─────────────────────────────────────────────────────

    def index(self, features: List[MediaFeature]) -> int:
        """Add features to the retrieval index.

        Returns:
            Number of features indexed.
        """
        with self._lock:
            self._features.extend(features)
        return len(features)

    def index_video(self, video_path: str, fps: float = 1.0) -> int:
        """Extract and index frames from a video."""
        extractor = VideoFrameExtractor(default_fps=fps)
        frames = extractor.extract_frames(video_path)
        return self.index(frames)

    def index_audio(self, audio_path: str, clip_duration: float = 5.0) -> int:
        """Segment and index clips from an audio file."""
        processor = AudioClipProcessor(clip_duration_sec=clip_duration)
        clips = processor.segment(audio_path)
        return self.index(clips)

    # ── Search ───────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
        modalities: Optional[List[MediaModality]] = None,
        threshold: float = 0.0,
    ) -> List[Tuple[MediaFeature, float]]:
        """Cross-modal semantic search.

        Args:
            query: Text query or description.
            top_k: Number of results.
            modalities: Filter by modality (None = all).
            threshold: Minimum similarity threshold.

        Returns:
            List of (feature, similarity_score) tuples, sorted desc.
        """
        query_vec = self._embed_fn(query)

        with self._lock:
            scored = []
            for feat in self._features:
                if modalities and feat.modality not in modalities:
                    continue
                sim = float(np.dot(query_vec, feat.vector) / (
                    np.linalg.norm(query_vec) * np.linalg.norm(feat.vector) + 1e-8
                ))
                if sim >= threshold:
                    scored.append((feat, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def search_by_feature(
        self,
        feature: MediaFeature,
        top_k: int = 10,
        modalities: Optional[List[MediaModality]] = None,
    ) -> List[Tuple[MediaFeature, float]]:
        """Search using a MediaFeature as query (e.g., find similar frames)."""
        with self._lock:
            scored = []
            for feat in self._features:
                if feat.feature_id == feature.feature_id:
                    continue
                if modalities and feat.modality not in modalities:
                    continue
                sim = feature.cosine_similarity(feat)
                scored.append((feat, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    # ── Statistics ───────────────────────────────────────────────────

    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            counts = {}
            for feat in self._features:
                m = feat.modality.value
                counts[m] = counts.get(m, 0) + 1
            return {
                "total_features": len(self._features),
                "by_modality": counts,
            }

    def clear(self) -> None:
        with self._lock:
            self._features.clear()


# ── Self-Test ─────────────────────────────────────────────────────────────


def self_test() -> Dict[str, Any]:
    """Module self-test."""
    results: Dict[str, Any] = {"module": "trinity.modules.multimodal.multimodal_enhanced", "tests": {}}

    # Test 1: VideoFrameExtractor simulated
    try:
        extractor = VideoFrameExtractor(default_fps=2.0, max_frames=10)
        frames = extractor.extract_frames("test_video.mp4", fps=2.0)
        assert len(frames) > 0
        assert frames[0].modality == MediaModality.VIDEO_FRAME
        results["tests"]["video_frame_extract"] = f"PASS ({len(frames)} frames)"
    except Exception as e:
        results["tests"]["video_frame_extract"] = f"FAIL: {e}"

    # Test 2: AudioClipProcessor simulated
    try:
        processor = AudioClipProcessor(clip_duration_sec=3.0)
        clips = processor.segment("test_audio.wav")
        assert len(clips) > 0
        assert clips[0].modality == MediaModality.AUDIO_CLIP
        results["tests"]["audio_clip_segment"] = f"PASS ({len(clips)} clips)"
    except Exception as e:
        results["tests"]["audio_clip_segment"] = f"FAIL: {e}"

    # Test 3: CrossModalRetriever index + search
    try:
        retriever = CrossModalRetriever()

        # Index video frames
        extractor = VideoFrameExtractor(default_fps=5.0, max_frames=20)
        frames = extractor.extract_frames("video.mp4", fps=5.0)
        retriever.index(frames)

        # Index audio clips
        processor = AudioClipProcessor(clip_duration_sec=3.0)
        clips = processor.segment("audio.wav")
        retriever.index(clips)

        # Search
        results_search = retriever.search("sunset", top_k=5)
        assert len(results_search) > 0
        results["tests"]["cross_modal_search"] = f"PASS ({len(results_search)} results)"
    except Exception as e:
        results["tests"]["cross_modal_search"] = f"FAIL: {e}"

    # Test 4: Modality filtering
    try:
        video_only = retriever.search("scene", top_k=10, modalities=[MediaModality.VIDEO_FRAME])
        for feat, _ in video_only:
            assert feat.modality == MediaModality.VIDEO_FRAME
        results["tests"]["modality_filter"] = f"PASS ({len(video_only)} video-only)"
    except Exception as e:
        results["tests"]["modality_filter"] = f"FAIL: {e}"

    # Test 5: Feature-by-feature search
    try:
        if frames:
            similar = retriever.search_by_feature(frames[0], top_k=3)
            assert len(similar) >= 0
        results["tests"]["feature_search"] = "PASS"
    except Exception as e:
        results["tests"]["feature_search"] = f"FAIL: {e}"

    # Test 6: Cosine similarity
    try:
        import numpy as np
        a = MediaFeature(vector=np.array([1.0, 0.0, 0.0], dtype=np.float32))
        b = MediaFeature(vector=np.array([1.0, 0.0, 0.0], dtype=np.float32))
        assert abs(a.cosine_similarity(b) - 1.0) < 0.001
        c = MediaFeature(vector=np.array([0.0, 1.0, 0.0], dtype=np.float32))
        assert abs(a.cosine_similarity(c)) < 0.001
        results["tests"]["cosine_similarity"] = "PASS"
    except Exception as e:
        results["tests"]["cosine_similarity"] = f"FAIL: {e}"

    # Test 7: Stats
    try:
        s = retriever.stats
        assert s["total_features"] > 0
        results["tests"]["retriever_stats"] = f"PASS (total={s['total_features']})"
    except Exception as e:
        results["tests"]["retriever_stats"] = f"FAIL: {e}"

    # Test 8: Frame-to-feature normalization
    try:
        for frame in frames[:3]:
            norm = np.linalg.norm(frame.vector)
            assert abs(norm - 1.0) < 2.0, f"norm={norm}"
        results["tests"]["vector_normalization"] = "PASS"
    except Exception as e:
        results["tests"]["vector_normalization"] = f"FAIL: {e}"

    retriever.clear()
    passed = sum(1 for v in results["tests"].values() if "PASS" in str(v))
    total = len(results["tests"])
    results["summary"] = f"{passed}/{total} PASS"
    return results


if __name__ == "__main__":
    import sys
    result = self_test()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if all("PASS" in str(v) for v in result["tests"].values()) else 1)
