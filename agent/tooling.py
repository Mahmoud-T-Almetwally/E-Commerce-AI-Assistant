import asyncio
import logging
import re
from typing import Any, Callable, Dict, List

from langchain_core.messages import ToolMessage
from langgraph.types import interrupt

from agent.state import AgentState
from utils.config import config

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, Dict[str, Any]], None]

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ToolValidationError(Exception):
    """Non-retryable: the model produced arguments that fail schema validation."""


def _noop_emit(event: str, payload: Dict[str, Any]) -> None:
    return None


def resolve_emit(configurable: Dict[str, Any]) -> EmitFn:
    """Pulls the status-emitter callback injected by the chat route, if any."""
    emit = (configurable or {}).get("emit_status")
    return emit if callable(emit) else _noop_emit


def sanitize_text(value: Any, *, max_length: int = 2000) -> str:
    """Strips control characters (prompt-injection carriers) and bounds length."""
    return _CONTROL_CHARS.sub("", str(value)).strip()[:max_length]


def validate_tool_arguments(tool, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validates tool arguments against the tool's pydantic schema, then returns
    the original arguments with string values sanitized. Raises
    ToolValidationError (non-retryable) on schema failure.
    """
    args = args or {}
    schema = getattr(tool, "args_schema", None)
    if schema is not None:
        try:
            schema.model_validate(args)
        except Exception as exc:
            raise ToolValidationError(f"schema validation failed: {exc}") from exc
    return {
        key: sanitize_text(value) if isinstance(value, str) else value
        for key, value in args.items()
    }


def _is_retryable(exc: BaseException) -> bool:
    """Transient infrastructure failures may be retried; everything else may not."""
    try:
        import sqlalchemy.exc as sa_exc
        operational = (sa_exc.OperationalError,)
    except ImportError:  # pragma: no cover
        operational = ()
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError) + operational)


def _format_tool_error(name: str, exc: BaseException, *, attempt: int,
                       max_attempts: int, retryable: bool) -> str:
    if not retryable:
        return (
            f"TOOL_ERROR [invalid]: '{name}' rejected its arguments: {exc}. "
            f"Correct the arguments and call it again — do not repeat the identical call."
        )
    if attempt < max_attempts:
        return (
            f"TOOL_ERROR [retryable]: '{name}' failed (attempt {attempt}/{max_attempts}): {exc}. "
            f"The system is retrying automatically — do not re-issue the tool call yourself."
        )
    return (
        f"TOOL_ERROR [fatal]: '{name}' failed after {max_attempts} attempt(s): {exc}. "
        f"Do not retry. Apologize to the user and offer an alternative."
    )


async def execute_tool_calls(state: AgentState, tools: List[Any],
                             runnable_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Executes every pending tool call in the last AIMessage with:
      - strict argument validation (schema + sanitization),
      - bounded retries with exponential-ish backoff for transient failures,
      - structured error ToolMessages fed back to the model on failure,
      - per-attempt status events for the front-end.
    """
    messages = state.get("messages") or []
    if not messages:
        return {}
    last = messages[-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    if not tool_calls:
        return {}

    runtime = config.agent
    emit = resolve_emit((runnable_config or {}).get("configurable"))
    by_name = {t.name: t for t in tools}
    results: List[ToolMessage] = []

    for tc in tool_calls:
        tool = by_name.get(tc.get("name"))
        call_id = tc.get("id") or ""
        if tool is None:
            results.append(ToolMessage(
                content=f"TOOL_ERROR [invalid]: unknown tool '{tc.get('name')}'.",
                tool_call_id=call_id, status="error"))
            continue

        max_attempts = 1 + runtime.max_tool_retries
        for attempt in range(1, max_attempts + 1):
            if runtime.status_events_enabled:
                emit("tool_executing", {"tool": tool.name, "attempt": attempt,
                                        "max_attempts": max_attempts})
            try:
                args = validate_tool_arguments(tool, tc.get("args") or {})
                content = await tool.ainvoke(args, config=runnable_config)
                results.append(ToolMessage(content=str(content), tool_call_id=call_id))
                break
            except ToolValidationError as exc:
                results.append(ToolMessage(
                    content=_format_tool_error(tool.name, exc, attempt=attempt,
                                               max_attempts=max_attempts, retryable=False),
                    tool_call_id=call_id, status="error"))
                break
            except Exception as exc:  # noqa: BLE001 — fed back to the model by design
                retryable = _is_retryable(exc)
                if not retryable or attempt >= max_attempts:
                    results.append(ToolMessage(
                        content=_format_tool_error(tool.name, exc, attempt=attempt,
                                                   max_attempts=max_attempts, retryable=retryable),
                        tool_call_id=call_id, status="error"))
                    logger.warning("Tool '%s' failed permanently: %s", tool.name, exc)
                    break
                logger.info("Tool '%s' attempt %d/%d failed (retryable): %s",
                            tool.name, attempt, max_attempts, exc)
                if runtime.status_events_enabled:
                    emit("tool_retrying", {"tool": tool.name, "attempt": attempt + 1,
                                           "max_attempts": max_attempts,
                                           "error": str(exc)[:200]})
                await asyncio.sleep(runtime.tool_retry_backoff_seconds * attempt)

    return {"messages": results}


def make_tool_node(tools: List[Any]):
    """Wraps tools in a plain graph node (safe tools — no confirmation needed)."""
    async def tool_node(state: AgentState, runnable_config) -> Dict[str, Any]:
        return await execute_tool_calls(state, tools, runnable_config)

    tool_node.__name__ = "safe_tools"
    return tool_node


def make_hitl_tool_node(tools: List[Any]):
    """
    Wraps sensitive tools in a graph node that always asks the user for
    confirmation first, using langgraph's dynamic `interrupt()`.

    First pass: raises an interrupt whose payload describes the pending tool
    calls (surfaced to the user by the chat route via SSE).
    On resume (`Command(resume={"approved": bool, "note": str})`):
      - approved  -> execute with the standard retry/error semantics,
      - declined  -> feed a structured DECLINED ToolMessage back to the model.
    """
    tool_names = {t.name for t in tools}

    async def sensitive_tools(state: AgentState, runnable_config) -> Dict[str, Any]:
        messages = state.get("messages") or []
        pending = []
        if messages:
            for tc in (getattr(messages[-1], "tool_calls", None) or []):
                if tc.get("name") in tool_names:
                    pending.append({"id": tc.get("id"), "name": tc.get("name"),
                                    "args": tc.get("args") or {}})
        if not pending:
            return {}

        decision = interrupt({"type": "tool_confirmation", "tool_calls": pending})
        approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
        note = (decision.get("note") or "").strip() if isinstance(decision, dict) else ""

        if not approved:
            return {"messages": [
                ToolMessage(
                    content=(
                        f"ACTION_DECLINED: the user declined to confirm '{tc['name']}'"
                        + (f" (reason: {sanitize_text(note, max_length=200)})." if note else ".")
                        + " Acknowledge the refusal and do not re-attempt unless the user "
                          "explicitly asks you to."
                    ),
                    tool_call_id=tc.get("id") or "",
                    status="error",
                )
                for tc in pending
            ]}

        if config.agent.status_events_enabled:
            resolve_emit((runnable_config or {}).get("configurable"))(
                "confirmation_granted", {"tools": [tc["name"] for tc in pending]})
        return await execute_tool_calls(state, tools, runnable_config)

    sensitive_tools.__name__ = "sensitive_tools"
    return sensitive_tools
