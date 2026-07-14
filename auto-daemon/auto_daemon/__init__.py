"""
auto-daemon — 50-tier Guardian Chain for LLM Safety

A standalone runtime safety middleware for LLM agents and memory systems.
Provides 50 layers of defense from input filtering through reasoning drift detection.

Quick Start:
    from auto_daemon import GuardianChain
    
    guard = GuardianChain()
    result = guard.check("user input", context={"role": "user"})
    if result["proceed"]:
        print("Safe to proceed")
    else:
        print(f"Blocked: {result['blocks']}")

Installation:
    pip install auto-daemon
"""

from auto_daemon.engine import GuardianChain, GuardianConfig
from auto_daemon.integrations import LangChainGuard, OpenAIGuard

__version__ = "1.0.0"
__all__ = ["GuardianChain", "GuardianConfig", "LangChainGuard", "OpenAIGuard"]
