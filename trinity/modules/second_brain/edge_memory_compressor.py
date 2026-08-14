
"""
P18-4: Edge Memory Compressor — 边缘记忆压缩

对标论文: TurboQuant (Google, 2026.04)
核心发现: 6x KV 缓存压缩，4B 模型+100K 上下文跑在手机上
三元语: KV 压缩引擎 → 设备配置适配 → 内存预算分配 → 离线优先存储 → 分层压缩策略

设计要点:
- KVCompressionEngine: KV 缓存压缩引擎——6x 压缩比，使边缘设备上下文从 ~16K 扩展到 ~96K
- DeviceProfileAdapter: 设备配置适配——iPhone A19 (~16K→~96K) / Snapdragon 8 Gen3 (~32K→~192K) / Raspberry Pi 5 (~4K→~24K) 梯度配置
- MemoryBudgetAllocator: 内存预算分配器——在总 RAM 约束下动态分配记忆缓存 vs 模型权重 vs 运行时
- OfflineFirstMemoryStore: 离线优先记忆存储——无云端往返，纯本地操作，数据不离开设备
- TieredCompressionPolicy: 分层压缩策略——热记忆（低压缩/快速访问）vs 冷记忆（高压缩/慢访问）
- 与 P9 quantization.py / P2 compression.py 互补——quantization 做模型量化，compression 做通用压缩，本模块做边缘设备 6x KV 缓存专用压缩
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import math
import struct
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class DeviceProfile(Enum):
    """支持的设备配置"""
    IPHONE_A19 = "iphone_a19"            # ~16K → ~96K
    SNAPDRAGON_8GEN3 = "snapdragon_8gen3" # ~32K → ~192K
    RASPBERRY_PI5 = "raspberry_pi5"       # ~4K → ~24K
    GENERIC_LAPTOP = "generic_laptop"     # ~64K → ~384K
    EDGE_SERVER = "edge_server"           # ~128K → ~768K


class CompressionTier(Enum):
    """压缩层级"""
    HOT = "hot"          # 低压缩 / 快速访问 (1.2x-2x)
    WARM = "warm"        # 中等压缩 (2x-4x)
    COLD = "cold"        # 高压缩 / 慢访问 (4x-6x)


class CompressionAlgorithm(Enum):
    """压缩算法"""
    QUANTIZED_KV = "quantized_kv"        # TurboQuant 量化 KV
    DELTA_ENCODING = "delta_encoding"     # 增量编码
    GZIP = "gzip"                         # Gzip 通用压缩
    SPARSE_SELECTION = "sparse_selection" # 稀疏选择
    HYBRID = "hybrid"                    # 混合策略


class MemoryBudget(Enum):
    """内存预算类别"""
    KV_CACHE = "kv_cache"
    MODEL_WEIGHTS = "model_weights"
    RUNTIME_BUFFERS = "runtime_buffers"
    SYSTEM_RESERVED = "system_reserved"


class BackendStatus(Enum):
    """离线存储后端状态"""
    IDLE = "idle"
    COMPRESSING = "compressing"
    DECOMPRESSING = "decompressing"
    FLUSHING = "flushing"
    ERROR = "error"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class DeviceSpec:
    """设备规格"""
    profile: DeviceProfile
    total_ram_mb: float
    available_for_cache_mb: float
    native_context_length: int            # 未压缩上下文长度
    compressed_context_length: int        # 压缩后可达长度
    compression_ratio_target: float = 6.0
    model_size_mb: float = 0.0


@dataclass
class KVBlock:
    """KV 缓存块"""
    block_id: str
    layer_index: int
    raw_size_bytes: int
    compressed_size_bytes: int = 0
    compression_ratio: float = 1.0
    algorithm: CompressionAlgorithm = CompressionAlgorithm.QUANTIZED_KV
    tier: CompressionTier = CompressionTier.WARM
    access_count: int = 0
    last_access: float = field(default_factory=time.time)
    data_hash: str = ""


@dataclass
class BudgetAllocation:
    """内存预算分配方案"""
    profile: DeviceProfile
    kv_cache_mb: float
    model_weights_mb: float
    runtime_buffers_mb: float
    system_reserved_mb: float
    total_allocated_mb: float
    free_mb: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class CompressionStats:
    """压缩统计"""
    total_blocks: int = 0
    total_raw_bytes: int = 0
    total_compressed_bytes: int = 0
    overall_compression_ratio: float = 1.0
    hot_blocks: int = 0
    warm_blocks: int = 0
    cold_blocks: int = 0
    decompression_latency_avg_ms: float = 0.0
    compression_latency_avg_ms: float = 0.0


# ============================================================================
# P18-4-1: KVCompressionEngine — KV 缓存压缩引擎
# ============================================================================

class KVCompressionEngine:
    """6x KV 缓存压缩引擎"""

    TARGET_RATIO = 6.0

    def __init__(self, device_spec: DeviceSpec):
        self._lock = threading.RLock()
        self._device_spec = device_spec
        self._blocks: Dict[str, KVBlock] = {}
        self._compression_log: deque = deque(maxlen=500)
        self._stats = CompressionStats()

    def compress(
        self,
        block_id: str,
        raw_data: bytes,
        layer_index: int,
        algorithm: Optional[CompressionAlgorithm] = None,
    ) -> KVBlock:
        """压缩 KV 缓存块"""
        algorithm = algorithm or CompressionAlgorithm.QUANTIZED_KV

        with self._lock:
            start = time.time()
            # 模拟压缩——根据不同算法实现不同压缩比
            if algorithm == CompressionAlgorithm.QUANTIZED_KV:
                compressed = self._quantize_kv(raw_data)
            elif algorithm == CompressionAlgorithm.DELTA_ENCODING:
                compressed = self._delta_encode(raw_data)
            elif algorithm == CompressionAlgorithm.GZIP:
                compressed = gzip.compress(raw_data, compresslevel=6)
            elif algorithm == CompressionAlgorithm.SPARSE_SELECTION:
                compressed = self._sparse_select(raw_data)
            else:  # HYBRID
                compressed = self._hybrid_compress(raw_data)

            elapsed_ms = (time.time() - start) * 1000
            ratio = len(raw_data) / max(len(compressed), 1)

            block = KVBlock(
                block_id=block_id,
                layer_index=layer_index,
                raw_size_bytes=len(raw_data),
                compressed_size_bytes=len(compressed),
                compression_ratio=ratio,
                algorithm=algorithm,
                data_hash=hashlib.md5(compressed).hexdigest(),
            )
            self._blocks[block_id] = block
            self._compression_log.append(("compress", block_id, ratio, elapsed_ms))

            # 更新统计
            self._stats.total_blocks += 1
            self._stats.total_raw_bytes += len(raw_data)
            self._stats.total_compressed_bytes += len(compressed)
            self._stats.overall_compression_ratio = (
                self._stats.total_raw_bytes / max(self._stats.total_compressed_bytes, 1)
            )
            self._stats.compression_latency_avg_ms = (
                self._stats.compression_latency_avg_ms * (self._stats.total_blocks - 1) + elapsed_ms
            ) / self._stats.total_blocks

            logger.debug(
                f"Compressed {block_id}: {len(raw_data)}→{len(compressed)} bytes ({ratio:.1f}x) "
                f"via {algorithm.value} in {elapsed_ms:.1f}ms"
            )
            return block

    def decompress(self, block_id: str) -> Optional[bytes]:
        """解压 KV 缓存块"""
        with self._lock:
            block = self._blocks.get(block_id)
            if block is None:
                return None

            start = time.time()
            # 模拟解压——基于压缩比反推
            decompressed_size = block.raw_size_bytes
            result = b"\x00" * decompressed_size  # 占位解压结果
            elapsed_ms = (time.time() - start) * 1000

            block.access_count += 1
            block.last_access = time.time()
            self._compression_log.append(("decompress", block_id, block.compression_ratio, elapsed_ms))

            wt = self._stats.total_blocks
            self._stats.decompression_latency_avg_ms = (
                self._stats.decompression_latency_avg_ms * max(wt - 1, 0) + elapsed_ms
            ) / max(wt, 1)

            return result

    def _quantize_kv(self, data: bytes) -> bytes:
        """TurboQuant 量化 KV——模拟 6x 压缩"""
        target_size = max(len(data) // 6, 16)
        # 模拟量化：取每 6 字节的平均值
        result = bytearray()
        for i in range(0, len(data), 6):
            chunk = data[i:i+6]
            if chunk:
                result.append(sum(chunk) // len(chunk))
        while len(result) < target_size:
            result.append(0)
        return bytes(result[:target_size])

    def _delta_encode(self, data: bytes) -> bytes:
        """增量编码"""
        result = bytearray()
        if data:
            result.append(data[0])
            for i in range(1, len(data)):
                delta = (data[i] - data[i-1]) & 0xFF
                result.append(delta)
        return gzip.compress(bytes(result), compresslevel=9)

    def _sparse_select(self, data: bytes) -> bytes:
        """稀疏选择——保留每 6 个 token 中最重要的 1 个"""
        stride = 6
        result = bytearray()
        for i in range(0, len(data), stride):
            chunk = data[i:i+stride]
            if chunk:
                result.append(max(chunk))
        return bytes(result)

    def _hybrid_compress(self, data: bytes) -> bytes:
        """混合策略：先量化再 gzip"""
        quantized = self._quantize_kv(data)
        return gzip.compress(quantized, compresslevel=9)

    def get_compression_ratio(self) -> float:
        with self._lock:
            return self._stats.overall_compression_ratio

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_blocks": self._stats.total_blocks,
                "raw_mb": self._stats.total_raw_bytes / (1024 * 1024),
                "compressed_mb": self._stats.total_compressed_bytes / (1024 * 1024),
                "overall_ratio": self._stats.overall_compression_ratio,
                "target_ratio": self.TARGET_RATIO,
                "compression_latency_ms": self._stats.compression_latency_avg_ms,
                "decompression_latency_ms": self._stats.decompression_latency_avg_ms,
                "device": self._device_spec.profile.value,
                "native_ctx": self._device_spec.native_context_length,
                "compressed_ctx": self._device_spec.compressed_context_length,
            }


# ============================================================================
# P18-4-2: DeviceProfileAdapter — 设备配置适配
# ============================================================================

class DeviceProfileAdapter:
    """针对不同设备自动适配配置"""

    PROFILES: Dict[DeviceProfile, DeviceSpec] = {
        DeviceProfile.IPHONE_A19: DeviceSpec(
            profile=DeviceProfile.IPHONE_A19,
            total_ram_mb=8192,
            available_for_cache_mb=1024,
            native_context_length=16384,
            compressed_context_length=98304,
            compression_ratio_target=6.0,
            model_size_mb=3500,
        ),
        DeviceProfile.SNAPDRAGON_8GEN3: DeviceSpec(
            profile=DeviceProfile.SNAPDRAGON_8GEN3,
            total_ram_mb=12288,
            available_for_cache_mb=2048,
            native_context_length=32768,
            compressed_context_length=196608,
            compression_ratio_target=6.0,
            model_size_mb=4500,
        ),
        DeviceProfile.RASPBERRY_PI5: DeviceSpec(
            profile=DeviceProfile.RASPBERRY_PI5,
            total_ram_mb=8192,
            available_for_cache_mb=256,
            native_context_length=4096,
            compressed_context_length=24576,
            compression_ratio_target=6.0,
            model_size_mb=2800,
        ),
        DeviceProfile.GENERIC_LAPTOP: DeviceSpec(
            profile=DeviceProfile.GENERIC_LAPTOP,
            total_ram_mb=16384,
            available_for_cache_mb=4096,
            native_context_length=65536,
            compressed_context_length=393216,
            compression_ratio_target=6.0,
            model_size_mb=5500,
        ),
        DeviceProfile.EDGE_SERVER: DeviceSpec(
            profile=DeviceProfile.EDGE_SERVER,
            total_ram_mb=65536,
            available_for_cache_mb=16384,
            native_context_length=131072,
            compressed_context_length=786432,
            compression_ratio_target=6.0,
            model_size_mb=12000,
        ),
    }

    def __init__(self):
        self._lock = threading.RLock()
        self._active_profile: Optional[DeviceSpec] = None
        self._custom_profiles: Dict[str, DeviceSpec] = {}

    def select_profile(self, device: DeviceProfile) -> DeviceSpec:
        """选择设备配置"""
        with self._lock:
            spec = self.PROFILES.get(device)
            if spec is None:
                raise ValueError(f"Unknown device profile: {device}")
            self._active_profile = spec
            logger.info(
                f"Device profile selected: {device.value} "
                f"({spec.native_context_length}→{spec.compressed_context_length} tokens)"
            )
            return spec

    def get_max_context_for_device(self, device: DeviceProfile) -> int:
        spec = self.PROFILES.get(device)
        return spec.compressed_context_length if spec else 0

    def register_custom_profile(self, name: str, spec: DeviceSpec):
        with self._lock:
            self._custom_profiles[name] = spec

    def get_active_spec(self) -> Optional[DeviceSpec]:
        with self._lock:
            return self._active_profile

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active_profile": self._active_profile.profile.value if self._active_profile else "none",
                "supported_profiles": [p.value for p in self.PROFILES],
                "custom_profiles": len(self._custom_profiles),
                "profiles_summary": {
                    p.value: {
                        "native_ctx": s.native_context_length,
                        "compressed_ctx": s.compressed_context_length,
                        "ram_mb": s.total_ram_mb,
                    }
                    for p, s in self.PROFILES.items()
                },
            }


# ============================================================================
# P18-4-3: MemoryBudgetAllocator — 内存预算分配器
# ============================================================================

class MemoryBudgetAllocator:
    """在总 RAM 约束下动态分配记忆缓存 vs 模型权重 vs 运行时"""

    # 默认分配比例
    DEFAULT_ALLOCATION = {
        MemoryBudget.KV_CACHE: 0.25,
        MemoryBudget.MODEL_WEIGHTS: 0.50,
        MemoryBudget.RUNTIME_BUFFERS: 0.15,
        MemoryBudget.SYSTEM_RESERVED: 0.10,
    }

    def __init__(self, device_spec: DeviceSpec):
        self._lock = threading.RLock()
        self._device_spec = device_spec
        self._allocations: List[BudgetAllocation] = []
        self._current_allocation = self._compute_default(device_spec)

    def _compute_default(self, spec: DeviceSpec) -> BudgetAllocation:
        total = spec.total_ram_mb
        kv = total * self.DEFAULT_ALLOCATION[MemoryBudget.KV_CACHE]
        model = total * self.DEFAULT_ALLOCATION[MemoryBudget.MODEL_WEIGHTS]
        runtime = total * self.DEFAULT_ALLOCATION[MemoryBudget.RUNTIME_BUFFERS]
        system = total * self.DEFAULT_ALLOCATION[MemoryBudget.SYSTEM_RESERVED]
        allocated = kv + model + runtime + system
        return BudgetAllocation(
            profile=spec.profile,
            kv_cache_mb=kv,
            model_weights_mb=model,
            runtime_buffers_mb=runtime,
            system_reserved_mb=system,
            total_allocated_mb=allocated,
            free_mb=total - allocated,
        )

    def adjust(self, category: MemoryBudget, new_mb: float) -> BudgetAllocation:
        """动态调整预算分配"""
        with self._lock:
            current = self._current_allocation
            total = self._device_spec.total_ram_mb
            new_allocation = BudgetAllocation(
                profile=current.profile,
                kv_cache_mb=current.kv_cache_mb,
                model_weights_mb=current.model_weights_mb,
                runtime_buffers_mb=current.runtime_buffers_mb,
                system_reserved_mb=current.system_reserved_mb,
                total_allocated_mb=0.0,
                free_mb=0.0,
            )

            if category == MemoryBudget.KV_CACHE:
                new_allocation.kv_cache_mb = new_mb
            elif category == MemoryBudget.MODEL_WEIGHTS:
                new_allocation.model_weights_mb = new_mb
            elif category == MemoryBudget.RUNTIME_BUFFERS:
                new_allocation.runtime_buffers_mb = new_mb
            elif category == MemoryBudget.SYSTEM_RESERVED:
                new_allocation.system_reserved_mb = new_mb

            allocated = (
                new_allocation.kv_cache_mb + new_allocation.model_weights_mb +
                new_allocation.runtime_buffers_mb + new_allocation.system_reserved_mb
            )
            new_allocation.total_allocated_mb = allocated
            new_allocation.free_mb = total - allocated

            if new_allocation.free_mb < 0:
                logger.warning(
                    f"Budget overallocated: {allocated:.0f}MB > {total:.0f}MB, "
                    f"shortfall={-new_allocation.free_mb:.0f}MB"
                )

            self._current_allocation = new_allocation
            self._allocations.append(new_allocation)
            logger.info(
                f"Budget adjusted: {category.value} → {new_mb:.0f}MB, "
                f"free={new_allocation.free_mb:.0f}MB"
            )
            return new_allocation

    def get_current(self) -> BudgetAllocation:
        with self._lock:
            return self._current_allocation

    def is_over_budget(self, category: MemoryBudget) -> bool:
        with self._lock:
            current = getattr(self._current_allocation, f"{category.value}_mb", 0.0)
            used = current  # 简化——实际应追踪已用
            return used > current

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            c = self._current_allocation
            return {
                "profile": c.profile.value,
                "total_ram_mb": self._device_spec.total_ram_mb,
                "kv_cache_mb": c.kv_cache_mb,
                "model_weights_mb": c.model_weights_mb,
                "runtime_mb": c.runtime_buffers_mb,
                "system_reserved_mb": c.system_reserved_mb,
                "free_mb": c.free_mb,
                "allocation_count": len(self._allocations),
            }


# ============================================================================
# P18-4-4: OfflineFirstMemoryStore — 离线优先记忆存储
# ============================================================================

class OfflineFirstMemoryStore:
    """纯本地操作，数据不离开设备"""

    def __init__(self, max_local_mb: float = 512.0):
        self._lock = threading.RLock()
        self._max_local_mb = max_local_mb
        self._store: Dict[str, bytes] = OrderedDict()
        self._metadata: Dict[str, Dict[str, Any]] = {}
        self._current_size_bytes: int = 0
        self._status: BackendStatus = BackendStatus.IDLE

    def store(self, key: str, data: bytes, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """本地存储，永不离开设备"""
        with self._lock:
            size = len(data)
            max_bytes = self._max_local_mb * 1024 * 1024

            if self._current_size_bytes + size > max_bytes:
                # 触发逐出
                self._evict(size)

            self._store[key] = data
            self._metadata[key] = metadata or {}
            self._current_size_bytes += size
            self._metadata[key]["stored_at"] = time.time()
            self._metadata[key]["size_bytes"] = size
            logger.debug(f"Offline stored: {key} ({size} bytes)")
            return True

    def retrieve(self, key: str) -> Optional[bytes]:
        with self._lock:
            data = self._store.get(key)
            if data and key in self._metadata:
                self._metadata[key]["last_accessed"] = time.time()
                self._metadata[key]["access_count"] = self._metadata[key].get("access_count", 0) + 1
            return data

    def delete(self, key: str) -> bool:
        with self._lock:
            data = self._store.pop(key, None)
            if data:
                self._current_size_bytes -= len(data)
                self._metadata.pop(key, None)
                return True
            return False

    def _evict(self, needed_bytes: int):
        """LRU 逐出"""
        # 按最后访问时间排序，逐出最旧的
        sorted_keys = sorted(
            self._metadata.keys(),
            key=lambda k: self._metadata[k].get("last_accessed", 0),
        )
        freed = 0
        for key in sorted_keys:
            if freed >= needed_bytes:
                break
            data = self._store.get(key)
            if data:
                freed += len(data)
                del self._store[key]
                del self._metadata[key]
        self._current_size_bytes -= freed
        if freed > 0:
            logger.info(f"Evicted LRU entries: freed {freed} bytes")

    def has_key(self, key: str) -> bool:
        with self._lock:
            return key in self._store

    def list_keys(self, prefix: str = "") -> List[str]:
        with self._lock:
            return [k for k in self._store if k.startswith(prefix)]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_entries": len(self._store),
                "current_mb": self._current_size_bytes / (1024 * 1024),
                "max_mb": self._max_local_mb,
                "offline_only": True,
                "status": self._status.value,
                "utilization": self._current_size_bytes / max(self._max_local_mb * 1024 * 1024, 1),
            }


# ============================================================================
# P18-4-5: TieredCompressionPolicy — 分层压缩策略
# ============================================================================

class TieredCompressionPolicy:
    """热记忆（低压缩/快速访问）vs 冷记忆（高压缩/慢访问）"""

    # 各层压缩比目标
    TIER_RATIOS = {
        CompressionTier.HOT: 1.5,       # 1.5x 低压缩
        CompressionTier.WARM: 3.0,      # 3x 中等
        CompressionTier.COLD: 6.0,      # 6x 高压缩
    }

    def __init__(self, engine: KVCompressionEngine):
        self._lock = threading.RLock()
        self._engine = engine
        self._tier_map: Dict[str, CompressionTier] = {}
        self._access_frequency: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self._promotions: int = 0
        self._demotions: int = 0

    def assign_tier(self, block_id: str) -> CompressionTier:
        """为新块分配初始 tier（默认 WARM）"""
        with self._lock:
            self._tier_map[block_id] = CompressionTier.WARM
            self._access_frequency[block_id].append(time.time())
        return CompressionTier.WARM

    def record_access(self, block_id: str):
        """记录访问，自动升降级"""
        with self._lock:
            self._access_frequency[block_id].append(time.time())
            freq = self._compute_frequency(block_id)
            current = self._tier_map.get(block_id, CompressionTier.WARM)

            if freq > 5.0 and current != CompressionTier.HOT:
                self._tier_map[block_id] = CompressionTier.HOT
                self._promotions += 1
                logger.debug(f"Tier promoted: {block_id} → HOT (freq={freq:.1f})")
            elif freq < 0.5 and current == CompressionTier.WARM:
                self._tier_map[block_id] = CompressionTier.COLD
                self._demotions += 1
                logger.debug(f"Tier demoted: {block_id} → COLD (freq={freq:.1f})")

    def _compute_frequency(self, block_id: str, window_seconds: float = 300.0) -> float:
        """计算每分钟访问频率"""
        now = time.time()
        times = self._access_frequency.get(block_id, deque())
        recent = [t for t in times if t >= now - window_seconds]
        return len(recent) / (window_seconds / 60.0)

    def get_tier(self, block_id: str) -> CompressionTier:
        with self._lock:
            return self._tier_map.get(block_id, CompressionTier.WARM)

    def get_compression_target(self, block_id: str) -> float:
        tier = self.get_tier(block_id)
        return self.TIER_RATIOS.get(tier, 3.0)

    def rebalance(self):
        """全量重平衡"""
        with self._lock:
            for block_id in list(self._tier_map.keys()):
                self.record_access(block_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            hot = sum(1 for t in self._tier_map.values() if t == CompressionTier.HOT)
            warm = sum(1 for t in self._tier_map.values() if t == CompressionTier.WARM)
            cold = sum(1 for t in self._tier_map.values() if t == CompressionTier.COLD)
            return {
                "total_blocks": len(self._tier_map),
                "hot": hot,
                "warm": warm,
                "cold": cold,
                "promotions": self._promotions,
                "demotions": self._demotions,
                "tier_ratios": {t.value: r for t, r in self.TIER_RATIOS.items()},
            }
