#!/usr/bin/env python3
"""
Trinity REST API Server — A2A protocol routes (non-marvis).
"""

from fastapi import APIRouter, HTTPException

from ._deps import _live_memory as get_memory
from ._models import (
    A2AMessageRequest,
    A2ATaskRequest,
    A2ATaskUpdateRequest,
    AgentCardRequest,
    CapabilityAuthorizeRequest,
    CapabilityRevokeRequest,
    SecuritySignRequest,
    SecurityVerifyRequest,
    TaskGrantRequest,
)

router = APIRouter()


_a2a_task_manager = None
_a2a_capability_registry = None
_a2a_protocol = None


def _get_a2a_task_manager():
    global _a2a_task_manager
    if _a2a_task_manager is None:
        from trinity.a2a.task_manager import TaskManager
        mem = get_memory()
        _a2a_task_manager = TaskManager(
            adapter=mem._adapter if hasattr(mem, '_adapter') else None,
        )
    return _a2a_task_manager


def _get_a2a_registry():
    global _a2a_capability_registry
    if _a2a_capability_registry is None:
        from trinity.a2a.capability_registry import CapabilityRegistry
        mem = get_memory()
        _a2a_capability_registry = CapabilityRegistry(
            adapter=mem._adapter if hasattr(mem, '_adapter') else None,
        )
    return _a2a_capability_registry


def _get_a2a_protocol():
    global _a2a_protocol
    if _a2a_protocol is None:
        from trinity.a2a.protocol import A2AProtocol
        _a2a_protocol = A2AProtocol()
    return _a2a_protocol


def _get_a2a_capability_auth():
    """Lazy singleton for CapabilityAuth."""
    from trinity.a2a.security import get_capability_auth
    return get_capability_auth()


def _get_a2a_task_permission():
    """Lazy singleton for TaskPermission."""
    from trinity.a2a.security import get_task_permission
    return get_task_permission()


@router.get("/a2a/agents", tags=["A2A Protocol"], summary="列出所有注册Agent")
async def a2a_list_agents():
    """列出所有注册的 Agent。"""
    reg = _get_a2a_registry()
    return reg.list_all_agents()


@router.get("/a2a/agents/{agent_id}/card", tags=["A2A Protocol"], summary="获取 Agent 能力卡片")
async def a2a_get_agent_card(agent_id: str):
    """获取指定 Agent 的能力卡片。"""
    reg = _get_a2a_registry()
    from trinity.a2a.agent_card import generate_card
    card = generate_card(agent_id)
    return card


@router.post("/a2a/agents/register", tags=["A2A Protocol"], summary="注册 Agent 到联邦目录")
async def a2a_register_agent(req: AgentCardRequest):
    """注册 Agent 到联邦能力目录。"""
    from trinity.a2a.agent_card import AgentCard, SkillDef
    skills = [SkillDef(name=s.get("name", ""), description=s.get("description", ""),
                        input_schema=s.get("input_schema", {}), output_schema=s.get("output_schema", {}),
                        examples=s.get("examples", []))
              for s in req.skills]
    card = AgentCard(
        agent_id=req.agent_id,
        name=req.name,
        description=req.description,
        version=req.version,
        capabilities=req.capabilities,
        endpoints=req.endpoints,
        skills=skills,
        input_modes=req.input_modes,
        output_modes=req.output_modes,
        security_level=req.security_level,
    )
    reg = _get_a2a_registry()
    result = reg.register_agent(card)
    return result


@router.delete("/a2a/agents/{agent_id}", tags=["A2A Protocol"], summary="注销 Agent")
async def a2a_unregister_agent(agent_id: str):
    """注销 Agent。"""
    reg = _get_a2a_registry()
    result = reg.unregister_agent(agent_id)
    return result


@router.post("/a2a/tasks", tags=["A2A Protocol"], summary="创建跨Agent 任务")
async def a2a_create_task(req: A2ATaskRequest):
    """创建跨Agent 任务。"""
    tm = _get_a2a_task_manager()
    result = tm.create_task(req.from_agent, req.to_agent, req.payload)
    return result


@router.get("/a2a/tasks/{task_id}", tags=["A2A Protocol"], summary="查询任务状态")
async def a2a_query_task(task_id: str):
    """查询跨Agent 任务状态。"""
    tm = _get_a2a_task_manager()
    result = tm.query_task(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return result


@router.put("/a2a/tasks/{task_id}", tags=["A2A Protocol"], summary="更新任务状态")
async def a2a_update_task(task_id: str, req: A2ATaskUpdateRequest):
    """更新跨Agent 任务状态（含SSE 推送）。"""
    tm = _get_a2a_task_manager()
    result = tm.update_task(task_id, req.status, req.result)
    if not result:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return {"status": "ok", "task_id": task_id, "new_status": req.status}


@router.get("/a2a/tasks", tags=["A2A Protocol"], summary="列出所有任务")
async def a2a_list_tasks(agent_id: str = None, status: str = None, limit: int = 50):
    """列出跨Agent 任务。"""
    tm = _get_a2a_task_manager()
    tasks = tm.list_tasks(agent_id=agent_id, status=status)
    return {"tasks": tasks}


@router.post("/a2a/message", tags=["A2A Protocol"], summary="发送A2A 消息")
async def a2a_send_message(req: A2AMessageRequest):
    """发送A2A 消息（JSON-RPC 2.0）。"""
    proto = _get_a2a_protocol()
    if req.to_agent:
        result = proto.send_message(req.from_agent, req.to_agent,
                                    req.method, req.params, req.id)
    else:
        result = proto.broadcast(req.from_agent, req.method, req.params, req.id)
    return result


@router.get("/a2a/match", tags=["A2A Protocol"], summary="按能力匹配Agent")
async def a2a_match_agent(capability: str = None):
    """按能力匹配最佳Agent。"""
    reg = _get_a2a_registry()
    if capability:
        agents = reg.find_agent_by_capability(capability)
        return {"matched": agents, "capability": capability}
    return {"agents": reg.list_all_agents()}


@router.post("/a2a/security/sign", tags=["A2A Security"], summary="AgentCard RSA 签名")
async def a2a_security_sign(req: SecuritySignRequest):
    """对AgentCard 进行 RSA 签名，返回哈希和签名。
    如果未提供private_key_path，则自动生成临时密钥对。    """
    from trinity.a2a.security import AgentCardSigner
    from trinity.a2a.agent_card import generate_card, AgentCard
    import tempfile, os

    card = generate_card(req.agent_id, name=req.name, capabilities=req.capabilities)

    if req.private_key_path:
        priv_path = req.private_key_path
    else:
        # Auto-generate key pair for convenience
        tmpdir = tempfile.mkdtemp(prefix="a2a_keys_")
        AgentCardSigner.generate_key_pair(tmpdir)
        priv_path = os.path.join(tmpdir, "private.pem")

    card_hash = AgentCardSigner.get_card_hash(card)
    signature = AgentCardSigner.sign(card, priv_path)

    return {
        "agent_id": req.agent_id,
        "card_hash": card_hash,
        "signature": signature,
        "algorithm": "RSA-SHA256",
    }


@router.post("/a2a/security/verify", tags=["A2A Security"], summary="验证 AgentCard 签名")
async def a2a_security_verify(req: SecurityVerifyRequest):
    """验证 AgentCard 的RSA 签名是否有效。"""
    from trinity.a2a.security import AgentCardSigner
    from trinity.a2a.agent_card import generate_card

    card = generate_card(req.agent_id, name=req.name or req.agent_id,
                         capabilities=req.capabilities)

    if not req.public_key_path:
        raise HTTPException(status_code=400, detail="public_key_path is required for verification")

    valid = AgentCardSigner.verify(card, req.signature, req.public_key_path)

    return {
        "agent_id": req.agent_id,
        "valid": valid,
        "card_hash": AgentCardSigner.get_card_hash(card),
    }


@router.post("/a2a/security/capability/authorize", tags=["A2A Security"],
          summary="授予 Agent 能力")
async def a2a_capability_authorize(req: CapabilityAuthorizeRequest):
    """为指定Agent 授予一项能力（加入白名单）。"""
    reg = _get_a2a_registry()
    return reg.authorize_capability(req.agent_id, req.capability)


@router.post("/a2a/security/capability/revoke", tags=["A2A Security"],
          summary="撤销 Agent 能力")
async def a2a_capability_revoke(req: CapabilityRevokeRequest):
    """撤销指定 Agent 的一项已授权能力。"""
    reg = _get_a2a_registry()
    return reg.revoke_capability(req.agent_id, req.capability)


@router.get("/a2a/security/capability/{agent_id}", tags=["A2A Security"],
         summary="查询 Agent 能力授权")
async def a2a_capability_query(agent_id: str):
    """查询指定 Agent 当前的授权能力列表。"""
    auth = _get_a2a_capability_auth()
    return auth.get_agent_policy(agent_id)


@router.post("/a2a/security/task/grant", tags=["A2A Security"],
          summary="授予任务访问权")
async def a2a_task_grant(req: TaskGrantRequest):
    """为指定Agent 授予对某个任务的 guest 访问权限。"""
    tp = _get_a2a_task_permission()
    return tp.grant_task_access(req.task_id, req.agent_id)


@router.get("/a2a/security/task/{task_id}/acl", tags=["A2A Security"],
         summary="查询任务 ACL")
async def a2a_task_acl(task_id: str):
    """查询指定任务的访问控制列表（creator/assignee/guests/superiors）。"""
    tp = _get_a2a_task_permission()
    acl = tp.get_task_acl(task_id)
    if acl is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found in ACL")
    return acl


