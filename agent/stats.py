"""In-memory turn statistics — the hook the admin dashboard will consume.

Process-local by design (threading async mode, single worker): the registry
keeps the last N turns per user plus global counters. Swap for a persistent
store when the dashboard lands; TurnStats is already a stable shape.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional
import logging


logger = logging.getLogger(__name__)


@dataclass
class TurnStats:
    user_id: int
    thread_id: str
    conversation_id: Optional[int]
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: str = "running"           # running | completed | interrupted | error
    intent: Optional[str] = None
    guard_blocked: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0                # assistant turns in the transcript
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    carousels: int = 0
    agent_notes: int = 0
    superseded_confirmations: int = 0
    error: Optional[str] = None


class TurnStatsCollector:
    """Fed by the turn runner from graph stream chunks; owns one TurnStats."""

    def __init__(self, user_id: int, thread_id: str,
                 conversation_id: Optional[int] = None):
        self.stats = TurnStats(user_id=user_id, thread_id=thread_id,
                               conversation_id=conversation_id,
                               started_at=datetime.now(timezone.utc))

    def note_custom(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "tool_status" and payload.get("state") in (
                "done", "error", "declined", "blocked"):
            try:
                attempts = int(payload.get("attempt") or 1)
            except (TypeError, ValueError):
                attempts = 1
            self.stats.tool_calls.append({
                "tool": payload.get("tool"),
                "state": payload.get("state"),
                "duration_ms": payload.get("duration_ms"),
                "attempts": attempts,
                "order_id": payload.get("order_id"),   # successful chat checkouts
            })
        elif kind == "carousel":
            self.stats.carousels += 1
        elif kind == "agent_note":
            self.stats.agent_notes += 1

    def note_update(self, chunk: Any) -> None:
        """updates-mode chunk: {node_name: state_delta | None}."""
        if not isinstance(chunk, dict):
            return
        for delta in chunk.values():
            if not isinstance(delta, dict):
                continue
            for m in delta.get("messages") or []:
                if getattr(m, "type", "") == "ai":
                    self.stats.llm_calls += 1
                    usage = getattr(m, "usage_metadata", None) or {}
                    try:
                        self.stats.tokens_in += int(usage.get("input_tokens") or 0)
                        self.stats.tokens_out += int(usage.get("output_tokens") or 0)
                    except (TypeError, ValueError):
                        pass

    def note_interrupt(self, payload: Any) -> None:
        self.stats.status = "interrupted"

    def note_superseded(self) -> None:
        self.stats.superseded_confirmations += 1

    def persist(self) -> None:
        """
        Best-effort SQL persistence (agent_turn_stats + agent_tool_calls).
        Owns its session and never raises — stats must not break a chat turn.
        Skipped when the conversation was deleted mid-turn, so a racing
        admin deletion can never leave orphaned stat rows behind.
        """
        stats = self.stats
        if stats.conversation_id is None:
            return
        try:
            from database.db_setup import SessionLocal
            from database.models import (
                AgentToolCall as ToolCallRow,
                AgentTurnStats as TurnRow,
                Conversation,
            )

            with SessionLocal() as db:
                if db.get(Conversation, stats.conversation_id) is None:
                    return

                error_text = (stats.error or "").strip() or None
                if error_text:
                    error_text = error_text[:500]

                turn_row = TurnRow(
                    conversation_id=stats.conversation_id,
                    user_id=stats.user_id,
                    thread_id=stats.thread_id,
                    started_at=stats.started_at,
                    finished_at=stats.finished_at,
                    status=str(stats.status or "error")[:20],
                    intent=(stats.intent[:50] if stats.intent else None),
                    guard_blocked=bool(stats.guard_blocked),
                    tokens_in=int(stats.tokens_in),
                    tokens_out=int(stats.tokens_out),
                    llm_calls=int(stats.llm_calls),
                    error=error_text,
                )
                db.add(turn_row)

                for call in stats.tool_calls:
                    duration = call.get("duration_ms")
                    try:
                        duration = int(duration) if duration is not None else None
                    except (TypeError, ValueError):
                        duration = None
                    order_id = call.get("order_id")
                    try:
                        order_id = int(order_id) if order_id is not None else None
                    except (TypeError, ValueError):
                        order_id = None
                    db.add(ToolCallRow(
                        turn=turn_row,
                        conversation_id=stats.conversation_id,
                        user_id=stats.user_id,
                        tool_name=str(call.get("tool") or "unknown")[:100],
                        state=str(call.get("state") or "done")[:20],
                        duration_ms=duration,
                        attempts=int(call.get("attempts") or 1),
                        order_id=order_id,
                    ))
                db.commit()
        except Exception:
            logger.warning("Failed to persist turn stats (thread=%s, status=%s).",
                           stats.thread_id, stats.status, exc_info=True)

    def finalize(self, status: str, values: Optional[Dict[str, Any]] = None,
                 error: Optional[str] = None) -> TurnStats:
        self.stats.status = status
        self.stats.error = error
        self.stats.finished_at = datetime.now(timezone.utc)
        if values:
            self.stats.intent = values.get("intent")
            verdict = values.get("guard_verdict") or {}
            self.stats.guard_blocked = bool(verdict.get("blocked"))
        self.persist()                       # SQL first (dashboard, full length)
        get_stats_registry().add(self.stats)  # in-memory tail (live panels)
        return self.stats


class StatsRegistry:
    _PER_USER = 50

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_user: Dict[int, Deque[TurnStats]] = {}
        self._totals: Dict[str, int] = {
            "turns": 0, "completed": 0, "interrupted": 0, "errors": 0,
            "tokens_in": 0, "tokens_out": 0, "llm_calls": 0,
            "tool_calls": 0, "guard_blocks": 0, "carousels": 0,
        }

    def add(self, stats: TurnStats) -> None:
        with self._lock:
            history = self._by_user.setdefault(
                stats.user_id, deque(maxlen=self._PER_USER))
            history.append(stats)
            totals = self._totals
            totals["turns"] += 1
            if stats.status == "completed":
                totals["completed"] += 1
            elif stats.status == "interrupted":
                totals["interrupted"] += 1
            else:
                totals["errors"] += 1
            totals["tokens_in"] += stats.tokens_in
            totals["tokens_out"] += stats.tokens_out
            totals["llm_calls"] += stats.llm_calls
            totals["tool_calls"] += len(stats.tool_calls)
            totals["carousels"] += stats.carousels
            if stats.guard_blocked:
                totals["guard_blocks"] += 1

    def user_turns(self, user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            history = list(self._by_user.get(user_id) or [])[-limit:]
        return [{
            "conversation_id": s.conversation_id,
            "thread_id": s.thread_id,
            "status": s.status,
            "intent": s.intent,
            "tokens_in": s.tokens_in,
            "tokens_out": s.tokens_out,
            "llm_calls": s.llm_calls,
            "tool_calls": len(s.tool_calls),
            "started_at": s.started_at.isoformat(),
            "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        } for s in history]

    def summary(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._totals)


_REGISTRY: Optional[StatsRegistry] = None
_REGISTRY_LOCK = threading.Lock()


def get_stats_registry() -> StatsRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                _REGISTRY = StatsRegistry()
    return _REGISTRY