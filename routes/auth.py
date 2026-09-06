from functools import wraps
from urllib.parse import urlparse
from werkzeug.security import check_password_hash
from flask import Blueprint, render_template, request, redirect, url_for, session, flash

from database.db_setup import SessionLocal
from database.models import User, UserRole

auth_bp = Blueprint('auth', __name__)

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
def login():
    """
    Handles Admin Dashboard authentication.
    """
    if session.get('user_id') and session.get('role') == UserRole.ADMIN.value:
        return redirect(url_for('admin.dashboard_home'))

    next_page = request.args.get('next')

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        with SessionLocal() as db:
            user = db.query(User).filter(User.email == email).first()

            if user and user.password_hash and check_password_hash(user.password_hash, password):
                
                if user.role != UserRole.ADMIN:
                    flash('Access denied. Administrator privileges required.', 'danger')
                    return redirect(url_for('auth.login'))
                
                if not user.is_active:
                    flash('Your account has been deactivated.', 'danger')
                    return redirect(url_for('auth.login'))

                session.clear()
                
                session['user_id'] = user.id
                session['role'] = user.role.value
                session['name'] = user.name

                flash('Logged in successfully.', 'success')
                
                if next_page:
                    parsed_url = urlparse(next_page)
                    if parsed_url.netloc != '' or parsed_url.scheme != '':
                        next_page = url_for('admin.dashboard_home')
                else:
                    next_page = url_for('admin.dashboard_home')
                    
                return redirect(next_page)
            else:
                flash('Invalid email or password.', 'danger')

    return render_template('admin/login.html')


@auth_bp.route('/logout')
def logout():
    """
    Clears the session and logs the user out.
    """
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))