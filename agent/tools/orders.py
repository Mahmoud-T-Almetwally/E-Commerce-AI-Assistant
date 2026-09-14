"""Order tools — strictly scoped to the authenticated customer."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Dict

from langchain_core.tools import InjectedToolArg
from sqlalchemy.orm import joinedload

from agent.tooling import agent_tool, success
from database.db_setup import SessionLocal
from database.models import Order, OrderItem
from utils.exceptions import RecordNotFoundError

logger = logging.getLogger(__name__)


def _order_summary(order: Order) -> Dict[str, Any]:
    return {
        "order_id": order.id,
        "status": order.status.value if hasattr(order.status, "value") else str(order.status),
        "total": float(order.total_amount or 0),
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "items": [{
            "product_id": i.product_id,
            "name": i.product.name if i.product is not None else f"product {i.product_id}",
            "quantity": i.quantity,
            "unit_price": float(i.unit_price),
        } for i in order.items],
    }


@agent_tool()
def list_recent_orders(limit: int = 3, retries: int | None = None,
                       user_id: Annotated[int, InjectedToolArg] = 0) -> dict:
    """
    List the customer's most recent orders (newest first) with status, totals
    and items.

    The optional `retries` argument sets automatic retries on transient failures.
    """
    if user_id <= 0:
        raise ValueError("Missing authenticated user context.")
    try:
        limit = max(1, min(int(limit or 3), 10))
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer.")

    with SessionLocal() as db:
        orders = (db.query(Order)
                  .options(joinedload(Order.items).joinedload(OrderItem.product))
                  .filter(Order.customer_id == user_id)
                  .order_by(Order.created_at.desc())
                  .limit(limit).all())
        data = [_order_summary(o) for o in orders]

    if not data:
        return success({"orders": [], "message": "No orders found yet."})
    return success({"orders": data, "count": len(data)})


@agent_tool()
def get_order_status(order_id: int, retries: int | None = None,
                     user_id: Annotated[int, InjectedToolArg] = 0) -> dict:
    """
    Full details of one of the customer's orders: current status, items,
    total, and when it was placed. Customers can only access their own orders.

    The optional `retries` argument sets automatic retries on transient failures.
    """
    if user_id <= 0:
        raise ValueError("Missing authenticated user context.")
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        raise ValueError("order_id must be an integer.")
    if order_id <= 0:
        raise ValueError("order_id must be a positive integer.")

    with SessionLocal() as db:
        order = (db.query(Order)
                 .options(joinedload(Order.items).joinedload(OrderItem.product))
                 .filter(Order.id == order_id, Order.customer_id == user_id)
                 .first())
        if order is None:
            raise RecordNotFoundError("Order", order_id)
        data = _order_summary(order)
    return success(data)