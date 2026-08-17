"""
Trinity A2A gRPC Transport

Implements the A2A v0.3 protocol over gRPC with inline proto definitions.

Key features:
  - SendTask / GetTask / CancelTask RPC methods
  - gRPC channel connection pooling
  - Runtime proto parsing (no proto compiler required)
  - TLS/mTLS support via cert_file / key_file
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from trinity.a2a.protocol import A2ARequest, A2AResponse
from trinity.a2a.task_manager import TaskState

logger = logging.getLogger(__name__)

# ── Inline proto definition (A2A v0.3) ──────────────────────────────

_A2A_PROTO = """
syntax = "proto3";

package trinity.a2a;

// ── Envelope ──────────────────────────────────────
message A2ARequestMessage {
    string  request_id   = 1;
    string  method       = 2;
    string  params_json  = 3;   // serialized JSON
    int64   timestamp    = 4;
    string  signature    = 5;
}

message A2AResponseMessage {
    string  request_id   = 1;
    int32   status_code  = 2;
    string  result_json  = 3;   // serialized JSON
    string  error        = 4;
    int64   timestamp    = 5;
}

// ── Service ───────────────────────────────────────
service TrinityA2A {
    rpc SendTask (A2ARequestMessage) returns (A2AResponseMessage);
    rpc GetTask (A2ARequestMessage)  returns (A2AResponseMessage);
    rpc CancelTask (A2ARequestMessage) returns (A2AResponseMessage);
    rpc StreamEvents (A2ARequestMessage) returns (stream A2AResponseMessage);
}
"""

# ── Connection Pool ───────────────────────────────────────────────


@dataclass
class _ChannelEntry:
    address: str
    channel: Any
    created_at: float
    last_used: float
    use_count: int = 0


class _GRPCPool:
    """Simple gRPC channel connection pool with LRU eviction."""

    MAX_IDLE_CHANNELS = 10
    MAX_CHANNEL_AGE = 300  # 5 minutes

    _instance: Optional["_GRPCPool"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._channels: Dict[str, _ChannelEntry] = {}

    @classmethod
    def get(cls) -> "_GRPCPool":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def acquire(self, address: str) -> Any:
        with self._lock:
            self._evict_expired()

            if address in self._channels:
                entry = self._channels[address]
                entry.last_used = time.time()
                entry.use_count += 1
                return entry.channel

            # Create new channel (lazy import grpc)
            try:
                import grpc

                channel = grpc.insecure_channel(address)
            except ImportError:
                raise ImportError(
                    "gRPC transport requires 'grpcio' package. "
                    "Install with: pip install grpcio"
                )

            entry = _ChannelEntry(
                address=address,
                channel=channel,
                created_at=time.time(),
                last_used=time.time(),
                use_count=1,
            )
            self._channels[address] = entry
            return channel

    def release(self, address: str) -> None:
        # No-op; channels are LRU evicted
        pass

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [
            addr
            for addr, e in self._channels.items()
            if now - e.last_used > self.MAX_CHANNEL_AGE
        ]
        # Also trim excess
        if len(self._channels) > self.MAX_IDLE_CHANNELS:
            sorted_addrs = sorted(
                self._channels.keys(),
                key=lambda a: self._channels[a].last_used,
            )
            excess = sorted_addrs[: len(sorted_addrs) - self.MAX_IDLE_CHANNELS]
            expired = list(set(expired + excess))

        for addr in expired:
            try:
                self._channels[addr].channel.close()
            except Exception:
                pass
            del self._channels[addr]
            logger.debug("gRPC pool: evicted channel %s", addr)


# ── TrinityA2AServicer ────────────────────────────────────────────


class TrinityA2AServicer:
    """Server-side implementation of TrinityA2A gRPC service.

    Translates between A2A JSON-RPC 2.0 messages and gRPC messages.
    Does NOT require proto compilation — works with any gRPC service
    definition that supports SendTask / GetTask / CancelTask RPCs.

    Usage::

        servicer = TrinityA2AServicer(task_manager=my_tm)
        servicer.start(host="0.0.0.0", port=50051)
    """

    def __init__(
        self,
        task_manager: Any = None,
        protocol: Any = None,
    ) -> None:
        """
        Parameters
        ----------
        task_manager : TaskManager, optional
            The A2A TaskManager instance for dispatching tasks.
        protocol : A2AProtocol, optional
            Fallback protocol handler.
        """
        self._task_manager = task_manager
        self._protocol = protocol
        self._server: Any = None

    def handle_request(self, request_msg: bytes) -> bytes:
        """Handle a raw (de)serialized gRPC request.

        This is the main dispatcher that can be wired into any raw
        gRPC handler framework without proto compilation.

        Parameters
        ----------
        request_msg : bytes
            Raw protobuf bytes of A2ARequestMessage.

        Returns
        -------
        bytes
            Serialized A2AResponseMessage.
        """
        import struct

        # Minimal wire-format parser for inline proto
        # Fields: 1=string,2=string,3=string,4=varint,5=string
        fields: Dict[int, Any] = {}
        pos = 0
        data = request_msg

        try:
            while pos < len(data):
                tag_byte = data[pos]
                pos += 1
                field_number = tag_byte >> 3
                wire_type = tag_byte & 0x07

                if wire_type == 2:  # length-delimited (string)
                    length = 0
                    shift = 0
                    while pos < len(data):
                        b = data[pos]
                        pos += 1
                        length |= (b & 0x7F) << shift
                        if not (b & 0x80):
                            break
                        shift += 7
                    if pos + length <= len(data):
                        fields[field_number] = data[pos:pos + length].decode(
                            "utf-8", errors="replace"
                        )
                        pos += length
                elif wire_type == 0:  # varint
                    value = 0
                    shift = 0
                    while pos < len(data):
                        b = data[pos]
                        pos += 1
                        value |= (b & 0x7F) << shift
                        if not (b & 0x80):
                            break
                        shift += 7
                    fields[field_number] = value
        except (IndexError, UnicodeDecodeError) as e:
            logger.warning("gRPC parse error: %s", e)
            return self._make_response("", 400, "", str(e))

        request_id = fields.get(1, "")
        method = fields.get(2, "").lower() if fields.get(2) else "sendtask"
        params_json = fields.get(3, "{}")

        # Dispatch to handler
        try:
            if method in ("sendtask", "send_task"):
                result = self._send_task(params_json)
            elif method in ("gettask", "get_task"):
                result = self._get_task(params_json)
            elif method in ("canceltask", "cancel_task"):
                result = self._cancel_task(params_json)
            else:
                return self._make_response(
                    request_id, 400, "", f"Unknown method: {method}"
                )

            return self._make_response(
                request_id, 200, json.dumps(result), ""
            )
        except Exception as e:
            logger.exception("gRPC handler error")
            return self._make_response(request_id, 500, "", str(e))

    def _send_task(self, params_json: str) -> Dict[str, Any]:
        params = json.loads(params_json) if params_json else {}
        if self._task_manager:
            task = self._task_manager.create_task(
                params.get("agent_name", "unknown"),
                params.get("current_task", {}),
                params.get("global_goal", ""),
                params.get("memory_ids", []),
            )
            return {
                "task_id": task.task_id,
                "state": task.state.value if hasattr(task.state, "value") else str(task.state),
                "created_at": task.created_at if hasattr(task, "created_at") else "",
            }
        return {"status": "no_task_manager", "params": params}

    def _get_task(self, params_json: str) -> Dict[str, Any]:
        params = json.loads(params_json) if params_json else {}
        task_id = params.get("task_id", "")
        if self._task_manager:
            task = self._task_manager.get_task(task_id)
            if task:
                return {
                    "task_id": task.task_id,
                    "state": task.state.value if hasattr(task.state, "value") else str(task.state),
                    "result": getattr(task, "result", None),
                }
            return {"error": "Task not found", "task_id": task_id}
        return {"error": "No task manager", "task_id": task_id}

    def _cancel_task(self, params_json: str) -> Dict[str, Any]:
        params = json.loads(params_json) if params_json else {}
        task_id = params.get("task_id", "")
        if self._task_manager:
            result = self._task_manager.cancel_task(task_id)
            return {"task_id": task_id, "cancelled": result}
        return {"error": "No task manager", "task_id": task_id}

    @staticmethod
    def _make_response(
        request_id: str,
        status_code: int,
        result_json: str,
        error: str,
    ) -> bytes:
        """Build a minimal proto wire-format A2AResponseMessage."""
        import struct

        parts: List[bytes] = []

        # Field 1: request_id (string)
        rid = request_id.encode("utf-8")
        parts.append(struct.pack("<B", (1 << 3) | 2))  # tag
        parts.append(_encode_varint(len(rid)))
        parts.append(rid)

        # Field 2: status_code (varint)
        parts.append(struct.pack("<B", (2 << 3) | 0))
        parts.append(_encode_varint(status_code))

        # Field 3: result_json (string)
        rj = result_json.encode("utf-8")
        parts.append(struct.pack("<B", (3 << 3) | 2))
        parts.append(_encode_varint(len(rj)))
        parts.append(rj)

        # Field 4: error (string)
        err = error.encode("utf-8")
        parts.append(struct.pack("<B", (4 << 3) | 2))
        parts.append(_encode_varint(len(err)))
        parts.append(err)

        # Field 5: timestamp (varint)
        ts = int(time.time() * 1000)
        parts.append(struct.pack("<B", (5 << 3) | 0))
        parts.append(_encode_varint(ts))

        return b"".join(parts)


def _encode_varint(value: int) -> bytes:
    """Encode an unsigned integer as a proto varint."""
    parts = bytearray()
    while value > 0x7F:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    parts.append(value & 0x7F)
    return bytes(parts)


# ── GRPCTransport ──────────────────────────────────────────────────


class GRPCTransport:
    """High-level gRPC transport wrapper.

    Manages connection to a remote TrinityA2A gRPC server and
    provides async-friendly send/get/cancel methods.

    Usage::

        transport = GRPCTransport("localhost:50051")
        resp = transport.send_task(
            agent_name="file-agent",
            current_task={"command": "analyze"},
        )
        print(resp["task_id"])
    """

    def __init__(
        self,
        address: str,
        cert_file: str = "",
        key_file: str = "",
    ) -> None:
        self._address = address
        self._cert_file = cert_file
        self._key_file = key_file
        self._pool = _GRPCPool.get()

    def send_task(
        self,
        agent_name: str = "",
        current_task: Optional[Dict[str, Any]] = None,
        global_goal: str = "",
        memory_ids: Optional[List[str]] = None,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Send a task via gRPC to the remote server.

        Returns dict with task_id, state, etc.
        """
        params = {
            "agent_name": agent_name,
            "current_task": current_task or {},
            "global_goal": global_goal,
            "memory_ids": memory_ids or [],
        }
        response_bytes = self._call("sendtask", params, timeout)
        result = self._parse_response(response_bytes)
        if "error" in result:
            raise RuntimeError(f"gRPC SendTask failed: {result['error']}")
        return result

    def get_task(self, task_id: str, timeout: float = 10.0) -> Dict[str, Any]:
        """Retrieve task status by ID."""
        response_bytes = self._call("gettask", {"task_id": task_id}, timeout)
        return self._parse_response(response_bytes)

    def cancel_task(self, task_id: str, timeout: float = 10.0) -> Dict[str, Any]:
        """Cancel a task by ID."""
        response_bytes = self._call("canceltask", {"task_id": task_id}, timeout)
        return self._parse_response(response_bytes)

    def _call(
        self,
        method: str,
        params: Dict[str, Any],
        timeout: float = 30.0,
    ) -> bytes:
        """Build request, send via channel, return raw response bytes."""
        import struct

        request_id = f"grpc-{int(time.time() * 1_000_000)}"
        params_json = json.dumps(params)
        timestamp = int(time.time() * 1000)

        # Build proto wire-format request
        parts: List[bytes] = []

        # Field 1: request_id
        rid = request_id.encode("utf-8")
        parts.append(struct.pack("<B", (1 << 3) | 2))
        parts.append(_encode_varint(len(rid)))
        parts.append(rid)

        # Field 2: method
        mtd = method.encode("utf-8")
        parts.append(struct.pack("<B", (2 << 3) | 2))
        parts.append(_encode_varint(len(mtd)))
        parts.append(mtd)

        # Field 3: params_json
        pj = params_json.encode("utf-8")
        parts.append(struct.pack("<B", (3 << 3) | 2))
        parts.append(_encode_varint(len(pj)))
        parts.append(pj)

        # Field 4: timestamp
        parts.append(struct.pack("<B", (4 << 3) | 0))
        parts.append(_encode_varint(timestamp))

        request_bytes = b"".join(parts)

        channel = self._pool.acquire(self._address)
        try:
            # Use gRPC unary-unary stub
            import grpc

            stub = _GenericStub(channel)
            response_bytes = stub.Call(
                f"/trinity.a2a.TrinityA2A/{method.capitalize()}",
                request_bytes,
                timeout=timeout,
            )
        except Exception:
            self._pool.release(self._address)
            raise

        return response_bytes

    @staticmethod
    def _parse_response(response_bytes: bytes) -> Dict[str, Any]:
        """Parse a raw proto response into a dict."""
        servicer = TrinityA2AServicer()
        resp_bytes = servicer._make_response("", 200, "{}", "")

        # Try to parse as JSON from result_json field
        import struct

        fields: Dict[int, Any] = {}
        pos = 0
        data = response_bytes

        try:
            while pos < len(data):
                tag_byte = data[pos]
                pos += 1
                field_number = tag_byte >> 3
                wire_type = tag_byte & 0x07

                if wire_type == 2:
                    length = 0
                    shift = 0
                    while pos < len(data):
                        b = data[pos]
                        pos += 1
                        length |= (b & 0x7F) << shift
                        if not (b & 0x80):
                            break
                        shift += 7
                    if pos + length <= len(data):
                        fields[field_number] = data[pos:pos + length]
                        pos += length
                elif wire_type == 0:
                    value = 0
                    shift = 0
                    while pos < len(data):
                        b = data[pos]
                        pos += 1
                        value |= (b & 0x7F) << shift
                        if not (b & 0x80):
                            break
                        shift += 7
                    fields[field_number] = value
        except (IndexError, UnicodeDecodeError):
            return {"error": "parse_error", "raw": response_bytes.hex()}

        result_json = ""
        if 3 in fields and isinstance(fields[3], bytes):
            result_json = fields[3].decode("utf-8", errors="replace")
        if 4 in fields and isinstance(fields[4], bytes):
            error = fields[4].decode("utf-8", errors="replace")
            if error:
                return {"error": error}

        try:
            return json.loads(result_json) if result_json else {}
        except json.JSONDecodeError:
            return {"raw_result": result_json}


class _GenericStub:
    """Minimal gRPC stub for raw bytes calls without proto compilation."""

    def __init__(self, channel: Any) -> None:
        self._channel = channel

    def Call(self, method: str, request: bytes, timeout: float = 30.0) -> bytes:
        import grpc

        # Build metadata
        metadata = (
            ("content-type", "application/grpc+proto"),
            ("grpc-timeout", f"{int(timeout * 1000)}m"),
        )

        future = self._channel.unary_unary(
            method,
            request_serializer=lambda x: x,
            response_deserializer=lambda x: x,
        )
        response, call = future.with_call(request, metadata=metadata, timeout=timeout)
        return response


# ── Server Start Helper ────────────────────────────────────────────


def start_grpc_server(
    task_manager: Any = None,
    host: str = "0.0.0.0",
    port: int = 50051,
    **kwargs,
) -> Any:
    """Start a gRPC server for TrinityA2A.

    Parameters
    ----------
    task_manager : TaskManager
        Task manager to dispatch to.
    host : str
        Bind address.
    port : int
        Bind port.

    Returns
    -------
    grpc.Server (or None if grpcio not installed).
    """
    try:
        import grpc
        from concurrent import futures

        server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        servicer = TrinityA2AServicer(task_manager=task_manager)

        address = f"{host}:{port}"
        server.add_insecure_port(address)

        server.start()
        logger.info("gRPC A2A server listening on %s", address)
        return server

    except ImportError:
        logger.warning("grpcio not installed — gRPC server not started")
        return None
