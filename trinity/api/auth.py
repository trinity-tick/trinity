"""
API Key authentication middleware for FastAPI.

Usage:
    from trinity.api.auth import require_api_key

    @app.get("/protected")
    async def protected_route(auth=Depends(require_api_key)):
        return {"message": "authenticated"}

Environment variables:
    TRINITY_API_KEYS  — Comma-separated list of valid API keys
    TRINITY_API_KEY   — Single API key (fallback, checked first)

If neither is set, authentication is disabled (backward compatible).
"""

import os
from typing import List, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# ── Load API keys from environment ──────────────────────────────────────

def _load_api_keys() -> List[str]:
    """Load valid API keys from environment variables.

    Priority:
        1. TRINITY_API_KEY (single key)
        2. TRINITY_API_KEYS (comma-separated list)
    """
    single_key = os.environ.get("TRINITY_API_KEY", "").strip()
    if single_key:
        return [single_key]

    keys_str = os.environ.get("TRINITY_API_KEYS", "").strip()
    if keys_str:
        return [k.strip() for k in keys_str.split(",") if k.strip()]

    return []


# Module-level cache of valid keys
_VALID_API_KEYS: List[str] = _load_api_keys()

# HTTP bearer scheme — used by the dependency
_bearer_scheme = HTTPBearer(auto_error=False)


def require_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[str]:
    """FastAPI dependency that validates an API key from the Authorization header.

    If no API keys are configured (TRINITY_API_KEYS / TRINITY_API_KEY not set),
    the check is skipped entirely and the request proceeds (backward compatible).

    Args:
        credentials: Automatically extracted Bearer token from the request.

    Returns:
        The validated API key string (or None if auth is disabled).

    Raises:
        HTTPException 401: If the provided API key is invalid.
        HTTPException 403: If no credentials provided and auth is required.
    """
    if not _VALID_API_KEYS:
        # Auth is disabled — pass through
        return None

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authentication required. Provide a Bearer token in the Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    if token not in _VALID_API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token


def reload_api_keys() -> None:
    """Reload API keys from environment variables.

    Useful for hot-reload scenarios or after updating env vars at runtime.
    """
    global _VALID_API_KEYS
    _VALID_API_KEYS = _load_api_keys()


def get_valid_keys() -> List[str]:
    """Return the current list of valid API keys (for diagnostics)."""
    return list(_VALID_API_KEYS)
