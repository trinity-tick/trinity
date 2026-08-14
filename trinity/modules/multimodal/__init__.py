"""
MultiModal Memory Extension — 多模态记忆扩展模块 (v1.1.0)
============================================================
Extends Trinity's tiered memory architecture from M119 to support
image, audio, video frame, and audio clip modalities.

P1-3 Enhancement: Cross-modal retrieval with video frame extraction
and audio clip segmentation.

References:
  - M119 TrainFreeEngramMemory: TF-Engram (arXiv 2607.07388)
  - M120 MultiModalMemoryAgentCollaboration

New in v1.1.0 (P1-3):
  VideoFrameExtractor   — Extract frames from video with deterministic features
  AudioClipProcessor    — Segment audio into clips with MFCC/spectral features
  CrossModalRetriever   — Cross-modal similarity search across all modalities
  MediaModality         — Extended modality enum (TEXT/IMAGE/VIDEO_FRAME/AUDIO_CLIP)
"""

from __future__ import annotations

import hashlib
import math
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

from trinity.modules.multimodal.image_encoder import ImageMemoryEncoder
from trinity.modules.multimodal.audio_encoder import AudioMemoryEncoder
from trinity.modules.multimodal.multimodal_memory import MultiModalMemory, ModalityType, StorageTier
from trinity.modules.multimodal.multimodal_enhanced import (
    VideoFrameExtractor,
    AudioClipProcessor,
    CrossModalRetriever,
    MediaModality,
    MediaFeature,
    FeatureType,
    self_test as multimodal_enhanced_self_test,
)


MODULE_ID = "MULTIMODAL_EXT"
MODULE_VERSION = "1.1.0"
PAPER_REF = "TF-Engram (arXiv 2607.07388) + MultiModal Extension + P1-3 Enhancement"


__all__ = [
    "ImageMemoryEncoder",
    "AudioMemoryEncoder",
    "MultiModalMemory",
    # P1-3: Enhanced cross-modal
    "VideoFrameExtractor",
    "AudioClipProcessor",
    "CrossModalRetriever",
    "MediaModality",
    "MediaFeature",
    "FeatureType",
    "multimodal_enhanced_self_test",
    "MODULE_ID",
    "MODULE_VERSION",
    "PAPER_REF",
]
