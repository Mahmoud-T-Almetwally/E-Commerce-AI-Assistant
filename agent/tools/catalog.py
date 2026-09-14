"""Catalog tools: product search and product details."""

from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy import case, func, or_

from agent.tooling import agent_tool, error, success
from database.db_setup import SessionLocal
from database.models import Product, Tag
from utils.exceptions import RecordNotFoundError
from utils.sanitizers import escape_like

logger = logging.getLogger(__name__)

_MAX_RESULTS = 8


def _product_card(p: Product) -> Dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "price": float(p.price),
        "category": p.category,
        "stock_quantity": p.stock_quantity,
        "in_stock": p.stock_quantity > 0,
        "tags": [t.name for t in p.tags],
    }


@agent_tool()
def search_products(query: str = "", category: str = "", tags: str = "",
                    price_min: float | None = None, price_max: float | None = None,
                    max_results: int = 5, retries: int | None = None) -> dict:
    """
    Search the product catalog. Combine a free-text query with optional
    category, comma-separated tags, and price bounds (price_min/price_max).
    Returns a compact list (id, name, price, category, stock, tags) and shows
    the results as a product carousel in the customer's UI. Always use this
    before referencing any product by ID.

    The optional `retries` argument sets automatic retries on transient
    failures; omit it unless a retryable error was returned.
    """
    query = (query or "").strip()
    category = (category or "").strip()
    tag_list = [t.strip().lower() for t in (tags or "").split(",") if t.strip()]

    if not (query or category or tag_list or price_min is not None or price_max is not None):
        return error("invalid_arguments",
                     "Provide at least one of: query, category, tags, price_min, price_max.")
    try:
        limit = max(1, min(int(max_results or 5), _MAX_RESULTS))
        p_min = float(price_min) if price_min is not None else None
        p_max = float(price_max) if price_max is not None else None
    except (TypeError, ValueError):
        return error("invalid_arguments", "max_results / price bounds must be numbers.")

    with SessionLocal() as db:
        stmt = db.query(Product)
        if query:
            pattern = f"%{escape_like(query)}%"
            stmt = stmt.filter(or_(
                Product.name.ilike(pattern, escape="\\"),
                Product.description.ilike(pattern, escape="\\"),
            ))
        if category:
            stmt = stmt.filter(Product.category.ilike(f"%{escape_like(category)}%", escape="\\"))
        if tag_list:
            stmt = stmt.filter(Product.tags.any(func.lower(Tag.name).in_(tag_list)))
        if p_min is not None:
            stmt = stmt.filter(Product.price >= p_min)
        if p_max is not None:
            stmt = stmt.filter(Product.price <= p_max)
        if query:  # light relevance: name matches first, then newest
            pattern = f"%{escape_like(query)}%"
            stmt = stmt.order_by(case((Product.name.ilike(pattern, escape="\\"), 0), else_=1),
                                 Product.id.desc())
        else:
            stmt = stmt.order_by(Product.id.desc())
        products = stmt.limit(limit).all()
        results = [_product_card(p) for p in products]     # build inside the session
        product_ids = [p.id for p in products]

    if not results:
        return success({"results": [], "count": 0,
                        "message": "No products matched these filters."})
    return success({"results": results, "count": len(results)},
                   ui_event={"event": "display_product_carousel", "product_ids": product_ids})


@agent_tool()
def get_product_details(product_id: int, retries: int | None = None) -> dict:
    """
    Full details for one product by ID: description, price, stock, and tags.
    Resolve IDs with search_products first.

    The optional `retries` argument sets automatic retries on transient
    failures.
    """
    try:
        product_id = int(product_id)
    except (TypeError, ValueError):
        raise ValueError("product_id must be an integer.")
    if product_id <= 0:
        raise ValueError("product_id must be a positive integer.")

    with SessionLocal() as db:
        product = db.get(Product, product_id)
        if product is None:
            raise RecordNotFoundError("Product", product_id)
        data = _product_card(product)
        data["description"] = (product.description or "")[:1200]
    return success(data)