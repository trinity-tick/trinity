"""
RBAC Middleware for Trinity API (P0.6)
======================================
Wires the existing RBACEngine into the FastAPI server as an HTTP middleware
and as a dependency injection helper.

Features:
  - ``RBACMiddleware``: Extracts ``X-Agent-ID`` / ``X-App-ID`` / ``X-Agent-Role``
    headers, constructs a Subject, and checks permissions against route metadata.
  - ``require_permission``: Dependency factory for per-endpoint access control.
  - ``check_agent_access``: Lightweight utility for direct permission queries.

Integration:
  In ``server.py``:
      from trinity.api.rbac_middleware import RBACMiddleware, get_rbac_engine
      app.add_middleware(RBACMiddleware, exempt_paths=["/health", "/docs", ...],
                         agent_roles_default={"admin", "viewer"})

  Per-endpoint:
      @app.post("/memories")
      @require_permission(PermissionAction.WRITE, ResourceType.MEMORY)
      async def store_memory(...): ...
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from trinity.audit.rbac import (
    AccessDecision,
    PermissionAction,
    PermissionPolicy,
    RBACEngine,
    Resource,
    ResourceType,
    Role,
    Subject,
)

logger = logging.getLogger(__name__)

# ── 角色可见性规则（2026-08-26 Budibase 借鉴 Phase 3b）──────────────
# env: TRINITY_VISIBILITY_<ROLE>（如 TRINITY_VISIBILITY_VIEWER="importance >= 0.3 AND category != 'lme'"），
# 多角色取并集表达式（AND 拼接）；行级规则在 search 路由未显式指定时自动应用。
import os as _os  # noqa: E402


def visibility_rule_for_roles(roles: Optional[Set[str]]) -> Optional[str]:
    """按角色集合解析可见性规则（env 白名单；无配置返回 None）。"""
    if not roles:
        return None
    parts = []
    for r in sorted(roles):
        key = f"TRINITY_VISIBILITY_{r.upper().replace('-', '_')}"
        v = _os.environ.get(key, "").strip()
        if v:
            parts.append(v)
    return " AND ".join(parts) if parts else None


# ── Module-level engine singleton ────────────────────────────────────
_rbac_engine: Optional[RBACEngine] = None

# Six predefined Trinity roles (from original design)
# Each permission is a (action, resource_type) pair
_ALL_RESOURCES = [
    ResourceType.MEMORY, ResourceType.GRAPH, ResourceType.EMBEDDING,
    ResourceType.AGENT, ResourceType.IDENTITY, ResourceType.AUDIT_LOG,
    ResourceType.CONSTITUTION, ResourceType.CONFIGURATION,
    ResourceType.TENANT, ResourceType.GLOBAL,
]
_FULL_ACCESS = [(a, r) for a in PermissionAction for r in _ALL_RESOURCES]

_PREDEFINED_ROLES: List[Dict[str, Any]] = [
    {
        "name": "admin",
        "permissions": _FULL_ACCESS,
        "inherits_from": [],
        "metadata": {"description": "Full administrative access to all resources"},
    },
    {
        "name": "operator",
        "permissions": [
            (PermissionAction.READ, r) for r in _ALL_RESOURCES
        ] + [
            (PermissionAction.WRITE, ResourceType.MEMORY),
            (PermissionAction.EXECUTE, ResourceType.AGENT),
            (PermissionAction.AUDIT, ResourceType.AUDIT_LOG),
            (PermissionAction.SEARCH, ResourceType.MEMORY),
            (PermissionAction.INGEST, ResourceType.MEMORY),
        ],
        "inherits_from": [],
        "metadata": {"description": "Operational access for day-to-day tasks"},
    },
    {
        "name": "developer",
        "permissions": [
            (PermissionAction.READ, r) for r in _ALL_RESOURCES
        ] + [
            (PermissionAction.WRITE, ResourceType.MEMORY),
            (PermissionAction.WRITE, ResourceType.CONFIGURATION),
            (PermissionAction.EXECUTE, ResourceType.AGENT),
            (PermissionAction.SEARCH, ResourceType.MEMORY),
            (PermissionAction.INGEST, ResourceType.MEMORY),
            (PermissionAction.CONFIGURE, ResourceType.CONFIGURATION),
        ],
        "inherits_from": ["viewer"],
        "metadata": {"description": "Dev access with configuration privileges"},
    },
    {
        "name": "viewer",
        "permissions": [
            (PermissionAction.READ, r) for r in _ALL_RESOURCES
        ] + [
            (PermissionAction.SEARCH, ResourceType.MEMORY),
        ],
        "inherits_from": [],
        "metadata": {"description": "Read-only access to memory and audit logs"},
    },
    {
        "name": "auditor",
        "permissions": [
            (PermissionAction.READ, r) for r in _ALL_RESOURCES
        ] + [
            (PermissionAction.AUDIT, ResourceType.AUDIT_LOG),
            (PermissionAction.EXPORT, ResourceType.AUDIT_LOG),
            (PermissionAction.SEARCH, ResourceType.MEMORY),
        ],
        "inherits_from": ["viewer"],
        "metadata": {"description": "Audit-focused access with export rights"},
    },
    {
        "name": "agent",
        "permissions": [
            (PermissionAction.READ, ResourceType.MEMORY),
            (PermissionAction.WRITE, ResourceType.MEMORY),
            (PermissionAction.SEARCH, ResourceType.MEMORY),
            (PermissionAction.INGEST, ResourceType.MEMORY),
        ],
        "inherits_from": [],
        "metadata": {"description": "Default sub-agent access"},
    },
]

# Route → (action, resource_type) mapping
# If a path prefix matches, the middleware enforces the corresponding permission.
_ENDPOINT_ACL_MAP: Dict[str, tuple] = {
    "POST /memories": (PermissionAction.WRITE, ResourceType.MEMORY),
    "DELETE /memories": (PermissionAction.DELETE, ResourceType.MEMORY),
    "POST /vector/index": (PermissionAction.WRITE, ResourceType.EMBEDDING),
    "POST /constitution": (PermissionAction.WRITE, ResourceType.CONSTITUTION),
    "DELETE /constitution": (PermissionAction.DELETE, ResourceType.CONSTITUTION),
    "GET /constitution": (PermissionAction.READ, ResourceType.CONSTITUTION),
    "GET /audit": (PermissionAction.AUDIT, ResourceType.AUDIT_LOG),
    "POST /agents/register": (PermissionAction.WRITE, ResourceType.AGENT),
    "POST /agents/memory/write": (PermissionAction.INGEST, ResourceType.MEMORY),
    "POST /agents/memory/bulk_write": (PermissionAction.INGEST, ResourceType.MEMORY),
}

# ── Engine singleton ─────────────────────────────────────────────────

def get_rbac_engine() -> RBACEngine:
    """Return or create the module-level RBAC engine singleton with predefined roles."""
    global _rbac_engine
    if _rbac_engine is None:
        _rbac_engine = RBACEngine()
        for role_def in _PREDEFINED_ROLES:
            role = Role(
                name=role_def["name"],
                permissions=role_def["permissions"],
                inherits_from=role_def["inherits_from"],
                metadata=role_def["metadata"],
            )
            _rbac_engine.register_role(role)
            # Materialize each (action, resource_type) permission into an
            # explicit ALLOW policy, because the RBACEngine grants access
            # exclusively through PermissionPolicy objects (default-deny).
            for action, resource_type in role_def["permissions"]:
                _rbac_engine.add_policy(
                    PermissionPolicy(
                        role_name=role_def["name"],
                        action=action,
                        resource_type=resource_type,
                        effect="allow",
                    )
                )
        logger.info(
            "Initialized RBAC engine with %d predefined roles",
            len(_PREDEFINED_ROLES),
        )
    return _rbac_engine


def _get_or_create_subject(
    engine: RBACEngine,
    agent_id: str,
    roles: Set[str],
    app_id: Optional[str] = None,
) -> Subject:
    """Return an existing subject (preserving roles) or create a new one.

    The RBAC engine has no public ``get_subject`` method; we rely on
    ``get_subject_roles`` to detect whether a subject has been registered.
    """
    existing_roles = engine.get_subject_roles(agent_id)
    if existing_roles:
        # Subject already registered — preserve effective roles
        return Subject(
            subject_id=agent_id,
            subject_type="agent",
            roles=existing_roles,
            attributes={"app_id": app_id} if app_id else {},
        )
    # New subject — register with provided roles
    subject = Subject(
        subject_id=agent_id,
        subject_type="agent",
        roles=roles,
        attributes={"app_id": app_id} if app_id else {},
    )
    engine.register_subject(subject)
    return subject


def reset_rbac_engine() -> None:
    """Reset the engine singleton (for testing)."""
    global _rbac_engine
    _rbac_engine = None


# ── Dependency factory ───────────────────────────────────────────────

def require_permission(action: PermissionAction, resource_type: ResourceType):
    """FastAPI dependency factory: enforce a permission on the current request.

    Usage::

        @app.post("/memories")
        @require_permission(PermissionAction.WRITE, ResourceType.MEMORY)
        async def store_memory(request: Request, ...):
            ...

    Returns a dependency callable suitable for ``Depends()`` — use as decorator
    (recommended) or as ``Depends(require_permission(...))`` directly.
    """
    async def _dependency(request: Request) -> None:
        agent_id = (request.headers.get("X-Agent-ID") or
                    request.headers.get("x-agent-id") or
                    "anonymous")
        app_id = (request.headers.get("X-App-ID") or
                  request.headers.get("x-app-id"))
        role_header = (request.headers.get("X-Agent-Role") or
                       request.headers.get("x-agent-role"))
        roles = {role_header} if role_header else {"agent"}

        engine = get_rbac_engine()

        # Ensure subject is registered (idempotent)
        subject = _get_or_create_subject(engine, agent_id, roles, app_id)

        # Sync roles from header if present
        if role_header:
            for r in roles:
                if r not in engine.get_subject_roles(agent_id):
                    engine.assign_role(agent_id, r)

        # Build resource
        resource = Resource(
            resource_type=resource_type,
            resource_id=app_id or None,
            tenant_id=app_id or None,
        )

        decision = engine.check_permission(subject, action, resource)
        if decision != AccessDecision.ALLOW:
            logger.warning(
                "Access denied: agent=%s action=%s resource=%s decision=%s",
                agent_id, action.value, resource_type.value, decision.name,
            )
            # Store decision in request state for endpoint introspection
            request.state.rbac_decision = decision
            raise PermissionError(
                f"Access denied: agent '{agent_id}' does not have "
                f"'{action.value}' permission on '{resource_type.value}'"
            )

        request.state.rbac_decision = decision
        request.state.rbac_subject = subject

    return _dependency


# ── Middleware ───────────────────────────────────────────────────────

class RBACMiddleware(BaseHTTPMiddleware):
    """HTTP middleware that enforces ACL rules on protected endpoints.

    Headers inspected:
      - ``X-Agent-ID``: The requesting agent's unique ID (required on protected routes).
      - ``X-App-ID``: The application context (optional, maps to tenant_id).
      - ``X-Agent-Role``: Explicit role override (optional).

    Parameters
    ----------
    app : the FastAPI application.
    exempt_paths : paths that skip RBAC enforcement (e.g. /health, /docs).
    agent_roles_default : default role set when no X-Agent-Role header is present.
    enable_route_map : whether to enforce ``_ENDPOINT_ACL_MAP`` entries at the
                       middleware level. Set False to use ``require_permission``
                       dependencies exclusively.
    """

    def __init__(
        self,
        app,
        *,
        exempt_paths: Optional[Sequence[str]] = None,
        agent_roles_default: Optional[Set[str]] = None,
        enable_route_map: bool = True,
    ):
        super().__init__(app)
        self._exempt = set(exempt_paths or ["/health", "/docs", "/redoc", "/openapi.json", "/"])
        self._default_roles = agent_roles_default or {"agent"}
        self._enable_route_map = enable_route_map

    async def dispatch(self, request: Request, call_next: Callable[[Request], Any]) -> Response:
        # Skip exempt paths
        if request.url.path in self._exempt:
            return await call_next(request)

        # 角色可见性规则注入（2026-08-26 Budibase 借鉴 Phase 3b）：在 ACL map
        # 检查之前注入 request.state.rbac_visibility——search 路由未显式传
        # visibility_rule 时自动应用角色级行级规则。
        try:
            _role_header = (request.headers.get("X-Agent-Role") or
                            request.headers.get("x-agent-role"))
            _roles = {_role_header} if _role_header else self._default_roles
            _vrule = visibility_rule_for_roles(_roles)
            if _vrule:
                request.state.rbac_visibility = _vrule
        except Exception:
            pass

        # Skip if route map is disabled (using per-endpoint dependencies instead)
        if not self._enable_route_map:
            return await call_next(request)

        # Build route key
        route_key = f"{request.method} {request.url.path}"

        if route_key not in _ENDPOINT_ACL_MAP:
            return await call_next(request)

        action, resource_type = _ENDPOINT_ACL_MAP[route_key]

        # Extract headers
        agent_id = (request.headers.get("X-Agent-ID") or
                    request.headers.get("x-agent-id"))
        app_id = (request.headers.get("X-App-ID") or
                  request.headers.get("x-app-id"))

        # If no agent ID on a protected route, deny
        if not agent_id:
            logger.warning("Missing X-Agent-ID on protected route %s", route_key)
            return JSONResponse(
                status_code=401,
                content={
                    "error": "authentication_required",
                    "detail": "X-Agent-ID header is required for this endpoint",
                    "path": request.url.path,
                },
            )

        role_header = (request.headers.get("X-Agent-Role") or
                       request.headers.get("x-agent-role"))
        roles = {role_header} if role_header else self._default_roles

        engine = get_rbac_engine()

        # Register / sync subject
        subject = _get_or_create_subject(engine, agent_id, roles, app_id)
        if role_header:
            for r in roles:
                if r not in engine.get_subject_roles(agent_id):
                    engine.assign_role(agent_id, r)

        resource = Resource(
            resource_type=resource_type,
            resource_id=app_id or None,
            tenant_id=app_id or None,
        )

        decision = engine.check_permission(subject, action, resource)

        if decision != AccessDecision.ALLOW:
            logger.warning(
                "RBAC denied: agent=%s action=%s resource=%s route=%s decision=%s",
                agent_id, action.value, resource_type.value, route_key, decision.name,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error": "access_denied",
                    "detail": (
                        f"Agent '{agent_id}' (roles={sorted(engine.get_subject_roles(agent_id))}) "
                        f"does not have '{action.value}' permission on '{resource_type.value}'"
                    ),
                    "path": request.url.path,
                    "agent_id": agent_id,
                    "required_action": action.value,
                    "required_resource": resource_type.value,
                },
            )

        # Store for downstream handlers
        request.state.rbac_decision = decision
        request.state.rbac_subject = subject

        return await call_next(request)


# ── Utility ──────────────────────────────────────────────────────────

def check_agent_access(
    agent_id: str,
    action: str,
    resource_type: str,
    app_id: Optional[str] = None,
    roles: Optional[Set[str]] = None,
) -> bool:
    """Lightweight sync permission check (non-request-context use).

    Parameters
    ----------
    agent_id : agent identifier.
    action : one of ``PermissionAction`` member values (e.g. ``"write"``).
    resource_type : one of ``ResourceType`` member values (e.g. ``"memory"``).
    app_id : optional tenant/app scope.
    roles : optional role set; if None, reads existing subject roles or defaults to ``{agent}``.

    Returns
    -------
    ``True`` if ALLOW, ``False`` otherwise.
    """
    engine = get_rbac_engine()

    # Resolve action and resource type
    try:
        pa = PermissionAction(action)
    except ValueError:
        logger.error("Unknown action: %s", action)
        return False
    try:
        rt = ResourceType(resource_type)
    except ValueError:
        logger.error("Unknown resource type: %s", resource_type)
        return False

    subject = _get_or_create_subject(engine, agent_id, roles or {"agent"}, app_id)

    resource = Resource(resource_type=rt, resource_id=app_id, tenant_id=app_id)
    decision = engine.check_permission(subject, pa, resource)
    return decision == AccessDecision.ALLOW


# ── Self-test ────────────────────────────────────────────────────────

def self_test() -> Dict[str, Any]:
    """Validate RBAC middleware: role registration, permission checks, and access flow."""
    reset_rbac_engine()
    engine = get_rbac_engine()

    passed = 0
    failed = 0
    details: List[str] = []

    # ── Test 1: Role registration ──
    roles = engine.list_roles()
    if len(roles) >= 6:
        passed += 1
    else:
        failed += 1
        details.append(f"Expected >=6 roles, got {len(roles)}")

    # ── Test 2: admin has permissions ──
    admin_role = engine.get_role("admin")
    if admin_role and len(admin_role.permissions) > 0:
        passed += 1
    else:
        failed += 1
        details.append(f"admin missing permissions, got {len(admin_role.permissions) if admin_role else 'None'}")

    # ── Test 3: viewer is read-only ──
    viewer_role = engine.get_role("viewer")
    # permissions are List[Tuple[PermissionAction, ResourceType]]
    viewer_actions = {p[0].value for p in viewer_role.permissions} if viewer_role else set()
    if {"read", "search"} <= viewer_actions and "write" not in viewer_actions and "delete" not in viewer_actions:
        passed += 1
    else:
        failed += 1
        details.append(f"viewer actions incorrect: {viewer_actions}")

    # ── Test 4: admin can write memory ──
    admin_subject = Subject(subject_id="test_admin", subject_type="agent",
                            roles={"admin"})
    engine.register_subject(admin_subject)
    decision = engine.check_permission(
        admin_subject, PermissionAction.WRITE,
        Resource(ResourceType.MEMORY),
    )
    if decision == AccessDecision.ALLOW:
        passed += 1
    else:
        failed += 1
        details.append(f"admin write memory returned {decision}")

    # ── Test 5: viewer cannot write memory ──
    viewer_subject = Subject(subject_id="test_viewer", subject_type="agent",
                             roles={"viewer"})
    engine.register_subject(viewer_subject)
    decision = engine.check_permission(
        viewer_subject, PermissionAction.WRITE,
        Resource(ResourceType.MEMORY),
    )
    if decision == AccessDecision.DENY:
        passed += 1
    else:
        failed += 1
        details.append(f"viewer write memory returned {decision} (expected DENY)")

    # ── Test 6: developer inherits viewer.read ──
    dev_subject = Subject(subject_id="test_dev", subject_type="agent",
                          roles={"developer"})
    engine.register_subject(dev_subject)
    decision = engine.check_permission(
        dev_subject, PermissionAction.READ,
        Resource(ResourceType.MEMORY),
    )
    if decision == AccessDecision.ALLOW:
        passed += 1
    else:
        failed += 1
        details.append(f"developer.read (inherited) returned {decision}")

    # ── Test 7: agent can ingest but cannot delete ──
    agent_subject = Subject(subject_id="test_agent", subject_type="agent",
                            roles={"agent"})
    engine.register_subject(agent_subject)
    ingest_ok = engine.check_permission(
        agent_subject, PermissionAction.INGEST,
        Resource(ResourceType.MEMORY),
    ) == AccessDecision.ALLOW
    delete_ok = engine.check_permission(
        agent_subject, PermissionAction.DELETE,
        Resource(ResourceType.MEMORY),
    ) == AccessDecision.DENY
    if ingest_ok and delete_ok:
        passed += 1
    else:
        failed += 1
        details.append(f"agent ingest={ingest_ok} delete={delete_ok}")

    # ── Test 8: check_agent_access utility works ──
    ok = check_agent_access("inline_test", "write", "memory", roles={"admin"})
    if ok:
        passed += 1
    else:
        failed += 1
        details.append("check_agent_access returned False for admin write memory")

    return {
        "module": "trinity.api.rbac_middleware",
        "result": "PASS" if failed == 0 else "FAIL",
        "passed": passed,
        "failed": failed,
        "total": passed + failed,
        "details": details,
    }
