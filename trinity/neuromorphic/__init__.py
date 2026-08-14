"""Trinity Neuromorphic Sub-package — Loihi / TrueNorth Adapters.

P2-7: 类脑计算芯片适配层
Extreme energy efficiency inference via neuromorphic processors.
"""

from trinity.neuromorphic.loihi import (
    Loihi2Chip,
    Loihi2Neurocore,
    LoihiInferenceBridge,
    NeuronConfig,
    SpikeEvent,
    SpikeTrain,
    SynapseType,
    NeurocoreStatus,
    NeurocoreState,
    _self_test_loihi,
)

from trinity.neuromorphic.truenorth import (
    TrueNorthChip,
    TrueNorthCore,
    TrueNorthInferenceBridge,
    CoreletConfig,
    TrueNorthPacket,
    PacketType,
    CoreMode,
    CoreState,
    _self_test_truenorth,
)

__all__ = [
    # Loihi 2
    "Loihi2Chip",
    "Loihi2Neurocore",
    "LoihiInferenceBridge",
    "NeuronConfig",
    "SpikeEvent",
    "SpikeTrain",
    "SynapseType",
    "NeurocoreStatus",
    "NeurocoreState",
    "_self_test_loihi",
    # TrueNorth
    "TrueNorthChip",
    "TrueNorthCore",
    "TrueNorthInferenceBridge",
    "CoreletConfig",
    "TrueNorthPacket",
    "PacketType",
    "CoreMode",
    "CoreState",
    "_self_test_truenorth",
]

__version__ = "8.2.0"


def self_test() -> dict:
    """Run all neuromorphic self-tests, return merged stats."""
    loihi_result = _self_test_loihi()
    tn_result = _self_test_truenorth()
    total_passed = loihi_result["passed"] + tn_result["passed"]
    total_failed = loihi_result["failed"] + tn_result["failed"]
    return {
        "module": "P2-7_neuromorphic",
        "components": ["loihi2", "truenorth"],
        "passed": total_passed,
        "failed": total_failed,
        "total": total_passed + total_failed,
        "loihi": loihi_result,
        "truenorth": tn_result,
    }
