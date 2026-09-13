import asyncio
import concurrent.futures
import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Blueprint, Response, jsonify, request
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from agent.graph import (
    compile_agent_graph,
    extract_interrupt_value,
    extract_pending_tool_calls,
)
from agent.persistence import (
    checkpointer_context,
    conversation_access_required,
    find_conversation,
    get_or_create_conversation,
    list_conversations,
    touch_conversation,
)
from database.models import UserRole
from utils.auth import get_current_user, login_required
from utils.config import config
from utils.extensions import socketio

logger = logging.getLogger(__name__)

bp = Blueprint("chat", __name__, url_prefix="/chat")


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


_broker_lock = threading.Lock()
_subscribers: Dict[str, "queue.Queue"] = {}


def publish_event(thread_id: str, event: str, payload: Dict[str, Any]) -> None:
    with _broker_lock:
        q = _subscribers.get(thread_id)
    if q is not None:
        try:
            q.put_nowait({"event": event, "payload": payload})
        except queue.Full:
            pass


def _subscribe(thread_id: str) -> "queue.Queue":
    q: "queue.Queue" = queue.Queue(maxsize=200)
    with _broker_lock:
        _subscribers[thread_id] = q
    return q


def _unsubscribe(thread_id: str) -> None:
    with _broker_lock:
        _subscribers.pop(thread_id, None)


@bp.get("/conversations/<thread_id>/events")
@login_required
@conversation_access_required
def conversation_events():
    """SSE stream: agent status + confirmation requests for this thread."""
    conv = request._conversation

    def stream():
        q = _subscribe(conv.thread_id)
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    item = q.get(timeout=15)
                    yield f"event: {item['event']}\ndata: {json.dumps(item['payload'])}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            _unsubscribe(conv.thread_id)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _serialize_conversation(conv, include_user: bool) -> Dict[str, Any]:
    data = {
        "id": conv.id,
        "thread_id": conv.thread_id,
        "user_id": conv.user_id,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
    }
    if include_user and conv.user is not None:
        data["user_email"] = conv.user.email
    return data


@bp.get("/conversations")
@login_required
def conversations_index():
    user = get_current_user()
    convs = list_conversations(user)
    is_admin = user.role == UserRole.ADMIN
    return jsonify([_serialize_conversation(c, include_user=is_admin) for c in convs])


def _stringify_content(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text").strip()
    return str(content)


@bp.get("/conversations/<thread_id>/messages")
@login_required
@conversation_access_required
def conversation_messages():
    conv = request._conversation

    async def _load():
        async with checkpointer_context() as saver:
            graph = compile_agent_graph(saver)
            state = await graph.aget_state(
                {"configurable": {"thread_id": conv.thread_id}})
            if state is None or not state.values:
                return []
            out = []
            for m in state.values.get("messages", []):
                if isinstance(m, HumanMessage):
                    out.append({"role": "user", "content": _stringify_content(m.content)})
                elif isinstance(m, AIMessage) and not getattr(m, "tool_calls", None):
                    out.append({"role": "assistant", "content": _stringify_content(m.content)})
            return out

    return jsonify({"thread_id": conv.thread_id, "messages": _run(_load())})


_pending_lock = threading.Lock()
_pending_confirmations: Dict[str, Dict[str, Any]] = {}


@bp.post("/conversations/<thread_id>/confirm")
@login_required
@conversation_access_required
def conversation_confirm():
    conv = request._conversation
    body = request.get_json(silent=True) or {}
    approved = bool(body.get("approved"))
    note = str(body.get("note") or "")[:500]

    with _pending_lock:
        pending = _pending_confirmations.pop(conv.thread_id, None)
    if pending is None:
        return jsonify({"error": "No pending confirmation for this conversation."}), 409
    if datetime.now(timezone.utc) > pending["expires_at"]:
        return jsonify({"error": "Confirmation request has expired. Please ask again."}), 410

    publish_event(conv.thread_id, "confirmation_resolved", {"approved": approved})

    async def _resume():
        async with checkpointer_context() as saver:
            graph = compile_agent_graph(saver)
            cfg = {"configurable": {"thread_id": conv.thread_id, "user_id": conv.user_id}}
            result = await graph.ainvoke(
                Command(resume={"approved": approved, "note": note}), config=cfg)
            return result

    result = _run(_resume())
    last_ai = _last_ai_message(result)
    if last_ai:
        _emit_to_thread(conv.thread_id, "assistant_message", {
            "thread_id": conv.thread_id,
            "content": _stringify_content(last_ai.content),
        })
    touch_conversation(conv.thread_id)
    return jsonify({"ok": True, "thread_id": conv.thread_id})


def _emit_to_thread(thread_id: str, event: str, payload: Dict[str, Any]) -> None:
    try:
        socketio.server.emit(event, payload, room=thread_id, namespace="/chat")
    except Exception:  # noqa: BLE001 — never let a push failure kill the agent
        logger.debug("socketio emit failed", exc_info=True)


def _emit_error(room: Optional[str], message: str) -> None:
    payload = {"error": message}
    if room:
        _emit_to_thread(room, "error", payload)
    else:
        try:
            socketio.server.emit("error", payload, room=request.sid, namespace="/chat")
        except Exception:
            pass


@socketio.on("join", namespace="/chat")
def on_join(data: Dict[str, Any]):
    if get_current_user() is None:
        return
    thread_id = (data or {}).get("thread_id")
    if thread_id:
        from flask_socketio import join_room
        join_room(thread_id)


def _validate_attachments(attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Upload gate: extension allow-list + size cap, gated on vision capability."""
    runtime = config.agent
    cleaned = []
    for att in attachments:
        filename = os.path.basename(str(att.get("filename") or "upload"))
        ext = os.path.splitext(filename)[1].lower()
        if ext not in runtime.allowed_upload_extensions:
            raise ValueError(f"File type '{ext or 'unknown'}' is not allowed.")
        data_url = str(att.get("data_url") or "")
        # base64 payload approx: 4 chars per 3 bytes
        approx_mb = (len(data_url) * 3 / 4) / (1024 * 1024)
        if approx_mb > runtime.max_upload_mb:
            raise ValueError(f"File exceeds the {runtime.max_upload_mb} MB upload limit.")
        cleaned.append({"filename": filename, "data_url": data_url})
    return cleaned


def _last_ai_message(state_values: Optional[Dict[str, Any]]) -> Optional[AIMessage]:
    if not state_values:
        return None
    for m in reversed(state_values.get("messages", [])):
        if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None):
            return m
    return None


async def _process_user_message(conv, content: str,
                                attachments: List[Dict[str, Any]]) -> None:
    blocks: List[Dict[str, Any]] = []
    if content:
        blocks.append({"type": "text", "text": content})
    for att in attachments:
        blocks.append({"type": "image_url",
                       "image_url": {"url": att["data_url"]}})
    if not blocks:
        blocks.append({"type": "text", "text": "[attachment]"})

    if len(blocks) == 1 and blocks[0]["type"] == "text":
        human = HumanMessage(content=blocks[0]["text"])
    else:
        human = HumanMessage(content=blocks)

    def emit_status(stage: str, payload: Optional[Dict[str, Any]] = None) -> None:
        publish_event(conv.thread_id, "status",
                      {"stage": stage, **(payload or {})})

    emit_status("connecting")
    cfg = {
        "configurable": {
            "thread_id": conv.thread_id,
            "user_id": conv.user_id,
            "emit_status": emit_status,
        }
    }

    pending_calls: Optional[List[Dict[str, Any]]] = None
    async with checkpointer_context() as saver:
        graph = compile_agent_graph(saver)
        async for chunk in graph.astream(
            {"messages": [human], "user_id": conv.user_id},
            config=cfg,
            stream_mode="updates",
        ):
            interrupt_value = extract_interrupt_value(chunk)
            if interrupt_value is not None:
                pending_calls = extract_pending_tool_calls(interrupt_value)
            else:
                for node_name in chunk:
                    emit_status("node_started", {"node": node_name})
        state = await graph.aget_state(cfg)

    touch_conversation(conv.thread_id)

    if pending_calls:
        expires_at = datetime.now(timezone.utc).timestamp() + config.agent.confirmation_timeout_seconds
        with _pending_lock:
            _pending_confirmations[conv.thread_id] = {
                "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc),
            }
        payload = {
            "thread_id": conv.thread_id,
            "tool_calls": pending_calls,
            "expires_in": config.agent.confirmation_timeout_seconds,
        }
        publish_event(conv.thread_id, "confirmation_required", payload)
        _emit_to_thread(conv.thread_id, "confirmation_required", payload)  # socket fallback
        emit_status("awaiting_confirmation")
        return

    last_ai = _last_ai_message(state.values if state else None)
    if last_ai:
        _emit_to_thread(conv.thread_id, "assistant_message", {
            "thread_id": conv.thread_id,
            "content": _stringify_content(last_ai.content),
        })
        emit_status("done")
    else:
        _emit_to_thread(conv.thread_id, "assistant_message", {
            "thread_id": conv.thread_id,
            "content": "",
        })


@socketio.on("send_message", namespace="/chat")
async def on_send_message(data: Dict[str, Any]):
    data = data or {}
    user = get_current_user()
    if user is None:
        _emit_error(None, "Authentication required.")
        return

    thread_id = data.get("thread_id")
    content = (data.get("content") or "").strip()
    attachments = data.get("attachments") or []

    try:
        # --- upload gate -----------------------------------------------------
        if attachments and not config.llm_config.vision_capable:
            _emit_error(thread_id,
                        "File uploads are disabled: the configured model is not "
                        "vision-capable. Enable vision_capable in the LLM settings.")
            return
        attachments = _validate_attachments(attachments) if attachments else []

        if not content and not attachments:
            _emit_error(thread_id, "Message content is required.")
            return

        # --- ownership / creation -------------------------------------------
        is_admin = user.role == UserRole.ADMIN
        existing = find_conversation(thread_id) if thread_id else None
        if existing is not None and existing.user_id is not None \
                and existing.user_id != user.id and not is_admin:
            _emit_error(thread_id, "You do not have access to this conversation.")
            return

        conv = get_or_create_conversation(thread_id, user.id)
        _emit_to_thread(conv.thread_id, "status", {"stage": "received",
                                                   "thread_id": conv.thread_id})
        await _process_user_message(conv, content, attachments)
    except ValueError as exc:
        _emit_error(thread_id, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat message processing failed")
        _emit_error(thread_id, "Something went wrong while processing your message.")
