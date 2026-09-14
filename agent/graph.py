"""
LangGraph topology for the e-commerce assistant.

    START → guard → classify_intent → (customer_service → retrieve_knowledge) → agent
            ↘ END (refusal)                                              ↕ execute_tools (loop)

`execute_tools` runs ONE tool call per super-step (pending calls are derived
from unanswered tool_call ids). This is what makes confirmation interrupts
safe: on resume the node re-runs from its start, and re-rolling a single
pending call is side-effect free, whereas re-rolling a batch could replay
mutations that already executed before the pause.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent.checkpointer import get_checkpointer
from agent.prompts import (
    GUARD_SYSTEM_PROMPT,
    INTENT_SYSTEM_PROMPT,
    REFUSAL_MESSAGE,
    build_system_prompt,
)
from agent.state import INTENTS, AgentState, GuardVerdict, IntentClassification
from agent.tooling import (
    TOOL_REGISTRY,
    call_fingerprint,
    classify_exception,
    error,
    get_event_writer,
    resolve_retries,
)
from agent.tools import get_toolset
from agent.providers import ModelFactory
from utils.config import config

logger = logging.getLogger(__name__)

_CLASSIFIER_CONTEXT_MESSAGES = 8
_PER_MESSAGE_CHARS = 300


# Message helpers

def message_text(message: Any) -> str:
    """Plain-text view of a message content (handles multimodal blocks)."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return str(content) if content else ""


def _last_message(state: Dict[str, Any], msg_type: str) -> Optional[Any]:
    for m in reversed(state.get("messages") or []):
        if getattr(m, "type", None) == msg_type:
            return m
    return None


def _transcript(state: Dict[str, Any]) -> str:
    lines = []
    for m in (state.get("messages") or [])[-_CLASSIFIER_CONTEXT_MESSAGES:]:
        if m.type == "human":
            lines.append(f"customer: {message_text(m)[:_PER_MESSAGE_CHARS]}")
        elif m.type == "ai":
            lines.append(f"assistant: {message_text(m)[:_PER_MESSAGE_CHARS]}")
    return "\n".join(lines)


def _to_dict(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return result
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    return {}


# Guard: heuristics first, LLM classifier second, fail-open on errors

_INJECTION_PATTERNS = (
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?|messages?)",
    r"disregard\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)",
    r"(?:reveal|show|print|repeat|display|output|leak)\s+(?:your|the)\s+(?:system|initial|original|hidden|secret)\s+(?:prompt|instructions?|rules?)",
    r"summar(?:ize|ise)\s+(?:your|the)\s+(?:system|initial)\s+(?:prompt|instructions)",
    r"(?:developer|god|dan)\s*mode",
    r"\bdo\s+anything\s+now\b",
    r"jailbreak",
    r"(?:pretend|act)\s+(?:that\s+)?(?:you\s+)?(?:are|have)\s+no\s+(?:restrictions?|guidelines?|filters?|rules?)",
    r"unfiltered\s+(?:mode|assistant|ai|model)",
    r"system\s*prompt\s*:",
    r"<\|(?:im_start|im_end|system|endoftext)\|>",
    r"repeat\s+(?:the\s+)?(?:text|words|everything)\s+(?:above|before)",
    r"encode\s+this\s+(?:in|as)\s+base64",
)
_INJECTION_REGEXES = tuple(re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS)
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/=]{400,}")


def _heuristic_injection(text: str) -> Optional[str]:
    if not text:
        return None
    for pattern in _INJECTION_REGEXES:
        if pattern.search(text):
            return pattern.pattern
    if _BASE64_BLOB.search(text):
        return "suspicious base64 payload"
    return None


def _guard_input(state: Dict[str, Any]) -> str:
    latest = _last_message(state, "human")
    text = message_text(latest) if latest is not None else ""
    if not text:
        text = "(message contains no text — image attachment)"
    if config.agent.guard_include_context:
        transcript = _transcript(state)
        if transcript:
            return "Recent conversation:\n" + transcript + "\n\nMessage to inspect:\n" + text
    return "Message to inspect:\n" + text


def _make_guard_node(llm):
    def guard_node(state: Dict[str, Any]) -> Dict[str, Any]:
        writer = get_event_writer()
        if not config.agent.guard_enabled:      # hot-reloadable toggle
            return {"guard_verdict": None}      # (also clears stale verdicts)

        writer({"type": "phase", "phase": "safety_check"})
        text = message_text(_last_message(state, "human") or "")

        verdict: Dict[str, Any]
        pattern = _heuristic_injection(text)
        if pattern:
            verdict = {"blocked": True, "reason": f"matched heuristic: {pattern}",
                       "confidence": 1.0, "method": "heuristic"}
        else:
            try:
                result = llm.with_structured_output(GuardVerdict).invoke([
                    SystemMessage(content=GUARD_SYSTEM_PROMPT),
                    HumanMessage(content=_guard_input(state)),
                ])
                data = _to_dict(result)
                verdict = {"blocked": bool(data.get("blocked")),
                           "reason": str(data.get("reason") or "")[:300],
                           "confidence": float(data.get("confidence") or 0.0),
                           "method": "llm"}
            except Exception as exc:
                # Fail-open by design: availability beats blocking
                logger.warning("Guard classifier failed (fail-open): %s", exc)
                verdict = {"blocked": False, "reason": f"classifier unavailable: {exc}",
                           "confidence": 0.0, "method": "fail_open"}

        if verdict["blocked"]:
            writer({"type": "guard_blocked", "reason": verdict["reason"]})
            logger.info("Guard blocked a message (method=%s).", verdict["method"])
            return {"messages": [AIMessage(content=REFUSAL_MESSAGE)],
                    "guard_verdict": verdict}
        return {"guard_verdict": verdict}
    return guard_node


# Intent classification

def _classify_input(state: Dict[str, Any]) -> str:
    transcript = _transcript(state)
    latest = _last_message(state, "human")
    text = message_text(latest) if latest is not None else ""
    if transcript:
        return ("Conversation so far:\n" + transcript +
                "\n\nLatest customer message to classify:\n" + text)
    return "Customer message to classify:\n" + text


def _make_classify_node(llm):
    def classify_node(state: Dict[str, Any]) -> Dict[str, Any]:
        writer = get_event_writer()
        writer({"type": "phase", "phase": "classifying"})
        try:
            result = llm.with_structured_output(IntentClassification).invoke([
                SystemMessage(content=INTENT_SYSTEM_PROMPT),
                HumanMessage(content=_classify_input(state)),
            ])
            data = _to_dict(result)
            intent = data.get("intent")
            if intent not in INTENTS:
                intent = None
            logger.debug("Intent classified: %r", intent)
            return {"intent": intent}
        except Exception as exc:
            # Fallback = agent with the full toolset (safe superset).
            logger.warning("Intent classification failed; falling back to full toolset: %s", exc)
            return {"intent": None}
    return classify_node


# RAG retrieval 

def retrieve_knowledge_node(state: Dict[str, Any]) -> Dict[str, Any]:
    writer = get_event_writer()
    writer({"type": "phase", "phase": "retrieving"})
    query = message_text(_last_message(state, "human") or "").strip()
    if not query:
        return {"rag_context": None, "rag_unavailable": False}
    try:
        from database.rag_manager import get_rag_manager  # lazy import (acyclic)
        docs = get_rag_manager().search(query, k=4)
    except Exception as exc:
        logger.warning("RAG retrieval failed: %s", exc)
        return {"rag_context": None, "rag_unavailable": True}

    if not docs:
        return {"rag_context": None, "rag_unavailable": False}

    blocks = []
    for i, doc in enumerate(docs, 1):
        title = (doc.metadata or {}).get("title", "untitled")
        doc_type = (doc.metadata or {}).get("doc_type", "")
        blocks.append(f"[{i}] {title} ({doc_type}):\n{(doc.page_content or '')[:800]}")
    return {"rag_context": "\n\n".join(blocks), "rag_unavailable": False}


# Agent node

def _make_agent_node(llm):
    def agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
        writer = get_event_writer()
        writer({"type": "phase", "phase": "thinking"})

        specs = get_toolset(state.get("intent"))
        system = build_system_prompt(state)
        messages = [SystemMessage(content=system)] + list(state.get("messages") or [])

        if specs:
            response = llm.bind_tools([s.tool for s in specs]).invoke(messages)
        else:
            response = llm.invoke(messages)

        # The interim-text feature: content produced alongside tool calls is
        # surfaced live ("I'll add that to your cart now.") and is NOT part
        # of the final answer.
        note = message_text(response).strip()
        if note and getattr(response, "tool_calls", None):
            writer({"type": "agent_note", "content": note})
        return {"messages": [response]}
    return agent_node


# Tool execution node: one call per super-step, confirmation + retries

def _last_human_anchor(state: Dict[str, Any]) -> str:
    """Identity of the latest user message — scopes declined-call blocks to one turn."""
    m = _last_message(state, "human")
    if m is None:
        return "no-user-message"
    if getattr(m, "id", None):
        return str(m.id)
    digest = hashlib.sha256(message_text(m).encode("utf-8")).hexdigest()[:16]
    return f"text:{digest}"


def _public_args(raw_args: Dict[str, Any]) -> Dict[str, Any]:
    safe = {k: v for k, v in (raw_args or {}).items() if k not in ("retries", "user_id")}
    try:
        return json.loads(json.dumps(safe, default=str))
    except Exception:
        return {"repr": str(safe)}


def _unanswered_call(state: Dict[str, Any]):
    """First tool call of the last AIMessage that has no ToolMessage yet."""
    last_ai = _last_message(state, "ai")
    calls = list(getattr(last_ai, "tool_calls", None) or []) if last_ai is not None else []
    if not calls:
        return None
    answered = {m.tool_call_id for m in (state.get("messages") or [])
                if getattr(m, "type", "") == "tool"}
    return next((c for c in calls if c.get("id") and c["id"] not in answered), None)


def execute_tools(state: Dict[str, Any]) -> Dict[str, Any]:
    writer = get_event_writer()
    user_id = int(state.get("user_id") or 0)

    call = _unanswered_call(state)
    if call is None:
        return {}                      # routing sends us back to the agent anyway

    name = str(call.get("name") or "")
    raw_args = dict(call.get("args") or {})
    call_id = str(call["id"])

    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        available = ", ".join(sorted(TOOL_REGISTRY))
        envelope = error("unknown_tool",
                         f"Tool '{name}' does not exist. Available tools: {available}.",
                         retryable=False, hint="Only call the documented tools.")
        return {"messages": [ToolMessage(content=json.dumps(envelope),
                                         tool_call_id=call_id, name=name)]}

    retries = resolve_retries(raw_args)
    fingerprint = call_fingerprint(name, raw_args)
    declined = list(state.get("declined_calls") or [])
    anchor = _last_human_anchor(state)

    # ---- confirmation gate -------
    if spec.sensitive and name in config.agent.sensitive_tool_names:
        prior = next((e for e in declined
                      if e.get("fingerprint") == fingerprint and e.get("anchor") == anchor),
                     None)
        if prior is not None:
            # The model re-issued an exact call the user refused this turn.
            envelope = error(
                "declined_earlier",
                "The user already declined this exact action during the current turn.",
                retryable=False,
                hint="Do not issue the same call again this turn. Ask the user what "
                     "they would like instead.")
            writer({"type": "tool_status", "tool": name, "state": "blocked"})
            return {"messages": [ToolMessage(content=json.dumps(envelope),
                                             tool_call_id=call_id, name=name)]}

        message = None
        if spec.confirmation is not None:
            try:
                message = spec.confirmation(raw_args, user_id)
            except Exception:
                logger.warning("Confirmation renderer failed for %s; generic prompt used.",
                               name, exc_info=True)
        if not message:
            message = (f"Allow the assistant to run '{name}' with "
                       f"{json.dumps(_public_args(raw_args))}?")

        writer({"type": "tool_status", "tool": name, "state": "awaiting_confirmation"})
        approved = interrupt({
            "request_id": str(uuid.uuid4()),
            "tool": name,
            "args": _public_args(raw_args),
            "message": message,
        })
        if approved is not True:
            declined = [e for e in declined if e.get("fingerprint") != fingerprint]
            declined.append({"fingerprint": fingerprint, "anchor": anchor})
            envelope = error(
                "user_declined",
                "The user declined this action.",
                retryable=False,
                hint="Do not retry the same call. Acknowledge the refusal, then ask "
                     "what they would like to change or offer an alternative.")
            writer({"type": "tool_status", "tool": name, "state": "declined"})
            return {
                "messages": [ToolMessage(content=json.dumps(envelope),
                                         tool_call_id=call_id, name=name)],
                "declined_calls": declined,
            }

    # ---- execution with the retry budget ---------------------------------
    writer({"type": "tool_status", "tool": name, "state": "running",
            "attempt": 1, "retries": retries})
    attempts = retries + 1
    envelope: Dict[str, Any] = {}
    attempt = 1
    started = time.monotonic()
    for attempt in range(1, attempts + 1):
        try:
            invoke_args = dict(raw_args)
            if spec.needs_user_id:
                invoke_args["user_id"] = user_id
            result = spec.tool.invoke(invoke_args)
            envelope = result if isinstance(result, dict) else {"status": "success", "data": result}
        except Exception as exc:
            logger.warning("Tool '%s' failed on attempt %d: %s", name, attempt, exc,
                           exc_info=not isinstance(exc, (TypeError, ValueError)))
            envelope = classify_exception(exc)

        if envelope.get("status") == "success":
            break
        if not (envelope.get("error") or {}).get("retryable", False):
            break
        if attempt < attempts:
            delay = float(config.agent.tool_retry_backoff_seconds) * attempt
            if delay > 0:
                time.sleep(delay)
            writer({"type": "tool_status", "tool": name, "state": "running",
                    "attempt": attempt + 1, "retries": retries})

    failed = envelope.get("status") != "success"
    writer({"type": "tool_status", "tool": name,
            "state": "error" if failed else "done",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "attempt": attempt, "retries": retries})

    # ---- UI events (carousel today; generic channel for more later) ------
    ui_event = envelope.get("ui_event") if isinstance(envelope, dict) else None
    if isinstance(ui_event, dict) and ui_event.get("event") == "display_product_carousel":
        writer({"type": "carousel", "product_ids": list(ui_event.get("product_ids") or [])})

    return {"messages": [ToolMessage(content=json.dumps(envelope, default=str),
                                     tool_call_id=call_id, name=name)]}


def _route_after_guard(state: Dict[str, Any]) -> str:
    verdict = state.get("guard_verdict") or {}
    return "end" if verdict.get("blocked") else "classify_intent"


def _route_after_classify(state: Dict[str, Any]) -> str:
    return "retrieve_knowledge" if state.get("intent") == "customer_service" else "agent"


def _route_after_agent(state: Dict[str, Any]) -> str:
    last_ai = _last_message(state, "ai")
    if last_ai is not None and getattr(last_ai, "tool_calls", None):
        return "execute_tools"
    return "end"


def _route_after_tools(state: Dict[str, Any]) -> str:
    return "execute_tools" if _unanswered_call(state) is not None else "agent"


# Graph assembly + fingerprint cache (auto-rebuild on llm_config hot reload)

_GRAPH_CACHE: Dict[tuple, Any] = {}
_CACHE_LOCK = threading.Lock()


def _llm_fingerprint() -> tuple:
    llm = config.llm_config
    return (llm.provider, llm.model_name, llm.temperature, llm.max_tokens,
            llm.top_p, llm.seed, llm.max_retries, llm.timeout_seconds)


def _build_graph(llm):
    builder = StateGraph(AgentState)
    builder.add_node("guard", _make_guard_node(llm))
    builder.add_node("classify_intent", _make_classify_node(llm))
    builder.add_node("retrieve_knowledge", retrieve_knowledge_node)
    builder.add_node("agent", _make_agent_node(llm))
    builder.add_node("execute_tools", execute_tools)

    builder.add_edge(START, "guard")
    builder.add_conditional_edges("guard", _route_after_guard,
                                  {"classify_intent": "classify_intent", "end": END})
    builder.add_conditional_edges("classify_intent", _route_after_classify,
                                  {"retrieve_knowledge": "retrieve_knowledge",
                                   "agent": "agent"})
    builder.add_edge("retrieve_knowledge", "agent")
    builder.add_conditional_edges("agent", _route_after_agent,
                                  {"execute_tools": "execute_tools", "end": END})
    builder.add_conditional_edges("execute_tools", _route_after_tools,
                                  {"execute_tools": "execute_tools", "agent": "agent"})
    return builder.compile(checkpointer=get_checkpointer())


def get_agent_graph():
    """Compiled graph, cached per LLM-config fingerprint. The checkpointer
    survives rebuilds, so conversation state is never lost on config change."""
    fingerprint = _llm_fingerprint()
    with _CACHE_LOCK:
        graph = _GRAPH_CACHE.get(fingerprint)
        if graph is None:
            llm = ModelFactory.get_llm(config.llm_config)
            graph = _build_graph(llm)
            _GRAPH_CACHE.clear()          # single-entry cache: drop stale builds
            _GRAPH_CACHE[fingerprint] = graph
            logger.info("Agent graph compiled (provider=%s, model=%s).",
                        config.llm_config.provider, config.llm_config.model_name)
        return graph


def reset_agent_graph() -> None:
    """Force a rebuild on the next get_agent_graph() call."""
    with _CACHE_LOCK:
        _GRAPH_CACHE.clear()