"""
Trinity A2A Transports — Pluggable transport layer implementations.

Supports three transport backends:
  - REST  (default): HTTP/JSON-RPC 2.0 via A2AProtocol
  - gRPC:           Binary protocol with bidirectional streaming
  - SSE:            Server-Sent Events for real-time task progress

Usage::

    from trinity.a2a import A2AProtocol, TaskManager
    from trinity.a2a.transports import set_transport

    protocol = A2AProtocol()
    set_transport(protocol, "grpc", host="0.0.0.0", port=50051)

    With SSE:
    set_transport(protocol, "sse", host="0.0.0.0", port=8080)
"""

from trinity.a2a.transports.grpc_transport import (
    TrinityA2AServicer,
    GRPCTransport,
    start_grpc_server,
)
from trinity.a2a.transports.sse_transport import (
    SSETransport,
    TaskEvent,
    start_sse_server,
)


def set_transport(
    protocol: "A2AProtocol",          # noqa: F821
    transport_type: str,
    **kwargs,
) -> None:
    """Switch the transport layer for an A2AProtocol instance.

    Parameters
    ----------
    protocol : A2AProtocol
        The protocol instance whose transport to switch.
    transport_type : str
        One of 'rest' (default), 'grpc', or 'sse'.
    **kwargs
        Transport-specific options (host, port, cert_file, etc.).
    """
    from trinity.a2a.protocol import A2AProtocol as _AP
    if isinstance(protocol, _AP):
        protocol._transport_type = transport_type
        protocol._transport_config = kwargs


__all__ = [
    "TrinityA2AServicer",
    "GRPCTransport",
    "start_grpc_server",
    "SSETransport",
    "TaskEvent",
    "start_sse_server",
    "set_transport",
]
