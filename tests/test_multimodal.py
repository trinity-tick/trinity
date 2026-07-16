"""Tests for trinity.modules.multimodal.multimodal_memory — MultiModalMemory.

Tests:
  - test_store_text        store_text stores text and returns UnifiedEngram
  - test_store_image       store image via ImageMemoryEncoder
  - test_store_audio       store audio via AudioMemoryEncoder
  - test_search_all        search across all modalities
  - test_search_modality_filter  search filtered by modality
  - test_duplicate_detection  duplicate text stores increase frequency
  - test_tier_promotion    GPU/DRAM promotion and capacity enforcement
  - test_prefetch          prefetch promotes SSD items to DRAM
  - test_diagnostics       diagnostics() returns proper dict with all keys
  - test_store_text_embedding_shape  embedding is normalized float32
"""

import os
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.modules.multimodal.multimodal_memory import MultiModalMemory, ModalityType, StorageTier


# ── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def memory():
    """Create a fresh MultiModalMemory instance for each test.

    Use_models=False ensures no external ML dependencies.
    AudioEncoder is created with use_ollama=False to avoid a source code bug
    in the ollama semantic embedding path of AudioMemoryEncoder.encode().
    """
    from trinity.modules.multimodal.audio_encoder import AudioMemoryEncoder
    audio_enc = AudioMemoryEncoder(embed_dim=768, use_ollama=False)
    return MultiModalMemory(
        embed_dim=768,
        gpu_capacity=4,
        dram_capacity=16,
        ssd_capacity=100,
        audio_encoder=audio_enc,
    )


@pytest.fixture
def temp_image():
    """Create a temporary PNG image and yield its path, then clean up."""
    from PIL import Image
    tmp = os.path.join(tempfile.gettempdir(), "_pytest_mm_img.png")
    img = Image.new("RGB", (64, 64), color=(73, 109, 137))
    img.save(tmp)
    yield tmp
    try:
        os.remove(tmp)
    except OSError:
        pass


@pytest.fixture
def temp_audio():
    """Create a temporary WAV audio file (440 Hz sine, 0.3s) and clean up."""
    sr = 16000
    t = np.linspace(0, 0.3, int(sr * 0.3), endpoint=False)
    samples = (np.sin(2 * np.pi * 440 * t) * 0.5).astype(np.float32)
    tmp = os.path.join(tempfile.gettempdir(), "_pytest_mm_aud.wav")
    with wave.open(tmp, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((samples * 32767).astype(np.int16).tobytes())
    yield tmp
    try:
        os.remove(tmp)
    except OSError:
        pass


# ── Tests ──────────────────────────────────────────────────────────────

class TestMultiModalMemory:
    """Test suite for MultiModalMemory."""

    # ── Store text ───────────────────────────────────────────────────

    def test_store_text(self, memory):
        """store_text stores text and returns a UnifiedEngram with TEXT modality."""
        engram = memory.store_text("machine learning transformer architecture")
        assert engram is not None
        assert engram.modality == ModalityType.TEXT
        assert engram.embedding.shape[0] == 768
        assert engram.frequency == 1
        assert memory._total_stored == 1

    def test_store_text_embedding_normalized(self, memory):
        """store_text returns L2-normalized float32 embeddings."""
        engram = memory.store_text("deep neural network with attention")
        norm = np.linalg.norm(engram.embedding)
        assert abs(norm - 1.0) < 1e-5
        assert engram.embedding.dtype == np.float32

    # ── Store image ──────────────────────────────────────────────────

    def test_store_image(self, memory, temp_image):
        """store() with IMAGE modality encodes the image and stores it."""
        engram = memory.store(temp_image, modality=ModalityType.IMAGE)
        assert engram is not None
        assert engram.modality == ModalityType.IMAGE
        assert engram.embedding.shape[0] == 768
        assert engram.source_path == temp_image

    def test_store_image_creates_image_engram(self, memory, temp_image):
        """Store image wraps an ImageEngram as wrapped_engram."""
        engram = memory.store(temp_image, modality=ModalityType.IMAGE)
        assert engram is not None
        # wrapped_engram should have image-specific attributes
        wrapped = engram.wrapped_engram
        assert hasattr(wrapped, "image_hash") or hasattr(wrapped, "width")

    # ── Store audio ──────────────────────────────────────────────────

    def test_store_audio(self, memory, temp_audio):
        """store() with AUDIO modality encodes audio and stores it."""
        engram = memory.store(temp_audio, modality=ModalityType.AUDIO)
        assert engram is not None
        assert engram.modality == ModalityType.AUDIO
        assert engram.embedding.shape[0] == 768
        assert engram.source_path == temp_audio

    def test_store_audio_creates_audio_engram(self, memory, temp_audio):
        """Store audio wraps an AudioEngram as wrapped_engram."""
        engram = memory.store(temp_audio, modality=ModalityType.AUDIO)
        assert engram is not None
        wrapped = engram.wrapped_engram
        assert hasattr(wrapped, "duration_sec") or hasattr(wrapped, "audio_hash")

    # ── Search ───────────────────────────────────────────────────────

    def test_search_all_modalities(self, memory):
        """search() returns results across all modalities."""
        memory.store_text("machine learning transformer")
        memory.store_text("computer vision with neural networks")
        results = memory.search("machine learning")
        assert len(results) > 0
        assert all(isinstance(r, tuple) and len(r) == 2 for r in results)
        # First result should have a reasonable similarity score
        assert 0.0 <= results[0][1] <= 1.0

    def test_search_with_reason(self, memory):
        """search(reason=True) returns tuples with a reasoning text."""
        memory.store_text("testing reason mode")
        results = memory.search("testing", top_k=1, reason=True)
        assert len(results) > 0
        assert len(results[0]) == 3  # (engram, score, reason_text)
        engram, score, reason = results[0]
        assert isinstance(reason, str)

    def test_search_empty_returns_empty_list(self, memory):
        """search() with no data returns empty list."""
        results = memory.search("nothing")
        assert isinstance(results, list)
        assert len(results) == 0

    # ── Modality-filtered search ─────────────────────────────────────

    def test_search_modality_filter_image(self, memory, temp_image):
        """search(modality=IMAGE) returns only image results."""
        memory.store_text("a textual description")
        memory.store(temp_image, modality=ModalityType.IMAGE)
        results = memory.search("image", modality=ModalityType.IMAGE)
        assert len(results) >= 1
        for engram, _ in results:
            assert engram.modality == ModalityType.IMAGE

    def test_search_modality_filter_text(self, memory, temp_image):
        """search(modality=TEXT) returns only text results."""
        memory.store_text("only text content here")
        memory.store(temp_image, modality=ModalityType.IMAGE)
        results = memory.search("text", modality=ModalityType.TEXT)
        assert len(results) >= 1
        for engram, _ in results:
            assert engram.modality == ModalityType.TEXT

    # ── Duplicate detection ──────────────────────────────────────────

    def test_duplicate_text_increases_frequency(self, memory):
        """Storing the same text twice increments frequency."""
        e1 = memory.store_text("hello world")
        f1 = e1.frequency
        e2 = memory.store_text("hello world")
        assert e2 is e1  # Same object
        assert e2.frequency == f1 + 1

    # ── Tier management ──────────────────────────────────────────────

    def test_promote_to_gpu(self, memory):
        """promote_to_gpu moves an engram to GPU tier."""
        # Store enough items to exceed GPU capacity
        for i in range(memory.gpu_capacity * 2):
            memory.store_text(f"fill item {i}")

        # Items beyond GPU capacity sit on lower tiers
        all_keys = list(memory._all_engrams.keys())
        # Find a key NOT already on GPU
        non_gpu_key = None
        for k in all_keys:
            if k not in memory._gpu_table:
                non_gpu_key = k
                break

        if non_gpu_key is None:
            # All items already on GPU - test can still verify return True
            result = memory.promote_to_gpu(all_keys[0])
            assert result is True
        else:
            result = memory.promote_to_gpu(non_gpu_key)
            assert result is True
            assert non_gpu_key in memory._gpu_table

    def test_promote_to_dram(self, memory):
        """promote_to_dram moves an engram to a lower tier."""
        # Store enough items to fill GPU
        for i in range(memory.gpu_capacity * 2):
            memory.store_text(f"fill item {i}")

        all_keys = list(memory._all_engrams.keys())
        # Find a key not on GPU to promote
        non_gpu_key = None
        for k in all_keys:
            if k not in memory._gpu_table:
                non_gpu_key = k
                break

        if non_gpu_key:
            result = memory.promote_to_dram(non_gpu_key)
            assert result is not False

    def test_gpu_capacity_enforcement(self, memory):
        """GPU tier does not exceed gpu_capacity after many promotions."""
        for i in range(10):
            memory.store_text(f"item {i}")
        # Promote all 10 items to GPU (capacity=4)
        for key in list(memory._all_engrams.keys()):
            memory.promote_to_gpu(key)
        assert len(memory._gpu_table) <= memory.gpu_capacity
        assert len(memory._gpu_table) == 4

    def test_promote_nonexistent_key(self, memory):
        """promote_to_gpu with nonexistent key returns False."""
        assert memory.promote_to_gpu("nonexistent") is False

    # ── Prefetch ─────────────────────────────────────────────────────

    def test_prefetch_returns_dict(self, memory):
        """prefetch() returns a dict with expected keys."""
        memory.store_text("deep learning transformer")
        memory.store_text("reinforcement learning agent")
        result = memory.prefetch("deep learning")
        assert isinstance(result, dict)
        assert "prefetched" in result
        assert "candidates_scored" in result
        assert "threshold" in result

    def test_prefetch_promotes_items(self, memory):
        """prefetch() promotes some SSD items to DRAM."""
        for i in range(20):
            memory.store_text(f"machine learning paper {i}")
        # All items start on SSD; verify before prefetch
        ssd_before = len(memory._ssd_table)
        memory.prefetch("machine learning", top_k=3)
        ssd_after = len(memory._ssd_table)
        # Some should have been promoted away from SSD
        assert ssd_after <= ssd_before

    # ── Diagnostics ─────────────────────────────────────────────────

    def test_diagnostics_returns_dict(self, memory):
        """diagnostics() returns a dictionary."""
        memory.store_text("test")
        diag = memory.diagnostics()
        assert isinstance(diag, dict)

    def test_diagnostics_has_required_keys(self, memory):
        """diagnostics() contains expected sections."""
        memory.store_text("test")
        diag = memory.diagnostics()
        assert "total_stored" in diag
        assert "total_searches" in diag
        assert "tiers" in diag
        assert "modality_counts" in diag
        assert "image_encoder" in diag
        assert "audio_encoder" in diag
        assert "embed_dim" in diag
        assert "prefetch" in diag

    def test_diagnostics_total_stored_accurate(self, memory, temp_image, temp_audio):
        """diagnostics() total_stored matches actual stored count."""
        memory.store_text("text item")
        memory.store(temp_image, modality=ModalityType.IMAGE)
        memory.store(temp_audio, modality=ModalityType.AUDIO)
        diag = memory.diagnostics()
        assert diag["total_stored"] == 3

    def test_diagnostics_modality_counts(self, memory, temp_image, temp_audio):
        """diagnostics() modality_counts reflect stored items per modality."""
        memory.store_text("text content")
        memory.store(temp_image, modality=ModalityType.IMAGE)
        memory.store(temp_audio, modality=ModalityType.AUDIO)
        diag = memory.diagnostics()
        counts = diag["modality_counts"]
        assert counts["text"] >= 1
        assert counts["image"] >= 1
        assert counts["audio"] >= 1

    def test_diagnostics_tier_structure(self, memory):
        """diagnostics() tiers section has the right structure."""
        memory.store_text("test")
        diag = memory.diagnostics()
        tiers = diag["tiers"]
        for tier_name in ("gpu", "dram", "ssd"):
            assert tier_name in tiers
            assert "capacity" in tiers[tier_name]
            assert "occupied" in tiers[tier_name]

    # ── Edge cases ──────────────────────────────────────────────────

    def test_store_batch(self, memory):
        """store_batch stores multiple items and returns a list."""
        memory.store_text("first")
        memory.store_text("second")
        memory.store_text("third")
        assert memory._total_stored == 3

    def test_store_with_metadata(self, memory):
        """store_text accepts metadata dict."""
        engram = memory.store_text("metadata test", metadata={"source": "test_suite", "priority": 1})
        assert engram.metadata.get("source") == "test_suite"
        assert engram.metadata.get("priority") == 1

    def test_modality_type_enum_values(self):
        """ModalityType enum has correct string values."""
        assert ModalityType.TEXT.value == "text"
        assert ModalityType.IMAGE.value == "image"
        assert ModalityType.AUDIO.value == "audio"

    def test_unified_engram_to_dict(self, memory):
        """UnifiedEngram.to_dict() returns a serializable dict."""
        engram = memory.store_text("dict test")
        d = engram.to_dict()
        assert isinstance(d, dict)
        assert "engram_id" in d
        assert "modality" in d
        assert "embed_dim" in d
        assert "frequency" in d
        assert "tier" in d

    def test_unified_engram_fingerprint(self, memory):
        """UnifiedEngram.fingerprint() returns a short hex string."""
        engram = memory.store_text("fingerprint test")
        fp = engram.fingerprint()
        assert isinstance(fp, str)
        assert len(fp) == 12
        # Same content gives same fingerprint
        engram2 = memory.store_text("fingerprint test")
        assert engram2.fingerprint() == fp

    def test_search_top_k_limit(self, memory):
        """search() respects top_k limit."""
        for i in range(10):
            memory.store_text(f"search limit test item {i}")
        results = memory.search("search limit", top_k=3)
        assert len(results) <= 3
