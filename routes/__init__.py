from flask import Flask
from datetime import datetime
# from utils.extensions import csrf

from utils.config import config

from .auth import auth_bp
from .admin import admin_bp
from .store import store_bp
from .rag import rag_bp
from .chat import chat_bp
from .llm_config import llm_config_bp
from .stats import stats_bp
from .lookup import lookup_bp
# from .webhook import webhook_bp

def register_routes(app: Flask):
    """
    Registers all Flask Blueprints with the main application.
    """
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(rag_bp, url_prefix='/admin/knowledge')
    app.register_blueprint(store_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(llm_config_bp, url_prefix='/admin/llm-config')
    app.register_blueprint(stats_bp, url_prefix='/admin')
    app.register_blueprint(lookup_bp)

    # enable via config
    # if config.meta_config.enabled:
    #     app.register_blueprint(webhook_bp, url_prefix='/webhook')
    #     csrf.exempt(webhook_bp)

    @app.context_processor
    def inject_template_globals():
        """Brand name + year for every template."""
        return {
            "company_name": config.system_context.company_name,
            "current_year": datetime.now().year,
        }