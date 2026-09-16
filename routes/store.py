"""
Customer-facing storefront and cart management API.

    GET    /                          Redirects to the main products listing.
    GET    /products                  Browse catalog (search, filter, sort, paginate).
    GET    /products/<id>             View product details and related items.
    GET    /cart                      View active user's shopping cart.
    POST   /cart/add                  Add an item to the cart (enforces stock limits).
    POST   /cart/update               Update quantity of an existing cart item.
    POST   /cart/remove/<id>          Remove an item from the cart.
    GET    /checkout                  View order summary before purchase.
    POST   /checkout                  Process cart, deduct stock safely, and create an Order.
    GET    /orders                    List a customer's order history.
    GET    /orders/<id>               View details of a specific past order.

Endpoints interacting with the cart, checkout, or user orders require session
authentication via the `@login_required` decorator. Stock deductions during
checkout utilize optimistic row-level locking to prevent race conditions.
"""


import logging

from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from sqlalchemy import or_, update, func
from sqlalchemy.orm import joinedload

from database.db_setup import SessionLocal
from database.models import Product, Tag, CartItem, Order, OrderItem, OrderStatus
from utils.auth import login_required
from utils.exceptions import OutOfStockError
from utils.pagination import get_pagination
from utils.sanitizers import escape_like

logger = logging.getLogger(__name__)

store_bp = Blueprint('store', __name__)

STORE_SORT_OPTIONS = {
    'newest': (Product.id, 'desc'),
    'price_asc': (Product.price, 'asc'),
    'price_desc': (Product.price, 'desc'),
    'name': (Product.name, 'asc'),
}

@store_bp.context_processor
def inject_cart_count():
    """Cart badge count — only evaluated for store blueprint views."""
    user_id = session.get('user_id')
    if not user_id:
        return {'cart_count': 0}
    with SessionLocal() as db:
        count = db.query(func.sum(CartItem.quantity)).filter(CartItem.user_id == user_id).scalar()
    return {'cart_count': count or 0}

@store_bp.route('/')
def index():
    """Redirect root to the products listing page."""
    return redirect(url_for('store.list_products'))


@store_bp.route('/products')
def list_products():
    """Public route to browse products with search and filtering capabilities."""
    page = max(request.args.get('page', 1, type=int), 1)

    search = request.args.get('search', '').strip()
    category = request.args.get('category', '').strip()
    tag = request.args.get('tag', '').strip()
    min_price = request.args.get('min_price', '').strip()
    max_price = request.args.get('max_price', '').strip()

    with SessionLocal() as db:
        query = db.query(Product).options(joinedload(Product.tags))

        if search:
            escaped = escape_like(search)
            query = query.filter(or_(
                Product.name.ilike(f"%{escaped}%", escape="\\"),
                Product.description.ilike(f"%{escaped}%", escape="\\")
            ))
        if category:
            query = query.filter(Product.category.ilike(f"%{escape_like(category)}%", escape="\\"))

        tag_filter = [t.strip().lower() for t in tag.split(',') if t.strip()]
        if tag_filter:
            query = query.filter(Product.tags.any(func.lower(Tag.name).in_(tag_filter)))

        try:
            if min_price:
                query = query.filter(Product.price >= float(min_price))
            if max_price:
                query = query.filter(Product.price <= float(max_price))
        except ValueError:
            flash("Invalid price filter format. Ignored.", "warning")

        sort = request.args.get('sort', 'newest')
        if sort not in STORE_SORT_OPTIONS:
            sort = 'newest'
        sort_col, sort_dir = STORE_SORT_OPTIONS[sort]
        query = query.order_by(sort_col.desc() if sort_dir == 'desc' else sort_col.asc())

        products, total, total_pages = get_pagination(query, page, per_page=12)

        trending_rows = (
            db.query(Product, func.sum(OrderItem.quantity).label('units'))
            .join(OrderItem, OrderItem.product_id == Product.id)
            .join(Order, Order.id == OrderItem.order_id)
            .filter(Order.status != OrderStatus.CANCELLED)
            .group_by(Product.id)
            .order_by(func.sum(OrderItem.quantity).desc())
            .limit(4)
            .all()
        )
        trending = [
            {"id": p.id, "name": p.name, "category": p.category,
             "price": p.price, "units": r or 0}
            for p, r in trending_rows
        ]

        return render_template(
            'store/products.html',
            products=products, page=page, total=total, total_pages=total_pages,
            search=search, category=category, tag=tag,
            min_price=min_price, max_price=max_price,
            sort=sort, trending=trending,
        )


@store_bp.route('/products/<int:product_id>')
def product_detail(product_id):
    """Public route to view a single product's details."""
    with SessionLocal() as db:
        product = db.query(Product).options(joinedload(Product.tags)).filter(Product.id == product_id).first()

        if not product:
            flash('Product not found.', 'danger')
            return redirect(url_for('store.list_products'))

        related = (
            db.query(Product)
            .filter(Product.category == product.category, Product.id != product.id)
            .order_by(func.random())
            .limit(4)
            .all()
        )
        return render_template('store/product_detail.html', product=product, related=related)


@store_bp.route('/cart', methods=['GET'])
@login_required
def view_cart():
    """Protected route to view the user's shopping cart."""
    user_id = session.get('user_id')
    with SessionLocal() as db:
        cart_items = db.query(CartItem).options(
            joinedload(CartItem.product)
        ).filter(CartItem.user_id == user_id).all()

        total_amount = sum(item.product.price * item.quantity for item in cart_items)

        return render_template('store/cart.html', cart_items=cart_items, total_amount=total_amount)


@store_bp.route('/cart/add', methods=['POST'])
@login_required
def add_to_cart():
    """Protected route to add a product to the cart."""
    product_id = request.form.get('product_id', type=int)
    quantity = request.form.get('quantity', type=int, default=1)

    if not product_id or quantity <= 0:
        flash('Invalid product or quantity.', 'danger')
        return redirect(request.referrer or url_for('store.list_products'))

    user_id = session.get('user_id')

    with SessionLocal() as db:
        product = db.query(Product).filter(Product.id == product_id).first()

        if not product:
            flash('Product not found.', 'danger')
            return redirect(url_for('store.list_products'))

        if product.stock_quantity < quantity:
            flash(f'Only {product.stock_quantity} units available for {product.name}.', 'warning')
            return redirect(request.referrer or url_for('store.list_products'))

        cart_item = db.query(CartItem).filter(
            CartItem.user_id == user_id,
            CartItem.product_id == product_id
        ).first()

        if cart_item:
            if product.stock_quantity < (cart_item.quantity + quantity):
                flash('Cannot add more of this item. Stock limit reached.', 'warning')
            else:
                cart_item.quantity += quantity
                db.commit()
                flash('Cart updated.', 'success')
        else:
            new_item = CartItem(user_id=user_id, product_id=product_id, quantity=quantity)
            db.add(new_item)
            db.commit()
            flash('Item added to cart.', 'success')

    return redirect(request.referrer or url_for('store.list_products'))


@store_bp.route('/cart/update', methods=['POST'])
@login_required
def update_cart():
    """Protected route to update item quantities in the cart."""
    cart_item_id = request.form.get('cart_item_id', type=int)
    new_quantity = request.form.get('quantity', type=int)

    if not cart_item_id or new_quantity is None or new_quantity < 1:
        flash('Invalid quantity.', 'danger')
        return redirect(url_for('store.view_cart'))

    user_id = session.get('user_id')

    with SessionLocal() as db:
        cart_item = db.query(CartItem).options(joinedload(CartItem.product)).filter(
            CartItem.id == cart_item_id,
            CartItem.user_id == user_id
        ).first()

        if not cart_item:
            flash('Item not found in your cart.', 'danger')
        elif cart_item.product.stock_quantity < new_quantity:
            flash(f'Only {cart_item.product.stock_quantity} units available for {cart_item.product.name}.', 'warning')
        else:
            cart_item.quantity = new_quantity
            db.commit()
            flash('Cart updated.', 'success')

    return redirect(url_for('store.view_cart'))


@store_bp.route('/cart/remove/<int:item_id>', methods=['POST'])
@login_required
def remove_from_cart(item_id):
    """Protected route to completely remove an item from the cart."""
    user_id = session.get('user_id')
    with SessionLocal() as db:
        cart_item = db.query(CartItem).filter(CartItem.id == item_id, CartItem.user_id == user_id).first()
        if cart_item:
            db.delete(cart_item)
            db.commit()
            flash('Item removed from cart.', 'success')
        else:
            flash('Item not found in your cart.', 'danger')

    return redirect(url_for('store.view_cart'))


@store_bp.route('/checkout', methods=['GET', 'POST'])
@login_required
def checkout():
    """Protected route to process the cart and create an order."""
    user_id = session.get('user_id')
    with SessionLocal() as db:
        cart_items = db.query(CartItem).options(
            joinedload(CartItem.product)
        ).filter(CartItem.user_id == user_id).all()

        if not cart_items:
            flash('Your cart is empty.', 'info')
            return redirect(url_for('store.list_products'))

        total_amount = sum(item.product.price * item.quantity for item in cart_items)

        if request.method == 'POST':
            try:
                new_order = Order(
                    customer_id=user_id,
                    status=OrderStatus.PENDING,
                    total_amount=total_amount
                )
                db.add(new_order)
                db.flush()

                for item in cart_items:
                    released = db.execute(
                        update(Product)
                        .where(
                            Product.id == item.product_id,
                            Product.stock_quantity >= item.quantity
                        )
                        .values(stock_quantity=Product.stock_quantity - item.quantity)
                    ).rowcount

                    if released != 1:
                        raise OutOfStockError(
                            product_name=item.product.name,
                            requested=item.quantity,
                            available=item.product.stock_quantity,
                        )

                    db.add(OrderItem(
                        order_id=new_order.id,
                        product_id=item.product_id,
                        quantity=item.quantity,
                        unit_price=item.product.price
                    ))
                    db.delete(item)

                db.commit()
                flash('Your order has been successfully placed!', 'success')
                return redirect(url_for('store.order_detail', order_id=new_order.id))

            except OutOfStockError as e:
                db.rollback()
                flash(str(e), 'warning')
                return redirect(url_for('store.view_cart'))
            except Exception:
                db.rollback()
                logger.exception("Checkout failed for user %s", user_id)
                flash('An error occurred while processing your order. Please try again.', 'danger')

        return render_template('store/checkout.html', cart_items=cart_items, total_amount=total_amount)


@store_bp.route('/orders', methods=['GET'])
@login_required
def list_orders():
    """Protected route for users to view their past orders."""
    user_id = session.get('user_id')
    with SessionLocal() as db:
        orders = db.query(Order).options(
            joinedload(Order.items).joinedload(OrderItem.product)
        ).filter(
            Order.customer_id == user_id
        ).order_by(Order.created_at.desc()).all()

        return render_template('store/orders.html', orders=orders)


@store_bp.route('/orders/<int:order_id>', methods=['GET'])
@login_required
def order_detail(order_id):
    """Protected route for users to view details of a specific order."""
    user_id = session.get('user_id')
    with SessionLocal() as db:
        order = db.query(Order).options(
            joinedload(Order.items).joinedload(OrderItem.product)
        ).filter(Order.id == order_id, Order.customer_id == user_id).first()

        if not order:
            flash('Order not found.', 'danger')
            return redirect(url_for('store.list_orders'))

        return render_template('store/order_detail.html', order=order)