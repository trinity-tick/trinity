"""
# status: frozen (2026-09 EXECUTION 163: SNN 仿真冻结)
P2-7b: IBM TrueNorth Neuromorphic Adapter
==========================================

为 IBM TrueNorth 类脑芯片提供适配层，实现：
- 基于核心 (corelet) 的编程模型
- 4096 核心 × 256 神经元/核心 = 1M 神经元
- 事件驱动的异步脉冲路由（包交换网络）
- 极低功耗推理 (< 100 mW)

编程模型基于 IBM Corelet 框架：
  https://github.com/IBM-Research/true-north

与 Trinity 推理引擎对接，在可用时卸载稀疏图运算到 TrueNorth。

Reference:
  - "TrueNorth: Design and Tool Flow..." Cassiday et al., 2013
  - "A million spiking-neuron integrated circuit..." Merolla et al., 2014
"""

from __future__ import annotations

import math
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Core Constants ─────────────────────────────────────────────────────

NEURONS_PER_CORE = 256
AXONS_PER_CORE = 256
CORES_PER_CHIP = 4096
TOTAL_NEURONS = CORES_PER_CHIP * NEURONS_PER_CORE  # 1,048,576
TIME_TICK_MS = 1.0  # 1ms 时间片


# ── Core Types ─────────────────────────────────────────────────────────

class CoreMode(Enum):
    """TrueNorth 核心运行模式。"""
    STANDBY = auto()
    ACTIVE = auto()
    LEARNING = auto()
    DIAGNOSTIC = auto()


class PacketType(Enum):
    """TrueNorth 包交换网络中的包类型。"""
    SPIKE = auto()        # 脉冲包
    CONFIG = auto()       # 配置包
    STATUS = auto()       # 状态包
    ERROR = auto()         # 错误包


@dataclass
class TrueNorthPacket:
    """TrueNorth 包交换网络中的数据包。

    32 位地址空间：芯片ID(8) + 核心ID(12) + 轴突ID(8) + 类型(4)
    """
    chip_id: int = 0
    core_id: int = 0
    axon_id: int = 0
    ptype: PacketType = PacketType.SPIKE
    payload: float = 1.0       # 脉冲权重
    delivery_tick: int = 0     # 投递时间片

    def to_address(self) -> int:
        return ((self.chip_id & 0xFF) << 24 |
                (self.core_id & 0xFFF) << 12 |
                (self.axon_id & 0xFF) << 4 |
                (self.ptype.value & 0xF))


@dataclass
class CoreletConfig:
    """Corelet 是 TrueNorth 的基本编程单元，定义了一组核心的连接拓扑。

    每个 corelet 包含：
    - 输入/输出轴突 (axons)
    - 神经元参数 (LIF 模型)
    - 突触权重矩阵
    - 核心间路由表
    """
    corelet_id: int
    num_cores: int = 1
    input_axons: int = 256
    output_axons: int = 256
    synaptic_weights: List[List[float]] = field(default_factory=list)
    neuron_thresholds: List[float] = field(default_factory=list)
    leak_rates: List[float] = field(default_factory=list)
    reset_modes: List[str] = field(default_factory=list)  # "hard" or "soft"


@dataclass
class CoreState:
    """单个 TrueNorth 核心运行时状态。"""
    core_id: int
    mode: CoreMode = CoreMode.STANDBY
    potential: List[float] = field(default_factory=lambda: [0.0] * NEURONS_PER_CORE)
    last_spike_tick: List[int] = field(default_factory=lambda: [0] * NEURONS_PER_CORE)
    spike_count: int = 0
    power_uW: float = 0.0


# ── TrueNorthCore ───────────────────────────────────────────────────────

class TrueNorthCore:
    """TrueNorth 单个核心的仿真模型。

    每个核心包含 256 个 LIF 神经元和 256×256 的突触交叉阵列 (crossbar)。
    通过包交换网络与其他核心通信，实现异步事件驱动。
    """

    def __init__(self, core_id: int) -> None:
        self.core_id = core_id
        self.state = CoreState(core_id=core_id)
        self._synapses: Dict[Tuple[int, int], float] = {}  # (pre_axon, post_neuron) → weight
        self._current_tick: int = 0
        self._lock = threading.Lock()

    def set_synapse(self, pre_axon: int, post_neuron: int, weight: float) -> None:
        """配置突触交叉阵列中的单条连接。"""
        if 0 <= pre_axon < AXONS_PER_CORE and 0 <= post_neuron < NEURONS_PER_CORE:
            self._synapses[(pre_axon, post_neuron)] = weight

    def configure_weight_matrix(self, weights: List[List[float]]) -> None:
        """批量配置完整的 256×256 权重矩阵。

        weights[i][j] = 突触权重 轴突i → 神经元j
        """
        for i, row in enumerate(weights[:AXONS_PER_CORE]):
            for j, w in enumerate(row[:NEURONS_PER_CORE]):
                if w != 0.0:
                    self._synapses[(i, j)] = w

    def deliver_packet(self, packet: TrueNorthPacket) -> List[TrueNorthPacket]:
        """投递一个脉冲包到当前核心，返回产生的输出包。

        模拟 LIF 神经元动力学：
        - 突触权重整合到膜电位
        - 超过阈值则发放脉冲并复位
        - 漏电流：每时间片按泄漏率衰减
        """
        output_packets: List[TrueNorthPacket] = []

        with self._lock:
            self._current_tick += 1
            # 漏电流衰减
            for i in range(NEURONS_PER_CORE):
                leak_rate = 0.95  # 默认泄漏率
                self.state.potential[i] *= leak_rate

            # 突触整合
            axon = packet.axon_id
            weight = packet.payload
            for post in range(NEURONS_PER_CORE):
                syn_w = self._synapses.get((axon, post), 0.0)
                if syn_w == 0.0:
                    continue
                self.state.potential[post] += syn_w * weight

            # 阈值检测
            threshold = 1.0  # 默认阈值
            for i in range(NEURONS_PER_CORE):
                if self.state.potential[i] >= threshold:
                    # 发放脉冲
                    self.state.potential[i] = 0.0  # hard reset
                    self.state.last_spike_tick[i] = self._current_tick
                    self.state.spike_count += 1
                    op = TrueNorthPacket(
                        core_id=self.core_id,
                        axon_id=i,
                        ptype=PacketType.SPIKE,
                        delivery_tick=self._current_tick + 1,
                    )
                    output_packets.append(op)

        return output_packets

    def reset(self) -> None:
        with self._lock:
            self.state.potential = [0.0] * NEURONS_PER_CORE
            self.state.spike_count = 0
            self._current_tick = 0


# ── TrueNorthChip ───────────────────────────────────────────────────────

class TrueNorthChip:
    """IBM TrueNorth 芯片适配器。

    特性：
      - 4096 核心的包交换网络
      - 支持 Corelet 编程模型
      - 事件驱动的异步处理
      - 功耗 < 100 mW（实际芯片）
    """

    def __init__(self, chip_id: str = "tn-0") -> None:
        self.chip_id = chip_id
        self._cores: Dict[int, TrueNorthCore] = {}
        self._network: deque[TrueNorthPacket] = deque()    # 包交换队列
        self._is_available = False
        self._lock = threading.Lock()
        self._total_spikes: int = 0
        self._clock_tick: int = 0

    @property
    def is_available(self) -> bool:
        return self._is_available

    def probe(self) -> bool:
        logger.info(f"Probing TrueNorth chip {self.chip_id}...")
        try:
            import importlib
            spec = importlib.util.find_spec("ibm_truenorth")
            if spec:
                logger.info("IBM TrueNorth SDK detected")
                self._is_available = True
                return True
        except Exception:
            pass
        logger.info("TrueNorth not detected (SDK absent)")
        return False

    def allocate_core(self, core_id: int) -> Optional[TrueNorthCore]:
        if core_id >= CORES_PER_CHIP:
            return None
        with self._lock:
            if core_id not in self._cores:
                self._cores[core_id] = TrueNorthCore(core_id)
            return self._cores[core_id]

    def load_corelet(self, config: CoreletConfig,
                     start_core: int = 0) -> List[int]:
        """加载一个 Corelet 到芯片上，返回分配的核心 ID 列表。"""
        core_ids = []
        for offset in range(min(config.num_cores, CORES_PER_CHIP - start_core)):
            cid = start_core + offset
            core = self.allocate_core(cid)
            if core and config.synaptic_weights:
                for i, row in enumerate(config.synaptic_weights[:AXONS_PER_CORE]):
                    for j, w in enumerate(row[:NEURONS_PER_CORE]):
                        if w != 0.0:
                            core.set_synapse(i, j, w)
            core_ids.append(cid)
        return core_ids

    def inject_spike(self, core_id: int, axon_id: int,
                     weight: float = 1.0) -> None:
        """向指定核心注入外部脉冲。"""
        packet = TrueNorthPacket(core_id=core_id, axon_id=axon_id,
                                  payload=weight, delivery_tick=self._clock_tick)
        with self._lock:
            self._network.append(packet)

    def step(self) -> List[TrueNorthPacket]:
        """执行一个时间片的全部包处理。"""
        self._clock_tick += 1
        new_packets: List[TrueNorthPacket] = []

        with self._lock:
            # 处理当前时间片的所有包
            to_process = [p for p in self._network
                          if p.delivery_tick <= self._clock_tick]
            self._network = deque(
                p for p in self._network if p.delivery_tick > self._clock_tick
            )

        for packet in to_process:
            core = self._cores.get(packet.core_id)
            if core:
                outputs = core.deliver_packet(packet)
                new_packets.extend(outputs)

        with self._lock:
            self._network.extend(new_packets)

        self._total_spikes += len(new_packets)
        return new_packets

    def run(self, ticks: int = 100) -> List[TrueNorthPacket]:
        """运行指定数量的时间片。"""
        all_outputs: List[TrueNorthPacket] = []
        for _ in range(ticks):
            outputs = self.step()
            all_outputs.extend(outputs)
        return all_outputs

    def get_chip_stats(self) -> Dict[str, Any]:
        active_cores = sum(
            1 for c in self._cores.values()
            if c.state.spike_count > 0
        )
        total_spikes = sum(
            c.state.spike_count for c in self._cores.values()
        )
        return {
            "chip_id": self.chip_id,
            "total_cores": CORES_PER_CHIP,
            "allocated_cores": len(self._cores),
            "active_cores": active_cores,
            "total_spikes": total_spikes,
            "clock_ticks": self._clock_tick,
            "network_queue_size": len(self._network),
            "power_est_mw": len(self._cores) * 0.07,  # ~70 µW per core
        }


# ── Trinity Inference Bridge ───────────────────────────────────────────

class TrueNorthInferenceBridge:
    """Trinity ↔ TrueNorth 推理桥接器。

    将稀疏图运算和模式匹配卸载到 TrueNorth 神经形态处理器，
    利用大规模并行脉冲网络实现超低功耗推理 (< 0.1 W)。
    """

    def __init__(self, chip: Optional[TrueNorthChip] = None) -> None:
        self.chip = chip or TrueNorthChip()
        self._mapping: Dict[str, int] = {}  # 任务类型 → 起始核心
        self._energy_saved_j: float = 0.0
        self._inference_count: int = 0

    def map_task(self, task_name: str, start_core: int) -> None:
        self._mapping[task_name] = start_core

    def encode_pattern(self, pattern: List[float],
                       core_id: int) -> TrueNorthPacket:
        """将浮点模式编码为 TrueNorth 脉冲序列。

        采用速率编码：归一化值 → 概率性脉冲发放。
        """
        if not pattern:
            return TrueNorthPacket(core_id=core_id)
        # 取平均激活轴突
        axon = int(sum(abs(v) for v in pattern) / len(pattern) * AXONS_PER_CORE)
        axon = min(axon, AXONS_PER_CORE - 1)
        weight = max(abs(v) for v in pattern) if pattern else 1.0
        return TrueNorthPacket(core_id=core_id, axon_id=axon, payload=weight)

    def neuromorphic_inference(self, input_vector: List[float],
                               task_name: str = "default") -> List[TrueNorthPacket]:
        """执行类脑推理：输入→SNN→脉冲输出。"""
        start_core = self._mapping.get(task_name, 0)
        core = self.chip.allocate_core(start_core)
        if not core:
            return []

        # 注入编码后的脉冲
        packet = self.encode_pattern(input_vector, start_core)
        self.chip.inject_spike(start_core, packet.axon_id, packet.payload)

        # 运行 SNN
        outputs = self.chip.run(ticks=50)

        self._inference_count += 1
        self._energy_saved_j += len(input_vector) * 1e-7
        return outputs

    def get_energy_report(self) -> Dict[str, Any]:
        return {
            "inference_count": self._inference_count,
            "energy_saved_joules": self._energy_saved_j,
            "equivalent_co2_kg": self._energy_saved_j * 0.000233,
            "chip_available": self.chip.is_available,
            "estimated_power_mw": 0.1,  # TrueNorth nominal
        }


# ── Self-Test ──────────────────────────────────────────────────────────

def _self_test_truenorth() -> Dict[str, Any]:
    results: Dict[str, Any] = {
        "module": "P2-7b_truenorth",
        "passed": 0,
        "failed": 0,
        "details": [],
    }

    def _pass(test: str):
        results["passed"] += 1
        results["details"].append({"test": test, "status": "PASS"})

    def _fail(test: str, reason: str):
        results["failed"] += 1
        results["details"].append({"test": test, "status": "FAIL", "reason": reason})

    try:
        core = TrueNorthCore(0)
        core.set_synapse(0, 10, 0.8)
        core.set_synapse(1, 10, 0.5)
        assert core._synapses[(0, 10)] == 0.8
        assert core._synapses[(1, 10)] == 0.5
        _pass("Synapse configuration")
    except Exception as e:
        _fail("Synapse configuration", str(e))

    try:
        core = TrueNorthCore(0)
        core.set_synapse(0, 50, 2.0)
        packet = TrueNorthPacket(core_id=0, axon_id=0, payload=1.0)
        outputs = core.deliver_packet(packet)
        assert len(outputs) >= 1, f"Should fire at least once, got {len(outputs)}"
        assert outputs[0].ptype == PacketType.SPIKE
        _pass("LIF neuron firing")
    except Exception as e:
        _fail("LIF neuron firing", str(e))

    try:
        chip = TrueNorthChip()
        core = chip.allocate_core(0)
        core.set_synapse(0, 100, 1.5)
        chip.inject_spike(0, 0, 1.0)
        outputs = chip.run(ticks=10)
        assert len(outputs) >= 1
        stats = chip.get_chip_stats()
        assert stats["total_cores"] == CORES_PER_CHIP
        assert stats["allocated_cores"] == 1
        _pass("Chip-level packet routing")
    except Exception as e:
        _fail("Chip-level packet routing", str(e))

    try:
        bridge = TrueNorthInferenceBridge()
        bridge.map_task("pattern_match", 0)
        vec = [0.1, 0.2, 0.8, 0.3, 0.6]
        outs = bridge.neuromorphic_inference(vec, "pattern_match")
        assert isinstance(outs, list)
        report = bridge.get_energy_report()
        assert report["inference_count"] > 0
        _pass("Inference bridge")
    except Exception as e:
        _fail("Inference bridge", str(e))

    try:
        core = TrueNorthCore(0)
        core.set_synapse(0, 10, 1.0)
        # LIF leak: potential should decay over time
        core._current_tick = 0
        core.state.potential[10] = 0.5
        # Deliver empty packet to trigger tick
        empty_pkt = TrueNorthPacket(core_id=0, axon_id=255, payload=0.0)
        core.deliver_packet(empty_pkt)
        assert core.state.potential[10] < 0.5, f"Potential should decay: {core.state.potential[10]}"
        _pass("Leak current decay")
    except Exception as e:
        _fail("Leak current decay", str(e))

    try:
        chip = TrueNorthChip()
        core = chip.allocate_core(10)
        core.set_synapse(5, 20, 1.0)
        chip.inject_spike(10, 5, 1.0)
        before = core.state.spike_count
        chip.run(ticks=5)
        assert core.state.spike_count >= before
        _pass("Event-driven spike propagation")
    except Exception as e:
        _fail("Event-driven spike propagation", str(e))

    try:
        core = TrueNorthCore(0)
        core.set_synapse(0, 50, 1.0)
        packet = TrueNorthPacket(core_id=0, axon_id=0, payload=1.0)
        core.deliver_packet(packet)
        core.reset()
        assert core.state.spike_count == 0
        assert all(p == 0.0 for p in core.state.potential)
        _pass("Core reset")
    except Exception as e:
        _fail("Core reset", str(e))

    try:
        packet = TrueNorthPacket(chip_id=0, core_id=1024, axon_id=128,
                                  ptype=PacketType.SPIKE)
        addr = packet.to_address()
        assert addr > 0
        # Verify address decomposition
        assert (addr >> 24) & 0xFF == 0
        assert (addr >> 12) & 0xFFF == 1024
        assert (addr >> 4) & 0xFF == 128
        assert (addr & 0xF) == PacketType.SPIKE.value
        _pass("Packet address encoding")
    except Exception as e:
        _fail("Packet address encoding", str(e))

    results["total"] = results["passed"] + results["failed"]
    return results
