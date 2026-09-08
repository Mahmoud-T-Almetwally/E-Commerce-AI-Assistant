from flask import request


def apply_sorting(query, allowed: dict, default: str, default_direction: str = 'asc'):
    """
    Server-side table sorting driven by ?sort=&direction= query params.
    `allowed` maps query-param names to SQLAlchemy columns. Unknown or
    missing values fall back to the default, so arbitrary column injection
    is impossible.
    """
    sort = request.args.get('sort', default)
    direction = request.args.get('direction', default_direction)

    if sort not in allowed:
        sort = default
    if direction not in ('asc', 'desc'):
        direction = default_direction

    column = allowed[sort]
    query = query.order_by(column.desc() if direction == 'desc' else column.asc())
    return query, sort, direction