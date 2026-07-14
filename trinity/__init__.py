"""
Trinity — 三位一体智能记忆系统
===============================
v6.36 | 122 modules | 50-tier guardian chain | 47 retrieval channels | 129 papers aligned

Usage:
    from trinity import Trinity
    memory = Trinity()
    memory.ingest("user prefers dark mode")
    results = memory.search("user preferences", top_k=5)
"""

__version__ = "6.37.0"
__all__ = ["Trinity", "TrinityClient", "__version__"]

from trinity.core.client import Trinity, TrinityClient
