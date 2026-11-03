import json

import pytest

from app import ai, create_app
from app.extensions import db
from app.models import Membership, User, Version, Workspace
from app.blueprint import schema


@pytest.fixture(autouse=True)
def no_review(monkeypatch):
    """Scripted turns answer exactly what they're asked: the check against the request is tested on its own."""
    from app.builder import chat
    monkeypatch.setattr(chat, "REVIEW_AFTER", 10 ** 6)
    monkeypatch.setattr(chat, "TRY_OUT", False)  # trying the app out is tested on its own (test_try_out.py)


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "WORKER": False, "DATA_DIR": tmp_path, "SECRET_KEY": "test",
                      "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}", "WTF_CSRF_ENABLED": False,
                      "DEFAULT_TIMEZONE": "Australia/Perth", "MICROSOFT_CLIENT_ID": "", "MICROSOFT_CLIENT_SECRET": "",
                      "BASE_URL": "http://localhost:5050", "OLLAMA_EMBED_MODEL": ""})
    from flask import g

    @app.before_request
    def fresh_request():
        # Tests keep one app context open (to query the database); real requests each get their own, so clear what a
        # request leaves behind (the signed-in person, the workspace) as a real one would start without it.
        for key in ("_login_user", "ws", "member", "engine"):
            g.pop(key, None)

    from app import security
    security.sign_ins.hits.clear()
    security.form_posts.hits.clear()
    with app.app_context():
        yield app
        db.session.remove()
    ai.set_backend(None)
    ai.set_embedder(None)


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
