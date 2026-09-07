import logging
import re
from functools import wraps
from urllib.parse import urlparse

from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from werkzeug.security import check_password_hash, generate_password_hash

from database.db_setup import SessionLocal
from database.models import User, UserRole
from utils.extensions import limiter

auth_bp = Blueprint('auth', __name__)
logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"\d{9}|\d{11}")


def login_required(f):
    """
    Decorator to ensure a user is logged in.
    Use this on routes that require authentication but not necessarily admin privileges.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_id'):
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('auth.login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """
    Decorator to ensure a user is logged in and has the ADMIN role.
    Use this on any route that requires administrative privileges.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_id') or session.get('role') != UserRole.ADMIN.value:
            flash('Please log in as an administrator to access this page.', 'warning')
            return redirect(url_for('auth.login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    """
    Handles authentication for both Admin and Customer users.
    Redirects based on role after successful login.
    """
    if session.get('user_id'):
        if session.get('role') == UserRole.ADMIN.value:
            return redirect(url_for('admin.dashboard_home'))
        return redirect(url_for('store.index'))

    next_page = request.args.get('next')

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        with SessionLocal() as db:
            user = db.query(User).filter(User.email == email).first()

            if user and user.password_hash and check_password_hash(user.password_hash, password):
                if not user.is_active:
                    flash('Your account has been deactivated. Please contact support.', 'danger')
                    return redirect(url_for('auth.login'))

                session.clear()
                session['user_id'] = user.id
                session['role'] = user.role.value
                session['name'] = user.name
                session.permanent = True  # activates PERMANENT_SESSION_LIFETIME

                flash(f'Welcome back, {user.name}!', 'success')

                if next_page:
                    parsed_url = urlparse(next_page)
                    if parsed_url.netloc != '' or parsed_url.scheme != '':
                        next_page = None

                if user.role == UserRole.ADMIN:
                    return redirect(next_page or url_for('admin.dashboard_home'))
                else:
                    return redirect(next_page or url_for('store.index'))
            else:
                flash('Invalid email or password.', 'danger')

    return render_template('auth/login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=["POST"])
def register():
    """
    Handles Customer registration.
    Creates only CUSTOMER accounts. Admins must be created via the admin panel.
    """
    if session.get('user_id'):
        flash('You are already logged in.', 'info')
        if session.get('role') == UserRole.ADMIN.value:
            return redirect(url_for('admin.dashboard_home'))
        return redirect(url_for('store.index'))

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        phone = (request.form.get('phone') or '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        # Validation
        errors = []
        if not name:
            errors.append('Full name is required.')
        if not email:
            errors.append('Email address is required.')
        elif not EMAIL_RE.match(email):
            errors.append('Please enter a valid email address.')
        if not password:
            errors.append('Password is required.')
        elif len(password) < 6:
            errors.append('Password must be at least 6 characters long.')
        if password != confirm_password:
            errors.append('Passwords do not match.')
        if phone and not PHONE_RE.fullmatch(phone):
            errors.append('Phone number must be 9 or 11 digits (digits only).')

        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('auth/register.html',
                                   name=name, email=email, phone=phone)

        with SessionLocal() as db:
            existing = db.query(User).filter(User.email == email).first()
            if existing:
                flash('An account with this email already exists. Please log in instead.', 'danger')
                return render_template('auth/register.html',
                                       name=name, email=email, phone=phone)

            try:
                new_customer = User(
                    email=email,
                    name=name,
                    password_hash=generate_password_hash(password),
                    role=UserRole.CUSTOMER,
                    is_active=True,
                    phone=phone if phone else None
                )
                db.add(new_customer)
                db.commit()

                flash('Account created successfully! Please log in.', 'success')
                return redirect(url_for('auth.login'))

            except Exception:
                db.rollback()
                logger.exception("Registration failed for %s", email)
                flash('Registration failed due to an unexpected error. Please try again.', 'danger')

    return render_template('auth/register.html')


@auth_bp.route('/logout')
def logout():
    """
    Clears the session and logs the user out.
    Redirects to main store page.
    """
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('store.index'))