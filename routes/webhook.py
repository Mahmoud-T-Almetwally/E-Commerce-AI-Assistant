"""
Facebook Messenger transport for the shopping assistant — the second adapter
next to routes/chat.py. The agent graph, tools, checkpointer and stats are
shared; only the transport differs.

Protocol translation (web chat → Messenger):
  user_message          →  `messages` webhook entry (text)
  confirmation_response →  postback payload CONFIRM:<request_id>:ACCEPT|DECLINE
  agent_status          →  sender_action typing_on (throttled refresh) / typing_off
  agent_note            →  text message (interim "I'll check that…" notes)
  chat_message          →  text message(s), chunked at 2000 chars
  display_product_carousel → generic template (cards + "View product" button)
  get_user_confirmation →  button template with Accept / Decline

Delivery model: the POST sink verifies the HMAC signature, deduplicates
Meta's at-least-once redeliveries, echoes our own sends (is_echo), then
enqueues every event onto a per-PSID queue processed sequentially by one
background drain — Meta users habitually multi-text, and ordering also makes
confirmation races impossible. The sink always responds 200 fast (Meta times
out around 20s; LLM turns take longer).

All in-memory maps share the web transport's single-process assumption.
"""

import hashlib
import hmac
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from flask import Blueprint, Response, current_app, request, url_for
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from sqlalchemy.exc import IntegrityError

from agent.graph import get_agent_graph, message_text
from agent.stats import TurnStatsCollector
from database.db_setup import SessionLocal
from database.models import Conversation, MessengerIdentity, Product, User, UserRole
# Shared runner helpers (same pattern as stats.py importing build_transcript).
from routes.chat import _final_assistant_message, _heal_thread
from utils.config import config
from utils.extensions import socketio
from utils.meta_client import messenger

webhook_bp = Blueprint("webhook", __name__)
logger = logging.getLogger(__name__)

_RECURSION_LIMIT = 30          # keep in sync with routes/chat.py
_MAX_AUTO_DECLINES = 5         # safety valve for stacked confirmations
_MAX_CAROUSEL_CARDS = 10       # Messenger generic template limit
_SEEN_MAX = 4096               # bounded dedup LRU (Meta redeliveries)

_LOCK = threading.Lock()
_SEEN_EVENTS: "OrderedDict[str, None]" = OrderedDict()
_QUEUES: Dict[str, List[Dict[str, Any]]] = {}     # psid → pending items
_BUSY_PSIDS: set = set()                          # psids with a running drain
_META_ACTIVE_USERS: set = set()                   # user ids with a running turn
_META_PENDING: Dict[str, Dict[str, Any]] = {}     # request_id → record
_META_PENDING_BY_PSID: Dict[str, str] = {}        # psid → request_id


# Small helpers

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _verify_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """X-Hub-Signature-256: sha256=HMAC_SHA256(app_secret, raw body)."""
    app_secret = config.meta_config.app_secret
    if not app_secret or not raw_body:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(
        expected.encode("utf-8"),
        signature_header[len("sha256="):].strip().encode("utf-8"))


def parse_postback_payload(payload: str) -> Optional[Dict[str, Any]]:
    """CONFIRM:<request_id>:ACCEPT|DECLINE → {"request_id", "accepted"}, else None."""
    if not payload.startswith("CONFIRM:"):
        return None
    parts = payload.split(":")
    if len(parts) != 3 or parts[2] not in ("ACCEPT", "DECLINE"):
        return None
    return {"request_id": parts[1], "accepted": parts[2] == "ACCEPT"}


def _mark_seen(key: str) -> bool:
    """True when key is new; False for Meta redeliveries. Bounded LRU."""
    with _LOCK:
        if key in _SEEN_EVENTS:
            return False
        _SEEN_EVENTS[key] = None
        if len(_SEEN_EVENTS) > _SEEN_MAX:
            _SEEN_EVENTS.popitem(last=False)
        return True


def _set_user_active(user_id: int) -> None:
    with _LOCK:
        _META_ACTIVE_USERS.add(user_id)


def _unset_user_active(user_id: int) -> None:
    with _LOCK:
        _META_ACTIVE_USERS.discard(user_id)


def _https_absolute(base: str, path: Optional[str]) -> Optional[str]:
    """Only https URLs are usable in Messenger templates/buttons."""
    if not path:
        return None
    if path.startswith("https://"):
        return path
    if base.startswith("https://") and path.startswith("/"):
        return base + path
    return None


class _MetaIo:
    """Per-turn Messenger output adapter: throttled typing indicator + text."""

    TYPING_REFRESH_SECONDS = 10.0   # the indicator auto-expires; refresh mid-turn

    def __init__(self, psid: str):
        self.psid = psid
        self._last_typing = 0.0

    def typing(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_typing < self.TYPING_REFRESH_SECONDS:
            return
        self._last_typing = now
        messenger.set_typing(self.psid, True)

    def typing_off(self) -> None:
        self._last_typing = 0.0
        messenger.set_typing(self.psid, False)

    def text(self, content: str) -> None:
        messenger.send_text(self.psid, content)


def _context(user: User, conversation: Conversation) -> Dict[str, Any]:
    return {"user_id": user.id, "user_name": user.name,
            "conversation_id": conversation.id, "thread_id": conversation.thread_id}


def _resolve_meta_user(psid: str):
    """
    → ("ok", context, is_new) | ("disabled", None, False) | ("error", None, False)

    Auto-provisions User + MessengerIdentity + Conversation on first contact.
    A later account-linking flow can re-point the identity row at a real
    customer without any migration.
    """
    for _ in range(2):
        with SessionLocal() as db:
            identity = (db.query(MessengerIdentity)
                        .filter(MessengerIdentity.psid == psid).first())
            if identity is not None:
                user = db.get(User, identity.user_id)
                if user is None:
                    # Dangling identity (user removed) — drop and re-provision.
                    db.delete(identity)
                    db.commit()
                    continue
                if not user.is_active:
                    return "disabled", None, False
                conversation = (db.query(Conversation)
                                .filter(Conversation.user_id == user.id)
                                .order_by(Conversation.updated_at.desc())
                                .first())
                if conversation is None:
                    conversation = Conversation(user_id=user.id, thread_id=uuid.uuid4().hex)
                    db.add(conversation)
                    db.commit()
                return "ok", _context(user, conversation), False

        # First contact: fetch the profile outside any DB session, then provision.
        profile = messenger.get_profile(psid)
        first = str(profile.get("first_name") or "").strip()
        last = str(profile.get("last_name") or "").strip()
        name = f"{first} {last}".strip() or "Messenger customer"
        try:
            with SessionLocal() as db:
                user = User(email=f"messenger+{psid}@noreply.invalid", name=name,
                            role=UserRole.CUSTOMER, is_active=True)
                db.add(user)
                db.flush()
                conversation = Conversation(user_id=user.id, thread_id=uuid.uuid4().hex)
                db.add(conversation)
                db.add(MessengerIdentity(psid=psid, user_id=user.id))
                db.commit()
                logger.info("Provisioned Messenger user %s for psid=%s.", user.email, psid)
                return "ok", _context(user, conversation), True
        except IntegrityError:
            # Lost a provisioning race (duplicate redelivery for a brand-new
            # PSID) — the next loop iteration re-queries and finds the winner.
            logger.info("Messenger provisioning race for psid=%s; re-querying.", psid)
            continue
        except Exception:
            logger.exception("Failed to provision a Messenger user for psid=%s.", psid)
            return "error", None, False
    return "error", None, False


def _register_meta_pending(psid: str, context: Dict[str, Any],
                           payload: Dict[str, Any]) -> Dict[str, Any]:
    now = _now()
    record = {
        "request_id": str(payload.get("request_id") or uuid.uuid4()),
        "psid": psid,
        "user_id": context["user_id"],
        "user_name": context["user_name"],
        "conversation_id": context["conversation_id"],
        "thread_id": context["thread_id"],
        "tool": payload.get("tool"),
        "args": payload.get("args") or {},
        "message": payload.get("message") or "Approve this action?",
        "created_at": now,
        "expires_at": now + timedelta(
            seconds=int(config.agent.confirmation_timeout_seconds)),
    }
    with _LOCK:
        stale = _META_PENDING_BY_PSID.get(psid)
        if stale:
            _META_PENDING.pop(stale, None)
        _META_PENDING[record["request_id"]] = record
        _META_PENDING_BY_PSID[psid] = record["request_id"]
    return record


def _pop_pending(psid: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        request_id = _META_PENDING_BY_PSID.pop(psid, None)
        if request_id is None:
            return None
        return _META_PENDING.pop(request_id, None)


def drop_meta_pending_confirmation(thread_id: str) -> Optional[Dict[str, Any]]:
    """
    Admin conversation-delete hook: drop a Messenger pending confirmation
    WITHOUT resuming — the parked thread is being deleted. Later button taps
    get a friendly "no longer active" reply instead of resuming a dead thread.
    """
    with _LOCK:
        request_id = None
        for rid, record in _META_PENDING.items():
            if record.get("thread_id") == thread_id:
                request_id = rid
                break
        if request_id is None:
            return None
        record = _META_PENDING.pop(request_id)
        if _META_PENDING_BY_PSID.get(record["psid"]) == request_id:
            _META_PENDING_BY_PSID.pop(record["psid"], None)
        return record


def meta_user_turn_active(user_id: Optional[int]) -> bool:
    """True while a Messenger turn/resume is executing for this user (admin guard)."""
    if user_id is None:
        return False
    with _LOCK:
        return user_id in _META_ACTIVE_USERS


def _emit_meta_event(app, io: _MetaIo, payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    kind = payload.get("type")
    if kind == "phase":
        io.typing()                                   # thinking / retrieving / …
    elif kind == "tool_status":
        if payload.get("state") in ("running", "awaiting_confirmation"):
            io.typing()
    elif kind == "agent_note":
        content = payload.get("content")
        if content:
            io.text(content)
    elif kind == "carousel":
        _send_meta_carousel(app, io, payload.get("product_ids") or [])
    # guard_blocked / other tool states have no Messenger representation.


def _carousel_element(base: str, product: Product, path: str) -> Dict[str, Any]:
    stock = int(product.stock_quantity or 0)
    stock_label = ("Out of stock" if stock <= 0
                   else f"Only {stock} left" if stock <= 5 else "In stock")
    element = {
        "title": (product.name or f"Product #{product.id}")[:80],
        "subtitle": f"${float(product.price):.2f} · {stock_label}"[:80],
    }
    image = _https_absolute(base, product.image_url)
    if image:
        element["image_url"] = image
    url = _https_absolute(base, path)
    if url:
        element["buttons"] = [{"type": "web_url", "url": url, "title": "View product"}]
    return element


def _send_meta_carousel(app, io: _MetaIo, product_ids: Any) -> None:
    ids: List[int] = []
    for raw in product_ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
        if len(ids) >= _MAX_CAROUSEL_CARDS:
            break
    if not ids:
        return
    base = (config.meta_config.public_base_url or "").rstrip("/")
    try:
        with app.app_context():                       # url_for needs a context
            with SessionLocal() as db:
                products = db.query(Product).filter(Product.id.in_(ids)).all()
                by_id = {p.id: p for p in products}
                elements = [
                    _carousel_element(base, by_id[pid],
                                      url_for("store.product_detail", product_id=pid))
                    for pid in ids if pid in by_id
                ]
        if elements:
            messenger.send_generic(io.psid, elements)
    except Exception:
        logger.exception("Failed to build/send a Messenger carousel.")


def _thread_is_paused(graph, thread_id: str) -> bool:
    """True while the thread is parked at an interrupt awaiting a resume."""
    try:
        snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
        if snapshot is None:
            return False
        if getattr(snapshot, "next", None):
            return True
        for task in (getattr(snapshot, "tasks", None) or {}).values():
            if getattr(task, "interrupts", None):
                return True
    except Exception:
        return False
    return False


def _finalize_meta_stats(collector: TurnStatsCollector, status: str,
                         thread_id: str) -> None:
    values: Dict[str, Any] = {}
    try:
        snapshot = get_agent_graph().get_state({"configurable": {"thread_id": thread_id}})
        values = (snapshot.values if snapshot else None) or {}
    except Exception:
        pass
    try:
        collector.finalize(status=status, values=values)
    except Exception:
        logger.debug("Messenger stats finalization failed.", exc_info=True)


def _touch_conversation(conversation_id: int) -> None:
    try:
        with SessionLocal() as db:
            conv = db.get(Conversation, conversation_id)
            if conv is not None:
                conv.updated_at = _now()
                db.commit()
    except Exception:
        logger.debug("Failed to touch conversation %s.", conversation_id, exc_info=True)


def _stream_meta_turn(app, io: _MetaIo, collector: TurnStatsCollector,
                      context: Dict[str, Any], graph_input: Any,
                      auto_decline: bool = False) -> str:
    """
    Run one graph input to completion or to the next confirmation interrupt.
    Returns "completed" | "interrupted" | "error". Mirrors routes/chat.py's
    _stream_turn, emitting through the Messenger adapter instead of Socket.IO.
    """
    graph = get_agent_graph()
    run_config = {"configurable": {"thread_id": context["thread_id"]},
                  "recursion_limit": _RECURSION_LIMIT}
    interrupt_payload = None
    try:
        for mode, chunk in graph.stream(graph_input, run_config,
                                        stream_mode=["custom", "updates"]):
            if mode == "custom":
                collector.note_custom(chunk)
                _emit_meta_event(app, io, chunk)
                continue
            if "__interrupt__" in chunk:
                interrupts = chunk.get("__interrupt__") or ()
                interrupt_payload = (getattr(interrupts[0], "value", None)
                                     if interrupts else None)
                break
            collector.note_update(chunk)
    except GraphRecursionError:
        logger.warning("Messenger turn hit the recursion limit (thread=%s).",
                       context["thread_id"])
        _heal_thread(context["thread_id"], "loop_limit",
                     "the turn exceeded the processing limit")
        io.text("That request got too complex to process here. Try a simpler version?")
        return "error"
    except Exception as exc:
        logger.exception("Messenger turn failed (thread=%s): %s",
                         context["thread_id"], exc)
        _heal_thread(context["thread_id"], "internal_error",
                     "the turn failed unexpectedly")
        io.text("I ran into an error while working on that. Please try again.")
        return "error"

    if interrupt_payload is not None:
        collector.note_interrupt(interrupt_payload)
        if auto_decline:
            return "interrupted"      # caller keeps resuming with False
        record = _register_meta_pending(io.psid, context, interrupt_payload)
        io.typing_off()
        messenger.send_confirmation(io.psid, record["message"], record["request_id"])
        return "interrupted"

    final = _final_assistant_message(graph, run_config)
    text = message_text(final) if final is not None else ""
    io.typing_off()
    io.text(text or "I couldn't produce a response for that. Please try rephrasing.")
    return "completed"


def _run_meta_message(app, psid: str, text: str) -> None:
    status, context, is_new = _resolve_meta_user(psid)
    if status == "disabled":
        messenger.send_text(psid, "This conversation has been disabled. Please contact support.")
        return
    if context is None:
        messenger.send_text(psid, "Sorry — something went wrong on our side. "
                                  "Please try again in a moment.")
        return

    if is_new and config.meta_config.welcome_new_users:
        first_name = context["user_name"].split(" ")[0]
        messenger.send_text(
            psid,
            f"Hi {first_name}! 👋 I'm {config.system_context.company_name}'s shopping "
            "assistant — ask me about products, your cart, or orders.")

    io = _MetaIo(psid)
    collector = TurnStatsCollector(context["user_id"], context["thread_id"],
                                   context["conversation_id"])
    _set_user_active(context["user_id"])
    try:
        # A pending confirmation is superseded by the new message: resume the
        # parked thread with a decline (bounded drain), then answer — the
        # agent produces its own "alright, I won't…" reply for the old thread.
        for _ in range(_MAX_AUTO_DECLINES):
            record = _pop_pending(psid)
            if record is None:
                break
            old = {"user_id": record["user_id"], "user_name": record["user_name"],
                   "conversation_id": record["conversation_id"],
                   "thread_id": record["thread_id"]}
            if _stream_meta_turn(app, io, collector, old,
                                 Command(resume=False),
                                 auto_decline=True) != "interrupted":
                break

        io.typing(force=True)
        human = HumanMessage(content=text, id=str(uuid.uuid4()))
        status = _stream_meta_turn(
            app, io, collector, context,
            {"messages": [human], "user_id": context["user_id"],
             "user_name": context["user_name"]})
    finally:
        _unset_user_active(context["user_id"])
    _finalize_meta_stats(collector, status, context["thread_id"])
    _touch_conversation(context["conversation_id"])


def _run_meta_resume(app, psid: str, context: Dict[str, Any], accepted: bool) -> None:
    """Continue a parked thread with the user's Accept/Decline (or a timeout)."""
    graph = get_agent_graph()
    if not _thread_is_paused(graph, context["thread_id"]):
        # Already resumed (raced a supersede/timeout sweep) or the thread was
        # deleted by an admin — nothing to do.
        logger.debug("Messenger resume skipped; thread %s is not paused.",
                     context["thread_id"])
        return
    io = _MetaIo(psid)
    io.typing(force=True)
    collector = TurnStatsCollector(context["user_id"], context["thread_id"],
                                   context["conversation_id"])
    _set_user_active(context["user_id"])
    try:
        status = _stream_meta_turn(app, io, collector, context,
                                   Command(resume=accepted))
    finally:
        _unset_user_active(context["user_id"])
    _finalize_meta_stats(collector, status, context["thread_id"])
    _touch_conversation(context["conversation_id"])


def _handle_confirm(app, psid: str, item: Dict[str, Any]) -> None:
    request_id = str(item.get("request_id") or "")
    with _LOCK:
        record = _META_PENDING.get(request_id)
        if record is not None and record.get("psid") == psid:
            _META_PENDING.pop(request_id, None)
            if _META_PENDING_BY_PSID.get(psid) == request_id:
                _META_PENDING_BY_PSID.pop(psid, None)
        else:
            record = None
    if record is None:
        # Duplicate tap, already-answered prompt, or deleted conversation.
        messenger.send_text(psid, "That request is no longer active.")
        return
    expired = _now() > record["expires_at"]
    accepted = bool(item.get("accepted")) and not expired
    context = {"user_id": record["user_id"], "user_name": record["user_name"],
               "conversation_id": record["conversation_id"],
               "thread_id": record["thread_id"]}
    _run_meta_resume(app, psid, context, accepted)


def _handle_new_chat(psid: str) -> None:
    status, context, _is_new = _resolve_meta_user(psid)
    if status == "disabled":
        messenger.send_text(psid, "This conversation has been disabled.")
        return
    if context is None:
        messenger.send_text(psid, "Sorry — something went wrong on our side.")
        return
    # Drop any pending confirmation without resuming: the parked thread is
    # abandoned (parked threads are inert in the checkpointer).
    _pop_pending(psid)
    try:
        with SessionLocal() as db:
            db.add(Conversation(user_id=context["user_id"], thread_id=uuid.uuid4().hex))
            db.commit()
    except Exception:
        logger.exception("Failed to start a new Messenger conversation for psid=%s.", psid)
        return
    messenger.send_text(psid, "Started a fresh conversation ✨")


def _enqueue(app, psid: str, item: Dict[str, Any]) -> None:
    with _LOCK:
        queue = _QUEUES.setdefault(psid, [])
        queue.append(item)
        should_start = psid not in _BUSY_PSIDS
        if should_start:
            _BUSY_PSIDS.add(psid)
    if should_start:
        socketio.start_background_task(_drain_psid, app, psid)


def _drain_psid(app, psid: str) -> None:
    """Serializes every event for one PSID; the lock guards queue ownership."""
    while True:
        with _LOCK:
            queue = _QUEUES.get(psid)
            item = queue[0] if queue else None
            if item is None:
                _QUEUES.pop(psid, None)
                _BUSY_PSIDS.discard(psid)
                return
            del queue[0]
        try:
            _process_item(app, psid, item)
        except Exception:
            logger.exception("Messenger queue: failed %r item for psid=%s.",
                             item.get("type"), psid)


def _process_item(app, psid: str, item: Dict[str, Any]) -> None:
    messenger.mark_seen(psid)
    kind = item.get("type")
    if kind == "message":
        _run_meta_message(app, psid, str(item.get("text") or ""))
    elif kind == "confirm":
        _handle_confirm(app, psid, item)
    elif kind == "resume":
        _run_meta_resume(app, psid, item.get("context") or {}, bool(item.get("accepted")))
    elif kind == "new_chat":
        _handle_new_chat(psid)
    elif kind == "attachment_note":
        messenger.send_text(
            psid, "I can't view images or files on Messenger just yet — "
                  "tell me what you're looking for and I'll help!")
    else:
        logger.debug("Messenger queue: ignoring unknown item type %r.", kind)


def _sweep_expired_pending(app) -> None:
    timeout = int(config.agent.confirmation_timeout_seconds)
    now = _now()
    to_resume = []
    with _LOCK:
        expired = [rid for rid, rec in _META_PENDING.items()
                   if (now - rec["created_at"]).total_seconds() > timeout]
        for rid in expired:
            record = _META_PENDING.pop(rid)
            if _META_PENDING_BY_PSID.get(record["psid"]) == rid:
                _META_PENDING_BY_PSID.pop(record["psid"], None)
            if record["psid"] in _BUSY_PSIDS:
                # Busy: keep it pending; the drain's supersede path or the
                # next sweep will settle it.
                _META_PENDING[rid] = record
                _META_PENDING_BY_PSID[record["psid"]] = rid
            else:
                to_resume.append(record)
    for record in to_resume:
        # Enqueued, not run inline, so it respects the PSID's ordering.
        context = {"user_id": record["user_id"], "user_name": record["user_name"],
                   "conversation_id": record["conversation_id"],
                   "thread_id": record["thread_id"]}
        _enqueue(app, record["psid"], {"type": "resume", "context": context,
                                       "accepted": False})


def _handle_event(app, entry_id: str, event: Dict[str, Any]) -> None:
    sender_id = str((event.get("sender") or {}).get("id") or "")
    if not sender_id:
        return

    message = event.get("message")
    postback = event.get("postback")

    if isinstance(message, dict):
        if message.get("is_echo"):
            return                       # our own sends echoing back — never process
        if not _mark_seen(f"{entry_id}:{message.get('mid', '')}"):
            return                       # duplicate delivery (Meta retries)
        text = str(message.get("text") or "").strip()
        if text:
            limit = int(config.agent.max_message_chars)
            # Text + attachment: the text is processed, the image ignored (v1).
            _enqueue(app, sender_id, {"type": "message", "text": text[:limit]})
        elif message.get("attachments"):
            _enqueue(app, sender_id, {"type": "attachment_note"})
        return

    if isinstance(postback, dict):
        payload = str(postback.get("payload") or "")
        if not _mark_seen(f"{entry_id}:pb:{event.get('timestamp', '')}:{payload}"):
            return
        parsed = parse_postback_payload(payload)
        if parsed is not None:
            _enqueue(app, sender_id, {"type": "confirm",
                                      "request_id": parsed["request_id"],
                                      "accepted": parsed["accepted"]})
            return
        if payload == "NEW_CHAT":
            _enqueue(app, sender_id, {"type": "new_chat"})
            return
        if payload == "GET_STARTED":
            # Let the agent greet naturally (also triggers provisioning+welcome).
            _enqueue(app, sender_id, {"type": "message", "text": "Hello"})
            return
        # Any other menu/button (e.g. a custom persistent-menu item): run its
        # title through the agent as if the user typed it.
        title = str(postback.get("title") or "").strip()
        if title:
            limit = int(config.agent.max_message_chars)
            _enqueue(app, sender_id, {"type": "message", "text": title[:limit]})
        return
    # delivery / read / reaction / referral events: acknowledged, ignored.


def _handle_entry(app, entry: Any) -> None:
    if not isinstance(entry, dict):
        return
    entry_id = str(entry.get("id") or "")
    events = entry.get("messaging")
    if not isinstance(events, list):
        return
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            _handle_event(app, entry_id, event)
        except Exception:
            logger.exception("Failed to dispatch a Messenger event.")


def setup_page_profile() -> bool:
    """Get Started button + persistent menu. Idempotent — safe to re-run."""
    return messenger.setup_messenger_profile({
        "get_started": {"payload": "GET_STARTED"},
        "persistent_menu": [{
            "locale": "default",
            "composer_input_disabled": False,
            "call_to_actions": [
                {"type": "postback", "title": "Start new chat", "payload": "NEW_CHAT"},
            ],
        }],
    })


@webhook_bp.route("/webhook", methods=["GET"])
def verify():
    """Meta's one-time webhook verification handshake."""
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    expected = config.meta_config.verify_token
    if (mode == "subscribe" and expected and challenge
            and hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))):
        # text/plain so the echoed challenge is never interpreted as HTML
        return Response(challenge, mimetype="text/plain")
    logger.warning("Webhook verification failed (hub.mode=%r).", mode)
    return "Forbidden", 403


@webhook_bp.route("/webhook", methods=["POST"])
def receive():
    """
    Meta's event sink. Signature-verified, deduplicated, dispatched to
    per-PSID background queues; always acknowledges fast (Meta times out
    around 20s and retries non-200s — our agent turns take longer).
    """
    raw = request.get_data()
    if not _verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        logger.warning("Webhook POST rejected: invalid or missing signature.")
        return "Forbidden", 403
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return "Bad Request", 400
    if not isinstance(payload, dict) or payload.get("object") != "page":
        return "ok", 200            # not a Page event — acknowledge & ignore

    app = current_app._get_current_object()
    _sweep_expired_pending(app)
    entries = payload.get("entry")
    if isinstance(entries, list):
        for entry in entries:
            _handle_entry(app, entry)
    return "ok", 200