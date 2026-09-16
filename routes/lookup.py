"""
Public lookup and autocomplete API.

    GET    /lookup/categories    Autocomplete product categories (sorted by popularity).
    GET    /lookup/tags          Autocomplete product tags (sorted by attachment count).
    GET    /lookup/products      Fetch product card details by a list of IDs.

These read-only endpoints power the UI's search filters and the AI chat's
product carousel (`display_product_carousel` tool). Results are rate-limited,
and the `/lookup/products` endpoint guarantees returned cards strictly match 
the requested ID order.
"""


import logging

from flask import Blueprint, jsonify, request, url_for
from sqlalchemy import func

from database.db_setup import SessionLocal
from database.models import Product, Tag, product_tags
from utils.extensions import limiter
from utils.sanitizers import escape_like

logger = logging.getLogger(__name__)

lookup_bp = Blueprint('lookup', __name__)

MAX_TERM_LENGTH = 50
MAX_RESULTS = 10
MAX_CAROUSEL_IDS = 12


@lookup_bp.route('/lookup/categories')
@limiter.limit("60 per minute")
def lookup_categories():
    """Public autocomplete for product categories, most-used first."""
    q = (request.args.get('q') or '').strip()[:MAX_TERM_LENGTH]
    with SessionLocal() as db:
        query = db.query(Product.category, func.count(Product.id)).group_by(Product.category)
        if q:
            escaped = escape_like(q)
            query = query.filter(Product.category.ilike(f"%{escaped}%", escape="\\"))
        rows = query.order_by(func.count(Product.id).desc()).limit(MAX_RESULTS).all()
    return jsonify([{"value": category, "count": count} for category, count in rows])


@lookup_bp.route('/lookup/tags')
@limiter.limit("60 per minute")
def lookup_tags():
    """Public autocomplete for tags, most-attached-to-products first."""
    q = (request.args.get('q') or '').strip()[:MAX_TERM_LENGTH]
    with SessionLocal() as db:
        query = (
            db.query(Tag.name, func.count(product_tags.c.product_id))
            .outerjoin(product_tags, product_tags.c.tag_id == Tag.id)
            .group_by(Tag.id)
        )
        if q:
            escaped = escape_like(q)
            query = query.filter(Tag.name.ilike(f"%{escaped}%", escape="\\"))
        rows = query.order_by(func.count(product_tags.c.product_id).desc()).limit(MAX_RESULTS).all()
    return jsonify([{"value": name, "count": count} for name, count in rows])


@lookup_bp.route('/lookup/products')
@limiter.limit("60 per minute")
def lookup_products():
    """
    Resolve product IDs into card data for the chat carousel event
    (display_product_carousel). Order preserved from the ids parameter.
    """
    ids: list = []
    for part in (request.args.get('ids') or '').split(','):
        part = part.strip()
        if part.isdigit():
            value = int(part)
            if value > 0 and value not in ids:
                ids.append(value)
        if len(ids) >= MAX_CAROUSEL_IDS:
            break
    if not ids:
        return jsonify([])

    with SessionLocal() as db:
        products = db.query(Product).filter(Product.id.in_(ids)).all()
    by_id = {p.id: p for p in products}
    cards = []
    for product_id in ids:
        p = by_id.get(product_id)
        if p is None:
            continue
        cards.append({
            "id": p.id,
            "name": p.name,
            "price": float(p.price),
            "image_url": p.image_url,
            "category": p.category,
            "stock_quantity": p.stock_quantity,
            "in_stock": p.stock_quantity > 0,
            "url": url_for('store.product_detail', product_id=p.id),
        })
    return jsonify(cards)