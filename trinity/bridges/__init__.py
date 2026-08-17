"""Trinity Bridges — 外部 Agent 实时同步桥接层."""

from trinity.bridges.marvis_bridge import (
    MarvisTrinityBridge,
    BUILTIN_AGENTS,
)

from trinity.bridges.retrieval_bridge import (
    TrinityRetrievalBridge,
    InsightsWriter,
)

from trinity.bridges.auto_syncer import (
    ConversationScanner,
    ConversationSyncDaemon,
    SyncState,
)

__all__ = [
    "MarvisTrinityBridge",
    "BUILTIN_AGENTS",
    "TrinityRetrievalBridge",
    "InsightsWriter",
    "ConversationScanner",
    "ConversationSyncDaemon",
    "SyncState",
]
