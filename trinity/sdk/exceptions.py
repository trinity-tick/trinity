"""
Trinity SDK Exception Classes.

All exceptions inherit from TrinityError, making it easy to catch
all SDK errors with a single ``except TrinityError``.
"""


class TrinityError(Exception):
    """Base exception for all Trinity SDK errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class ConnectionError(TrinityError):
    """Failed to connect to the Trinity server."""


class AuthenticationError(TrinityError):
    """Authentication failed (reserved for future auth)."""


class MemoryNotFound(TrinityError):
    """Requested memory entry does not exist."""


class DuplicateMemory(TrinityError):
    """A memory with the same content_hash already exists."""


class ConflictError(TrinityError):
    """A conflict was detected when resolving a memory conflict group."""


class ValidationError(TrinityError):
    """Invalid request parameters."""
