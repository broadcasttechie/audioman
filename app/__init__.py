from flask import Flask

from config import Config
from .extensions import db


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)

    from .api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix="/api")

    from .views import ui_bp
    app.register_blueprint(ui_bp)

    return app
