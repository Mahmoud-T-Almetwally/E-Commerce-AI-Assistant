import functools
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from flask import jsonify, request
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from database.db_setup import get_db
from database.models import Conversation, UserRole
from utils.auth import get_current_user
from utils.config import config

logger = logging.getLogger(__name__)


def new_thread_id() -> str:
    return str(uuid.uuid4())


def checkpointer_context():
    """
    Async context manager yielding an AsyncSqliteSaver.

    A fresh saver (and underlying connection) is created per request rather
    than cached globally: flask-socketio may execute handlers on different
    threads/loops, and an aiosqlite connection is bound to a single event loop.
    Compiling the graph per request is cheap — the structure is rebuilt, not
    the checkpoint data.
    """
    return AsyncSqliteSaver.from_conn_string(config.database_config.checkpoint_path)


def find_conversation(thread_id: str) -> Optional[Conversation]:
    with get_db() as db:
        return db.query(Conversation).filter(Conversation.thread_id == thread_id).first()


def get_or_create_conversation(thread_id: Optional[str], user_id: Optional[int]) -> Conversation:
    """
    Creates a thread row when unknown; claims an anonymous (user_id=None)
    conversation for the first authenticated user to send a message in it.
    Ownership validation must happen before calling this (see the route).
    """
    with get_db() as db:
        conv = None
        if thread_id:
            conv = db.query(Conversation).filter(Conversation.thread_id == thread_id).first()
        if conv is None:
            conv = Conversation(user_id=user_id, thread_id=thread_id or new_thread_id())
            db.add(conv)
        elif conv.user_id is None and user_id is not None:
            conv.user_id = user_id
        db.commit()
        db.refresh(conv)
        return conv


def touch_conversation(thread_id: str) -> None:
    with get_db() as db:
        db.query(Conversation).filter(Conversation.thread_id == thread_id).update(
            {Conversation.updated_at: datetime.now(timezone.utc)}
        )
        db.commit()


def list_conversations(user) -> List[Conversation]:
    """Customers see only their own threads; admins see everything."""
    with get_db() as db:
        q = db.query(Conversation).order_by(Conversation.updated_at.desc())
        if user.role != UserRole.ADMIN:
            q = q.filter(Conversation.user_id == user.id)
        return q.all()


def conversation_access_required(fn):
    """
    Route decorator enforcing per-conversation ownership. Uses the unified
    session-based auth (utils.auth.get_current_user) — no flask-login.

    - Unauthenticated            -> 401
    - Unknown thread_id          -> 404
    - Anonymous thread (no owner)-> admin only
    - Owned thread               -> owner or admin only
    - Admin                      -> full bypass (list/view all conversations)

    The authorized Conversation is attached to `request._conversation`.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if user is None:
            return jsonify({"error": "Authentication required."}), 401

        thread_id = (
            (request.view_args or {}).get("thread_id")
            or (request.get_json(silent=True) or {}).get("thread_id")
            or request.args.get("thread_id")
        )
        if not thread_id:
            return jsonify({"error": "thread_id is required."}), 400

        conv = find_conversation(thread_id)
        if conv is None:
            return jsonify({"error": "Conversation not found."}), 404

        is_admin = user.role == UserRole.ADMIN
        if not is_admin:
            if conv.user_id is None or conv.user_id != user.id:
                return jsonify({"error": "You do not have access to this conversation."}), 403

        request._conversation = conv
        return fn(*args, **kwargs)
    return wrapper
