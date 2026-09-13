"""
Single source of truth for authentication/authorization across the app.

Session-based (server-side session cookie), backed by the `users` table.
Everything — HTML routes, JSON APIs, and Socket.IO handlers — resolves the
current user through ``get_current_user()`` and the same two decorators:

    @login_required   -> authenticated users only
    @admin_required   -> users with role == UserRole.ADMIN

Decorator behavior is response-format aware: browser page requests get a
flash message + redirect to the login page (existing UX), while API requests
(paths under /api/ or /chat/, or clients sending Accept: application/json)
get a JSON error with the proper 401/403 status.
"""
import functools
from typing import Optional

from flask import flash, g, jsonify, redirect, request, session, url_for

from database.db_setup import SessionLocal
from database.models import User, UserRole


def get_current_user() -> Optional[User]:
    """
    Returns the logged-in User row for this request, or None.

    Resolved from session['user_id'] and cached on flask.g for the duration
    of the request. Inactive/deleted accounts are treated as logged out.
    Works in HTTP views and in flask-socketio event handlers (both run with
    a request context that exposes the session).
    """
    if "_current_user" in g:
        return g._current_user

    user: Optional[User] = None
    user_id = session.get("user_id")
    if user_id is not None:
        with SessionLocal() as db:
            candidate = db.get(User, user_id)
            if candidate is not None and candidate.is_active:
                user = candidate

    g._current_user = user
    return user


def _wants_json_response() -> bool:
    """True for API consumers (fetch/XHR/Socket.IO companions), False for browsers."""
    if request.path.startswith(("/api/", "/chat/")):
        return True
    best = request.accept_mimetypes.best
    return best == "application/json"


def _auth_failure(message: str, status: int):
    if _wants_json_response():
        return jsonify({"error": message}), status
    flash(message, "warning")
    return redirect(url_for("auth.login", next=request.url))


def login_required(fn):
    """Decorator ensuring the caller is an authenticated, active user."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if get_current_user() is None:
            return _auth_failure("Please log in to access this page.", 401)
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    """Decorator ensuring the caller is an authenticated user with the ADMIN role."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if user is None:
            return _auth_failure("Please log in as an administrator to access this page.", 401)
        if user.role != UserRole.ADMIN:
            return _auth_failure("Admin access required.", 403)
        return fn(*args, **kwargs)
    return wrapper
