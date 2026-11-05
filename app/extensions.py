from flask_login import LoginManager
from flask_wtf import CSRFProtect

from .core.database import Database

db = Database()  # plain SQLAlchemy 2, the same API Flask-SQLAlchemy gave (app/core/database.py)
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = None
csrf = CSRFProtect()
