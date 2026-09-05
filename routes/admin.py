import math

from flask import Blueprint, render_template, request, redirect, url_for, flash
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from database.db_setup import SessionLocal
from database.models import Product, Order, User, UserRole, OrderStatus, Tag
from routes.auth import admin_required
from utils.pagination import get_pagination

admin_bp = Blueprint('admin', __name__)


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


def parse_product_form():
    """
    Validates the product form. Returns (data, tags_str, error_message).
    `data` and `tags_str` are None when validation fails.
    """
    name = (request.form.get('name') or '').strip()
    category = (request.form.get('category') or '').strip()
    description = request.form.get('description') or ''
    price_str = (request.form.get('price') or '').strip()
    stock_str = (request.form.get('stock_quantity') or '').strip()
    
    # New fields
    image_url = (request.form.get('image_url') or '').strip()
    tags_str = (request.form.get('tags') or '').strip()

    if not name or not category:
        return None, None, 'Name and category are required.'

    try:
        price = float(price_str) if price_str else 0.0
        stock_quantity = int(stock_str) if stock_str else 0
    except (TypeError, ValueError):
        return None, None, 'Price must be a number and stock quantity must be a whole number.'

    if price < 0 or stock_quantity < 0:
        return None, None, 'Price and stock quantity cannot be negative.'

    data = {
        'name': name,
        'description': description,
        'price': price,
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
    with SessionLocal() as db:
        query = db.query(Product).options(joinedload(Product.tags)).order_by(Product.id.desc())
        products, total, total_pages = get_pagination(query, page, per_page=20)

        return render_template(
            'admin/products.html',
            products=products, page=page, total=total, total_pages=total_pages
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
                except IntegrityError as e:
                    db.rollback()
                    flash(f'Error adding product: {e.orig}', 'danger')
                except Exception as e:
                    db.rollback()
                    flash(f'Error adding product: {e}', 'danger')

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
                    flash('Product updated successfully!', 'success')
                    return redirect(url_for('admin.list_products'))
                except Exception as e:
                    db.rollback()
                    flash(f'Error updating product: {e}', 'danger')
                    
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
                flash('Product deleted successfully.', 'success')
            except IntegrityError:
                db.rollback()
                flash('Cannot delete this product: it is referenced by existing orders or shopping carts.', 'danger')
        else:
            flash('Product not found.', 'danger')

    return redirect(url_for('admin.list_products'))


@admin_bp.route('/orders')
@admin_required
def list_orders():
    page = max(request.args.get('page', 1, type=int), 1)
    with SessionLocal() as db:
        query = (
            db.query(Order)
            .options(joinedload(Order.customer))
            .order_by(Order.created_at.desc())
        )
        orders, total, total_pages = get_pagination(query, page, per_page=20)
        statuses = [status.value for status in OrderStatus]

        return render_template(
            'admin/orders.html',
            orders=orders, statuses=statuses, page=page, total=total, total_pages=total_pages
        )


@admin_bp.route('/orders/<int:order_id>/status', methods=['POST'])
@admin_required
def update_order_status(order_id):
    new_status_val = request.form.get('status')
    with SessionLocal() as db:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            flash('Order not found.', 'danger')
            return redirect(url_for('admin.list_orders'))
        try:
            order.status = OrderStatus(new_status_val)
            db.commit()
            flash(f'Order #{order.id} status updated to {order.status.value}.', 'success')
        except ValueError:
            db.rollback()
            flash(f'Invalid status: {new_status_val}', 'danger')

    return redirect(url_for('admin.list_orders'))


@admin_bp.route('/customers')
@admin_required
def list_customers():
    page = max(request.args.get('page', 1, type=int), 1)
    with SessionLocal() as db:
        query = db.query(User).filter(User.role == UserRole.CUSTOMER).order_by(User.created_at.desc())
        customers, total, total_pages = get_pagination(query, page, per_page=20)

        return render_template(
            'admin/customers.html',
            customers=customers, page=page, total=total, total_pages=total_pages
        )