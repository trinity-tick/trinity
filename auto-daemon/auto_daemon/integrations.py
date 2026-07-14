"""
auto-daemon integrations — LangChain and OpenAI adapters.

Provides guard wrappers that can be used as middleware or toolkits
for popular LLM frameworks.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from auto_daemon.engine import GuardianChain, GuardianConfig, TIER_REGISTRY

logger = logging.getLogger("auto_daemon.integrations")


# ─── LangChain Guard ──────────────────────────────────────────────────────

class LangChainGuard:
    """LangChain-compatible guard middleware.

    Can be used as a callback or tool wrapper.
    
    Usage:
        from auto_daemon import LangChainGuard
        
        guard = LangChainGuard()
        
        # As a tool wrapper:
        from langchain.tools import tool
        @tool
        def my_tool(query: str) -> str:
            guard.check_input(query)
            return "result"
        
        # As middleware (via callbacks):
        from langchain.callbacks.base import BaseCallbackHandler
        handler = guard.as_callback_handler()
    """

    def __init__(self, config: Optional[GuardianConfig] = None, max_tiers: int = 20):
        self.guardian = GuardianChain(config=config)
        self.max_tiers = max_tiers

    def check_input(self, content: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Check user/LM input before passing to the next stage.

        Args:
            content: Input text to check.
            context: Optional context (role, session_id, etc.).

        Returns:
            Dict with proceed flag and check results.

        Raises:
            GuardBlockedError: If the check fails and raise_on_block is True.
        """
        ctx = context or {"role": "user"}
        result = self.guardian.check(content, ctx, max_tiers=self.max_tiers)
        return result.to_dict()

    def check_output(self, content: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Check LLM output before sending to user.

        Args:
            content: Output text to check.
            context: Optional context.

        Returns:
            Dict with proceed flag.
        """
        ctx = context or {"role": "assistant"}
        result = self.guardian.check(content, ctx, max_tiers=self.max_tiers)
        return result.to_dict()

    def as_callback_handler(self):
        """Return a LangChain BaseCallbackHandler.

        Requires langchain installed.
        """
        try:
            from langchain.callbacks.base import BaseCallbackHandler
        except ImportError:
            raise ImportError("langchain not installed. Run: pip install langchain")

        class _GuardHandler(BaseCallbackHandler):
            def __init__(self, guard: LangChainGuard):
                self.guard = guard

            def on_llm_start(self, serialized, prompts, **kwargs):
                for prompt in prompts:
                    result = self.guard.check_input(prompt)
                    if not result["proceed"]:
                        logger.warning(
                            "Guard blocked LLM input: %s",
                            result.get("blocks", [])
                        )

            def on_llm_end(self, response, **kwargs):
                for gen in response.generations:
                    for g in gen:
                        result = self.guard.check_output(g.text)
                        if not result["proceed"]:
                            logger.warning(
                                "Guard blocked LLM output: %s",
                                result.get("blocks", [])
                            )

        return _GuardHandler(self)

    def wrap_tool(self, tool_fn: Callable) -> Callable:
        """Wrap a tool function with input/output guard checking."""
        def _wrapped(*args, **kwargs):
            input_str = str(args) + str(kwargs)
            in_result = self.check_input(input_str)
            if not in_result["proceed"]:
                raise GuardBlockedError(f"Input blocked: {in_result['blocks']}")
            
            output = tool_fn(*args, **kwargs)
            
            out_result = self.check_output(str(output))
            if not out_result["proceed"]:
                raise GuardBlockedError(f"Output blocked: {out_result['blocks']}")
            
            return output
        
        _wrapped.__name__ = tool_fn.__name__
        _wrapped.__doc__ = tool_fn.__doc__
        return _wrapped


# ─── OpenAI Guard ─────────────────────────────────────────────────────────

class OpenAIGuard:
    """OpenAI API-compatible guard middleware.

    Can wrap OpenAI API calls to check inputs and outputs.

    Usage:
        from auto_daemon import OpenAIGuard
        from openai import OpenAI
        
        guard = OpenAIGuard()
        client = guard.wrap_client(OpenAI())
        
        # All API calls are now guarded
        response = client.chat.completions.create(...)
    """

    def __init__(self, config: Optional[GuardianConfig] = None, max_tiers: int = 20):
        self.guardian = GuardianChain(config=config)
        self.max_tiers = max_tiers

    def check_messages(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """Check chat messages for safety before sending to API."""
        all_content = " ".join(m.get("content", "") for m in messages)
        result = self.guardian.check(all_content, {"role": "composite"}, max_tiers=self.max_tiers)
        return result.to_dict()

    def check_response(self, response_text: str) -> Dict[str, Any]:
        """Check API response before returning to user."""
        result = self.guardian.check(response_text, {"role": "assistant"}, max_tiers=self.max_tiers)
        return result.to_dict()

    def wrap_client(self, client: Any) -> Any:
        """Wrap an OpenAI client with guard checks.

        Currently wraps client.chat.completions.create.
        """
        _orig_create = client.chat.completions.create

        def _guarded_create(*args, **kwargs):
            messages = kwargs.get("messages", [])
            
            # Check input
            in_result = self.check_messages(messages)
            if not in_result["proceed"]:
                raise GuardBlockedError(f"Input blocked: {in_result['blocks']}")
            
            # Call original
            response = _orig_create(*args, **kwargs)
            
            # Check output
            if hasattr(response, "choices"):
                for choice in response.choices:
                    if hasattr(choice, "message") and hasattr(choice.message, "content"):
                        out_result = self.check_response(choice.message.content)
                        if not out_result["proceed"]:
                            raise GuardBlockedError(f"Output blocked: {out_result['blocks']}")
            
            return response

        client.chat.completions.create = _guarded_create
        return client


class GuardBlockedError(Exception):
    """Raised when a guard tier blocks execution."""
    pass


# ─── FastAPI Middleware ───────────────────────────────────────────────────

class FastAPIGuard:
    """FastAPI middleware for request/response guarding.

    Usage:
        from auto_daemon import FastAPIGuard
        from fastapi import FastAPI
        
        app = FastAPI()
        guard = FastAPIGuard(app)
    """

    def __init__(self, app: Any = None, config: Optional[GuardianConfig] = None):
        self.guardian = GuardianChain(config=config)

    async def check_request(self, body: bytes) -> Dict[str, Any]:
        """Check incoming request body."""
        content = body.decode("utf-8", errors="replace")
        result = self.guardian.check(content, {"role": "request"}, max_tiers=20)
        return result.to_dict()

    async def check_response(self, body: bytes) -> Dict[str, Any]:
        """Check outgoing response body."""
        content = body.decode("utf-8", errors="replace")
        result = self.guardian.check(content, {"role": "response"}, max_tiers=20)
        return result.to_dict()
