"""
Agent-to-Agent (A2A) Registry — Trinity 跨实例发现与通信适配器
================================================================
遵循 Google A2A 协议设计思路，使 Trinity 实例之间可以互相注册、
发现、心跳检测与任务分配。

核心特性:
  - Agent 注册与能力声明
  - JSON 文件持久化存储
  - 60 秒心跳超时自动离线标记
  - 跨实例传输包生成与解析
  - 按能力过滤发现

Usage:
    from trinity.a2a_registry import AgentRegistry, AgentInfo

    registry = AgentRegistry()
    registry.register(AgentInfo(
        agent_id="trinity-alpha",
        name="Alpha Instance",
        version="6.37.0",
        capabilities=["memory.search", "memory.store", "evolution.tick"],
        endpoint="mcp://localhost:8000",
        status="active",
    ))
"""

from __future__ import annotations

import json
import os
import time
import threading
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any


# ----------------------------------------------
# 数据结构
# ----------------------------------------------

@dataclass
class AgentInfo:
    """Agent 注册信息数据结构"""
    agent_id: str
    name: str
    version: str
    capabilities: List[str]          # ["memory.search", "memory.store", "evolution.tick", ...]
    endpoint: str                    # "mcp://localhost:8000" or "file://handoff.json"
    status: str                      # "active" | "idle" | "busy"
    last_heartbeat: float            # time.time() 时间戳
    metadata: Dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------
# 注册表核心
# ----------------------------------------------

class AgentRegistry:
    """
    轻量级 Agent 注册与发现系统。
    支持 Agent 注册、心跳、能力声明。
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "data", "a2a_registry.json"
        )
        self.db_path = os.path.normpath(self.db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        # 内存缓存 + 锁
        self._agents: Dict[str, AgentInfo] = {}
        self._lock = threading.Lock()
        self._load()

        # 任务日志（用于统计）
        self._recent_tasks: List[Dict[str, Any]] = []

    # -- 持久化 --------------------------------

    def _load(self) -> None:
        """从 JSON 文件加载已注册的 Agent"""
        if not os.path.exists(self.db_path):
            return
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("agents", []):
                info = AgentInfo(**item)
                self._agents[info.agent_id] = info
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"[A2A 注册表] 警告: 加载持久化数据失败 ({e})，将使用空注册表")

    def _save(self) -> None:
        """将当前注册表写入 JSON 文件"""
        data = {
            "registry_version": "1.0",
            "updated_at": time.time(),
            "agents": [asdict(a) for a in self._agents.values()],
        }
        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # -- 核心方法 ------------------------------

    def register(self, agent_info: AgentInfo) -> bool:
        """
        注册一个 Agent。
        如果 agent_id 已存在，则更新信息并重置心跳。
        返回 True 表示注册成功。
        """
        with self._lock:
            agent_info.last_heartbeat = time.time()
            if agent_info.agent_id in self._agents:
                old = self._agents[agent_info.agent_id]
                print(f"[A2A 注册表] 更新 Agent: {agent_info.agent_id} "
                      f"({old.status} -> {agent_info.status})")
            else:
                print(f"[A2A 注册表] 注册新 Agent: {agent_info.agent_id} "
                      f"({agent_info.name} v{agent_info.version}, "
                      f"能力数: {len(agent_info.capabilities)})")
            self._agents[agent_info.agent_id] = agent_info
            self._save()
        return True

    def discover(self, capability: Optional[str] = None) -> List[AgentInfo]:
        """
        发现所有 Agent。
        如果指定 capability，则只返回具备该能力的在线 Agent。
        自动清理超时 Agent（标记为离线）。
        """
        self._cleanup_stale()
        with self._lock:
            agents = list(self._agents.values())

        if capability is None:
            return agents

        # 按能力过滤 —— 匹配子字符串或完全匹配
        cap_lower = capability.lower()
        result = []
        for a in agents:
            if any(cap_lower in c.lower() for c in a.capabilities):
                result.append(a)

        return result

    def heartbeat(self, agent_id: str) -> bool:
        """
        更新指定 Agent 的心跳时间。
        如果 Agent 不存在，返回 False。
        """
        with self._lock:
            if agent_id not in self._agents:
                print(f"[A2A 注册表] 心跳失败: Agent '{agent_id}' 未注册")
                return False
            self._agents[agent_id].last_heartbeat = time.time()
            if self._agents[agent_id].status == "active":
                pass  # 保持活跃状态
            self._save()
        return True

    def unregister(self, agent_id: str) -> bool:
        """
        注销指定 Agent。
        返回 True 表示成功移除。
        """
        with self._lock:
            if agent_id not in self._agents:
                print(f"[A2A 注册表] 注销失败: Agent '{agent_id}' 未注册")
                return False
            info = self._agents.pop(agent_id)
            self._save()
            print(f"[A2A 注册表] 已注销 Agent: {agent_id} ({info.name})")
        return True

    def assign_task(self, agent_id: str, task: Dict[str, Any]) -> bool:
        """
        分配任务给指定 Agent。
        任务结构: {"task_id": str, "action": str, "params": dict, ...}
        返回 True 表示分配成功（Agent 存在且在线）。
        """
        with self._lock:
            if agent_id not in self._agents:
                print(f"[A2A 注册表] 任务分配失败: Agent '{agent_id}' 未注册")
                return False
            agent = self._agents[agent_id]
            if agent.status == "active":
                agent.status = "busy"
                self._save()

            task_record = {
                "task_id": task.get("task_id", f"task_{int(time.time())}"),
                "agent_id": agent_id,
                "action": task.get("action", "unknown"),
                "assigned_at": time.time(),
                "task": task,
            }
            self._recent_tasks.append(task_record)
            # 保留最近 100 条任务记录
            if len(self._recent_tasks) > 100:
                self._recent_tasks = self._recent_tasks[-100:]

        print(f"[A2A 注册表] 分配任务 '{task.get('task_id', 'N/A')}' -> {agent_id}")
        return True

    def get_stats(self) -> Dict[str, Any]:
        """
        统计信息：在线数、总能力数、最近任务。
        """
        self._cleanup_stale()
        with self._lock:
            agents = list(self._agents.values())

        online = [a for a in agents if a.status in ("active", "busy")]
        all_capabilities = set()
        for a in agents:
            all_capabilities.update(a.capabilities)

        # 最近 5 个任务
        recent = self._recent_tasks[-5:] if self._recent_tasks else []

        stats = {
            "total_agents": len(agents),
            "online_agents": len(online),
            "offline_agents": len(agents) - len(online),
            "total_capabilities": len(all_capabilities),
            "unique_capabilities": sorted(all_capabilities),
            "recent_tasks": len(self._recent_tasks),
            "recent_task_list": [
                {
                    "task_id": t["task_id"],
                    "agent_id": t["agent_id"],
                    "action": t["action"],
                }
                for t in recent
            ],
            "db_path": self.db_path,
            "timestamp": time.time(),
        }
        return stats

    # -- 心跳超时清理 --------------------------

    def _cleanup_stale(self, timeout: float = 60.0) -> None:
        """
        清理超过 timeout 秒无心跳的 Agent，将其标记为离线。
        不删除记录，仅将 status 改为 "idle"。
        """
        now = time.time()
        with self._lock:
            for agent_id, info in list(self._agents.items()):
                if info.status == "active" and (now - info.last_heartbeat) > timeout:
                    old_status = info.status
                    info.status = "idle"
                    print(f"[A2A 注册表] Agent '{agent_id}' 心跳超时 "
                          f"({timeout}s)，状态: {old_status} -> idle")
            self._save()

    # -- 跨实例传输 ----------------------------

    def prepare_transfer(self, agent_id: str, payload: Dict[str, Any]) -> Optional[str]:
        """
        生成跨实例传输的 JSON 包。
        用于 Trinity 实例间传递任务、状态或数据。
        返回 JSON 字符串。
        """
        with self._lock:
            if agent_id not in self._agents:
                print(f"[A2A 注册表] 传输失败: Agent '{agent_id}' 未注册")
                return None
            agent = self._agents[agent_id]

        transfer_packet = {
            "_a2a_protocol": "trinity_a2a_v1",
            "source_agent_id": agent_id,
            "source_name": agent.name,
            "source_version": agent.version,
            "created_at": time.time(),
            "ttl_seconds": 300,  # 默认 5 分钟有效
            "payload": payload,
        }

        transfer_json = json.dumps(transfer_packet, indent=2, ensure_ascii=False)
        print(f"[A2A 注册表] 已生成传输包 ({len(transfer_json)} bytes) -> {agent_id}")
        return transfer_json

    @staticmethod
    def receive_transfer(transfer_json: str) -> Optional[Dict[str, Any]]:
        """
        接收并解析跨实例传输包。
        返回解析后的字典，若格式无效则返回 None。
        """
        try:
            data = json.loads(transfer_json)
        except json.JSONDecodeError as e:
            print(f"[A2A 注册表] 传输包解析失败: JSON 格式错误 ({e})")
            return None

        if not isinstance(data, dict):
            print("[A2A 注册表] 传输包解析失败: 不是对象")
            return None

        protocol = data.get("_a2a_protocol", "")
        if protocol != "trinity_a2a_v1":
            print(f"[A2A 注册表] 传输包协议不匹配: '{protocol}' (期望 'trinity_a2a_v1')")
            return None

        # 检查 TTL
        created = data.get("created_at", 0)
        ttl = data.get("ttl_seconds", 300)
        if time.time() - created > ttl:
            age = time.time() - created
            print(f"[A2A 注册表] 传输包已过期 ({age:.1f}s > {ttl}s TTL)")
            return None

        print(f"[A2A 注册表] 成功接收来自 '{data.get('source_agent_id', 'unknown')}' 的传输包")
        return data


# ----------------------------------------------
# 自检
# ----------------------------------------------

def _selftest() -> None:
    """运行自检流程，验证所有核心功能"""
    print("=" * 60)
    print("  Trinity A2A AgentRegistry 自检")
    print("=" * 60)

    # 用临时路径，不影响正式数据
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="a2a_selftest_")
    registry = AgentRegistry(db_path=os.path.join(tmp_dir, "test_registry.json"))

    # 1. 注册 2 个 Agent
    print("\n[步骤 1/6] 注册 Agent...")
    agent_a = AgentInfo(
        agent_id="trinity-alpha",
        name="Alpha 实例",
        version="6.37.0",
        capabilities=["memory.search", "memory.store", "memory.delete", "evolution.tick"],
        endpoint="mcp://localhost:8000",
        status="active",
        last_heartbeat=time.time(),
        metadata={"region": "us-east-1", "role": "primary"},
    )
    agent_b = AgentInfo(
        agent_id="trinity-beta",
        name="Beta 实例",
        version="6.36.0",
        capabilities=["memory.search", "evolution.tick", "evolution.certify"],
        endpoint="file://handoffs/",
        status="active",
        last_heartbeat=time.time(),
        metadata={"region": "ap-southeast-1", "role": "worker"},
    )

    r1 = registry.register(agent_a)
    r2 = registry.register(agent_b)
    assert r1 and r2, "注册失败"
    print(f"  [OK] Alpha: {agent_a.capabilities}")
    print(f"  [OK] Beta:  {agent_b.capabilities}")

    # 2. 按能力发现
    print("\n[步骤 2/6] 按能力发现...")
    search_agents = registry.discover(capability="memory.search")
    assert len(search_agents) == 2, f"期望 2 个 Agent，发现 {len(search_agents)}"
    print(f"  [OK] 'memory.search' -> {len(search_agents)} 个 Agent: "
          f"{[a.agent_id for a in search_agents]}")

    store_agents = registry.discover(capability="memory.store")
    assert len(store_agents) == 1, f"期望 1 个 Agent，发现 {len(store_agents)}"
    print(f"  [OK] 'memory.store' -> {len(store_agents)} 个 Agent: "
          f"{[a.agent_id for a in store_agents]}")

    certify_agents = registry.discover(capability="evolution.certify")
    assert len(certify_agents) == 1, f"期望 1 个 Agent，发现 {len(certify_agents)}"
    print(f"  [OK] 'evolution.certify' -> {len(certify_agents)} 个 Agent: "
          f"{[a.agent_id for a in certify_agents]}")

    # 3. 心跳更新
    print("\n[步骤 3/6] 心跳更新...")
    time.sleep(0.1)
    hb_a = registry.heartbeat("trinity-alpha")
    hb_b = registry.heartbeat("trinity-beta")
    assert hb_a and hb_b, "心跳失败"
    # 测试不存在的心跳
    hb_fail = registry.heartbeat("trinity-nonexistent")
    assert not hb_fail, "不应返回 True"
    print("  [OK] Alpha & Beta 心跳已更新")
    print("  [OK] 不存在 Agent 心跳返回 False")

    # 4. 任务分配
    print("\n[步骤 4/6] 任务分配...")
    task1 = {"task_id": "t001", "action": "memory.search", "params": {"query": "user preferences"}}
    task2 = {"task_id": "t002", "action": "evolution.tick", "params": {}}
    ta1 = registry.assign_task("trinity-alpha", task1)
    ta2 = registry.assign_task("trinity-beta", task2)
    assert ta1 and ta2, "任务分配失败"
    # 测试分配给不存在的 Agent
    ta_fail = registry.assign_task("trinity-nonexistent", task1)
    assert not ta_fail, "不应返回 True"
    print("  [OK] 任务 t001 -> Alpha")
    print("  [OK] 任务 t002 -> Beta")
    print("  [OK] 不存在 Agent 分配返回 False")

    # 5. 跨实例传输
    print("\n[步骤 5/6] 跨实例传输...")
    transfer_json = registry.prepare_transfer(
        "trinity-alpha",
        {"action": "sync_memory", "keys": ["user_pref_dark_mode", "user_pref_language"]}
    )
    assert transfer_json is not None, "传输包生成失败"
    print(f"  [OK] 生成了传输包 ({len(transfer_json)} bytes)")

    # 模拟接收（同一实例接收自己的包）
    received = AgentRegistry.receive_transfer(transfer_json)
    assert received is not None, "传输包接收解析失败"
    assert received["source_agent_id"] == "trinity-alpha"
    assert received["payload"]["action"] == "sync_memory"
    print(f"  [OK] 成功接收并解析来自 '{received['source_agent_id']}' 的传输包")

    # 测试无效传输
    bad = AgentRegistry.receive_transfer("{invalid json")
    assert bad is None, "无效 JSON 不应返回数据"
    print("  [OK] 无效 JSON 返回 None (预期行为)")

    # 协议不匹配
    wrong_proto = AgentRegistry.receive_transfer(
        json.dumps({"_a2a_protocol": "unknown_v1", "source_agent_id": "test"})
    )
    assert wrong_proto is None, "协议不匹配不应返回数据"
    print("  [OK] 协议不匹配返回 None (预期行为)")
    assert wrong_proto is None

    # 6. 统计
    print("\n[步骤 6/6] 统计信息...")
    stats = registry.get_stats()
    print(f"  [OK] 总 Agent 数: {stats['total_agents']}")
    print(f"  [OK] 在线 Agent 数: {stats['online_agents']}")
    print(f"  [OK] 离线 Agent 数: {stats['offline_agents']}")
    print(f"  [OK] 总独立能力数: {stats['total_capabilities']}")
    print(f"  [OK] 最近任务数: {stats['recent_tasks']}")
    print(f"  [OK] 能力列表: {stats['unique_capabilities']}")
    print(f"  [OK] 最近任务: {stats['recent_task_list']}")

    # 清理临时文件
    try:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    # -- 结果 --
    print("\n" + "=" * 60)
    print("  自检结果: OK 全部通过")
    print("=" * 60)


if __name__ == "__main__":
    _selftest()
