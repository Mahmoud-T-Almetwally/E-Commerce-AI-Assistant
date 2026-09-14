"""
Tool infrastructure: registration, result envelopes, retry policy helpers.

Tools are plain functions over SessionLocal that return a uniform JSON
envelope (or raise domain exceptions, which the tool node maps to envelopes):

    success: {"status": "success", "data": {...}, "ui_event": {...}?}
    failure: {"status": "error",
              "error": {"code", "message", "retryable", "hint"}}

The @agent_tool decorator:
  * turns the function into a LangChain StructuredTool (schema inferred from
    signature + docstring) for llm.bind_tools();
  * hides the ``user_id`` parameter from the model (InjectedToolArg) while the
    graph's tool node injects the *authenticated* user's id at call time —
    the model can never forge identity;
  * gives every tool a model-visible ``retries`` parameter (resolved at
    runtime from config.agent.max_tool_retries when omitted, so it
    hot-reloads).

Execution policy (attempts, backoff, confirmation interrupts, event
emission) lives in agent.graph.execute_tools — tools stay pure.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from langchain_core.tools import InjectedToolArg
from langchain_core.tools import tool as langchain_tool

from utils.config import config
from utils.exceptions import (
    OutOfStockError,
    RAGError,
    RecordNotFoundError,
)

logger = logging.getLogger(__name__)

#: Codes the model may see. Membership in RETRYABLE_CODES controls whether the
#: tool node may automatically re-run the *same* call: transient failures yes,
#: user refusals and argument errors never.
RETRYABLE_CODES = frozenset({"internal_error", "rag_unavailable"})

KNOWN_CODES = frozenset({
    "user_declined", "declined_earlier", "out_of_stock", "not_found",
    "invalid_arguments", "cart_empty", "rag_unavailable",
    "internal_error", "unknown_tool",
})


def success(data: Any = None, ui_event: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a success envelope. `ui_event` is forwarded to the UI by the tool node."""
    return {"status": "success", "data": data, "ui_event": ui_event}


def error(code: str, message: str, *, retryable: Optional[bool] = None,
          hint: Optional[str] = None) -> Dict[str, Any]:
    """Build an error envelope the model can act on (see KNOWN_CODES)."""
    if code not in KNOWN_CODES:
        code = "internal_error"
    if retryable is None:
        retryable = code in RETRYABLE_CODES
    return {
        "status": "error",
        "error": {"code": code, "message": message,
                  "retryable": retryable, "hint": hint or ""},
    }


@dataclass(frozen=True)
class ToolSpec:
    """Everything the graph needs to know about a registered tool."""
    tool: Any                                  # StructuredTool (schema for the model)
    fn: Callable[..., Dict[str, Any]]          # the raw function
    name: str
    sensitive: bool
    needs_user_id: bool
    confirmation: Optional[Callable[[Dict[str, Any], int], str]]


TOOL_REGISTRY: Dict[str, ToolSpec] = {}


def agent_tool(*, sensitive: bool = False,
               confirmation: Optional[Callable[[Dict[str, Any], int], str]] = None):
    """
    Register a tool. `sensitive` marks tools requiring user confirmation.
    `confirmation(args, user_id)` renders the human-facing prompt for the
    confirmation dialog; falls back to a generic prompt on failure.
    """
    def decorator(fn):
        name = fn.__name__
        if name in TOOL_REGISTRY:
            raise RuntimeError(f"Duplicate agent tool registration: {name}")
        needs_user_id = "user_id" in inspect.signature(fn).parameters
        structured = langchain_tool(name)(fn)
        TOOL_REGISTRY[name] = ToolSpec(
            tool=structured, fn=fn, name=name, sensitive=sensitive,
            needs_user_id=needs_user_id, confirmation=confirmation,
        )
        logger.debug("Registered agent tool '%s' (sensitive=%s).", name, sensitive)
        return structured
    return decorator


def resolve_retries(raw_args: Optional[Dict[str, Any]]) -> int:
    """Per-call retry budget: model-supplied `retries`, else config default, clamped 0..5."""
    value = (raw_args or {}).get("retries")
    if value is None:
        value = int(config.agent.max_tool_retries)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = int(config.agent.max_tool_retries)
    return max(0, min(value, 5))


def call_fingerprint(tool_name: str, raw_args: Optional[Dict[str, Any]]) -> str:
    """Stable hash of (tool, business args) — detects repeated identical calls."""
    payload = {k: v for k, v in (raw_args or {}).items() if k != "retries"}
    blob = json.dumps({"tool": tool_name, "args": payload}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def classify_exception(exc: Exception) -> Dict[str, Any]:
    """Map a raised exception to the error envelope the model should see."""
    if isinstance(exc, OutOfStockError):
        return error("out_of_stock", str(exc), retryable=False,
                     hint="The requested quantity exceeds stock; re-call with a lower "
                          "quantity or suggest alternatives.")
    if isinstance(exc, RecordNotFoundError):
        return error("not_found", str(exc), retryable=False,
                     hint="The identifier is wrong; call search_products / view_cart "
                          "first to discover valid IDs.")
    if isinstance(exc, (TypeError, ValueError)):
        # pydantic ValidationError subclasses ValueError
        return error("invalid_arguments", f"Invalid arguments: {exc}", retryable=False,
                     hint="Fix the argument values/types and call again.")
    if isinstance(exc, RAGError):
        return error("rag_unavailable", f"Knowledge base failure: {exc}", retryable=True,
                     hint="The knowledge base may be transiently down; retry once, or "
                          "answer without it and say you cannot verify store specifics.")
    return error("internal_error", f"Unexpected tool failure: {exc}", retryable=True,
                 hint="A transient error occurred; you may retry the same call.")


def get_event_writer() -> Callable[[Dict[str, Any]], None]:
    """
    Safe accessor for langgraph's custom-stream writer. Nodes/tools push UI
    events (tool status, carousel, notes) through it without knowing about
    Socket.IO. Outside a streaming run it degrades to a no-op, so the graph
    stays runnable headless (tests, future batch jobs).
    """
    try:
        from langgraph.config import get_stream_writer
        writer = get_stream_writer()
    except Exception:
        writer = None

    if not callable(writer):
        return lambda payload: None

    def _safe_emit(payload: Dict[str, Any]) -> None:
        try:
            writer(payload)
        except Exception:
            logger.debug("Failed to emit graph event: %r", payload, exc_info=True)

    return _safe_emit