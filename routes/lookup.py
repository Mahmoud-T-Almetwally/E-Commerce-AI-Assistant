import logging

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from database.db_setup import SessionLocal
from database.models import Product, Tag, product_tags
from utils.extensions import limiter
from utils.sanitizers import escape_like

logger = logging.getLogger(__name__)

lookup_bp = Blueprint('lookup', __name__)

MAX_TERM_LENGTH = 50
MAX_RESULTS = 10


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