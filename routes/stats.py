"""
Admin stats & conversation-management API (dashboard + playground backend).

    GET    /admin/stats/overview                KPI totals + 24h/7d deltas
    GET    /admin/stats/tokens?period=&limit=   token series (day/week/month)
    GET    /admin/stats/activity?period=&limit= turns/tools/checkouts series
    GET    /admin/stats/tools?days=&limit=      tool popularity table
    GET    /admin/stats/checkouts?period=       checkout + revenue series
    GET    /admin/stats/checkouts/list          full-length ledger (paged)
    GET    /admin/conversations                 list + per-conversation stats
    GET    /admin/conversations/<id>            turns + tool calls detail
    GET    /admin/conversations/<id>/transcript full message replay
    DELETE /admin/conversations/<id>            delete thread (checkpointer +
                                                SQL stats cascade; orders stay)

Turn semantics: a confirmation-paused turn writes an 'interrupted' row plus a
terminal row on resume — token sums count every row, turn counts exclude
'interrupted'. All buckets/timestamps are UTC; the UI formats locally.

Mutating endpoints (DELETE) require the X-CSRFToken header.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, render_template, request
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import joinedload, selectinload

from agent.checkpointer import get_checkpointer
from database.db_setup import SessionLocal
from database.models import (
    AgentToolCall,
    AgentTurnStats,
    Conversation,
    Order,
    OrderStatus,
    User,
)
from utils.auth import admin_required
from routes.webhook import drop_meta_pending_confirmation, meta_user_turn_active
from routes.chat import (
    build_transcript,
    drop_pending_confirmation,
    user_turn_active,
)
from utils.extensions import limiter, socketio
from utils.pagination import get_pagination
from utils.sanitizers import escape_like

stats_bp = Blueprint('stats', __name__)
logger = logging.getLogger(__name__)

PERIODS = ("day", "week", "month")


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _parse_period_limit() -> Tuple[str, int]:
    period = (request.args.get("period") or "day").strip().lower()
    if period not in PERIODS:
        period = "day"
    limit = request.args.get("limit", 30, type=int)
    if limit is None:
        limit = 30
    return period, min(max(limit, 1), 366)


def _month_shift(d: date, months: int) -> date:
    """First day of the month `months` away from d's month."""
    total = d.year * 12 + (d.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def _window_start(period: str, limit: int) -> date:
    today = datetime.now(timezone.utc).date()
    if period == "week":
        monday = today - timedelta(days=today.weekday())
        return monday - timedelta(weeks=limit - 1)
    if period == "month":
        return _month_shift(today, -(limit - 1))
    return today - timedelta(days=limit - 1)


def _bucket_ranges(period: str, limit: int) -> List[Tuple[date, date]]:
    today = datetime.now(timezone.utc).date()
    if period == "day":
        days = [today - timedelta(days=i) for i in range(limit - 1, -1, -1)]
        return [(d, d) for d in days]
    if period == "week":      # Monday-start weeks
        monday = today - timedelta(days=today.weekday())
        weeks = [monday - timedelta(weeks=i) for i in range(limit - 1, -1, -1)]
        return [(w, w + timedelta(days=6)) for w in weeks]
    months = [_month_shift(today, -i) for i in range(limit - 1, -1, -1)]
    return [(m, _month_shift(m, 1) - timedelta(days=1)) for m in months]


def _roll(period: str, limit: int, daily: Dict[str, Dict[str, Any]],
          keys: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """Roll daily aggregates into continuous, zero-filled period buckets."""
    buckets: List[Dict[str, Any]] = []
    for start, end in _bucket_ranges(period, limit):
        acc = {k: 0 for k in keys}
        day = start
        while day <= end:
            for k, v in (daily.get(day.isoformat()) or {}).items():
                if k in acc:
                    acc[k] += v
            day += timedelta(days=1)
        buckets.append({"bucket_start": start.isoformat(), **acc})
    return buckets


def _window_stats(db, since: Optional[datetime]) -> Dict[str, Any]:
    """Aggregate block for a time window (or all time when since is None)."""
    turn_q = db.query(
        func.count(case((AgentTurnStats.status != "interrupted", 1), else_=None)).label("turns"),
        func.count(case((AgentTurnStats.status == "error", 1), else_=None)).label("errored_turns"),
        func.count(case((AgentTurnStats.guard_blocked.is_(True), 1), else_=None)).label("guard_blocks"),
        func.coalesce(func.sum(AgentTurnStats.tokens_in), 0).label("tokens_in"),
        func.coalesce(func.sum(AgentTurnStats.tokens_out), 0).label("tokens_out"),
    )
    tool_q = db.query(
        func.count(AgentToolCall.id).label("tool_calls"),
        func.count(case(
            (and_(AgentToolCall.tool_name == "checkout",
                  AgentToolCall.state == "done"), 1),
            else_=None)).label("chat_checkouts"),
    )
    if since is not None:
        turn_q = turn_q.filter(AgentTurnStats.started_at >= since)
        tool_q = tool_q.filter(AgentToolCall.created_at >= since)

    t = turn_q.one()
    c = tool_q.one()

    # Revenue over DISTINCT checked-out orders (one tool row per order, but
    # distinct-grouped so a duplicated row could never double-count).
    order_sq = (
        db.query(AgentToolCall.order_id.label("oid"),
                 func.max(AgentToolCall.created_at).label("ts"))
        .filter(AgentToolCall.tool_name == "checkout",
                AgentToolCall.state == "done",
                AgentToolCall.order_id.isnot(None))
        .group_by(AgentToolCall.order_id)
    )
    if since is not None:
        order_sq = order_sq.having(func.max(AgentToolCall.created_at) >= since)
    order_sq = order_sq.subquery()
    revenue = (db.query(func.coalesce(func.sum(Order.total_amount), 0))
               .select_from(Order)
               .join(order_sq, order_sq.c.oid == Order.id)
               .scalar())

    return {
        "turns": int(t.turns or 0),
        "errored_turns": int(t.errored_turns or 0),
        "guard_blocks": int(t.guard_blocks or 0),
        "tokens_in": int(t.tokens_in or 0),
        "tokens_out": int(t.tokens_out or 0),
        "tool_calls": int(c.tool_calls or 0),
        "chat_checkouts": int(c.chat_checkouts or 0),
        "chat_revenue": round(float(revenue or 0), 2),
    }


@stats_bp.route('/conversations')
@limiter.limit("60 per minute")
@admin_required
def conversations_page():
    """Browseable conversations management page (thin shell; the table,
    filters and drawer are client-rendered from the JSON API)."""
    return render_template('admin/conversations.html')


@stats_bp.route('/stats/overview')
@limiter.limit("60 per minute")
@admin_required
def stats_overview():
    """Dashboard header tiles: totals plus 24h/7d deltas."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        totals = _window_stats(db, None)
        totals["conversations"] = int(
            db.query(func.count(Conversation.id)).scalar() or 0)

        last_24h = _window_stats(db, now - timedelta(hours=24))
        last_24h["conversations"] = int(
            db.query(func.count(Conversation.id))
            .filter(Conversation.created_at >= now - timedelta(hours=24))
            .scalar() or 0)

        last_7d = _window_stats(db, now - timedelta(days=7))
        last_7d["conversations"] = int(
            db.query(func.count(Conversation.id))
            .filter(Conversation.created_at >= now - timedelta(days=7))
            .scalar() or 0)

    return jsonify({"totals": totals, "last_24h": last_24h, "last_7d": last_7d})


@stats_bp.route('/stats/tokens')
@limiter.limit("60 per minute")
@admin_required
def stats_tokens():
    """Token usage over time: [{bucket_start, tokens_in, tokens_out, turns}]."""
    period, limit = _parse_period_limit()
    window_start = _window_start(period, limit)
    with SessionLocal() as db:
        rows = (
            db.query(
                func.date(AgentTurnStats.started_at).label("day"),
                func.coalesce(func.sum(AgentTurnStats.tokens_in), 0).label("tokens_in"),
                func.coalesce(func.sum(AgentTurnStats.tokens_out), 0).label("tokens_out"),
                func.count(case((AgentTurnStats.status != "interrupted", 1), else_=None)).label("turns"),
            )
            .filter(func.date(AgentTurnStats.started_at) >= window_start.isoformat())
            .group_by(func.date(AgentTurnStats.started_at))
            .order_by(func.date(AgentTurnStats.started_at))
            .all()
        )
    daily = {str(r.day): {"tokens_in": int(r.tokens_in),
                          "tokens_out": int(r.tokens_out),
                          "turns": int(r.turns)} for r in rows}
    return jsonify({
        "period": period, "limit": limit,
        "buckets": _roll(period, limit, daily,
                        ("tokens_in", "tokens_out", "turns")),
    })


@stats_bp.route('/stats/activity')
@limiter.limit("60 per minute")
@admin_required
def stats_activity():
    """Activity over time: [{bucket_start, turns, guard_blocks, tool_calls,
    chat_checkouts}]."""
    period, limit = _parse_period_limit()
    window_start = _window_start(period, limit)
    with SessionLocal() as db:
        turn_rows = (
            db.query(
                func.date(AgentTurnStats.started_at).label("day"),
                func.count(case((AgentTurnStats.status != "interrupted", 1), else_=None)).label("turns"),
                func.count(case((AgentTurnStats.guard_blocked.is_(True), 1), else_=None)).label("guard_blocks"),
            )
            .filter(func.date(AgentTurnStats.started_at) >= window_start.isoformat())
            .group_by(func.date(AgentTurnStats.started_at))
            .all()
        )
        tool_rows = (
            db.query(
                func.date(AgentToolCall.created_at).label("day"),
                func.count(AgentToolCall.id).label("tool_calls"),
                func.count(case(
                    (and_(AgentToolCall.tool_name == "checkout",
                          AgentToolCall.state == "done"), 1),
                    else_=None)).label("chat_checkouts"),
            )
            .filter(func.date(AgentToolCall.created_at) >= window_start.isoformat())
            .group_by(func.date(AgentToolCall.created_at))
            .all()
        )
    daily: Dict[str, Dict[str, int]] = {}
    for r in turn_rows:
        daily.setdefault(str(r.day), {}).update(
            {"turns": int(r.turns), "guard_blocks": int(r.guard_blocks)})
    for r in tool_rows:
        daily.setdefault(str(r.day), {}).update(
            {"tool_calls": int(r.tool_calls), "chat_checkouts": int(r.chat_checkouts)})
    return jsonify({
        "period": period, "limit": limit,
        "buckets": _roll(period, limit, daily,
                         ("turns", "guard_blocks", "tool_calls", "chat_checkouts")),
    })


@stats_bp.route('/stats/checkouts')
@limiter.limit("60 per minute")
@admin_required
def stats_checkouts():
    """Chat checkouts + revenue over time: [{bucket_start, chat_checkouts, revenue}]."""
    period, limit = _parse_period_limit()
    window_start = _window_start(period, limit)
    with SessionLocal() as db:
        rows = (
            db.query(
                func.date(AgentToolCall.created_at).label("day"),
                func.count(AgentToolCall.id).label("chat_checkouts"),
                func.coalesce(func.sum(Order.total_amount), 0).label("revenue"),
            )
            .select_from(AgentToolCall)
            .join(Order, Order.id == AgentToolCall.order_id)
            .filter(
                AgentToolCall.tool_name == "checkout",
                AgentToolCall.state == "done",
                AgentToolCall.order_id.isnot(None),
                func.date(AgentToolCall.created_at) >= window_start.isoformat(),
            )
            .group_by(func.date(AgentToolCall.created_at))
            .order_by(func.date(AgentToolCall.created_at))
            .all()
        )
    daily = {str(r.day): {"chat_checkouts": int(r.chat_checkouts),
                          "revenue": round(float(r.revenue), 2)} for r in rows}
    return jsonify({
        "period": period, "limit": limit,
        "buckets": _roll(period, limit, daily, ("chat_checkouts", "revenue")),
    })


@stats_bp.route('/stats/tools')
@limiter.limit("60 per minute")
@admin_required
def stats_tools():
    """Tool popularity: calls, outcomes, average duration, share of total."""
    days = request.args.get('days', type=int)
    limit = request.args.get('limit', 50, type=int)
    if limit is None:
        limit = 50
    limit = min(max(limit, 1), 100)
    cutoff = None
    if days is not None and days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    with SessionLocal() as db:
        q = db.query(
            AgentToolCall.tool_name.label("tool_name"),
            func.count().label("calls"),
            func.count(case((AgentToolCall.state == "done", 1), else_=None)).label("done"),
            func.count(case((AgentToolCall.state == "error", 1), else_=None)).label("errors"),
            func.count(case((AgentToolCall.state == "declined", 1), else_=None)).label("declined"),
            func.count(case((AgentToolCall.state == "blocked", 1), else_=None)).label("blocked"),
            func.avg(case((AgentToolCall.state == "done", AgentToolCall.duration_ms),
                          else_=None)).label("avg_duration_ms"),
        )
        total_q = db.query(func.count(AgentToolCall.id))
        if cutoff is not None:
            q = q.filter(AgentToolCall.created_at >= cutoff)
            total_q = total_q.filter(AgentToolCall.created_at >= cutoff)
        rows = (q.group_by(AgentToolCall.tool_name)
                .order_by(func.count().desc())
                .limit(limit).all())
        total_calls = int(total_q.scalar() or 0)

    tools = []
    for r in rows:
        calls = int(r.calls)
        tools.append({
            "tool_name": r.tool_name,
            "calls": calls,
            "done": int(r.done),
            "errors": int(r.errors),
            "declined": int(r.declined),
            "blocked": int(r.blocked),
            "avg_duration_ms": (round(float(r.avg_duration_ms))
                                if r.avg_duration_ms is not None else None),
            "share_pct": round(calls * 100.0 / total_calls, 1) if total_calls else 0.0,
        })
    return jsonify({"window_days": days if cutoff is not None else None,
                    "total_calls": total_calls, "tools": tools})


@stats_bp.route('/stats/checkouts/list')
@limiter.limit("60 per minute")
@admin_required
def stats_checkout_list():
    """Full-length ledger of checkouts performed in chat."""
    page = max(request.args.get('page', 1, type=int), 1)
    per_page = request.args.get('per_page', 20, type=int)
    if per_page is None:
        per_page = 20
    per_page = min(max(per_page, 1), 100)
    include_cancelled = ((request.args.get('include_cancelled', 'true').strip().lower()
                          in ('1', 'true', 'yes', 'on')))

    with SessionLocal() as db:
        query = (
            db.query(AgentToolCall, Order, User)
            .select_from(AgentToolCall)
            .join(Order, Order.id == AgentToolCall.order_id)
            .outerjoin(User, User.id == AgentToolCall.user_id)
            .filter(AgentToolCall.tool_name == "checkout",
                    AgentToolCall.state == "done",
                    AgentToolCall.order_id.isnot(None))
        )
        if not include_cancelled:
            query = query.filter(Order.status != OrderStatus.CANCELLED)
        query = query.order_by(AgentToolCall.created_at.desc())
        rows, total, total_pages = get_pagination(query, page, per_page=per_page)

        checkouts = [{
            "created_at": _iso(tc.created_at),
            "user": ({"id": u.id, "name": u.name, "email": u.email}
                     if u is not None else None),
            "conversation_id": tc.conversation_id,
            "order_id": order.id,
            "order_total": float(order.total_amount or 0),
            "order_status": (order.status.value if hasattr(order.status, "value")
                             else str(order.status)),
            "attempts": tc.attempts,
            "duration_ms": tc.duration_ms,
        } for tc, order, u in rows]

    return jsonify({"checkouts": checkouts, "page": page, "total": total,
                    "total_pages": total_pages,
                    "include_cancelled": include_cancelled})


@stats_bp.route('/conversations/list')
@limiter.limit("60 per minute")
@admin_required
def list_conversations():
    """Conversations with per-thread aggregates, newest activity first."""
    page = max(request.args.get('page', 1, type=int), 1)
    per_page = request.args.get('per_page', 20, type=int)
    if per_page is None:
        per_page = 20
    per_page = min(max(per_page, 1), 100)
    user_id = request.args.get('user_id', type=int)
    q_search = (request.args.get('q') or '').strip()

    with SessionLocal() as db:
        turns_sub = (
            db.query(
                AgentTurnStats.conversation_id.label("cid"),
                func.count(case((AgentTurnStats.status != "interrupted", 1),
                                else_=None)).label("turns"),
                func.coalesce(func.sum(AgentTurnStats.tokens_in), 0).label("tokens_in"),
                func.coalesce(func.sum(AgentTurnStats.tokens_out), 0).label("tokens_out"),
            ).group_by(AgentTurnStats.conversation_id).subquery()
        )
        tools_sub = (
            db.query(
                AgentToolCall.conversation_id.label("cid"),
                func.count().label("tool_calls"),
                func.count(case(
                    (and_(AgentToolCall.tool_name == "checkout",
                          AgentToolCall.state == "done"), 1),
                    else_=None)).label("chat_checkouts"),
            ).group_by(AgentToolCall.conversation_id).subquery()
        )
        last_intent_sq = (
            db.query(AgentTurnStats.intent)
            .filter(AgentTurnStats.conversation_id == Conversation.id)
            .order_by(AgentTurnStats.started_at.desc())
            .limit(1)
            .correlate(Conversation)
            .scalar_subquery()
        )

        query = (
            db.query(Conversation, User,
                     turns_sub.c.turns, turns_sub.c.tokens_in, turns_sub.c.tokens_out,
                     tools_sub.c.tool_calls, tools_sub.c.chat_checkouts,
                     last_intent_sq)
            .outerjoin(User, Conversation.user_id == User.id)
            .outerjoin(turns_sub, turns_sub.c.cid == Conversation.id)
            .outerjoin(tools_sub, tools_sub.c.cid == Conversation.id)
        )
        if user_id is not None:
            query = query.filter(Conversation.user_id == user_id)
        if q_search:
            escaped = escape_like(q_search)
            query = query.filter(or_(
                User.name.ilike(f"%{escaped}%", escape="\\"),
                User.email.ilike(f"%{escaped}%", escape="\\"),
            ))
        query = query.order_by(Conversation.updated_at.desc())
        rows, total, total_pages = get_pagination(query, page, per_page=per_page)

        conversations = [{
            "id": conv.id,
            "thread_id": conv.thread_id,
            "user": ({"id": u.id, "name": u.name, "email": u.email}
                     if u is not None else None),
            "created_at": _iso(conv.created_at),
            "updated_at": _iso(conv.updated_at),
            "turns": int(turns or 0),
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "tool_calls": int(tool_calls or 0),
            "chat_checkouts": int(chat_checkouts or 0),
            "last_intent": last_intent,
        } for conv, u, turns, tokens_in, tokens_out, tool_calls, chat_checkouts,
             last_intent in rows]

    return jsonify({"conversations": conversations, "page": page,
                    "total": total, "total_pages": total_pages})


def _serialize_conversation(conv: Conversation, turns: List[AgentTurnStats]) -> Dict[str, Any]:
    tool_call_rows = [c for t in turns for c in t.tool_calls]
    return {
        "conversation": {
            "id": conv.id,
            "thread_id": conv.thread_id,
            "user": ({"id": conv.user.id, "name": conv.user.name, "email": conv.user.email}
                     if conv.user is not None else None),
            "created_at": _iso(conv.created_at),
            "updated_at": _iso(conv.updated_at),
        },
        "totals": {
            "turns": sum(1 for t in turns if t.status != "interrupted"),
            "interrupted_turns": sum(1 for t in turns if t.status == "interrupted"),
            "errored_turns": sum(1 for t in turns if t.status == "error"),
            "guard_blocks": sum(1 for t in turns if t.guard_blocked),
            "tokens_in": sum(t.tokens_in for t in turns),
            "tokens_out": sum(t.tokens_out for t in turns),
            "llm_calls": sum(t.llm_calls for t in turns),
            "tool_calls": len(tool_call_rows),
            "chat_checkouts": sum(1 for c in tool_call_rows
                                  if c.tool_name == "checkout" and c.state == "done"),
        },
        "turns": [{
            "id": t.id,
            "started_at": _iso(t.started_at),
            "finished_at": _iso(t.finished_at),
            "status": t.status,
            "intent": t.intent,
            "guard_blocked": t.guard_blocked,
            "tokens_in": t.tokens_in,
            "tokens_out": t.tokens_out,
            "llm_calls": t.llm_calls,
            "error": t.error,
            "tool_calls": [{
                "tool_name": c.tool_name,
                "state": c.state,
                "duration_ms": c.duration_ms,
                "attempts": c.attempts,
                "order_id": c.order_id,
                "created_at": _iso(c.created_at),
            } for c in t.tool_calls],
        } for t in turns],
    }


@stats_bp.route('/conversations/<int:conversation_id>')
@limiter.limit("60 per minute")
@admin_required
def conversation_detail(conversation_id):
    """Turn-level execution history for one conversation."""
    with SessionLocal() as db:
        conv = (db.query(Conversation).options(joinedload(Conversation.user))
                .filter(Conversation.id == conversation_id).first())
        if conv is None:
            return jsonify({"error": "Conversation not found."}), 404
        turns = (db.query(AgentTurnStats)
                 .options(selectinload(AgentTurnStats.tool_calls))
                 .filter(AgentTurnStats.conversation_id == conversation_id)
                 .order_by(AgentTurnStats.started_at.asc(), AgentTurnStats.id.asc())
                 .all())
        payload = _serialize_conversation(conv, turns)
    return jsonify(payload)


@stats_bp.route('/conversations/<int:conversation_id>/transcript')
@limiter.limit("60 per minute")
@admin_required
def conversation_transcript(conversation_id):
    """Full message replay for any user's conversation (read-only)."""
    with SessionLocal() as db:
        conv = (db.query(Conversation).options(joinedload(Conversation.user))
                .filter(Conversation.id == conversation_id).first())
        if conv is None:
            return jsonify({"error": "Conversation not found."}), 404
        meta = {
            "id": conv.id,
            "thread_id": conv.thread_id,
            "user": ({"id": conv.user.id, "name": conv.user.name, "email": conv.user.email}
                     if conv.user is not None else None),
            "created_at": _iso(conv.created_at),
            "updated_at": _iso(conv.updated_at),
        }
        thread_id = conv.thread_id

    try:
        transcript = build_transcript(thread_id)
    except Exception:
        logger.exception("Failed to build transcript for conversation %s",
                         conversation_id)
        return jsonify({"error": "Could not load the transcript."}), 500

    return jsonify({"conversation": meta, **transcript})


@stats_bp.route('/conversations/<int:conversation_id>', methods=['DELETE'])
@limiter.limit("30 per minute")
@admin_required
def delete_conversation(conversation_id):
    """
    Delete a conversation: pending confirmation (dropped, never resumed),
    checkpointer thread state, SQL rows (stats cascade with the thread;
    store orders are never touched).
    """
    with SessionLocal() as db:
        conv = (db.query(Conversation).options(joinedload(Conversation.user))
                .filter(Conversation.id == conversation_id).first())
        if conv is None:
            return jsonify({"error": "Conversation not found."}), 404
        thread_id = conv.thread_id
        owner_id = conv.user_id

    if user_turn_active(owner_id) or meta_user_turn_active(owner_id):
        return jsonify({"error": "This user has a chat turn in flight. "
                                 "Try again in a few seconds."}), 409

    pending = drop_pending_confirmation(thread_id)
    if pending is not None:
        _emit_to_user(pending.get("user_id"), "confirmation_resolved", {
            "request_id": pending.get("request_id"),
            "accepted": False,
            "reason": "deleted",
            "conversation_id": conversation_id,
        })

    drop_meta_pending_confirmation(thread_id)

    try:
        _delete_checkpointer_thread(get_checkpointer(), thread_id)
    except Exception:
        logger.exception("Failed to delete checkpointer thread for "
                         "conversation %s", conversation_id)
        return jsonify({"error": "Failed to delete the conversation's "
                                 "saved state."}), 500

    try:
        with SessionLocal() as db:
            tool_calls_removed = (
                db.query(AgentToolCall)
                .filter(AgentToolCall.conversation_id == conversation_id)
                .delete(synchronize_session=False))
            turns_removed = (
                db.query(AgentTurnStats)
                .filter(AgentTurnStats.conversation_id == conversation_id)
                .delete(synchronize_session=False))
            conv = db.get(Conversation, conversation_id)
            if conv is not None:
                db.delete(conv)
            db.commit()
    except Exception:
        logger.exception("Failed to delete conversation %s", conversation_id)
        return jsonify({"error": "Failed to delete the conversation."}), 500

    logger.info("Conversation %s (thread=%s) deleted by admin; %d turn(s), "
                "%d tool call(s) removed.",
                conversation_id, thread_id, turns_removed, tool_calls_removed)
    return jsonify({"deleted": True,
                    "conversation_id": conversation_id,
                    "turns_removed": int(turns_removed or 0),
                    "tool_calls_removed": int(tool_calls_removed or 0)})


def _emit_to_user(user_id: Optional[int], event: str,
                  payload: Dict[str, Any]) -> None:
    if user_id is None:
        return
    try:
        socketio.emit(event, payload, to=f"user_{user_id}")
    except Exception:
        logger.debug("Socket emit failed (%s).", event, exc_info=True)


def _delete_checkpointer_thread(checkpointer, thread_id: str) -> None:
    """Delete a thread's checkpoints — sync API first, asyncio fallback."""
    sync_delete = getattr(checkpointer, "delete_thread", None)
    if callable(sync_delete):
        sync_delete(thread_id)
        return
    async_delete = getattr(checkpointer, "adelete_thread", None)
    if callable(async_delete):
        import asyncio
        asyncio.run(async_delete(thread_id))
        return
    raise RuntimeError("The configured checkpointer does not support "
                       "thread deletion.")