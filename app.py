import logging
from flask import Flask, flash, redirect, request
from werkzeug.exceptions import RequestEntityTooLarge
from database.db_setup import init_db

from routes import register_routes
from utils.config import config


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def create_app() -> Flask:
    """
    Application factory to create and configure the Flask instance.
    """
    app = Flask(__name__)
    
    app.secret_key = config.flask_config.secret_key

    app.config['MAX_CONTENT_LENGTH'] = config.rag_config.max_file_size_mb * 1024 * 1024
    
    init_db()
    
    register_routes(app)
    
    @app.errorhandler(RequestEntityTooLarge)
    def handle_file_too_large(e):
        flash(f"File exceeds the maximum allowed size of {config.rag_config.max_file_size_mb} MB.", "danger")
        return redirect(request.referrer or '/')
        
    return app

if __name__ == '__main__':
    app = create_app()
    app.run(
        host=config.flask_config.host,
        port=config.flask_config.port,
        debug=config.flask_config.debug
    )