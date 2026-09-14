"""
Chat blueprint: one Socket.IO endpoint for the whole assistant.

client → server:
    user_message           {conversation_id?, content, attachment_id?}
    confirmation_response  {request_id, conversation_id?, accepted: bool}

server → client:
    connected                after authentication on connect (carries config flags)
    conversation_started     {conversation_id} when a message creates a thread
    agent_status             {phase, tool?, conversation_id}  thinking / running X …
    agent_note               text the model said while starting a tool
    display_product_carousel {product_ids, conversation_id}
    get_user_confirmation    {request_id, tool, message, args, expires_at}
    confirmation_resolved    {request_id, accepted, reason}
    chat_message             {role, content, conversation_id, usage}
    chat_error               {code, message, conversation_id?}

Execution model: each turn runs in a background thread
(socketio.start_background_task). LangGraph state persists per conversation
thread via the SqliteSaver checkpointer. A pending confirmation PAUSES the
graph — no worker is held open; resuming replays from the checkpoint.

Single-process assumptions (threading async mode, in-memory pending map,
stats, attachments). Before scaling to multiple workers: Socket.IO
message_queue + a shared store for _PENDING/_ATTACHMENTS.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, render_template, request
from flask_socketio import emit, join_room
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from agent.graph import get_agent_graph, message_text
from agent.stats import TurnStatsCollector
from agent.tooling import error as tool_error
from database.db_setup import SessionLocal
from database.models import Conversation
from utils.auth import get_current_user, login_required
from utils.config import config
from utils.extensions import limiter, socketio

logger = logging.getLogger(__name__)

chat_bp = Blueprint("chat", __name__)

_RECURSION_LIMIT = 30
_MAX_AUTO_DECLINES = 5              # safety valve for stacked confirmations
_ATTACHMENT_TTL_SECONDS = 600

_LOCK = threading.Lock()
_ACTIVE_TURNS: set = set()                    # user ids with a running turn
_PENDING: Dict[str, Dict[str, Any]] = {}      # request_id → confirmation record
_PENDING_BY_THREAD: Dict[str, str] = {}       # thread_id → request_id
_ATTACHMENTS: Dict[str, Dict[str, Any]] = {}  # attachment_id → record

# Shared helpers

def _chat_flags() -> Dict[str, Any]:
    return {
        "uploads_enabled": bool(config.llm_config.vision_capable),
        "max_upload_mb": int(config.agent.max_upload_mb),
        "allowed_upload_extensions": [e.lower() for e in config.agent.allowed_upload_extensions],
        "max_message_chars": int(config.agent.max_message_chars),
        "confirmation_timeout_seconds": int(config.agent.confirmation_timeout_seconds),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_emit(room: str, event: str, payload: Dict[str, Any]) -> None:
    try:
        socketio.emit(event, payload, to=room)
    except Exception:
        logger.debug("Socket emit failed (%s → %s).", event, room, exc_info=True)


def _client_error(code: str, message: str, conversation_id: Optional[int] = None) -> None:
    payload: Dict[str, Any] = {"code": code, "message": message}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    emit("chat_error", payload)


def _release_turn(user_id: int) -> None:
    with _LOCK:
        _ACTIVE_TURNS.discard(user_id)


def _touch_conversation(conversation_id: int) -> None:
    try:
        with SessionLocal() as db:
            conv = db.get(Conversation, conversation_id)
            if conv is not None:
                conv.updated_at = _now()
                db.commit()
    except Exception:
        logger.debug("Failed to touch conversation %s.", conversation_id, exc_info=True)


def _sweep_attachments_locked() -> None:
    """Must be called while holding _LOCK."""
    cutoff = _now() - timedelta(seconds=_ATTACHMENT_TTL_SECONDS)
    stale = [k for k, v in _ATTACHMENTS.items() if v["created_at"] < cutoff]
    for key in stale:
        _ATTACHMENTS.pop(key, None)


def _finalize_stats(collector: TurnStatsCollector, status: str, thread_id: str) -> None:
    values: Dict[str, Any] = {}
    try:
        snapshot = get_agent_graph().get_state({"configurable": {"thread_id": thread_id}})
        values = (snapshot.values if snapshot else None) or {}
    except Exception:
        pass
    try:
        collector.finalize(status=status, values=values)
    except Exception:
        logger.debug("Stats finalization failed.", exc_info=True)


def _conversation_label(when) -> str:
    """Compact sidebar label: 'Today 14:05', 'Yesterday 09:12', else a date."""
    if when is None:
        return "Conversation"
    now = datetime.now()
    try:
        if when.date() == now.date():
            return when.strftime("Today %H:%M")
        if when.date() == (now - timedelta(days=1)).date():
            return when.strftime("Yesterday %H:%M")
        return when.strftime("%a %d %b %Y")
    except Exception:
        return "Conversation"


@chat_bp.route("/chat")
@login_required
def chat_page():
    """
    Chat page (browser); the socket carries all live traffic.
    ?conversation_id= preselects an owned conversation; foreign/unknown ids
    render a plain fresh page (no redirect loops).
    """
    user = get_current_user()
    requested_id = request.args.get("conversation_id", type=int)

    with SessionLocal() as db:
        rows = (db.query(Conversation)
                .filter(Conversation.user_id == user.id)
                .order_by(Conversation.updated_at.desc())
                .limit(50).all())
        conversations = [{
            "id": row.id,
            "label": _conversation_label(row.updated_at or row.created_at),
        } for row in rows]

        initial_conversation_id = None
        if requested_id is not None:
            owned = (db.query(Conversation.id)
                     .filter(Conversation.id == requested_id,
                             Conversation.user_id == user.id)
                     .first() is not None)
            if owned:
                initial_conversation_id = requested_id
        for c in conversations:
            c["active"] = (c["id"] == initial_conversation_id)

    return render_template("chat/customer.html",
                           conversations=conversations,
                           chat_flags=_chat_flags(),
                           user_name=user.name,
                           initial_conversation_id=initial_conversation_id)


@chat_bp.route("/chat/ping")
@limiter.limit("120 per minute")
def chat_ping():
    """Liveness heartbeat: connection indicator, flag sync, session probe."""
    return jsonify({
        "ok": True,
        "server_time": _now().isoformat(),
        "authenticated": get_current_user() is not None,
        **_chat_flags(),
    })


@chat_bp.route("/chat/history/<int:conversation_id>")
@login_required
def chat_history(conversation_id):
    """Replay a conversation from the checkpointer (JSON)."""
    user = get_current_user()
    with SessionLocal() as db:
        conv = (db.query(Conversation)
                .filter(Conversation.id == conversation_id,
                        Conversation.user_id == user.id).first())
        if conv is None:
            return jsonify({"error": "Conversation not found."}), 404
        meta = {"id": conv.id, "thread_id": conv.thread_id,
                "created_at": conv.created_at.isoformat() if conv.created_at else None,
                "updated_at": conv.updated_at.isoformat() if conv.updated_at else None}

    messages = []
    pending_confirmation = None
    try:
        graph = get_agent_graph()
        snapshot = graph.get_state({"configurable": {"thread_id": conv.thread_id}})
        values = (snapshot.values if snapshot else None) or {}
        for m in values.get("messages") or []:
            if m.type == "human":
                messages.append({"role": "user", "content": message_text(m)})
            elif m.type == "ai":
                text = message_text(m)
                if not text:
                    continue
                messages.append({"role": "assistant", "content": text,
                                 "interim": bool(getattr(m, "tool_calls", None))})
            elif m.type == "tool":
                try:
                    envelope = json.loads(m.content)
                except (TypeError, ValueError):
                    continue
                ui_event = envelope.get("ui_event") if isinstance(envelope, dict) else None
                if (isinstance(ui_event, dict)
                        and ui_event.get("event") == "display_product_carousel"):
                    try:
                        ids = [int(i) for i in ui_event.get("product_ids") or []][:12]
                    except (TypeError, ValueError):
                        continue
                    if ids:
                        messages.append({"role": "carousel", "product_ids": ids})
        with _LOCK:
            request_id = _PENDING_BY_THREAD.get(conv.thread_id)
            record = _PENDING.get(request_id) if request_id else None
        if record:
            pending_confirmation = {
                "request_id": record["request_id"],
                "tool": record["tool"],
                "message": record["message"],
                "args": record["args"],
                "expires_at": record["expires_at"].isoformat(),
            }
    except Exception:
        logger.exception("Failed to load history for conversation %s", conversation_id)
        return jsonify({"error": "Could not load conversation history."}), 500

    return jsonify({"conversation": meta, "messages": messages,
                    "pending_confirmation": pending_confirmation})


@chat_bp.route("/chat/upload", methods=["POST"])
@login_required
def chat_upload():
    """
    Attach an image to the next message. Only active when
    config.llm_config.vision_capable is true. Requires the CSRF token header.
    Attachments live in memory for a single use / ten minutes — never on disk.
    """
    if not config.llm_config.vision_capable:
        return jsonify({"error": "File uploads are disabled."}), 403
    user = get_current_user()
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "No file provided."}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [e.lower() for e in config.agent.allowed_upload_extensions]:
        return jsonify({"error": f"Unsupported file type '{ext}'."}), 415
    data = file.read()
    if not data:
        return jsonify({"error": "Empty file."}), 400
    if len(data) > int(config.agent.max_upload_mb) * 1024 * 1024:
        return jsonify({"error": f"File exceeds {config.agent.max_upload_mb} MB."}), 413
    mime = (file.mimetype or "").lower()
    if not mime.startswith("image/"):
        return jsonify({"error": "Only image uploads are supported."}), 415

    attachment_id = uuid.uuid4().hex
    with _LOCK:
        _sweep_attachments_locked()
        _ATTACHMENTS[attachment_id] = {
            "user_id": user.id, "filename": file.filename,
            "mime": mime, "data": data, "created_at": _now(),
        }
    return jsonify({"attachment_id": attachment_id, "filename": file.filename,
                    "mime": mime, "size": len(data)})


@socketio.on("connect")
def handle_connect():
    user = get_current_user()
    if user is None:
        logger.info("Rejected unauthenticated socket connection from %s.",
                    request.remote_addr)
        return False                     # refuse the connection
    join_room(f"user_{user.id}")
    emit("connected", {"user_id": user.id, **_chat_flags()})
    _sweep_expired_pending(user.id)
    logger.debug("Socket connected for user %s.", user.id)


@socketio.on("disconnect")
def handle_disconnect():
    logger.debug("Socket disconnected.")


@socketio.on("user_message")
def handle_user_message(data):
    user = get_current_user()
    if user is None:
        _client_error("unauthorized", "Your session expired. Please log in again.")
        return
    if not isinstance(data, dict):
        _client_error("invalid_payload", "Malformed message payload.")
        return

    content = data.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        _client_error("invalid_payload", "'content' must be a string.")
        return
    content = content.strip()

    attachment = None
    attachment_id = data.get("attachment_id")
    if attachment_id:
        if not config.llm_config.vision_capable:
            _client_error("uploads_disabled", "File uploads are currently disabled.")
            return
        with _LOCK:
            record = _ATTACHMENTS.pop(str(attachment_id), None)
        if record is None or record["user_id"] != user.id:
            _client_error("attachment_not_found",
                          "Attachment not found or expired. Upload it again.")
            return
        attachment = record

    if not content and attachment is None:
        _client_error("empty_message", "Message is empty.")
        return
    if len(content) > int(config.agent.max_message_chars):
        _client_error("message_too_long",
                      f"Messages are limited to {config.agent.max_message_chars} characters.")
        return

    conversation_id = data.get("conversation_id")
    try:
        conversation_id = int(conversation_id) if conversation_id is not None else None
    except (TypeError, ValueError):
        _client_error("invalid_payload", "'conversation_id' must be an integer.")
        return

    # One in-flight turn per user — reject early, before creating anything.
    with _LOCK:
        if user.id in _ACTIVE_TURNS:
            _client_error("busy", "Your previous message is still being processed.")
            return
        _ACTIVE_TURNS.add(user.id)

    try:
        created = False
        with SessionLocal() as db:
            if conversation_id is not None:
                conv = (db.query(Conversation)
                        .filter(Conversation.id == conversation_id,
                                Conversation.user_id == user.id).first())
                if conv is None:
                    _release_turn(user.id)
                    _client_error("conversation_not_found", "Conversation not found.")
                    return
            else:
                conv = Conversation(user_id=user.id, thread_id=uuid.uuid4().hex)
                db.add(conv)
                db.commit()
                created = True
            conversation_id = conv.id
            thread_id = conv.thread_id
        if created:
            emit("conversation_started", {"conversation_id": conversation_id})

        # A pending confirmation on this thread is superseded by the new
        # message: the runner auto-declines it, then processes this message.
        superseded = None
        with _LOCK:
            request_id = _PENDING_BY_THREAD.get(thread_id)
            if request_id:
                superseded = _PENDING.pop(request_id, None)
                _PENDING_BY_THREAD.pop(thread_id, None)
        if superseded is not None:
            emit("confirmation_resolved", {"request_id": request_id,
                                           "accepted": False, "reason": "superseded",
                                           "conversation_id": conversation_id})

        socketio.start_background_task(
            _run_turn, user.id, user.name, conversation_id, thread_id,
            content, attachment, superseded,
        )
    except Exception:
        logger.exception("Failed to start turn for user %s.", user.id)
        _release_turn(user.id)
        _client_error("internal_error", "Could not process your message. Please try again.")


@socketio.on("confirmation_response")
def handle_confirmation_response(data):
    user = get_current_user()
    if user is None:
        _client_error("unauthorized", "Your session expired. Please log in again.")
        return
    if not isinstance(data, dict):
        _client_error("invalid_payload", "Malformed confirmation payload.")
        return
    request_id = data.get("request_id")
    if not request_id or not isinstance(request_id, str):
        _client_error("invalid_payload", "'request_id' is required.")
        return
    accepted = bool(data.get("accepted"))

    with _LOCK:
        record = _PENDING.get(request_id)
        if record is None or record["user_id"] != user.id:
            _client_error("unknown_confirmation",
                          "This confirmation is no longer active (already answered, "
                          "timed out, or superseded).")
            return
        _PENDING.pop(request_id, None)
        if _PENDING_BY_THREAD.get(record["thread_id"]) == request_id:
            _PENDING_BY_THREAD.pop(record["thread_id"], None)

    age = (_now() - record["created_at"]).total_seconds()
    expired = age > int(config.agent.confirmation_timeout_seconds)
    effective = accepted and not expired
    reason = "accepted" if effective else ("timeout" if expired else "declined")

    emit("confirmation_resolved", {"request_id": request_id,
                                   "accepted": effective, "reason": reason,
                                   "conversation_id": record["conversation_id"]})

    with _LOCK:
        if user.id in _ACTIVE_TURNS:
            _client_error("busy", "Still finishing the previous step — try again in a moment.")
            return
        _ACTIVE_TURNS.add(user.id)

    try:
        socketio.start_background_task(
            _run_resume, user.id, record["conversation_id"],
            record["thread_id"], effective, reason,
        )
    except Exception:
        logger.exception("Failed to start resume for user %s.", user.id)
        _release_turn(user.id)
        _client_error("internal_error", "Could not resume the action. Please try again.")


def _sweep_expired_pending(user_id: Optional[int] = None) -> None:
    """
    Timeout handling: resume paused graphs with a decline so threads never
    wedge. Called on connect. Busy users keep their pending entry (retried
    on the next sweep) — this avoids racing an in-flight turn.
    """
    timeout = int(config.agent.confirmation_timeout_seconds)
    now = _now()
    to_resume = []
    with _LOCK:
        expired = [rid for rid, rec in _PENDING.items()
                   if (user_id is None or rec["user_id"] == user_id)
                   and (now - rec["created_at"]).total_seconds() > timeout]
        for rid in expired:
            record = _PENDING.pop(rid)
            if _PENDING_BY_THREAD.get(record["thread_id"]) == rid:
                _PENDING_BY_THREAD.pop(record["thread_id"], None)
            if record["user_id"] in _ACTIVE_TURNS:
                _PENDING[rid] = record                  # busy: retry next sweep
                _PENDING_BY_THREAD[record["thread_id"]] = rid
            else:
                _ACTIVE_TURNS.add(record["user_id"])
                to_resume.append(record)
    for record in to_resume:
        _safe_emit(f"user_{record['user_id']}", "confirmation_resolved",
                   {"request_id": record["request_id"],
                    "accepted": False, "reason": "timeout",
                    "conversation_id": record["conversation_id"]})
        try:
            socketio.start_background_task(
                _run_resume, record["user_id"], record["conversation_id"],
                record["thread_id"], False, "timeout",
            )
        except Exception:
            _release_turn(record["user_id"])
            logger.exception("Failed to start timeout resume.")


class TurnEmitter:
    """Maps graph stream events to socket events for one user room."""

    _TOOL_STATES = {
        "running": "tool_running",
        "done": "tool_done",
        "error": "tool_error",
        "awaiting_confirmation": "awaiting_confirmation",
        "declined": "tool_declined",
        "blocked": "tool_blocked",
    }
    _PHASES = {"thinking", "safety_check", "classifying", "retrieving",
               "resuming", "done", "blocked"}

    def __init__(self, room: str, conversation_id: Optional[int]):
        self.room = room
        self.conversation_id = conversation_id
        self._last_status_key = None

    def status(self, phase: str, **extra: Any) -> None:
        if not config.agent.status_events_enabled:
            return
        key = (phase, extra.get("tool"))
        if key == self._last_status_key:
            return          # dedupe statuses replayed after an interrupt resume
        self._last_status_key = key
        _safe_emit(self.room, "agent_status",
                   {"phase": phase, "conversation_id": self.conversation_id, **extra})

    def message(self, content: str, usage: Optional[Dict[str, int]] = None) -> None:
        _safe_emit(self.room, "chat_message",
                   {"role": "assistant", "content": content,
                    "conversation_id": self.conversation_id, "usage": usage or {}})

    def error(self, code: str, message: str) -> None:
        _safe_emit(self.room, "chat_error",
                   {"code": code, "message": message,
                    "conversation_id": self.conversation_id})

    def confirmation(self, record: Dict[str, Any]) -> None:
        _safe_emit(self.room, "get_user_confirmation", {
            "request_id": record["request_id"],
            "tool": record["tool"],
            "message": record["message"],
            "args": record["args"],
            "expires_at": record["expires_at"].isoformat(),
            "conversation_id": self.conversation_id,
        })

    def custom(self, payload: Any) -> None:
        """Forward a graph custom-stream event to its socket equivalent."""
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "phase":
            phase = payload.get("phase")
            if phase in self._PHASES:
                self.status(phase)
        elif kind == "tool_status":
            phase = self._TOOL_STATES.get(payload.get("state"), "tool_running")
            extra: Dict[str, Any] = {}
            if payload.get("tool"):
                extra["tool"] = payload["tool"]
            if payload.get("duration_ms") is not None:
                extra["duration_ms"] = payload["duration_ms"]
            if payload.get("attempt") is not None:
                extra["attempt"] = payload["attempt"]
            self.status(phase, **extra)
        elif kind == "agent_note":
            content = payload.get("content")
            if content:
                _safe_emit(self.room, "agent_note",
                           {"content": content, "conversation_id": self.conversation_id})
        elif kind == "carousel":
            _safe_emit(self.room, "display_product_carousel",
                       {"product_ids": list(payload.get("product_ids") or []),
                        "conversation_id": self.conversation_id})
        elif kind == "guard_blocked":
            self.status("blocked")
        # unknown event types are ignored on purpose (forward-compatible)


def _build_human_message(content: str,
                          attachment: Optional[Dict[str, Any]]) -> HumanMessage:
    message_id = str(uuid.uuid4())
    if attachment is None:
        return HumanMessage(content=content, id=message_id)
    data_url = "data:{};base64,{}".format(
        attachment["mime"], base64.b64encode(attachment["data"]).decode("ascii"))
    blocks: list = []
    if content:
        blocks.append({"type": "text", "text": content})
    blocks.append({"type": "image_url", "image_url": {"url": data_url}})
    return HumanMessage(content=blocks, id=message_id)


def _final_assistant_message(graph, run_config) -> Optional[AIMessage]:
    try:
        snapshot = graph.get_state(run_config)
    except Exception:
        return None
    messages = ((snapshot.values if snapshot else None) or {}).get("messages") or []
    for m in reversed(messages):
        if m.type == "ai" and not getattr(m, "tool_calls", None):
            return m
    return None


def _heal_thread(thread_id: str, code: str, detail: str) -> None:
    """
    After an aborted turn (recursion limit / crash) the transcript may end
    with unanswered tool calls, which most providers reject on the next
    request. Append synthetic ToolMessages (+ a closing assistant message)
    via update_state so the thread stays usable.
    """
    try:
        graph = get_agent_graph()
        cfg = {"configurable": {"thread_id": thread_id}}
        snapshot = graph.get_state(cfg)
        messages = ((snapshot.values if snapshot else None) or {}).get("messages") or []
        last_ai = next((m for m in reversed(messages) if m.type == "ai"), None)
        calls = list(getattr(last_ai, "tool_calls", None) or []) if last_ai else []
        answered = {m.tool_call_id for m in messages if m.type == "tool"}
        additions = []
        for call in calls:
            call_id = call.get("id")
            if not call_id or call_id in answered:
                continue
            envelope = tool_error(code, f"Execution aborted: {detail}", retryable=False,
                                  hint="This attempt was aborted; only try again if "
                                       "the user repeats the request.")
            additions.append(ToolMessage(content=json.dumps(envelope),
                                         tool_call_id=call_id,
                                         name=call.get("name") or "tool"))
        if additions:
            additions.append(AIMessage(
                content="I ran into a problem while working on that request. "
                        "Please try again or rephrase it."))
            graph.update_state(cfg, {"messages": additions})
            logger.info("Healed thread %s with %d synthetic tool result(s).",
                        thread_id, len(additions) - 1)
    except Exception:
        logger.exception("Failed to heal thread %s.", thread_id)


def _register_pending(user_id: int, conversation_id: int, thread_id: str,
                      payload: Dict[str, Any]) -> Dict[str, Any]:
    now = _now()
    record = {
        "request_id": str(payload.get("request_id") or uuid.uuid4()),
        "user_id": user_id,
        "conversation_id": conversation_id,
        "thread_id": thread_id,
        "tool": payload.get("tool"),
        "args": payload.get("args") or {},
        "message": payload.get("message") or "Approve this action?",
        "created_at": now,
        "expires_at": now + timedelta(seconds=int(config.agent.confirmation_timeout_seconds)),
    }
    with _LOCK:
        stale = _PENDING_BY_THREAD.get(thread_id)
        if stale:
            _PENDING.pop(stale, None)
        _PENDING[record["request_id"]] = record
        _PENDING_BY_THREAD[thread_id] = record["request_id"]
    return record


def _stream_turn(user_id: int, conversation_id: int, thread_id: str,
                 emitter: TurnEmitter, collector: TurnStatsCollector,
                 graph_input: Any, auto_decline: bool = False) -> str:
    """
    Run one graph input (HumanMessage dict or Command) to completion or to
    the next confirmation interrupt.
    Returns "completed" | "interrupted" | "error".

    In auto_decline mode interrupts are NOT surfaced to the user — the caller
    keeps resuming with False (used when a new message supersedes a pending
    confirmation, including stacked multiple confirmations).
    """
    graph = get_agent_graph()
    run_config = {"configurable": {"thread_id": thread_id},
                  "recursion_limit": _RECURSION_LIMIT}
    interrupt_payload = None
    try:
        for mode, chunk in graph.stream(graph_input, run_config,
                                        stream_mode=["custom", "updates"]):
            if mode == "custom":
                collector.note_custom(chunk)
                emitter.custom(chunk)
                continue
            if "__interrupt__" in chunk:
                interrupts = chunk.get("__interrupt__") or ()
                interrupt_payload = (getattr(interrupts[0], "value", None)
                                     if interrupts else None)
                break
            collector.note_update(chunk)
    except GraphRecursionError:
        logger.warning("Recursion limit reached (thread=%s).", thread_id)
        _heal_thread(thread_id, "loop_limit", "the turn exceeded the processing limit")
        emitter.error("loop_limit",
                      "That request got too complex to process here. Try a simpler version.")
        return "error"
    except Exception as exc:
        logger.exception("Agent turn failed (thread=%s): %s", thread_id, exc)
        _heal_thread(thread_id, "internal_error", "the turn failed unexpectedly")
        emitter.error("internal_error",
                      "I ran into an error while working on that. Please try again.")
        return "error"

    if interrupt_payload is not None:
        collector.note_interrupt(interrupt_payload)
        if auto_decline:
            return "interrupted"           # caller resumes with Command(resume=False)
        
        record = _register_pending(user_id, conversation_id, thread_id, interrupt_payload)
        emitter.confirmation(record)

        def _delayed_timeout_sweep():
            socketio.sleep(int(config.agent.confirmation_timeout_seconds) + 1)
            _sweep_expired_pending(user_id)
            
        socketio.start_background_task(_delayed_timeout_sweep)
        return "interrupted"

    final = _final_assistant_message(graph, run_config)
    text = message_text(final) if final is not None else ""
    if text:
        emitter.message(text, usage={"tokens_in": collector.stats.tokens_in,
                                     "tokens_out": collector.stats.tokens_out})
        emitter.status("done")
        return "completed"
    emitter.error("empty_response",
                  "I couldn't produce a response for that. Please try rephrasing.")
    return "error"


def _run_turn(user_id: int, user_name: Optional[str], conversation_id: int,
              thread_id: str, content: str,
              attachment: Optional[Dict[str, Any]],
              superseded: Optional[Dict[str, Any]]) -> None:
    """Background task: (auto-decline any superseded confirmation,) then answer."""
    emitter = TurnEmitter(f"user_{user_id}", conversation_id)
    collector = TurnStatsCollector(user_id, thread_id, conversation_id)
    status = "completed"
    try:
        if superseded is not None:
            collector.note_superseded()
            for _ in range(_MAX_AUTO_DECLINES):
                emitter.status("resuming")
                outcome = _stream_turn(user_id, conversation_id, thread_id,
                                       emitter, collector, Command(resume=False),
                                       auto_decline=True)
                if outcome != "interrupted":
                    break
        human = _build_human_message(content, attachment)
        status = _stream_turn(
            user_id, conversation_id, thread_id, emitter, collector,
            {"messages": [human], "user_id": user_id, "user_name": user_name},
        )
    except Exception:
        logger.exception("Turn runner crashed (user=%s, thread=%s).", user_id, thread_id)
        emitter.error("internal_error",
                      "Something went wrong while processing your message.")
        status = "error"
    finally:
        _release_turn(user_id)
        _touch_conversation(conversation_id)
        _finalize_stats(collector, status, thread_id)


def _run_resume(user_id: int, conversation_id: int, thread_id: str,
                accepted: bool, reason: str) -> None:
    """Background task: continue a paused graph with the user's decision."""
    emitter = TurnEmitter(f"user_{user_id}", conversation_id)
    collector = TurnStatsCollector(user_id, thread_id, conversation_id)
    status = "completed"
    try:
        emitter.status("resuming")
        status = _stream_turn(user_id, conversation_id, thread_id, emitter,
                              collector, Command(resume=accepted))
    except Exception:
        logger.exception("Resume runner crashed (user=%s, thread=%s).", user_id, thread_id)
        emitter.error("internal_error", "Something went wrong while finishing that action.")
        status = "error"
    finally:
        _release_turn(user_id)
        _touch_conversation(conversation_id)
        _finalize_stats(collector, status, thread_id)