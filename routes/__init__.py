from flask import Flask

# Import the blueprints from the individual files
from .auth import auth_bp
from .admin import admin_bp
# from .main import main_bp
# from .api import api_bp
from .rag import rag_bp
# from .webhook import webhook_bp

def register_routes(app: Flask):
    """
    Registers all Flask Blueprints with the main application.
    """
    app.register_blueprint(auth_bp, url_prefix='/admin')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    
    # app.register_blueprint(main_bp)
    # app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(rag_bp, url_prefix='/admin/knowledge')
    # app.register_blueprint(webhook_bp, url_prefix='/webhook')