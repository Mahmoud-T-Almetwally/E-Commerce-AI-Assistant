import math


def get_pagination(query, page, per_page=20):
    """
    Paginates a SQLAlchemy Query result set.

    Returns (items, total, total_pages). The page number is clamped to >= 1
    so a zero/negative page can never produce a negative OFFSET.
    """
    page = max(page, 1)
    total = query.count()
    total_pages = math.ceil(total / per_page) if total > 0 else 1
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return items, total, total_pages