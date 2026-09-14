import logging
from datetime import timedelta

from flask import Flask, flash, redirect, request
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import RequestEntityTooLarge

from database.db_setup import SessionLocal, init_db
from database.models import KnowledgeDocument
from database.rag_manager import get_rag_manager
from routes import register_routes
from utils.config import config
from utils.extensions import csrf, limiter, socketio

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DEFAULT_SECRET = "dev-secret-key-change-in-production"


def _reconcile_vector_store() -> None:
    """
    Heals SQL <-> ChromaDB drift at boot (orphaned vectors, manual DB edits).
    Hash-based: documents whose content is unchanged cost zero embedding
    calls. Never blocks startup — RAG degrades gracefully on failure.
    """
    try:
        with SessionLocal() as db:
            docs = db.query(KnowledgeDocument).all()
        report = get_rag_manager().reconcile(docs)
        if any(report.values()):
            logger.info("Vector store reconciled: %s", report)
    except Exception:
        logger.exception(
            "Vector store reconciliation failed — RAG may serve stale "
            "content until the next successful sync."
        )


def _exempt_socketio_from_csrf(app: Flask) -> None:
    """
    CSRFProtect validates every POST, which would reject Socket.IO's
    long-polling transport.
    """
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith("/socket.io"):
            view = app.view_functions.get(rule.endpoint)
            if view is not None:
                csrf.exempt(view)
                logger.debug("CSRF exempted socket.io rule '%s'.", rule.rule)


def create_app() -> Flask:
    """
    Application factory to create and configure the Flask instance.
    """
    app = Flask(__name__)
    app.secret_key = config.flask_config.secret_key

    app.config.update(
        MAX_CONTENT_LENGTH=config.rag_config.max_file_size_mb * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=config.flask_config.session_cookie_httponly,
        SESSION_COOKIE_SAMESITE=config.flask_config.session_cookie_samesite,
        SESSION_COOKIE_SECURE=config.flask_config.session_cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=config.flask_config.permanent_session_lifetime_minutes),
    )

    if app.secret_key == DEFAULT_SECRET:
        logger.warning("FLASK_SECRET_KEY not set — using the insecure default dev secret.")

    csrf.init_app(app)
    limiter.init_app(app)
    socketio.init_app(app)
    _exempt_socketio_from_csrf(app)

    init_db()

    if config.rag_config.sync_on_startup:
        _reconcile_vector_store()

    register_routes(app)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_file_too_large(e):
        flash(f"File exceeds the maximum allowed size of {config.rag_config.max_file_size_mb} MB.", "danger")
        return redirect(request.referrer or '/')

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        flash("Your session expired or the form was tampered with. Please try again.", "danger")
        return redirect(request.referrer or '/')

    @app.errorhandler(429)
    def handle_rate_limit(e):
        flash("Too many requests. Please wait a moment and try again.", "warning")
        return redirect(request.referrer or '/')

    return app


if __name__ == '__main__':
    app = create_app()
    # The server MUST be started through socketio.run (not app.run) or the
    # socket endpoint is dead.
    # front it with a real reverse proxy / swap async mode for production.
    socketio.run(
        app,
        host=config.flask_config.host,
        port=config.flask_config.port,
        debug=config.flask_config.debug,
        allow_unsafe_werkzeug=True,
    )