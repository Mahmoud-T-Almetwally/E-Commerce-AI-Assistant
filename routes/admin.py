import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, redirect, url_for, flash
from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from werkzeug.security import generate_password_hash

from database.db_setup import SessionLocal
from database.models import Product, Order, User, UserRole, OrderStatus, Tag
from routes.auth import EMAIL_RE, PHONE_RE, admin_required
from utils.pagination import get_pagination
from utils.sanitizers import escape_like

admin_bp = Blueprint('admin', __name__)
logger = logging.getLogger(__name__)


ALLOWED_STATUS_TRANSITIONS = {
    OrderStatus.PENDING:    {OrderStatus.PROCESSING, OrderStatus.CANCELLED},
    OrderStatus.PROCESSING: {OrderStatus.SHIPPED, OrderStatus.CANCELLED},
    OrderStatus.SHIPPED:    {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED:  set(),
    OrderStatus.CANCELLED:  set(),
}


MAX_PRICE = Decimal("99999999.99")


def process_tags(db, tags_str: str):
    """
    Converts a comma-separated string of tags into a list of Tag objects.
    Queries existing tags by name, and creates new ones if they don't exist.
    """
    if not tags_str.strip():
        return []

    tag_names = list(set([t.strip() for t in tags_str.split(',') if t.strip()]))
    if not tag_names:
        return []

    lower_names = [name.lower() for name in tag_names]

    existing_tags = db.query(Tag).filter(func.lower(Tag.name).in_(lower_names)).all()

    existing_tag_map = {tag.name.lower(): tag for tag in existing_tags}

    tag_objects = []
    for name in tag_names:
        lower_name = name.lower()
        if lower_name in existing_tag_map:
            tag_objects.append(existing_tag_map[lower_name])
        else:
            new_tag = Tag(name=name)
            db.add(new_tag)
            tag_objects.append(new_tag)

    return tag_objects


def purge_orphan_tags(db):
    """Removes tags no longer attached to any product (after edits/deletes)."""
    db.query(Tag).filter(~Tag.products.any()).delete(synchronize_session=False)


def parse_product_form():
    """
    Validates the product form. Returns (data, tags_str, error_message).
    `data` and `tags_str` are None when validation fails. Prices are parsed
    as Decimal (matching the Numeric column) with at most 2 decimal places.
    """
    name = (request.form.get('name') or '').strip()
    category = (request.form.get('category') or '').strip()
    description = request.form.get('description') or ''
    price_str = (request.form.get('price') or '').strip()
    stock_str = (request.form.get('stock_quantity') or '').strip()

    image_url = (request.form.get('image_url') or '').strip()
    tags_str = (request.form.get('tags') or '').strip()

    if not name or not category:
        return None, None, 'Name and category are required.'

    try:
        price = Decimal(price_str) if price_str else Decimal("0.00")
    except InvalidOperation:
        return None, None, 'Price must be a valid number (e.g., 19.99).'

    if not price.is_finite():
        return None, None, 'Price must be a valid number (e.g., 19.99).'

    if price.as_tuple().exponent < -2:
        return None, None, 'Price supports at most 2 decimal places.'

    if price < 0:
        return None, None, 'Price cannot be negative.'

    if price > MAX_PRICE:
        return None, None, f'Price exceeds the supported maximum of {MAX_PRICE}.'

    try:
        stock_quantity = int(stock_str) if stock_str else 0
    except ValueError:
        return None, None, 'Stock quantity must be a whole number.'

    if stock_quantity < 0:
        return None, None, 'Stock quantity cannot be negative.'

    data = {
        'name': name,
        'description': description,
        'price': price.quantize(Decimal("0.01")),
        'stock_quantity': stock_quantity,
        'category': category,
        'image_url': image_url if image_url else "/static/images/default-product.png"
    }

    return data, tags_str, None


@admin_bp.route('/')
@admin_required
def dashboard_home():
    with SessionLocal() as db:
        total_customers = db.query(User).filter(User.role == UserRole.CUSTOMER).count()
        total_orders = db.query(Order).count()

        revenue = db.query(func.sum(Order.total_amount)).filter(
            Order.status != OrderStatus.CANCELLED
        ).scalar()
        total_revenue = float(revenue) if revenue else 0.0

        low_stock_items = db.query(Product).filter(Product.stock_quantity <= 5).count()

        recent_orders = (
            db.query(Order)
            .options(joinedload(Order.customer))
            .order_by(Order.created_at.desc())
            .limit(5)
            .all()
        )

        return render_template(
            'admin/dashboard.html',
            total_customers=total_customers,
            total_orders=total_orders,
            total_revenue=total_revenue,
            low_stock_items=low_stock_items,
            recent_orders=recent_orders
        )


@admin_bp.route('/products')
@admin_required
def list_products():
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
        if tag:
            query = query.filter(Product.tags.any(Tag.name.ilike(f"%{escape_like(tag)}%", escape="\\")))

        try:
            if min_price:
                query = query.filter(Product.price >= float(min_price))
            if max_price:
                query = query.filter(Product.price <= float(max_price))
        except ValueError:
            flash("Invalid price filter format. Ignored.", "warning")

        query = query.order_by(Product.id.desc())
        products, total, total_pages = get_pagination(query, page, per_page=20)

        return render_template(
            'admin/products.html',
            products=products, page=page, total=total, total_pages=total_pages,
            search=search, category=category, tag=tag, min_price=min_price, max_price=max_price
        )


@admin_bp.route('/products/add', methods=['GET', 'POST'])
@admin_required
def add_product():
    if request.method == 'POST':
        data, tags_str, error = parse_product_form()

        if error:
            flash(error, 'danger')
        else:
            with SessionLocal() as db:
                try:
                    new_product = Product(**data)
                    new_product.tags = process_tags(db, tags_str)

                    db.add(new_product)
                    db.commit()

                    flash('Product added successfully!', 'success')
                    return redirect(url_for('admin.list_products'))
                except IntegrityError:
                    db.rollback()
                    logger.exception("Integrity error while adding product %r", data.get('name'))
                    flash('A database constraint prevented adding this product. Please review the values and try again.', 'danger')
                except Exception:
                    db.rollback()
                    logger.exception("Unexpected error while adding product %r", data.get('name'))
                    flash('An unexpected error occurred while adding the product. Please try again.', 'danger')

    return render_template('admin/product_form.html', product=None, tags_str="")


@admin_bp.route('/products/<int:product_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_product(product_id):
    with SessionLocal() as db:
        product = db.query(Product).options(joinedload(Product.tags)).filter(Product.id == product_id).first()

        if not product:
            flash('Product not found.', 'danger')
            return redirect(url_for('admin.list_products'))

        if request.method == 'POST':
            data, tags_str, error = parse_product_form()

            if error:
                flash(error, 'danger')
            else:
                try:
                    product.name = data['name']
                    product.description = data['description']
                    product.price = data['price']
                    product.stock_quantity = data['stock_quantity']
                    product.category = data['category']
                    product.image_url = data['image_url']

                    product.tags = process_tags(db, tags_str)

                    db.commit()
                    purge_orphan_tags(db)
                    db.commit()
                    flash('Product updated successfully!', 'success')
                    return redirect(url_for('admin.list_products'))
                except Exception:
                    db.rollback()
                    logger.exception("Failed to update product %s", product_id)
                    flash('An unexpected error occurred while updating the product. Please try again.', 'danger')

        existing_tags = ", ".join(tag.name for tag in product.tags) if product.tags else ""

        return render_template('admin/product_form.html', product=product, tags_str=existing_tags)


@admin_bp.route('/products/<int:product_id>/delete', methods=['POST'])
@admin_required
def delete_product(product_id):
    with SessionLocal() as db:
        product = db.query(Product).filter(Product.id == product_id).first()
        if product:
            try:
                db.delete(product)
                db.commit()
                purge_orphan_tags(db)
                db.commit()
                flash('Product deleted successfully.', 'success')
            except IntegrityError:
                db.rollback()
                flash('Cannot delete this product: it is referenced by existing orders or shopping carts.', 'danger')
            except Exception:
                db.rollback()
                logger.exception("Failed to delete product %s", product_id)
                flash('An unexpected error occurred while deleting the product.', 'danger')
        else:
            flash('Product not found.', 'danger')

    return redirect(url_for('admin.list_products'))


@admin_bp.route('/orders')
@admin_required
def list_orders():
    page = max(request.args.get('page', 1, type=int), 1)

    status_filter = request.args.get('status', '').strip()
    customer_search = request.args.get('customer', '').strip()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()

    with SessionLocal() as db:
        query = db.query(Order).options(joinedload(Order.customer))

        if status_filter:
            try:
                query = query.filter(Order.status == OrderStatus(status_filter))
            except ValueError:
                pass

        if customer_search:
            escaped = escape_like(customer_search)
            query = query.join(User).filter(or_(
                User.name.ilike(f"%{escaped}%", escape="\\"),
                User.email.ilike(f"%{escaped}%", escape="\\")
            ))

        try:
            if start_date:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                query = query.filter(Order.created_at >= start_dt)
            if end_date:
                end_dt = datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S")
                query = query.filter(Order.created_at <= end_dt)
        except ValueError:
            flash("Invalid date format. Use YYYY-MM-DD.", "warning")

        query = query.order_by(Order.created_at.desc())
        orders, total, total_pages = get_pagination(query, page, per_page=20)
        statuses = [status.value for status in OrderStatus]

        return render_template(
            'admin/orders.html',
            orders=orders, statuses=statuses, page=page, total=total, total_pages=total_pages,
            status_filter=status_filter, customer_search=customer_search,
            start_date=start_date, end_date=end_date
        )


@admin_bp.route('/orders/<int:order_id>/status', methods=['POST'])
@admin_required
def update_order_status(order_id):
    new_status_val = request.form.get('status')
    with SessionLocal() as db:
        order = db.query(Order).filter(Order.id == order_id).with_for_update().first()
        if not order:
            flash('Order not found.', 'danger')
            return redirect(url_for('admin.list_orders'))

        try:
            new_status = OrderStatus(new_status_val)
        except ValueError:
            flash(f'Invalid status: {new_status_val}', 'danger')
            return redirect(url_for('admin.list_orders'))

        if new_status == order.status:
            flash(f"Order #{order.id} is already '{order.status.value}'.", 'info')
            return redirect(url_for('admin.list_orders'))

        if new_status not in ALLOWED_STATUS_TRANSITIONS.get(order.status, set()):
            flash(
                f"Cannot move order #{order.id} from '{order.status.value}' "
                f"to '{new_status.value}'.",
                'danger'
            )
            return redirect(url_for('admin.list_orders'))

        try:
            # Business rule: cancelling an order returns its items to inventory.
            if new_status == OrderStatus.CANCELLED:
                for item in order.items:
                    db.execute(
                        update(Product)
                        .where(Product.id == item.product_id)
                        .values(stock_quantity=Product.stock_quantity + item.quantity)
                    )

            order.status = new_status
            db.commit()
            flash(f'Order #{order.id} status updated to {order.status.value}.', 'success')
        except Exception:
            db.rollback()
            logger.exception("Failed to update status for order %s", order_id)
            flash('An unexpected error occurred while updating the order status.', 'danger')

    return redirect(url_for('admin.list_orders'))


@admin_bp.route('/customers')
@admin_required
def list_customers():
    page = max(request.args.get('page', 1, type=int), 1)

    search = request.args.get('search', '').strip()
    is_active = request.args.get('is_active', '').strip()

    with SessionLocal() as db:
        query = db.query(User).filter(User.role == UserRole.CUSTOMER)

        if search:
            escaped = escape_like(search)
            query = query.filter(or_(
                User.name.ilike(f"%{escaped}%", escape="\\"),
                User.email.ilike(f"%{escaped}%", escape="\\"),
                User.phone.ilike(f"%{escaped}%", escape="\\")
            ))

        if is_active in ['true', '1', 'True']:
            query = query.filter(User.is_active == True)
        elif is_active in ['false', '0', 'False']:
            query = query.filter(User.is_active == False)

        query = query.order_by(User.created_at.desc())
        customers, total, total_pages = get_pagination(query, page, per_page=20)

        return render_template(
            'admin/customers.html',
            customers=customers, page=page, total=total, total_pages=total_pages,
            search=search, is_active=is_active
        )


@admin_bp.route('/users/create-admin', methods=['POST'])
@admin_required
def create_admin():
    """
    Creates a new administrator account.
    Only existing admins can create new admin accounts.
    """
    name = (request.form.get('name') or '').strip()
    email = (request.form.get('email') or '').strip().lower()
    password = request.form.get('password', '')
    phone = (request.form.get('phone') or '').strip()

    errors = []
    if not name:
        errors.append('Name is required.')
    if not email:
        errors.append('Email is required.')
    elif not EMAIL_RE.match(email):
        errors.append('Please enter a valid email address.')
    if not password or len(password) < 6:
        errors.append('Password is required and must be at least 6 characters.')
    if phone and not PHONE_RE.fullmatch(phone):
        errors.append('Phone number must be 9 or 11 digits (digits only).')

    if errors:
        for error in errors:
            flash(error, 'danger')
        return redirect(url_for('admin.list_customers'))

    with SessionLocal() as db:
        existing = db.query(User).filter(User.email == email).first()
        if existing:
            flash(f'A user with email "{email}" already exists.', 'danger')
            return redirect(url_for('admin.list_customers'))

        try:
            new_admin = User(
                email=email,
                name=name,
                password_hash=generate_password_hash(password),
                role=UserRole.ADMIN,
                is_active=True,
                phone=phone if phone else None
            )
            db.add(new_admin)
            db.commit()
            flash(f'Admin account "{name}" ({email}) created successfully.', 'success')
        except Exception:
            db.rollback()
            logger.exception("Failed to create admin account %s", email)
            flash('Failed to create the admin account. Please try again.', 'danger')

    return redirect(url_for('admin.list_customers'))