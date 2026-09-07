import logging
from datetime import timedelta

from flask import Flask, flash, redirect, request
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import RequestEntityTooLarge

from database.db_setup import init_db
from routes import register_routes
from utils.config import config
from utils.extensions import csrf, limiter

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DEFAULT_SECRET = "dev-secret-key-change-in-production"


def create_app() -> Flask:
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

    init_db()
    register_routes(app)  # must csrf.exempt(webhook_bp)

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
    app.run(
        host=config.flask_config.host,
        port=config.flask_config.port,
        debug=config.flask_config.debug,
    )