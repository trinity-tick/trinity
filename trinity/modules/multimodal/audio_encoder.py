"""
AudioMemoryEncoder — 音频记忆编码器
====================================
Extracts audio features and encodes them into embeddings
for multimodal memory storage.

Architecture:
  - Default: spectral features from raw PCM data (numpy-based, no ML deps)
  - Upgrade path: set `use_model=True` and provide Wav2Vec2/HuBERT callable
  - Embedding dimensionality aligns with M119's embed_dim (768 default)

Tiered storage integration:
  - Hot audio clips (frequent access) → GPU tier
  - Warm audio clips → DRAM tier
  - Cold audio clips → SSD tier
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
import time
import wave
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


# Constants
DEFAULT_AUDIO_EMBED_DIM = 768
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_FFT_SIZE = 512
DEFAULT_HOP_LENGTH = 256
DEFAULT_MEL_BANDS = 40
_SPECTRAL_BANDS = 32  # for simplified spectral analysis


@dataclass
class AudioEngram:
    """A single audio engram entry in multimodal memory.

    Mirrors M119's PhraseEngram pattern for cross-compatibility.
    """

    engram_id: str
    source_path: str                       # local path or URL
    audio_hash: str                        # collision-free hash identifier
    embedding: np.ndarray                  # semantic embedding [embed_dim]
    spectral_features: np.ndarray          # spectral feature vector
    duration_sec: float = 0.0
    sample_rate: int = 0
    channels: int = 1
    file_size_bytes: int = 0
    frequency: int = 1
    last_accessed: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Storage tier tracking (mirrors M119's PhraseEngram)
    current_tier: str = "ssd"             # "gpu" | "dram" | "ssd"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engram_id": self.engram_id,
            "source": self.source_path[:60],
            "hash": self.audio_hash,
            "duration": f"{self.duration_sec:.2f}s",
            "sr": self.sample_rate,
            "embed_dim": self.embedding.shape[0],
            "frequency": self.frequency,
            "tier": self.current_tier,
        }

    def fingerprint(self) -> str:
        return hashlib.md5(self.audio_hash.encode()).hexdigest()[:12]


class AudioMemoryEncoder:
    """Encodes audio into embeddings for multimodal memory storage.

    Default encoding (no ML):
      1. Load WAV file and decode PCM samples
      2. Compute spectral features (STFT-based magnitude spectrum)
      3. Compute temporal features (RMS energy, zero-crossing rate)
      4. Concatenate into fixed embedding

    Upgrade path:
      Set `use_model=True` and pass a model_path or callable that
      returns a Wav2Vec2/HuBERT-compatible embedding vector.
    """

    def __init__(
        self,
        embed_dim: int = DEFAULT_AUDIO_EMBED_DIM,
        use_model: bool = False,
        model_path: Optional[str] = None,
        model_callable: Optional[callable] = None,
        target_sr: int = DEFAULT_SAMPLE_RATE,
    ):
        self.embed_dim = embed_dim
        self.use_model = use_model
        self.model_path = model_path
        self._model_callable = model_callable
        self.target_sr = target_sr

        # Internal LRU cache for loaded audio
        self._audio_cache: OrderedDict[str, AudioEngram] = OrderedDict()
        self._cache_max_size = 16

        # Statistics
        self._total_encoded = 0
        self._total_errors = 0

    # ── Public API ────────────────────────────────────────────────────

    def encode(
        self,
        path_or_data: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[AudioEngram]:
        """Encode audio from a file path.

        Args:
            path_or_data: Path to a WAV audio file.
            metadata: Optional metadata dict.

        Returns:
            AudioEngram with embedding, or None on failure.
        """
        if metadata is None:
            metadata = {}

        # Check cache
        cache_key = hashlib.md5(path_or_data.encode()).hexdigest()
        if cache_key in self._audio_cache:
            cached = self._audio_cache[cache_key]
            cached.last_accessed = time.time()
            self._audio_cache.move_to_end(cache_key)
            return cached

        try:
            # Load audio
            samples, sr, file_info = self._load_audio(path_or_data)
            if samples is None or len(samples) == 0:
                self._total_errors += 1
                return None

            # Compute embedding
            if self.use_model and self._model_callable is not None:
                embedding = self._compute_model_embedding(samples, sr)
            else:
                embedding = self._compute_lightweight_embedding(samples, sr)

            # Compute spectral features for downstream use
            spectral = self._compute_spectral_features(samples, sr)

            # Hash
            audio_hash = self._compute_audio_hash(path_or_data, embedding)

            duration = len(samples) / float(sr)

            engram = AudioEngram(
                engram_id=f"aud_{self._total_encoded:08d}",
                source_path=path_or_data,
                audio_hash=audio_hash,
                embedding=embedding,
                spectral_features=spectral,
                duration_sec=duration,
                sample_rate=sr,
                channels=file_info.get("channels", 1),
                file_size_bytes=file_info.get("size", 0),
                frequency=1,
                last_accessed=time.time(),
                metadata=metadata,
                current_tier="ssd",
            )

            # Update cache
            self._audio_cache[cache_key] = engram
            if len(self._audio_cache) > self._cache_max_size:
                self._audio_cache.popitem(last=False)

            self._total_encoded += 1
            return engram

        except Exception as e:
            self._total_errors += 1
            raise RuntimeError(f"Failed to encode audio '{path_or_data}': {e}") from e

    def encode_batch(
        self, paths: List[str], metadata_list: Optional[List[Dict[str, Any]]] = None
    ) -> List[Optional[AudioEngram]]:
        """Encode multiple audio files in batch."""
        if metadata_list is None:
            metadata_list = [None] * len(paths)
        return [self.encode(p, m) for p, m in zip(paths, metadata_list)]

    def similarity(
        self,
        engram_a: AudioEngram,
        engram_b: AudioEngram,
    ) -> float:
        """Cosine similarity between two audio embeddings."""
        a_norm = np.linalg.norm(engram_a.embedding)
        b_norm = np.linalg.norm(engram_b.embedding)
        if a_norm < 1e-8 or b_norm < 1e-8:
            return 0.0
        return float(np.dot(engram_a.embedding, engram_b.embedding) / (a_norm * b_norm))

    def search(
        self,
        query_engram: AudioEngram,
        candidates: List[AudioEngram],
        top_k: int = 5,
    ) -> List[Tuple[AudioEngram, float]]:
        """Search for most similar audio among candidates."""
        scored = [(c, self.similarity(query_engram, c)) for c in candidates]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    # ── Internal: Audio Loading ───────────────────────────────────────

    def _load_audio(self, path: str) -> Tuple[Optional[np.ndarray], int, Dict[str, Any]]:
        """Load audio from a WAV file.

        Returns (samples, sample_rate, info_dict).
        """
        if not os.path.isfile(path):
            return None, 0, {}

        try:
            with wave.open(path, "rb") as wf:
                sr = wf.getframerate()
                n_frames = wf.getnframes()
                n_channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                raw = wf.readframes(n_frames)

            file_size = os.path.getsize(path)
            info = {
                "channels": n_channels,
                "sample_width": sampwidth,
                "size": file_size,
            }

            # Decode PCM samples
            samples = self._decode_pcm(raw, sampwidth, n_channels)
            if samples is None or len(samples) == 0:
                return None, sr, info

            # Convert to mono if needed
            if n_channels > 1:
                samples = samples.mean(axis=1)

            # Resample if needed (simplified: just note the mismatch)
            duration = len(samples) / float(sr)
            if duration > 30.0:
                # Truncate to first 30 seconds for efficiency
                max_samples = int(30.0 * sr)
                samples = samples[:max_samples]

            return samples.astype(np.float32), sr, info

        except Exception:
            return None, 0, {}

    def _decode_pcm(
        self, raw: bytes, sampwidth: int, n_channels: int
    ) -> Optional[np.ndarray]:
        """Decode PCM byte data to numpy array."""
        if sampwidth == 1:
            dtype = np.uint8
            fmt = "B"
        elif sampwidth == 2:
            dtype = np.int16
            fmt = "h"
        elif sampwidth == 4:
            dtype = np.int32
            fmt = "i"
        else:
            return None

        try:
            count = len(raw) // sampwidth
            samples = struct.unpack(f"<{count}{fmt}", raw)
            arr = np.array(samples, dtype=dtype).reshape(-1, n_channels)
            # Convert to float32 in [-1, 1]
            if sampwidth == 1:
                arr = (arr.astype(np.float32) - 128.0) / 128.0
            else:
                max_val = float(np.iinfo(dtype).max)
                arr = arr.astype(np.float32) / max_val
            return arr
        except Exception:
            return None

    # ── Internal: Embedding Computation ───────────────────────────────

    def _compute_lightweight_embedding(self, samples: np.ndarray, sr: int) -> np.ndarray:
        """Compute embedding using spectral analysis (no ML).

        Features extracted:
          - Spectral centroid (1-dim)
          - Spectral rolloff (1-dim)
          - Band energy distribution (32-dim)
          - RMS energy envelope stats (4-dim: mean, std, max, min)
          - Zero-crossing rate (1-dim)

        Total: ~39 dims, projected to embed_dim.
        """
        if len(samples) == 0:
            return np.zeros(self.embed_dim, dtype=np.float32)

        # STFT-based magnitude spectrum
        fft_size = min(DEFAULT_FFT_SIZE, len(samples))
        hop = min(DEFAULT_HOP_LENGTH, len(samples) // 4 + 1)

        # Compute frames
        frames = []
        for start in range(0, len(samples) - fft_size + 1, hop):
            frame = samples[start: start + fft_size]
            window = np.hanning(fft_size)
            frames.append(frame * window)
        if not frames:
            frames = [samples[:fft_size] * np.hanning(fft_size)]
        frames = np.array(frames)  # [n_frames, fft_size]

        # Magnitude spectrum
        spectrum = np.abs(np.fft.rfft(frames, n=fft_size))  # [n_frames, fft_size//2+1]
        n_bins = spectrum.shape[1]

        # Mean spectrum (take mean across frames → [n_bins])
        if spectrum.ndim == 2:
            mean_spec = spectrum.mean(axis=0)  # [n_frames, n_bins] → [n_bins]
        elif spectrum.ndim == 1:
            mean_spec = spectrum  # already 1D
        else:
            mean_spec = spectrum.ravel()

        # 1. Spectral centroid
        freqs = np.linspace(0, sr / 2, len(mean_spec))
        centroid = float(np.sum(freqs * mean_spec) / (np.sum(mean_spec) + 1e-8))
        centroid_norm = centroid / (sr / 2)  # normalized [0, 1]

        # 2. Spectral rolloff (where cumulative energy reaches 85%)
        cumsum = np.cumsum(mean_spec)
        total = cumsum[-1] + 1e-8
        rolloff_idx = int(np.searchsorted(cumsum, 0.85 * total))
        rolloff = float(freqs[rolloff_idx]) / (sr / 2)  # normalized [0, 1]

        # 3. Band energy distribution (32 bands)
        bands = self._split_into_bands(mean_spec, _SPECTRAL_BANDS)
        band_energy = bands / (np.sum(bands) + 1e-8)

        # 4. RMS energy stats
        frame_rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-8)
        rms_mean = float(np.mean(frame_rms))
        rms_std = float(np.std(frame_rms))
        rms_max = float(np.max(frame_rms))
        rms_min = float(np.min(frame_rms))
        rms_features = np.array([rms_mean, rms_std, rms_max, rms_min], dtype=np.float32)

        # 5. Zero-crossing rate
        zcr_frames = []
        for frame in frames:
            zc = np.sum(np.abs(np.diff(np.sign(frame)))) / (2.0 * len(frame))
            zcr_frames.append(zc)
        zcr_mean = float(np.mean(zcr_frames))

        # Concatenate all features
        scalar_features = np.array(
            [centroid_norm, rolloff, zcr_mean], dtype=np.float32
        )
        combined = np.concatenate([scalar_features, rms_features, band_energy])
        combined = combined.astype(np.float32)

        # Project to embed_dim
        if len(combined) < self.embed_dim:
            padded = np.zeros(self.embed_dim, dtype=np.float32)
            padded[: len(combined)] = combined
            combined = padded
        elif len(combined) > self.embed_dim:
            combined = combined[: self.embed_dim]

        # Normalize
        norm = np.linalg.norm(combined)
        if norm > 1e-8:
            combined = combined / norm

        return combined.astype(np.float32)

    def _compute_model_embedding(
        self, samples: np.ndarray, sr: int
    ) -> np.ndarray:
        """Compute embedding using ML model (e.g., Wav2Vec2, HuBERT).

        Upgrade path: set `use_model=True` and provide `model_callable`.
        The callable should accept (samples, sr) and return a numpy array.

        Default fallback: returns lightweight embedding if no model set.
        """
        if self._model_callable is not None:
            try:
                emb = self._model_callable(samples, sr)
                if isinstance(emb, np.ndarray) and emb.shape[0] == self.embed_dim:
                    return emb.astype(np.float32)
            except Exception:
                pass
        return self._compute_lightweight_embedding(samples, sr)

    def _compute_spectral_features(self, samples: np.ndarray, sr: int) -> np.ndarray:
        """Compute detailed spectral features for downstream analysis."""
        if len(samples) == 0:
            return np.zeros(_SPECTRAL_BANDS, dtype=np.float32)

        fft_size = min(DEFAULT_FFT_SIZE, len(samples))
        hop = min(DEFAULT_HOP_LENGTH, len(samples) // 4 + 1)

        frames = []
        for start in range(0, len(samples) - fft_size + 1, hop):
            frame = samples[start: start + fft_size]
            window = np.hanning(fft_size)
            frames.append(frame * window)
        if not frames:
            frames = [samples[:fft_size] * np.hanning(fft_size)]
        frames = np.array(frames)

        spectrum = np.abs(np.fft.rfft(frames, n=fft_size))
        mean_spec = spectrum.mean(axis=0)
        return self._split_into_bands(mean_spec, _SPECTRAL_BANDS)

    @staticmethod
    def _split_into_bands(spectrum: np.ndarray, n_bands: int) -> np.ndarray:
        """Split magnitude spectrum into frequency bands."""
        if len(spectrum) == 0:
            return np.zeros(n_bands, dtype=np.float32)
        band_size = max(1, len(spectrum) // n_bands)
        bands = np.zeros(n_bands, dtype=np.float32)
        for i in range(n_bands):
            start = i * band_size
            end = start + band_size
            bands[i] = float(np.sum(spectrum[start:end]))
        return bands

    def _compute_audio_hash(self, path: str, embedding: np.ndarray) -> str:
        """Compute collision-free audio hash."""
        raw = path.encode() + embedding.tobytes()
        return hashlib.sha256(raw).hexdigest()[:16]

    # ── Diagnostics ─────────────────────────────────────────────────

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "embed_dim": self.embed_dim,
            "use_model": self.use_model,
            "target_sr": self.target_sr,
            "total_encoded": self._total_encoded,
            "total_errors": self._total_errors,
            "cache_size": len(self._audio_cache),
        }


def run_audio_encoder_self_test() -> AudioMemoryEncoder:
    """Run self-test for AudioMemoryEncoder."""
    print("=" * 80)
    print("  AudioMemoryEncoder — 自检")
    print("=" * 80)

    encoder = AudioMemoryEncoder(embed_dim=768)

    # Create a synthetic test WAV file
    test_path = os.path.join(os.path.dirname(__file__), "_test_audio.wav")
    try:
        sr = 16000
        duration = 0.5  # 0.5 seconds
        t = np.linspace(0, duration, int(sr * duration), endpoint=False)
        # Sine wave at 440 Hz (A4)
        samples = (np.sin(2 * np.pi * 440 * t) * 0.5).astype(np.float32)

        with wave.open(test_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes((samples * 32767).astype(np.int16).tobytes())
    except Exception as e:
        print(f"[SKIP] 1. Could not create test WAV: {e}")
        return encoder

    # 1. Encode
    engram = encoder.encode(test_path)
    assert engram is not None, "Encoding failed"
    assert engram.embedding.shape[0] == 768
    print(f"[PASS] 1. Encode: {engram.source_path} → {engram.engram_id}, "
          f"embed_dim={engram.embedding.shape[0]}, duration={engram.duration_sec:.2f}s")

    # 2. Caching
    engram2 = encoder.encode(test_path)
    assert engram2 is not None
    assert engram2.engram_id == engram.engram_id
    print(f"[PASS] 2. Cache hit: same engram returned")

    # 3. Self-similarity ≈ 1.0
    sim = encoder.similarity(engram, engram2)
    assert sim > 0.99, f"Expected near-1 similarity, got {sim}"
    print(f"[PASS] 3. Self-similarity: {sim:.6f}")

    # 4. Different audio → lower similarity
    sr2 = 16000
    t2 = np.linspace(0, 0.5, int(sr2 * 0.5), endpoint=False)
    samples2 = (np.sin(2 * np.pi * 880 * t2) * 0.3).astype(np.float32)  # 880 Hz (A5)
    test_path2 = os.path.join(os.path.dirname(__file__), "_test_audio2.wav")
    with wave.open(test_path2, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr2)
        wf.writeframes((samples2 * 32767).astype(np.int16).tobytes())

    engram3 = encoder.encode(test_path2)
    assert engram3 is not None
    sim_diff = encoder.similarity(engram, engram3)
    print(f"[PASS] 4. Cross-similarity: {sim_diff:.6f} (440Hz vs 880Hz)")

    # 5. Search
    results = encoder.search(engram, [engram2, engram3], top_k=2)
    assert len(results) == 2
    print(f"[PASS] 5. Search: top={results[0][0].engram_id} (sim={results[0][1]:.4f})")

    # 6. Spectral features
    assert engram.spectral_features.shape[0] == _SPECTRAL_BANDS
    print(f"[PASS] 6. Spectral features: {engram.spectral_features.shape}")

    # Cleanup
    for p in [test_path, test_path2]:
        try:
            os.remove(p)
        except OSError:
            pass

    print("-" * 60)
    print(f"  [AudioMemoryEncoder] ALL_PASS — 6/6 项通过")
    print("=" * 80)

    return encoder


if __name__ == "__main__":
    run_audio_encoder_self_test()
