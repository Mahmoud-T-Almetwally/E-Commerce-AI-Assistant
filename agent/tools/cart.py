"""
Cart and checkout tools.

Identity (user_id) is injected by the graph's tool node from authenticated
state — the model cannot supply or forge it. Checkout mirrors the atomic
stock-decrement logic of 'routes/store.py'.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Dict

from langchain_core.tools import InjectedToolArg
from sqlalchemy import update
from sqlalchemy.orm import joinedload

from agent.tooling import agent_tool, error, success
from database.db_setup import SessionLocal
from database.models import CartItem, Order, OrderItem, OrderStatus, Product
from utils.exceptions import OutOfStockError, RecordNotFoundError, ToolExecutionError

logger = logging.getLogger(__name__)


def _positive_int(value: Any, name: str) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer.")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


# Confirmation renderers

def _confirm_add_to_cart(args: Dict[str, Any], user_id: int) -> str:
    try:
        product_id = int(args.get("product_id") or 0)
        quantity = int(args.get("quantity") or 1)
    except (TypeError, ValueError):
        product_id, quantity = 0, 1
    try:
        with SessionLocal() as db:
            product = db.get(Product, product_id) if product_id > 0 else None
            if product is not None:
                return (f"Add {quantity} × {product.name} "
                        f"({float(product.price):.2f}) to your cart?")
    except Exception:
        logger.debug("Confirmation lookup failed for add_to_cart.", exc_info=True)
    return f"Add {quantity} item(s) to your cart?"


def _confirm_checkout(args: Dict[str, Any], user_id: int) -> str:
    try:
        with SessionLocal() as db:
            rows = (db.query(CartItem, Product)
                    .join(Product, CartItem.product_id == Product.id)
                    .filter(CartItem.user_id == user_id).all())
            if rows:
                units = sum(ci.quantity for ci, _ in rows)
                total = float(sum(p.price * ci.quantity for ci, p in rows))
                return (f"Place your order now? {len(rows)} product(s), "
                        f"{units} unit(s), total {total:.2f}.")
    except Exception:
        logger.debug("Confirmation lookup failed for checkout.", exc_info=True)
    return "Place your order now? (Your cart appears to be empty.)"


# Tools

@agent_tool()
def view_cart(retries: int | None = None,
              user_id: Annotated[int, InjectedToolArg] = 0) -> dict:
    """List the current contents of the customer's cart with quantities and totals."""
    with SessionLocal() as db:
        items = (db.query(CartItem).options(joinedload(CartItem.product))
                 .filter(CartItem.user_id == user_id).all())
        data_items = [{
            "cart_item_id": item.id,
            "product_id": item.product_id,
            "name": item.product.name,
            "quantity": item.quantity,
            "unit_price": float(item.product.price),
            "line_total": round(float(item.product.price) * item.quantity, 2),
        } for item in items]

    payload = {"items": data_items, "item_count": len(data_items),
               "total": round(sum(i["line_total"] for i in data_items), 2)}
    if not data_items:
        payload["message"] = "The cart is currently empty."
    return success(payload)


@agent_tool(sensitive=True, confirmation=_confirm_add_to_cart)
def add_to_cart(product_id: int, quantity: int = 1, retries: int | None = None,
                user_id: Annotated[int, InjectedToolArg] = 0) -> dict:
    """
    Add a product to the customer's cart. The customer must explicitly
    approve this action through a confirmation prompt before it runs.
    Resolve product IDs with search_products first.

    The optional `retries` argument sets automatic retries on transient
    failures; it never bypasses the confirmation or a stock refusal.
    """
    product_id = _positive_int(product_id, "product_id")
    quantity = _positive_int(quantity, "quantity")
    if user_id <= 0:
        raise ValueError("Missing authenticated user context.")

    with SessionLocal() as db:
        product = db.get(Product, product_id)
        if product is None:
            raise RecordNotFoundError("Product", product_id)
        item = (db.query(CartItem)
                .filter(CartItem.user_id == user_id,
                        CartItem.product_id == product_id).first())
        current = item.quantity if item is not None else 0
        if product.stock_quantity < current + quantity:
            raise OutOfStockError(product.name, current + quantity, product.stock_quantity)
        if item is not None:
            item.quantity += quantity
        else:
            db.add(CartItem(user_id=user_id, product_id=product_id, quantity=quantity))
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        data = {"product_id": product_id, "product_name": product.name,
                "quantity_in_cart": current + quantity,
                "unit_price": float(product.price),
                "available_stock": product.stock_quantity}
    return success(data)


@agent_tool()
def update_cart_quantity(product_id: int, quantity: int, retries: int | None = None,
                         user_id: Annotated[int, InjectedToolArg] = 0) -> dict:
    """
    Change the quantity of a product already in the cart. `quantity` must be
    at least 1 — use remove_from_cart to remove an item entirely.
    """
    product_id = _positive_int(product_id, "product_id")
    quantity = _positive_int(quantity, "quantity")

    with SessionLocal() as db:
        item = (db.query(CartItem).options(joinedload(CartItem.product))
                .filter(CartItem.user_id == user_id,
                        CartItem.product_id == product_id).first())
        if item is None:
            return error("not_found", f"Product {product_id} is not in the cart.",
                         retryable=False,
                         hint="Call view_cart to see the current cart contents.")
        if item.product.stock_quantity < quantity:
            raise OutOfStockError(item.product.name, quantity, item.product.stock_quantity)
        item.quantity = quantity
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        data = {"product_id": product_id, "product_name": item.product.name,
                "quantity": quantity, "unit_price": float(item.product.price)}
    return success(data)


@agent_tool()
def remove_from_cart(product_id: int, retries: int | None = None,
                     user_id: Annotated[int, InjectedToolArg] = 0) -> dict:
    """Remove a product from the customer's cart entirely."""
    product_id = _positive_int(product_id, "product_id")

    with SessionLocal() as db:
        item = (db.query(CartItem).options(joinedload(CartItem.product))
                .filter(CartItem.user_id == user_id,
                        CartItem.product_id == product_id).first())
        if item is None:
            return error("not_found", f"Product {product_id} is not in the cart.",
                         retryable=False,
                         hint="Call view_cart to see the current cart contents.")
        name = item.product.name
        db.delete(item)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
    return success({"product_id": product_id, "product_name": name, "removed": True})


@agent_tool(sensitive=True, confirmation=_confirm_checkout)
def checkout(retries: int | None = None,
             user_id: Annotated[int, InjectedToolArg] = 0) -> dict:
    """
    Place an order from the current cart (status: pending). The customer must
    explicitly approve this action through a confirmation prompt. Stock is
    decremented atomically; if any item is no longer available the entire
    order rolls back.
    """
    if user_id <= 0:
        raise ValueError("Missing authenticated user context.")

    with SessionLocal() as db:
        cart_items = (db.query(CartItem).options(joinedload(CartItem.product))
                      .filter(CartItem.user_id == user_id).all())
        if not cart_items:
            return error("cart_empty", "The cart is empty; there is nothing to check out.",
                         retryable=False,
                         hint="Help the customer add products first "
                              "(search_products, then add_to_cart).")

        items_summary = [{"product_id": ci.product_id, "name": ci.product.name,
                          "quantity": ci.quantity,
                          "unit_price": float(ci.product.price)} for ci in cart_items]
        total = sum(ci.product.price * ci.quantity for ci in cart_items)

        order = Order(customer_id=user_id, status=OrderStatus.PENDING, total_amount=total)
        try:
            db.add(order)
            db.flush()
            order_id = order.id
            for ci in cart_items:
                released = db.execute(
                    update(Product)
                    .where(Product.id == ci.product_id,
                           Product.stock_quantity >= ci.quantity)
                    .values(stock_quantity=Product.stock_quantity - ci.quantity)
                ).rowcount
                if released != 1:
                    raise OutOfStockError(ci.product.name, ci.quantity,
                                          ci.product.stock_quantity)
                db.add(OrderItem(order_id=order_id, product_id=ci.product_id,
                                 quantity=ci.quantity, unit_price=ci.product.price))
                db.delete(ci)
            db.commit()
        except OutOfStockError:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            raise ToolExecutionError("checkout", str(exc)) from exc

    return success({"order_id": order_id, "status": OrderStatus.PENDING.value,
                    "total": float(total), "items": items_summary})