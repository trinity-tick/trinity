"""
Trinity SDK — Standardized Python SDK for Trinity Memory System.

Usage::

    from trinity.sdk import TrinitySDK

    with TrinitySDK() as trinity:
        trinity.write("Hello world", modality="text")
        results = trinity.search("Hello", limit=5)
"""

from trinity.sdk.client import TrinitySDK
from trinity.sdk.types import (
    Memory,
    SearchResult,
    Entity,
    Relation,
    Stats,
    Health,
)
from trinity.sdk.exceptions import (
    TrinityError,
    ConnectionError,
    AuthenticationError,
    MemoryNotFound,
    DuplicateMemory,
    ConflictError,
    ValidationError,
)

__all__ = [
    "TrinitySDK",
    "Memory",
    "SearchResult",
    "Entity",
    "Relation",
    "Stats",
    "Health",
    "TrinityError",
    "ConnectionError",
    "AuthenticationError",
    "MemoryNotFound",
    "DuplicateMemory",
    "ConflictError",
    "ValidationError",
]
