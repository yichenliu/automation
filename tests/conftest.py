import json

import pytest

from app import ai, create_app
from app.extensions import db
from app.models import Membership, User, Version, Workspace
from app.blueprint import schema


@pytest.fixture(scope="session")
def database_url():
    """A throwaway PostgreSQL database for the whole run (beside the platform's own: ./start.sh setup), dropped after."""
    from app import database
    from app.config import Config
    if not Config.SQLALCHEMY_DATABASE_URI:
        pytest.exit("The tests need PostgreSQL: run ./start.sh setup", returncode=2)
    with database.throwaway(Config.SQLALCHEMY_DATABASE_URI, "test") as url:
        yield url


@pytest.fixture
def app(tmp_path, database_url):
    app = create_app({"TESTING": True, "WORKER": False, "DATA_DIR": tmp_path, "SECRET_KEY": "test",
                      "SQLALCHEMY_DATABASE_URI": database_url, "WTF_CSRF_ENABLED": False,
                      "SQLALCHEMY_ENGINE_OPTIONS": {"pool_size": 5, "max_overflow": 10},
                      "DEFAULT_TIMEZONE": "Australia/Perth", "MICROSOFT_CLIENT_ID": "", "MICROSOFT_CLIENT_SECRET": "",
                      "BASE_URL": "http://localhost:5050", "OLLAMA_EMBED_MODEL": ""})
    # The test keeps the app's context open (to query the database). Each request from app.test_client() shares its
    # database session, with a request.state of its own, and leaves the session no workspace's (app/web/http.py).
    from app import security
    security.sign_ins.hits.clear()
    security.form_posts.hits.clear()
    with app.app_context():
        yield app
        db.session.remove()
        _empty(db)  # the next test starts with nothing, its ids from 1, as a new database would
        db.engine.dispose()
        from app import ai_providers
        ai_providers.forget()  # what the last test chose for the AI (Settings › AI) isn't remembered into the next
    ai.set_backend(None)
    ai.set_embedder(None)


def _empty(db):
    from sqlalchemy import text
    names = ", ".join(f'"{t.name}"' for t in db.metadata.sorted_tables)
    with db.engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def owner(app):
    user = User(email="ethan@example.com", name="Ethan")
    user.set_password("password1234")
    db.session.add(user)
    db.session.commit()
    ws = Workspace(owner_id=user.id, name="Eth Analytic", about="A data consultancy.", blueprint=schema.empty("Eth Analytic"), version=0,
                   state={}, settings={"business": {"name": "Eth Analytic", "abn": "51824753556"}})
    db.session.add(ws)
    db.session.flush()
    db.session.add(Membership(workspace_id=ws.id, user_id=user.id, role="owner"))
    db.session.add(Version(workspace_id=ws.id, number=0, blueprint=ws.blueprint, changes=["Started"], source="start"))
    db.session.commit()
    return user, ws


def login(client):
    return client.post("/login", data={"email": "ethan@example.com", "password": "password1234"})


class ScriptedAI:
    """Answers in turn with the JSON given; records what it was asked."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, messages, schema_, on_progress):
        self.asked.append(messages)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer)
        if on_progress:
            on_progress("Thinking about clients and projects.", text)
        return text, "Thinking about clients and projects."
