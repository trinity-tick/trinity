#!/usr/bin/env python3
"""
Test: MultiModal Module — 多模态模块测试
==========================================
Validates the multimodal extension module loads and basic functionality works.

Tests:
  1. Module imports
  2. ImageMemoryEncoder basic functionality
  3. AudioMemoryEncoder basic functionality
  4. MultiModalMemory unified tiered storage
  5. Cross-modal search
  6. Diagnostic output
"""

import os
import sys
import time
import tempfile
import traceback

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

SEP = "=" * 80
SUB = "-" * 60


def test_module_imports():
    """Test 1: Module loads correctly."""
    print(f"\n{SUB}")
    print("  Test 1: Module Imports")
    print(f"{SUB}")

    try:
        from trinity.modules.multimodal import (
            ImageMemoryEncoder,
            AudioMemoryEncoder,
            MultiModalMemory,
            MODULE_ID,
            MODULE_VERSION,
        )
        print(f"    [OK] Loaded trinity.modules.multimodal")
        print(f"    [OK] MODULE_ID = {MODULE_ID}")
        print(f"    [OK] MODULE_VERSION = {MODULE_VERSION}")
        print(f"    [OK] Exports: ImageMemoryEncoder, AudioMemoryEncoder, MultiModalMemory")
        return True
    except Exception as e:
        print(f"    [FAIL] Import error: {e}")
        traceback.print_exc()
        return False


def test_image_encoder():
    """Test 2: ImageMemoryEncoder basic functionality."""
    print(f"\n{SUB}")
    print("  Test 2: ImageMemoryEncoder")
    print(f"{SUB}")

    try:
        from PIL import Image, ImageDraw
        from trinity.modules.multimodal import ImageMemoryEncoder

        encoder = ImageMemoryEncoder(embed_dim=768)

        # Create test image
        tmp = os.path.join(tempfile.gettempdir(), "_test_mm_img.png")
        img = Image.new("RGB", (100, 100), color=(73, 109, 137))
        draw = ImageDraw.Draw(img)
        draw.rectangle([20, 20, 80, 80], fill=(255, 200, 100))
        img.save(tmp)

        # Encode
        engram = encoder.encode(tmp)
        assert engram is not None, "encode() returned None"
        assert engram.embedding.shape[0] == 768, f"Expected embed_dim=768, got {engram.embedding.shape[0]}"
        assert engram.width == 100
        assert engram.height == 100

        # Cache hit
        engram2 = encoder.encode(tmp)
        assert engram2 is not None
        assert engram2.engram_id == engram.engram_id

        # Different image
        tmp2 = os.path.join(tempfile.gettempdir(), "_test_mm_img2.png")
        img2 = Image.new("RGB", (100, 100), color=(255, 0, 0))
        draw2 = ImageDraw.Draw(img2)
        draw2.ellipse([10, 10, 90, 90], fill=(0, 255, 0))
        img2.save(tmp2)

        engram3 = encoder.encode(tmp2)
        assert engram3 is not None

        # Similarity
        sim_self = encoder.similarity(engram, engram2)
        assert sim_self > 0.99, f"Self-similarity too low: {sim_self}"

        sim_diff = encoder.similarity(engram, engram3)
        assert sim_diff < 0.99, f"Cross-similarity too high: {sim_diff}"

        # Search
        results = encoder.search(engram, [engram2, engram3], top_k=2)
        assert len(results) == 2

        # Cleanup
        for p in [tmp, tmp2]:
            try:
                os.remove(p)
            except OSError:
                pass

        print(f"    [OK] Encode: embed_dim={engram.embedding.shape[0]}, hash={engram.image_hash}")
        print(f"    [OK] Cache: same engram returned on re-encode")
        print(f"    [OK] Self-similarity: {sim_self:.6f}")
        print(f"    [OK] Cross-similarity: {sim_diff:.6f}")
        print(f"    [OK] Search: {len(results)} results")
        print(f"    [OK] Diagnostics: {encoder.diagnostics()}")
        return True

    except Exception as e:
        print(f"    [FAIL] {e}")
        traceback.print_exc()
        return False


def test_audio_encoder():
    """Test 3: AudioMemoryEncoder basic functionality."""
    print(f"\n{SUB}")
    print("  Test 3: AudioMemoryEncoder")
    print(f"{SUB}")

    try:
        import wave
        import numpy as np
        from trinity.modules.multimodal import AudioMemoryEncoder

        encoder = AudioMemoryEncoder(embed_dim=768)

        # Create test audio (440 Hz sine wave)
        tmp = os.path.join(tempfile.gettempdir(), "_test_mm_aud.wav")
        sr = 16000
        t = np.linspace(0, 0.5, int(sr * 0.5), endpoint=False)
        samples = (np.sin(2 * np.pi * 440 * t) * 0.5).astype(np.float32)
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes((samples * 32767).astype(np.int16).tobytes())

        # Encode
        engram = encoder.encode(tmp)
        assert engram is not None, "encode() returned None"
        assert engram.embedding.shape[0] == 768, f"Expected embed_dim=768, got {engram.embedding.shape[0]}"
        assert engram.duration_sec > 0

        # Cache hit
        engram2 = encoder.encode(tmp)
        assert engram2 is not None
        assert engram2.engram_id == engram.engram_id

        # Different audio (880 Hz)
        tmp2 = os.path.join(tempfile.gettempdir(), "_test_mm_aud2.wav")
        t2 = np.linspace(0, 0.5, int(sr * 0.5), endpoint=False)
        samples2 = (np.sin(2 * np.pi * 880 * t2) * 0.3).astype(np.float32)
        with wave.open(tmp2, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes((samples2 * 32767).astype(np.int16).tobytes())

        engram3 = encoder.encode(tmp2)
        assert engram3 is not None

        # Similarity
        sim_self = encoder.similarity(engram, engram2)
        assert sim_self > 0.99, f"Self-similarity too low: {sim_self}"

        sim_diff = encoder.similarity(engram, engram3)
        print(f"    [INFO] Cross-similarity (440Hz vs 880Hz): {sim_diff:.6f}")

        # Search
        results = encoder.search(engram, [engram2, engram3], top_k=2)
        assert len(results) == 2

        # Cleanup
        for p in [tmp, tmp2]:
            try:
                os.remove(p)
            except OSError:
                pass

        print(f"    [OK] Encode: embed_dim={engram.embedding.shape[0]}, duration={engram.duration_sec:.2f}s")
        print(f"    [OK] Cache: same engram returned on re-encode")
        print(f"    [OK] Self-similarity: {sim_self:.6f}")
        print(f"    [OK] Search: {len(results)} results")
        return True

    except Exception as e:
        print(f"    [FAIL] {e}")
        traceback.print_exc()
        return False


def test_multimodal_memory():
    """Test 4: MultiModalMemory unified tiered storage."""
    print(f"\n{SUB}")
    print("  Test 4: MultiModalMemory — Unified Tiered Storage")
    print(f"{SUB}")

    try:
        import wave
        import numpy as np
        from PIL import Image, ImageDraw
        from trinity.modules.multimodal import MultiModalMemory, ModalityType

        memory = MultiModalMemory(
            embed_dim=768,
            gpu_capacity=4,
            dram_capacity=16,
            ssd_capacity=100,
        )

        # Store text
        texts = ["deep learning", "neural networks", "computer vision"]
        for t in texts:
            memory.store_text(t)

        # Store images
        img_paths = []
        for i, color in enumerate([(255, 0, 0), (0, 255, 0)]):
            tmp = os.path.join(tempfile.gettempdir(), f"_test_mm_mem_img_{i}.png")
            img = Image.new("RGB", (64, 64), color=color)
            img.save(tmp)
            img_paths.append(tmp)
            memory.store(tmp, modality=ModalityType.IMAGE)

        # Store audio
        aud_paths = []
        sr = 16000
        for i, freq in enumerate([440, 880]):
            tmp = os.path.join(tempfile.gettempdir(), f"_test_mm_mem_aud_{i}.wav")
            t = np.linspace(0, 0.3, int(sr * 0.3), endpoint=False)
            samples = (np.sin(2 * np.pi * freq * t) * 0.5).astype(np.float32)
            with wave.open(tmp, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes((samples * 32767).astype(np.int16).tobytes())
            aud_paths.append(tmp)
            memory.store(tmp, modality=ModalityType.AUDIO)

        total = len(texts) + len(img_paths) + len(aud_paths)
        assert memory._total_stored == total

        # Search all
        results = memory.search("deep learning")
        assert len(results) > 0

        # Filter by modality
        img_results = memory.search("image", modality=ModalityType.IMAGE)
        assert len(img_results) > 0

        aud_results = memory.search("audio", modality=ModalityType.AUDIO)
        assert len(aud_results) > 0

        # Tier promotion
        key = list(memory._all_engrams.keys())[0]
        memory.promote_to_gpu(key)
        assert memory._all_engrams[key].current_tier.value == "gpu"

        # Prefetch
        prefetch_result = memory.prefetch("learning")
        assert prefetch_result["prefetched"] >= 0

        # Cleanup
        for p in img_paths + aud_paths:
            try:
                os.remove(p)
            except OSError:
                pass

        print(f"    [OK] Stored {total} items ({len(texts)} text, {len(img_paths)} image, {len(aud_paths)} audio)")
        print(f"    [OK] Text search: {len(results)} results")
        print(f"    [OK] Image-filtered search: {len(img_results)} results")
        print(f"    [OK] Audio-filtered search: {len(aud_results)} results")
        print(f"    [OK] GPU promotion: item moved to GPU tier")
        print(f"    [OK] Prefetch: {prefetch_result['prefetched']} items promoted")
        return True

    except Exception as e:
        print(f"    [FAIL] {e}")
        traceback.print_exc()
        return False


def test_cross_modal_search():
    """Test 5: Cross-modal search."""
    print(f"\n{SUB}")
    print("  Test 5: Cross-modal Search")
    print(f"{SUB}")

    try:
        from trinity.modules.multimodal import MultiModalMemory, ModalityType

        memory = MultiModalMemory(embed_dim=768)

        # Store diverse items
        memory.store_text("a red apple fruit")
        memory.store_text("blue ocean water")
        memory.store_text("green forest trees")

        # Search across all modalities
        results = memory.search("fruit")
        assert len(results) > 0
        top_source = results[0][0].source_path
        top_score = results[0][1]

        print(f"    [OK] Cross-modal search 'fruit': {len(results)} results")
        print(f"    [OK] Top result: {top_source} (sim={top_score:.4f})")

        # Note: with deterministic text hashing, the search may not find exact matches
        # in production, replace _text_to_embedding with Sentence-BERT

        return True

    except Exception as e:
        print(f"    [FAIL] {e}")
        traceback.print_exc()
        return False


def test_diagnostics():
    """Test 6: Diagnostics output."""
    print(f"\n{SUB}")
    print("  Test 6: Diagnostics")
    print(f"{SUB}")

    try:
        from trinity.modules.multimodal import (
            ImageMemoryEncoder,
            AudioMemoryEncoder,
            MultiModalMemory,
        )

        # Image encoder diagnostics
        img_enc = ImageMemoryEncoder()
        diag_img = img_enc.diagnostics()
        assert "embed_dim" in diag_img
        assert "use_model" in diag_img
        assert "has_pil" in diag_img

        # Audio encoder diagnostics
        aud_enc = AudioMemoryEncoder()
        diag_aud = aud_enc.diagnostics()
        assert "embed_dim" in diag_aud
        assert "use_model" in diag_aud

        # Memory diagnostics
        memory = MultiModalMemory()
        memory.store_text("test")
        diag_mem = memory.diagnostics()
        assert "total_stored" in diag_mem
        assert "tiers" in diag_mem
        assert "modality_counts" in diag_mem
        assert "image_encoder" in diag_mem
        assert "audio_encoder" in diag_mem

        print(f"    [OK] ImageMemoryEncoder diagnostics: {diag_img}")
        print(f"    [OK] AudioMemoryEncoder diagnostics: {diag_aud}")
        print(f"    [OK] MultiModalMemory diagnostics: {diag_mem}")
        return True

    except Exception as e:
        print(f"    [FAIL] {e}")
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    print(SEP)
    print("  MultiModal Module — Validation Suite")
    print(f"  Started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(SEP)

    tests = [
        ("Import module", test_module_imports),
        ("ImageMemoryEncoder", test_image_encoder),
        ("AudioMemoryEncoder", test_audio_encoder),
        ("MultiModalMemory tiered storage", test_multimodal_memory),
        ("Cross-modal search", test_cross_modal_search),
        ("Diagnostics", test_diagnostics),
    ]

    passed = 0
    failed = 0

    for name, func in tests:
        print(f"\n  -> {name}...")
        try:
            result = func()
            if result:
                passed += 1
            else:
                failed += 1
                print(f"    [FAIL] {name}")
        except Exception as e:
            failed += 1
            print(f"    [FAIL] {name}: {e}")
            traceback.print_exc()

    print(f"\n{SEP}")
    print(f"  Results: {passed}/{len(tests)} passed, {failed} failed")
    if failed == 0:
        print("  [ALL PASS] MultiModal module is ready.")
    else:
        print(f"  [SOME FAILED] {failed} test(s) failed.")
    print(SEP)

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
