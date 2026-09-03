from flask import Flask, redirect, url_for
from utils.config import config
from database.db_setup import init_db

from routes.auth import auth_bp
from routes.admin import admin_bp
# from routes.api import api_bp       # To be added later
# from routes.webhook import webhook_bp # To be added later

def create_app() -> Flask:
    """
    Application factory to create and configure the Flask instance.
    """
    app = Flask(__name__)
    
    app.secret_key = config.flask_config.secret_key
    
    init_db()
    
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    
    @app.route('/')
    def index():
        """
        Default route. For now, we redirect anyone hitting the root URL 
        straight to the Admin Dashboard.
        """
        return redirect(url_for('admin.dashboard_home'))
        
    return app

if __name__ == '__main__':
    app = create_app()
    
    print(f"Starting server on http://{config.flask_config.host}:{config.flask_config.port}")
    app.run(
        host=config.flask_config.host,
        port=config.flask_config.port,
        debug=config.flask_config.debug
    )