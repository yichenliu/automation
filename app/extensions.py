from .core import tenancy  # noqa: F401 (each transaction told whose it is: row-level security)
from .core.database import Database

db = Database()  # plain SQLAlchemy 2: db.Model, db.session, db.engine (app/core/database.py)
