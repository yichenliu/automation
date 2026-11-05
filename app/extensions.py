from .core import tenancy  # noqa: F401 (each transaction told whose it is: row-level security)
from .core.database import Database
from .webkit import CSRFProtect, LoginManager

db = Database()  # plain SQLAlchemy 2, the same API Flask-SQLAlchemy gave (app/core/database.py)
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = None
csrf = CSRFProtect()
